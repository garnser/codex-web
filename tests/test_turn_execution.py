from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException

from codex_web.agent_runtime import AgentRuntimeResult
from codex_web.execution_workers import ExecutionRuntimeBinding
from codex_web.models import Project, ThreadRunSettings, TurnCreate, WorkItemState
from codex_web.resources import RepositoryTargetSource
from codex_web.runtime.execution import TurnExecutionService, install_turn_execution_service
from codex_web.services.agent_routing import AgentRoutingError
from codex_web.services.turns import TurnService
from codex_web.services.thread_bootstrap_bindings import (
    ThreadBootstrapBindingNotFoundError,
)
from codex_web.services.turn_execution_binding import TurnExecutionBindingError


class _Hub:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def publish(self, event: dict) -> None:
        self.events.append(event)


class _Host:
    def __init__(self) -> None:
        self.queues = {}
        self.active = {}
        self.events: list[dict] = []
        self.bulk_queue_loads = 0
        self.bulk_active_loads = 0
        self.hub = _Hub()
        self.codex = SimpleNamespace(request=AsyncMock())
        self.IS_SHUTTING_DOWN = False
        self.uuid = __import__("uuid")
        self.work_item_states = {}

    def _load_work_item_states(self):
        return {
            ref: state.model_copy(deep=True)
            for ref, state in self.work_item_states.items()
        }

    def _load_turn_queues(self):
        self.bulk_queue_loads += 1
        return {key: list(value) for key, value in self.queues.items()}

    def _save_turn_queues(self, queues):
        self.queues = {key: list(value) for key, value in queues.items()}

    def _thread_queue_record(self, thread_id):
        return list(self.queues.get(thread_id, []))

    def _update_thread_queue_record(self, thread_id, updater):
        current = list(self.queues.get(thread_id, []))
        updated = list(updater(current))
        if updated:
            self.queues[thread_id] = updated
        else:
            self.queues.pop(thread_id, None)
        return updated

    def _put_thread_queue_record(self, thread_id, items):
        self.queues[thread_id] = list(items)

    def _delete_thread_queue_record(self, thread_id):
        return self.queues.pop(thread_id, None) is not None

    def _thread_queue(self, thread_id):
        return list(self.queues.get(thread_id, []))

    def _thread_queue_depth(self, thread_id):
        return len(self.queues.get(thread_id, []))

    @staticmethod
    def _work_item_wakeup_entries(message):
        return []

    @staticmethod
    def _render_work_item_wakeup_batch(entries):
        return "batch"

    @staticmethod
    def _max_thread_queue_depth():
        return 10

    def _load_active_turns(self):
        self.bulk_active_loads += 1
        return dict(self.active)

    def _save_active_turns(self, active):
        self.active = dict(active)

    def _get_active_turn_record(self, thread_id):
        return self.active.get(thread_id)

    def _put_active_turn_record(self, thread_id, active):
        self.active[thread_id] = active

    def _delete_active_turn_record(self, thread_id):
        return self.active.pop(thread_id, None) is not None

    @staticmethod
    def _thread_run_settings(thread_id):
        return ThreadRunSettings()

    @staticmethod
    def _autonomy_enabled():
        return False

    @staticmethod
    def _schedule_native_recovery_cycles(**kwargs):
        return None

    @staticmethod
    def _effective_developer_instructions(thread_id, value):
        return value

    @staticmethod
    def _project_params(project, values):
        return {key: value for key, value in values.items() if value is not None}

    @staticmethod
    def _sandbox_policy(sandbox, path):
        return {"type": sandbox}

    @staticmethod
    def _with_relay_guard(message, source):
        return message

    @staticmethod
    def _turn_source_for_relay_guard(thread_id, source):
        return source

    def _append_bot_event(self, event):
        self.events.append(event)


class _RoutingFailure:
    def __init__(self, message: str) -> None:
        self.message = message

    async def route(self, request, *, actor):
        raise AgentRoutingError(self.message)


class _RoutingSuccess:
    def __init__(self) -> None:
        self.calls = []

    async def route(self, request, *, actor):
        self.calls.append((request, actor))
        binding = ExecutionRuntimeBinding(
            provider_id="openai",
            runtime_id="codex",
            capability_revision=1,
        )
        return SimpleNamespace(
            selected_runtime=SimpleNamespace(
                execution_binding=lambda: binding,
            ),
            agent_profile=None,
        )


class TurnExecutionPreflightRoutingTests(unittest.IsolatedAsyncioTestCase):
    def test_trusted_local_source_preserves_interactive_queue_provenance(self) -> None:
        with patch.dict(
            "os.environ",
            {"CODEX_WEB_TRUSTED_LOCAL_CODEX_SESSION": "1"},
        ):
            for source in ("web", "queued:web", "steer:web", "queued:steer:web"):
                with self.subTest(source=source):
                    self.assertTrue(
                        TurnExecutionService._trusted_local_codex_session_enabled(
                            source=source,
                            runtime_binding=None,
                        )
                    )
            for source in ("slack", "queued:slack", "steer:queued:slack"):
                with self.subTest(source=source):
                    self.assertFalse(
                        TurnExecutionService._trusted_local_codex_session_enabled(
                            source=source,
                            runtime_binding=None,
                        )
                    )

    async def test_standard_routing_allows_brokered_or_direct_provider_egress(self) -> None:
        routing = _RoutingSuccess()
        service = TurnExecutionService(
            _Host(),
            control_actor=SimpleNamespace(identity_id="control"),
            routing_service=routing,
        )

        await service._select_runtime_binding(
            project_id="p1",
            sandbox="workspace-write",
        )

        request, _actor = routing.calls[0]
        self.assertIsNone(request.required_network_profile)
        self.assertEqual(
            request.allowed_network_profiles,
            (
                "brokered-model-egress",
                "direct-provider-egress",
            ),
        )

    async def test_network_profile_mismatch_is_structured_preflight_blocker(self) -> None:
        service = TurnExecutionService(
            _Host(),
            control_actor=SimpleNamespace(identity_id="control"),
            routing_service=_RoutingFailure(
                "no eligible agent runtime: openai/codex:network_profile_mismatch"
            ),
        )

        with self.assertRaises(HTTPException) as caught:
            await service._select_runtime_binding(
                project_id="p1",
                sandbox="workspace-write",
            )

        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(
            caught.exception.detail["code"],
            "execution_preflight_blocked",
        )
        blocker = caught.exception.detail["blockers"][0]
        self.assertEqual(blocker["code"], "network_policy_unsupported")
        self.assertFalse(blocker["retryable"])
        self.assertEqual(blocker["target_type"], "agent_runtime")

    async def test_trusted_local_routing_does_not_require_worker_sandbox_profile(self) -> None:
        routing = _RoutingSuccess()
        service = TurnExecutionService(
            _Host(),
            control_actor=SimpleNamespace(identity_id="control"),
            routing_service=routing,
        )

        binding, profile = await service._select_runtime_binding(
            project_id="p1",
            sandbox="danger-full-access",
            trusted_local_codex_session=True,
        )

        self.assertEqual((binding.provider_id, binding.runtime_id), ("openai", "codex"))
        self.assertIsNone(profile)
        request, _actor = routing.calls[0]
        self.assertEqual(request.allowed_provider_ids, ("openai",))
        self.assertEqual(request.allowed_runtime_ids, ("codex",))
        self.assertEqual(request.preferred_provider_ids, ("openai",))
        self.assertEqual(request.preferred_runtime_ids, ("codex",))
        self.assertFalse(request.allow_fallback)
        self.assertIsNone(request.required_sandbox_profile)
        self.assertIsNone(request.required_network_profile)
        self.assertEqual(request.allowed_network_profiles, ())


class TurnExecutionQueueTests(unittest.TestCase):
    def test_queue_is_fifo_and_requeue_front_preserves_item(self) -> None:
        host = _Host()
        service = TurnExecutionService(host)

        first = service.enqueue_turn(thread_id="t1", project_id="p1", message="one")
        second = service.enqueue_turn(thread_id="t1", project_id="p1", message="two")

        self.assertEqual(host._thread_queue_depth("t1"), 2)
        popped = service.pop_next_queued_turn("t1")
        self.assertEqual(popped.id, first.id)
        service.requeue_turn_front(popped)
        self.assertEqual([item.id for item in host._thread_queue("t1")], [first.id, second.id])

    def test_queue_hot_path_uses_per_thread_records(self) -> None:
        host = _Host()
        service = TurnExecutionService(host)

        queued = service.enqueue_turn(
            thread_id="t1",
            project_id="p1",
            message="one",
        )
        self.assertEqual(host.bulk_queue_loads, 0)

        self.assertEqual(service.pop_next_queued_turn("t1").id, queued.id)
        self.assertEqual(host.bulk_queue_loads, 0)

    def test_active_turn_hot_path_uses_per_thread_records(self) -> None:
        host = _Host()
        service = TurnExecutionService(host)

        service.mark_thread_active("t1", turn_id="turn-1")
        self.assertTrue(service.thread_is_active("t1"))
        service.clear_thread_active("t1", turn_id="turn-1")

        self.assertEqual(host.bulk_active_loads, 0)
        self.assertNotIn("t1", host.active)

    def test_duplicate_source_and_message_reuses_existing_queue_item(self) -> None:
        host = _Host()
        service = TurnExecutionService(host)

        first = service.enqueue_turn(
            thread_id="t1",
            project_id="p1",
            message="same",
            source="slack",
        )
        duplicate = service.enqueue_turn(
            thread_id="t1",
            project_id="p1",
            message="same",
            source="slack",
        )

        self.assertIs(first, duplicate)
        self.assertEqual(host._thread_queue_depth("t1"), 1)


class _BindingService:
    def __init__(self) -> None:
        self.calls = []

    def prepare(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            assignment_id="assignment-1",
            workspace_id="workspace-1",
        )


class _CredentialMissingBindingService(_BindingService):
    def prepare(self, **kwargs):
        self.calls.append(kwargs)
        raise TurnExecutionBindingError(
            "worker credential reference configuration is unavailable for openai/codex",
            code="credential_reference_missing",
        )


class _Session:
    def __init__(self) -> None:
        self.workspace_path = Path("/isolated/workspace")
        self.requests = []
        self.work_item_ref = None

    def status(self):
        return SimpleNamespace(worker_id="worker-1", fence=7)

    def validate_current(self):
        return SimpleNamespace(
            id="assignment-1",
            execution_id="bootstrap-exec",
            execution_workspace_id="workspace-1",
            project_id="p1",
            sandbox="workspace-write",
            approval_policy="on-request",
            work_item_ref=self.work_item_ref,
        )

    async def request(self, method, params=None):
        self.requests.append((method, params))
        if method == "thread/resume":
            return {"thread": {"id": "t1"}}
        if method == "turn/start":
            return {"turn": {"id": "turn-1"}}
        return {"ok": True}


class _AlternateTurnAdapter:
    provider_id = "anthropic"
    runtime_id = "claude-code"
    runtime_type = "claude-agent-sdk"

    def __init__(self, session) -> None:
        self.session = session
        self.resumed = []
        self.turns = []

    async def resume_session(self, native_session_id, request):
        self.resumed.append((native_session_id, request))
        return AgentRuntimeResult(
            provider_native_session_id=native_session_id,
            payload={"type": "system", "subtype": "resumed"},
        )

    async def start_turn(self, native_session_id, request):
        self.turns.append((native_session_id, request))
        return AgentRuntimeResult(
            provider_native_session_id=native_session_id,
            provider_native_turn_id="claude-turn-1",
            payload={
                "type": "result",
                "subtype": "success",
                "session_id": native_session_id,
            },
        )


class _AlternateSession(_Session):
    def __init__(self, binding) -> None:
        super().__init__()
        self.binding = binding
        self.runtime = SimpleNamespace(native_session_id="claude-native-session")

    def validate_current(self):
        value = super().validate_current()
        value.runtime_binding = self.binding
        return value


class _SessionManager:
    def __init__(self) -> None:
        self.session = _Session()
        self.started = []
        self.completed = []

    async def start(self, assignment_id):
        self.started.append(assignment_id)
        return self.session

    def get(self, assignment_id):
        return self.session if assignment_id == "assignment-1" else None

    async def complete(self, assignment_id, **kwargs):
        self.completed.append((assignment_id, kwargs))
        return SimpleNamespace(id=assignment_id)


class _BootstrapBindings:
    def __init__(self, thread_id: str | None = None) -> None:
        self.thread_id = thread_id

    def get_by_thread(self, thread_id, actor):
        if self.thread_id != thread_id:
            raise ThreadBootstrapBindingNotFoundError(
                "thread bootstrap binding not found"
            )
        return SimpleNamespace(
            bootstrap_id="bootstrap-1",
            thread_id=thread_id,
            execution_id="bootstrap-exec",
            assignment_id="assignment-1",
            execution_workspace_id="workspace-1",
        )


class TurnExecutionStartTests(unittest.IsolatedAsyncioTestCase):
    def _service(
        self,
        *,
        bootstrap_thread_id: str | None = None,
        **service_kwargs,
    ):
        host = _Host()
        binding = _BindingService()
        sessions = _SessionManager()
        service = TurnExecutionService(
            host,
            binding_service=binding,
            session_manager=sessions,
            bootstrap_bindings=_BootstrapBindings(bootstrap_thread_id),
            control_actor=SimpleNamespace(identity_id="control"),
            **service_kwargs,
        )
        return host, binding, sessions, service

    def _credential_missing_service(self):
        host = _Host()
        binding = _CredentialMissingBindingService()
        sessions = _SessionManager()
        service = TurnExecutionService(
            host,
            binding_service=binding,
            session_manager=sessions,
            bootstrap_bindings=_BootstrapBindings(),
            control_actor=SimpleNamespace(identity_id="control"),
        )
        return host, binding, sessions, service

    async def test_explicit_trusted_local_web_turn_uses_authenticated_app_server(self) -> None:
        host, binding, sessions, service = self._credential_missing_service()
        project = Project(
            id="p1",
            name="Project",
            path="/workspace/project",
            sandbox="danger-full-access",
            approval_policy="never",
        )

        async def request(method, params=None):
            if method == "thread/resume":
                return {"thread": {"id": "t1"}}
            if method == "turn/start":
                return {"turn": {"id": "turn-local"}}
            return {"ok": True}

        host.codex.request.side_effect = request
        with patch.dict(
            "os.environ",
            {"CODEX_WEB_TRUSTED_LOCAL_CODEX_SESSION": "1"},
        ):
            result = await service.start_thread_turn_now(
                "t1",
                project=project,
                message="do local work",
                sandbox="danger-full-access",
                approval_policy="never",
                source="web",
                execution_id="exec-local",
            )

        self.assertEqual(result["turn"]["id"], "turn-local")
        self.assertEqual(binding.calls, [])
        self.assertEqual(sessions.started, [])
        calls = host.codex.request.await_args_list
        self.assertEqual([call.args[0] for call in calls], ["thread/resume", "turn/start"])
        self.assertEqual(calls[0].args[1]["cwd"], "/workspace/project")
        self.assertEqual(calls[1].args[1]["cwd"], "/workspace/project")
        self.assertEqual(
            calls[1].args[1]["sandboxPolicy"],
            {"type": "danger-full-access"},
        )
        active = host.active["t1"]
        self.assertIsNone(active.assignment_id)
        self.assertIsNone(active.execution_workspace_id)
        self.assertEqual(active.worker_id, "local-codex-app-server")
        self.assertEqual(
            service.last_inputs["t1"]["authentication_source"],
            "local_codex_session",
        )
        self.assertEqual(
            host.events[-1]["authentication_source"],
            "local_codex_session",
        )

    async def test_trusted_local_mode_does_not_apply_to_unattended_turns(self) -> None:
        host, binding, sessions, service = self._credential_missing_service()
        project = Project(id="p1", name="Project", path="/workspace/project")

        with patch.dict(
            "os.environ",
            {"CODEX_WEB_TRUSTED_LOCAL_CODEX_SESSION": "1"},
        ):
            with self.assertRaises(HTTPException) as caught:
                await service.start_thread_turn_now(
                    "t1",
                    project=project,
                    message="unattended work",
                    sandbox="workspace-write",
                    approval_policy="never",
                    source="slack",
                )

        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(
            caught.exception.detail["blockers"][0]["code"],
            "credential_reference_missing",
        )
        self.assertEqual(len(binding.calls), 1)
        self.assertEqual(sessions.started, [])
        host.codex.request.assert_not_awaited()

    async def test_trusted_local_mode_fails_clearly_when_app_server_is_unavailable(self) -> None:
        host, binding, sessions, service = self._credential_missing_service()
        project = Project(id="p1", name="Project", path="/workspace/project")
        host.codex.request.side_effect = RuntimeError("local Codex app-server unavailable")

        with patch.dict(
            "os.environ",
            {"CODEX_WEB_TRUSTED_LOCAL_CODEX_SESSION": "1"},
        ):
            with self.assertRaisesRegex(RuntimeError, "app-server unavailable"):
                await service.start_thread_turn_now(
                    "t1",
                    project=project,
                    message="local work",
                    sandbox="workspace-write",
                    approval_policy="never",
                    source="web",
                )

        self.assertEqual(binding.calls, [])
        self.assertEqual(sessions.started, [])
        self.assertNotIn("t1", host.active)

    async def test_start_routes_resume_and_turn_start_through_assignment_bound_session(self) -> None:
        host, binding, sessions, service = self._service()
        project = Project(
            id="p1",
            name="Project",
            path="/workspace/project",
            sandbox="workspace-write",
            approval_policy="on-request",
        )

        result = await service.start_thread_turn_now(
            "t1",
            project=project,
            message="do work",
            sandbox="workspace-write",
            approval_policy="on-request",
            source="web",
            execution_id="exec-1",
        )

        self.assertEqual(result["turn"]["id"], "turn-1")
        host.codex.request.assert_not_awaited()
        self.assertEqual(sessions.started, ["assignment-1"])
        self.assertEqual(
            [method for method, _params in sessions.session.requests],
            ["thread/resume", "turn/start"],
        )
        resume = sessions.session.requests[0][1]
        turn = sessions.session.requests[1][1]
        self.assertEqual(resume["cwd"], "/isolated/workspace")
        self.assertEqual(turn["cwd"], "/isolated/workspace")
        self.assertEqual(binding.calls[0]["execution_id"], "exec-1")
        active = host.active["t1"]
        self.assertEqual(active.turn_id, "turn-1")
        self.assertEqual(active.execution_id, "exec-1")
        self.assertEqual(active.assignment_id, "assignment-1")
        self.assertEqual(active.execution_workspace_id, "workspace-1")
        self.assertEqual(active.worker_id, "worker-1")
        self.assertEqual(active.fence, 7)
        self.assertEqual(service.last_inputs["t1"]["assignment_id"], "assignment-1")
        self.assertEqual(host.events[-1]["assignment_id"], "assignment-1")
        self.assertEqual(host.hub.events[-1]["type"], "queue.status")

    async def test_work_item_metadata_supplies_writable_repository_scope(self) -> None:
        host, binding, _sessions, service = self._service()
        ref = "group/app#613"
        state = WorkItemState(
            ref=ref,
            project_id="p1",
            current_owner="james",
            current_stage="implementation_active",
            last_meaningful_update_at=1.0,
            updated_at=1.0,
            created_at=1.0,
        )
        state.execution.writable_repository_resource_ids = (
            "repo-app",
            "repo-api",
        )
        host.work_item_states[ref] = state
        project = Project(
            id="p1",
            name="Project",
            path="/workspace/project",
            sandbox="workspace-write",
            approval_policy="on-request",
        )

        await service.start_thread_turn_now(
            "t1",
            project=project,
            message="coordinate work item repositories",
            sandbox="workspace-write",
            approval_policy="on-request",
            execution_id="exec-work-item-scope",
            work_item_ref=ref,
        )

        self.assertEqual(
            binding.calls[0]["writable_repository_ids"],
            ("repo-app", "repo-api"),
        )
        self.assertEqual(
            binding.calls[0]["writable_repository_source"],
            RepositoryTargetSource.WORK_ITEM,
        )

    async def test_explicit_writable_scope_conflicting_with_work_item_fails_closed(self) -> None:
        host, binding, sessions, service = self._service()
        ref = "group/app#613"
        state = WorkItemState(
            ref=ref,
            project_id="p1",
            current_owner="james",
            current_stage="implementation_active",
            last_meaningful_update_at=1.0,
            updated_at=1.0,
            created_at=1.0,
        )
        state.execution.writable_repository_resource_ids = (
            "repo-app",
            "repo-api",
        )
        host.work_item_states[ref] = state
        project = Project(
            id="p1",
            name="Project",
            path="/workspace/project",
            sandbox="workspace-write",
            approval_policy="on-request",
        )

        with self.assertRaises(HTTPException) as caught:
            await service.start_thread_turn_now(
                "t1",
                project=project,
                message="try conflicting scope",
                sandbox="workspace-write",
                approval_policy="on-request",
                execution_id="exec-conflicting-scope",
                work_item_ref=ref,
                writable_repository_resource_ids=(
                    "repo-app",
                    "repo-other",
                ),
            )

        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(
            caught.exception.detail["code"],
            "work_item_repository_scope_conflict",
        )
        self.assertEqual(binding.calls, [])
        self.assertEqual(sessions.started, [])

    async def test_work_item_delta_is_delivered_and_recorded_after_turn_start(self) -> None:
        recorded = []
        selection = {
            "mode": "delta",
            "reason": "verified_checkpoint_delta",
            "checkpoint_id": "checkpoint-4",
            "changed_fields": {"title": "Updated"},
            "removed_fields": [],
            "events": [{"event_type": "comment_added"}],
            "current": {"objective": "Finish issue #531"},
            "requires_progressive_retrieval": False,
            "metrics": {
                "baseline_context_bytes": 4096,
                "delta_context_bytes": 256,
                "estimated_tokens_reused": 960,
            },
            "snapshot": {
                "work_item_revision": "revision-5",
                "work_item_hash": "sha256:current",
                "event_watermark": "wi-events-v1:4:digest",
                "work_item_context": {"title": "Updated"},
            },
        }

        host, _binding, sessions, service = self._service(
            work_item_context_resolver=lambda ref: selection,
            work_item_context_recorder=lambda *args, **kwargs: recorded.append(
                (args, kwargs)
            ),
        )
        project = Project(
            id="p1",
            name="Project",
            path="/workspace/project",
            sandbox="workspace-write",
            approval_policy="on-request",
        )

        await service.start_thread_turn_now(
            "t1",
            project=project,
            message="continue work",
            sandbox="workspace-write",
            approval_policy="on-request",
            source="web",
            execution_id="exec-context",
            work_item_ref="group/app#531",
        )

        turn = sessions.session.requests[1][1]
        instructions = turn["developerInstructions"]
        self.assertIn("Canonical Work Item continuation context", instructions)
        self.assertIn('"mode":"delta"', instructions)
        self.assertIn('"title":"Updated"', instructions)
        self.assertNotIn('"work_item_context"', instructions)
        self.assertEqual(len(recorded), 1)
        args, kwargs = recorded[0]
        self.assertEqual(args[0], "group/app#531")
        self.assertEqual(args[1], "exec-context")
        self.assertEqual(args[2], selection)
        self.assertEqual(args[3]["mode"], "delta")
        self.assertEqual(
            kwargs["provenance"]["session_ref"],
            "t1",
        )

    async def test_untrusted_checkpoint_falls_back_to_full_work_item_context(self) -> None:
        selection = {
            "mode": "full",
            "reason": "checkpoint_provenance_incomplete",
            "blockers": ["delivery_not_proven"],
            "snapshot": {
                "work_item_context": {
                    "title": "Canonical full context",
                    "current_stage": "implementation_active",
                },
            },
        }
        _host, _binding, sessions, service = self._service(
            work_item_context_resolver=lambda ref: selection,
            work_item_context_recorder=lambda *args, **kwargs: None,
        )
        project = Project(
            id="p1",
            name="Project",
            path="/workspace/project",
            sandbox="workspace-write",
            approval_policy="on-request",
        )

        await service.start_thread_turn_now(
            "t1",
            project=project,
            message="continue safely",
            sandbox="workspace-write",
            approval_policy="on-request",
            execution_id="exec-full-context",
            work_item_ref="group/app#531",
        )

        instructions = sessions.session.requests[1][1]["developerInstructions"]
        self.assertIn('"mode":"full"', instructions)
        self.assertIn('"work_item_context"', instructions)
        self.assertIn("Canonical full context", instructions)
        self.assertIn("checkpoint_provenance_incomplete", instructions)

    async def test_bootstrap_turn_inherits_work_item_ref_from_assignment(self) -> None:
        resolved_refs = []
        selection = {
            "mode": "full",
            "reason": "checkpoint_missing",
            "snapshot": {
                "work_item_context": {"title": "Bootstrap Work Item"},
            },
        }
        _host, _binding, sessions, service = self._service(
            bootstrap_thread_id="t1",
            work_item_context_resolver=lambda ref: (
                resolved_refs.append(ref) or selection
            ),
            work_item_context_recorder=lambda *args, **kwargs: None,
        )
        sessions.session.work_item_ref = "group/app#531"
        project = Project(
            id="p1",
            name="Project",
            path="/workspace/project",
            sandbox="workspace-write",
            approval_policy="on-request",
        )

        await service.start_thread_turn_now(
            "t1",
            project=project,
            message="continue bootstrap work",
            sandbox="workspace-write",
            approval_policy="on-request",
            execution_id="ignored-bootstrap-exec",
        )

        self.assertEqual(resolved_refs, ["group/app#531"])
        self.assertIn(
            "Bootstrap Work Item",
            sessions.session.requests[1][1]["developerInstructions"],
        )

    async def test_work_item_context_resolution_failure_blocks_before_runtime_turn(self) -> None:
        def fail_resolution(_ref):
            raise RuntimeError("canonical context store unavailable")

        _host, _binding, sessions, service = self._service(
            work_item_context_resolver=fail_resolution,
        )
        project = Project(
            id="p1",
            name="Project",
            path="/workspace/project",
            sandbox="workspace-write",
            approval_policy="on-request",
        )

        with self.assertRaises(HTTPException) as caught:
            await service.start_thread_turn_now(
                "t1",
                project=project,
                message="continue safely",
                sandbox="workspace-write",
                approval_policy="on-request",
                execution_id="exec-context-failure",
                work_item_ref="group/app#531",
            )

        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(
            caught.exception.detail["code"],
            "work_item_context_unavailable",
        )
        self.assertEqual(
            [method for method, _params in sessions.session.requests],
            [],
        )

    async def test_checkpoint_write_failure_does_not_fail_accepted_runtime_turn(self) -> None:
        selection = {
            "mode": "full",
            "reason": "checkpoint_missing",
            "snapshot": {
                "work_item_context": {"title": "Safe full context"},
            },
        }

        def fail_recording(*_args, **_kwargs):
            raise RuntimeError("checkpoint store unavailable")

        _host, _binding, sessions, service = self._service(
            work_item_context_resolver=lambda ref: selection,
            work_item_context_recorder=fail_recording,
        )
        project = Project(
            id="p1",
            name="Project",
            path="/workspace/project",
            sandbox="workspace-write",
            approval_policy="on-request",
        )

        result = await service.start_thread_turn_now(
            "t1",
            project=project,
            message="continue safely",
            sandbox="workspace-write",
            approval_policy="on-request",
            execution_id="exec-record-failure",
            work_item_ref="group/app#531",
        )

        self.assertEqual(result["turn"]["id"], "turn-1")
        self.assertEqual(
            [method for method, _params in sessions.session.requests],
            ["thread/resume", "turn/start"],
        )

    async def test_idle_bootstrap_thread_request_uses_live_private_session(self) -> None:
        host, _binding, sessions, service = self._service(
            bootstrap_thread_id="t1"
        )

        response = await service.request_for_thread(
            "t1",
            "thread/read",
            {"threadId": "t1", "includeTurns": True},
        )

        self.assertEqual(response, {"ok": True})
        host.codex.request.assert_not_awaited()
        self.assertEqual(
            sessions.session.requests,
            [
                (
                    "thread/read",
                    {"threadId": "t1", "includeTurns": True},
                )
            ],
        )

    async def test_bootstrap_binding_without_live_session_fails_without_global_fallback(self) -> None:
        host, _binding, sessions, service = self._service(
            bootstrap_thread_id="t1"
        )
        sessions.session = None

        with self.assertRaises(HTTPException) as caught:
            await service.request_for_thread(
                "t1",
                "thread/read",
                {"threadId": "t1"},
            )

        self.assertEqual(caught.exception.status_code, 503)
        self.assertIn("bootstrap binding", caught.exception.detail)
        host.codex.request.assert_not_awaited()

    async def test_turn_on_bootstrap_thread_reuses_original_assignment_and_session(self) -> None:
        host, binding, sessions, service = self._service(
            bootstrap_thread_id="t1"
        )
        project = Project(
            id="p1",
            name="Project",
            path="/workspace/project",
            sandbox="workspace-write",
            approval_policy="on-request",
        )

        response = await service.start_thread_turn_now(
            "t1",
            project=project,
            message="continue work",
            sandbox="workspace-write",
            approval_policy="on-request",
            source="web",
            execution_id="queued-turn-exec",
        )

        self.assertEqual(response["turn"]["id"], "turn-1")
        host.codex.request.assert_not_awaited()
        self.assertEqual(binding.calls, [])
        self.assertEqual(sessions.started, [])
        self.assertEqual(
            [method for method, _params in sessions.session.requests],
            ["thread/resume", "turn/start"],
        )
        active = host.active["t1"]
        self.assertEqual(active.execution_id, "bootstrap-exec")
        self.assertEqual(active.assignment_id, "assignment-1")
        self.assertEqual(active.execution_workspace_id, "workspace-1")
        self.assertEqual(
            service.last_inputs["t1"]["requested_execution_id"],
            "queued-turn-exec",
        )
        self.assertEqual(
            service.last_inputs["t1"]["bootstrap_id"],
            "bootstrap-1",
        )


    async def test_bootstrap_turn_completion_finalizes_work_item_checkpoint_outcome(self) -> None:
        outcomes = []
        _host, _binding, sessions, service = self._service(
            bootstrap_thread_id="t1",
            work_item_outcome_recorder=lambda *args: outcomes.append(args),
        )
        sessions.session.work_item_ref = "group/app#531"
        service.mark_thread_active(
            "t1",
            turn_id="turn-1",
            project_id="p1",
            execution_id="bootstrap-exec",
            assignment_id="assignment-1",
            execution_workspace_id="workspace-1",
            worker_id="worker-1",
            fence=7,
        )

        service.record_thread_activity(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "t1",
                    "turn": {"id": "turn-1"},
                },
            }
        )

        self.assertEqual(
            outcomes,
            [("group/app#531", "bootstrap-exec", "succeeded")],
        )

    async def test_bootstrap_turn_dispatches_to_owning_non_codex_runtime(self) -> None:
        host = _Host()
        binding_service = _BindingService()
        default_manager = _SessionManager()
        default_manager.session = None
        selected = ExecutionRuntimeBinding(
            provider_id="anthropic",
            runtime_id="claude-code",
            capability_revision=1,
        )
        claude_manager = _SessionManager()
        claude_manager.session = _AlternateSession(selected)
        adapters = []

        def factory(binding, session):
            self.assertEqual(binding, selected)
            adapter = _AlternateTurnAdapter(session)
            adapters.append(adapter)
            return adapter

        service = TurnExecutionService(
            host,
            binding_service=binding_service,
            session_manager=default_manager,
            bootstrap_bindings=_BootstrapBindings("t1"),
            control_actor=SimpleNamespace(identity_id="control"),
            session_managers={
                ("openai", "codex"): default_manager,
                ("anthropic", "claude-code"): claude_manager,
            },
            runtime_adapter_factory=factory,
        )
        project = Project(
            id="p1",
            name="Project",
            path="/workspace/project",
            sandbox="workspace-write",
            approval_policy="on-request",
        )

        response = await service.start_thread_turn_now(
            "t1",
            project=project,
            message="continue with Claude",
            sandbox="workspace-write",
            approval_policy="on-request",
            execution_id="ignored-for-bootstrap",
        )

        self.assertEqual(response["type"], "result")
        self.assertEqual(default_manager.started, [])
        self.assertEqual(len(adapters), 1)
        adapter = adapters[0]
        self.assertEqual(adapter.resumed[0][0], "claude-native-session")
        self.assertEqual(adapter.turns[0][0], "claude-native-session")
        self.assertEqual(host.active["t1"].turn_id, "claude-turn-1")
        self.assertEqual(
            service.last_inputs["t1"]["assignment_id"],
            "assignment-1",
        )

    async def test_terminal_bootstrap_turn_retains_session_assignment(self) -> None:
        host, _binding, sessions, service = self._service(
            bootstrap_thread_id="t1"
        )
        service.mark_thread_active(
            "t1",
            turn_id="turn-1",
            project_id="p1",
            execution_id="bootstrap-exec",
            assignment_id="assignment-1",
            execution_workspace_id="workspace-1",
            worker_id="worker-1",
            fence=7,
        )

        service.record_thread_activity(
            {
                "method": "turn/completed",
                "params": {"threadId": "t1", "turn": {"id": "turn-1"}},
            }
        )

        await asyncio.sleep(0)
        self.assertNotIn("t1", host.active)
        self.assertEqual(sessions.completed, [])
        self.assertEqual(
            host.events[-1]["type"],
            "thread_bootstrap_turn_completed",
        )
        self.assertTrue(host.events[-1]["session_retained"])

    async def test_start_rechecks_active_state_under_shared_start_lock(self) -> None:
        host, binding, sessions, service = self._service()
        project = Project(
            id="p1",
            name="Project",
            path="/workspace/project",
            sandbox="workspace-write",
            approval_policy="on-request",
        )
        service.mark_thread_active(
            "t1",
            execution_id="already-running",
            assignment_id="assignment-existing",
        )

        with self.assertRaises(HTTPException) as caught:
            await service.start_thread_turn_now(
                "t1",
                project=project,
                message="racing turn",
                sandbox="workspace-write",
                approval_policy="on-request",
                execution_id="exec-race",
            )

        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(
            caught.exception.detail["code"],
            "thread_turn_already_active",
        )
        self.assertEqual(binding.calls, [])
        self.assertEqual(sessions.started, [])
        host.codex.request.assert_not_awaited()

    async def test_thread_resume_failure_completes_assignment_before_turn_start(self) -> None:
        host, _binding, sessions, service = self._service()
        project = Project(
            id="p1",
            name="Project",
            path="/workspace/project",
            sandbox="workspace-write",
            approval_policy="on-request",
        )
        sessions.session.request = AsyncMock(side_effect=RuntimeError("resume failed"))

        with self.assertRaisesRegex(RuntimeError, "resume failed"):
            await service.start_thread_turn_now(
                "t1",
                project=project,
                message="do work",
                sandbox="workspace-write",
                approval_policy="on-request",
                execution_id="exec-1",
            )

        host.codex.request.assert_not_awaited()
        self.assertNotIn("t1", host.active)
        self.assertEqual(len(sessions.completed), 1)
        assignment_id, kwargs = sessions.completed[0]
        self.assertEqual(assignment_id, "assignment-1")
        self.assertFalse(kwargs["succeeded"])
        self.assertEqual(kwargs["failure_code"], "codex_thread_resume_failed")

    async def test_active_thread_request_never_falls_back_when_session_missing(self) -> None:
        host, _binding, _sessions, service = self._service()
        service.mark_thread_active(
            "t1",
            execution_id="exec-1",
            assignment_id="assignment-missing",
        )

        with self.assertRaisesRegex(HTTPException, "no live Codex session"):
            await service.request_for_thread(
                "t1",
                "turn/interrupt",
                {"threadId": "t1"},
            )

        host.codex.request.assert_not_awaited()

    async def test_completed_activity_schedules_exact_assignment_completion(self) -> None:
        host, _binding, sessions, service = self._service()
        service.mark_thread_active(
            "t1",
            turn_id="turn-1",
            project_id="p1",
            execution_id="exec-1",
            assignment_id="assignment-1",
            execution_workspace_id="workspace-1",
            worker_id="worker-1",
            fence=7,
        )

        service.record_thread_activity(
            {
                "method": "turn/completed",
                "params": {"threadId": "t1", "turn": {"id": "turn-1"}},
            }
        )
        await asyncio.gather(*list(service.assignment_completion_tasks.values()))

        self.assertNotIn("t1", host.active)
        self.assertEqual(len(sessions.completed), 1)
        assignment_id, kwargs = sessions.completed[0]
        self.assertEqual(assignment_id, "assignment-1")
        self.assertTrue(kwargs["succeeded"])
        self.assertEqual(host.events[-1]["type"], "turn_assignment_completed")

    async def test_failed_activity_marks_assignment_failed(self) -> None:
        host, _binding, sessions, service = self._service()
        service.mark_thread_active(
            "t1",
            turn_id="turn-1",
            execution_id="exec-1",
            assignment_id="assignment-1",
        )

        service.record_thread_activity(
            {
                "method": "turn/failed",
                "params": {
                    "threadId": "t1",
                    "turn": {"id": "turn-1"},
                    "error": "provider failure",
                },
            }
        )
        await asyncio.gather(*list(service.assignment_completion_tasks.values()))

        _assignment_id, kwargs = sessions.completed[0]
        self.assertFalse(kwargs["succeeded"])
        self.assertEqual(kwargs["failure_code"], "codex_turn_failed")
        self.assertIn("provider failure", kwargs["failure_message"])


class TurnServiceConcurrentStartTests(unittest.IsolatedAsyncioTestCase):
    async def test_web_start_queues_when_thread_becomes_active_during_start_lock(self) -> None:
        host = _Host()
        project = Project(
            id="p1",
            name="Project",
            path="/workspace/project",
            sandbox="workspace-write",
            approval_policy="on-request",
        )
        host._truncate_text = lambda value, limit: str(value)[:limit]
        queue_owner = TurnExecutionService(host)
        queue_owner.publish_queue_status = AsyncMock()
        queue_owner.start_thread_turn_now = AsyncMock(
            side_effect=HTTPException(
                status_code=409,
                detail={
                    "code": "thread_turn_already_active",
                    "threadId": "t1",
                },
            )
        )
        queue_owner.schedule_queue_drain = lambda _thread_id: None

        projects = SimpleNamespace(
            get=lambda _project_id: project,
            params=lambda _project, values=None: values or {},
        )
        settings = SimpleNamespace(
            get=lambda _thread_id: ThreadRunSettings(),
            remember=lambda *_args, **kwargs: ThreadRunSettings(**kwargs),
        )
        recovery = SimpleNamespace(
            raise_if_thread_replaced=lambda _thread_id: None,
            release_stale_active_turn=lambda *_args: None,
        )
        resume_runtime = SimpleNamespace(
            is_timeout_error=lambda _exc: False,
            is_stale_thread_error=lambda _exc: False,
        )
        bindings = SimpleNamespace(for_thread=lambda _thread_id: [])
        queue_policy = SimpleNamespace(
            depth=lambda thread_id: len(host._thread_queue(thread_id)),
            queue=lambda thread_id: host._thread_queue(thread_id),
            record_steer=lambda _thread_id: None,
        )
        service = TurnService(
            projects=projects,
            settings=settings,
            recovery=recovery,
            resume_runtime=resume_runtime,
            bindings=bindings,
            queue_policy=queue_policy,
            execution=queue_owner,
            event_sink=host._append_bot_event,
            truncate_text=host._truncate_text,
            binding_public=lambda binding: binding.model_dump(),
        )

        result = await service.start(
            "t1",
            TurnCreate(project_id="p1", message="second request"),
        )

        self.assertTrue(result["queued"])
        self.assertEqual(result["queueDepth"], 1)
        self.assertEqual(host._thread_queue_depth("t1"), 1)
        self.assertEqual(
            host.events[-1]["type"],
            "web_turn_queued_after_concurrent_start",
        )


class TurnExecutionInstallationTests(unittest.TestCase):
    def test_installer_rebinds_execution_entrypoints_and_registries(self) -> None:
        app = FastAPI()
        host = _Host()

        first = install_turn_execution_service(app, host)
        second = install_turn_execution_service(app, host)

        self.assertIs(first, second)
        self.assertIs(app.state.turn_execution_service, first)
        self.assertIs(host._enqueue_turn.__self__, first)
        self.assertIs(host._start_thread_turn_now.__self__, first)
        self.assertIs(host._codex_request_for_thread.__self__, first)
        self.assertIs(host._schedule_queue_drain.__self__, first)
        self.assertIs(host._record_terminal_turn_result.__self__, first)
        self.assertIs(host.CODEX_TURN_START_LOCK, first.turn_start_lock)
        self.assertIs(host.QUEUE_DRAIN_TASKS, first.queue_drain_tasks)
        self.assertIs(host.TERMINAL_RECOVERY_TASKS, first.terminal_recovery_tasks)
        self.assertIs(host.THREAD_TERMINAL_FAILURES, first.terminal_failures)
        self.assertIs(host.THREAD_LAST_INPUTS, first.last_inputs)
        self.assertIs(host.ASSIGNMENT_COMPLETION_TASKS, first.assignment_completion_tasks)
        self.assertIs(
            host.THREAD_ASSIGNMENT_COMPLETION_TASKS,
            first.thread_completion_tasks,
        )


if __name__ == "__main__":
    unittest.main()
