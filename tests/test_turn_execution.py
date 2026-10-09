from __future__ import annotations

import asyncio
import tempfile
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI, HTTPException

from codex_web.agent_runtime import AgentRuntimeResult
from codex_web.agent_profiles import AgentProfileExecutionBinding
from codex_web.execution_workers import ExecutionRuntimeBinding
from codex_web.models import (
    ActiveThreadTurn,
    Project,
    ThreadRunSettings,
    TurnCreate,
    WorkItemState,
)
from codex_web.resources import RepositoryTargetSource
from codex_web.runtime.execution import (
    TurnExecutionService,
    _turn_failure_text,
    install_turn_execution_service,
)
from codex_web.services.agent_routing import AgentRoutingError
from codex_web.services.local_execution_worker import LocalExecutionWorkerCapacityError
from codex_web.services.agent_worker_session import (
    AssignmentBoundAgentSessionStaleError,
)
from codex_web.services.turns import TurnService
from codex_web.services.work_item_contracts import assignment_control_plane_instructions
from codex_web.services.thread_bootstrap_bindings import (
    ThreadBootstrapBindingNotFoundError,
)
from codex_web.services.turn_execution_binding import TurnExecutionBindingError
from codex_web.storage.runtime_state import RuntimeStateRepositories
from codex_web.storage.sqlite_state import SQLiteStateStore


OBSERVED_WORK_ITEM_COUNT = 1_728


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

    @staticmethod
    def _is_codex_timeout_error(exc):
        return isinstance(exc, (TimeoutError, asyncio.TimeoutError)) or (
            "timed out" in str(exc).casefold()
        )


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


class TurnExecutionQueueTests(unittest.IsolatedAsyncioTestCase):
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

    def test_nonterminal_runtime_event_refreshes_active_turn_heartbeat(self) -> None:
        host = _Host()
        service = TurnExecutionService(host)
        host.active["t1"] = ActiveThreadTurn(
            thread_id="t1",
            turn_id="turn-1",
            assignment_id="assignment-1",
            execution_workspace_id="workspace-1",
            started_at=100.0,
            updated_at=100.0,
        )

        with patch("codex_web.runtime.execution.time.time", return_value=200.0):
            service.record_thread_activity(
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": "t1",
                        "turnId": "turn-1",
                        "item": {"type": "commandExecution"},
                    },
                }
            )

        active = host.active["t1"]
        self.assertEqual(active.updated_at, 200.0)
        self.assertEqual(active.assignment_id, "assignment-1")
        self.assertEqual(active.execution_workspace_id, "workspace-1")

    def test_work_item_repository_lookup_uses_only_one_of_observed_records(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteStateStore(root / "state.sqlite3")
            repositories = RuntimeStateRepositories(
                store,
                thread_settings_file=root / "thread-settings.json",
                active_turns_file=root / "active-turns.json",
                work_item_states_file=root / "work-items.json",
            )
            target_ref = f"work-{OBSERVED_WORK_ITEM_COUNT - 1}"
            records = {}
            for number in range(OBSERVED_WORK_ITEM_COUNT):
                state = WorkItemState(
                    ref=f"work-{number}",
                    project_id="project-a",
                    current_owner="james",
                    current_stage="implementation_active",
                    last_meaningful_update_at=1.0,
                    updated_at=1.0,
                    created_at=1.0,
                )
                if state.ref == target_ref:
                    state.execution.writable_repository_resource_ids = (
                        "repo-1",
                    )
                records[state.ref] = state.model_dump(mode="json")
            store.record_replace(
                repositories.work_item_states.namespace,
                records,
            )

            host = _Host()
            host._get_work_item_state_record = (
                repositories.work_item_states.get
            )
            host._load_work_item_states = MagicMock(
                side_effect=AssertionError("whole work item collection loaded")
            )
            service = TurnExecutionService(host)
            with patch.object(
                store,
                "record_items",
                side_effect=AssertionError("whole record collection loaded"),
            ), patch.object(
                store,
                "record_get",
                wraps=store.record_get,
            ) as point_read:
                self.assertEqual(
                    service._work_item_writable_repository_ids(target_ref),
                    ("repo-1",),
                )

            point_read.assert_called_once_with(
                repositories.work_item_states.namespace,
                target_ref,
            )
            host._load_work_item_states.assert_not_called()

    def test_stale_nonterminal_event_cannot_replace_or_renew_new_active_turn(self) -> None:
        host = _Host()
        service = TurnExecutionService(host)
        original = ActiveThreadTurn(
            thread_id="t1", turn_id="new-turn", assignment_id="assignment-1",
            execution_workspace_id="workspace-1", started_at=100.0, updated_at=100.0,
        )
        host.active["t1"] = original
        for method in ("item/completed", "item/agentMessage/delta", "thread/tokenUsage/updated"):
            with self.subTest(method=method), patch("codex_web.runtime.execution.time.time", return_value=200.0):
                service.record_thread_activity({"method": method, "params": {
                    "threadId": "t1", "turnId": "completed-old-turn",
                }})
            self.assertIs(host.active["t1"], original)
            self.assertNotIn("t1", service.activity_heartbeat_at)

    def test_turnless_nonterminal_event_preserves_current_turn_and_renews_heartbeat(self) -> None:
        host = _Host()
        service = TurnExecutionService(host)
        host.active["t1"] = ActiveThreadTurn(
            thread_id="t1", turn_id="new-turn", assignment_id="assignment-1",
            started_at=100.0, updated_at=100.0,
        )
        with patch("codex_web.runtime.execution.time.time", return_value=200.0):
            service.record_thread_activity({"method": "item/completed", "params": {"threadId": "t1"}})
        self.assertEqual(host.active["t1"].turn_id, "new-turn")
        self.assertEqual(host.active["t1"].assignment_id, "assignment-1")
        self.assertEqual(host.active["t1"].updated_at, 200.0)

    def test_streaming_heartbeat_avoids_per_chunk_storage_work(self) -> None:
        host = _Host()
        service = TurnExecutionService(host)
        with patch("codex_web.runtime.execution.time.time", return_value=200.0):
            service.mark_thread_active("t1", turn_id="turn-1")
        event = {"method": "item/agentMessage/delta", "params": {"threadId": "t1", "turnId": "turn-1"}}
        with patch("codex_web.runtime.execution.time.time", return_value=201.0), patch.object(service, "_active_turn") as read:
            service.record_thread_activity(event)
        read.assert_not_called()
        with patch("codex_web.runtime.execution.time.time", return_value=206.0):
            service.record_thread_activity(event)
        self.assertEqual(host.active["t1"].updated_at, 206.0)

    def test_handoff_fences_observational_active_turn_clears_until_finish(self) -> None:
        host = _Host()
        service = TurnExecutionService(host)
        service.mark_thread_active("t1", turn_id="turn-1")

        self.assertTrue(service.begin_thread_handoff("t1"))
        self.assertFalse(service.begin_thread_handoff("t1"))
        service.clear_thread_active("t1", turn_id="turn-1")

        self.assertTrue(service.thread_is_active("t1"))
        self.assertTrue(service.thread_handoff_in_progress("t1"))

        service.finish_thread_handoff("t1", clear_active=True)

        self.assertFalse(service.thread_handoff_in_progress("t1"))
        self.assertFalse(service.thread_is_active("t1"))

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

    async def test_queue_drain_remembers_repository_scope_before_start(self) -> None:
        host = _Host()
        project = Project(
            id="p1",
            name="Project",
            path="/workspace/project",
            sandbox="workspace-write",
            approval_policy="never",
            model="codex/gpt-5.6-sol",
        )
        host._project = lambda _project_id: project
        host._remember_thread_run_settings = MagicMock()
        service = TurnExecutionService(host)
        service.start_thread_turn_now = AsyncMock(return_value={"ok": True})
        service.enqueue_turn(
            thread_id="t1",
            project_id="p1",
            message="queued repository work",
            repository_resource_id="repo-1",
            writable_repository_resource_ids=("repo-1",),
            read_only_repository_resource_ids=("repo-2",),
            execution_profile_id="repository-write",
            model="codex/gpt-5.6-sol",
        )

        await service.drain_thread_queue("t1")

        host._remember_thread_run_settings.assert_called_once_with(
            "t1",
            sandbox="workspace-write",
            approval_policy="never",
            model="codex/gpt-5.6-sol",
            reasoning_effort=None,
            repository_resource_id="repo-1",
            writable_repository_resource_ids=("repo-1",),
            read_only_repository_resource_ids=("repo-2",),
            execution_profile_id="repository-write",
        )
        service.start_thread_turn_now.assert_awaited_once()
        self.assertEqual(host._thread_queue_depth("t1"), 0)

    async def test_profiled_queue_drain_resolves_persisted_actor(self) -> None:
        host = _Host()
        project = Project(id="p1", name="Project", path="/workspace/project")
        host._project = lambda _project_id: project
        actor = object()
        resolver = MagicMock(return_value=actor)
        service = TurnExecutionService(host, actor_resolver=resolver)
        service.start_thread_turn_now = AsyncMock(return_value={"ok": True})
        service.enqueue_turn(
            thread_id="t1", project_id="p1", message="profiled work",
            agent_profile_id="reviewer", agent_profile_revision=4,
            agent_profile_actor_id="requesting-human",
        )
        await service.drain_thread_queue("t1")
        resolver.assert_called_once_with("requesting-human", project)
        arguments = service.start_thread_turn_now.await_args.kwargs
        self.assertIs(arguments["actor"], actor)
        self.assertEqual(arguments["agent_profile_id"], "reviewer")
        self.assertEqual(arguments["agent_profile_revision"], 4)
        self.assertEqual(arguments["agent_profile_actor_id"], "requesting-human")
        self.assertEqual(host._thread_queue_depth("t1"), 0)

    async def test_worker_capacity_wait_does_not_exhaust_message_retries(self) -> None:
        host = _Host()
        host._project = lambda _project_id: Project(id="p1", name="Project", path="/workspace/project")
        service = TurnExecutionService(host)
        queued = service.enqueue_turn(thread_id="t1", project_id="p1", message="human request", source="slack")
        service.start_thread_turn_now = AsyncMock(side_effect=LocalExecutionWorkerCapacityError("worker full"))
        with patch.object(asyncio.get_running_loop(), "call_later") as retry:
            for _ in range(4):
                await service.drain_thread_queue("t1")
            self.assertEqual(retry.call_count, 4)
        self.assertEqual(host._thread_queue_depth("t1"), 1)
        self.assertEqual(service._thread_queue("t1")[0].id, queued.id)
        self.assertEqual(service._thread_queue("t1")[0].attempts, 0)
        service.start_thread_turn_now = AsyncMock(return_value={"ok": True})
        await service.drain_thread_queue("t1")
        self.assertEqual(host._thread_queue_depth("t1"), 0)

    async def test_stale_queued_web_thread_uses_canonical_replacement(self) -> None:
        host = _Host()
        project = Project(id="p1", name="Project", path="/workspace/project")
        host._project = lambda _project_id: project
        host._is_stale_thread_error = lambda _exc: True
        host._bindings_for_thread = lambda _thread_id: []
        host._replace_stale_web_thread = AsyncMock(return_value="t2")
        host._truncate_text = lambda value, limit: str(value)[:limit]
        service = TurnExecutionService(host)
        service.start_thread_turn_now = AsyncMock(side_effect=RuntimeError("thread not found"))
        service.publish_queue_status = AsyncMock()
        service.schedule_queue_drain = MagicMock()
        queued = service.enqueue_turn(thread_id="t1", project_id="p1", message="delivery", source="web",
                                      repository_resource_id="repo-1", execution_profile_id="repository-write")
        await service.drain_thread_queue("t1")
        await asyncio.sleep(0)
        host._replace_stale_web_thread.assert_awaited_once_with("t1", project, "thread not found")
        moved = host._thread_queue("t2")
        self.assertEqual(len(moved), 1)
        self.assertEqual(moved[0].id, queued.id)
        self.assertEqual(moved[0].repository_resource_id, "repo-1")
        self.assertEqual(moved[0].execution_profile_id, "repository-write")
        self.assertEqual(moved[0].attempts, 0)
        service.schedule_queue_drain.assert_called_once_with("t2")

    async def test_repeated_bot_resume_timeout_replaces_thread(self) -> None:
        host = _Host()
        project = Project(
            id="p1",
            name="Project",
            path="/workspace/project",
        )
        host._project = lambda _project_id: project
        host._remember_thread_run_settings = MagicMock()
        host._is_stale_thread_error = lambda _exc: False
        host._is_codex_timeout_error = lambda _exc: True
        host._thread_resume_retry_delay = lambda: 3600.0
        host._bindings_for_thread = lambda _thread_id: [
            SimpleNamespace(thread_id="t1")
        ]
        host._replace_stale_bot_thread = AsyncMock(
            return_value=SimpleNamespace(thread_id="t2")
        )
        host._truncate_text = lambda value, limit: str(value)[:limit]
        service = TurnExecutionService(host)
        service.start_thread_turn_now = AsyncMock(
            side_effect=HTTPException(
                status_code=504,
                detail="Codex app-server request timed out after 60 seconds",
            )
        )
        service.publish_queue_status = AsyncMock()
        service.enqueue_turn(
            thread_id="t1",
            project_id="p1",
            message="queued bot work",
        )

        await service.drain_thread_queue("t1")

        first_retry = host._thread_queue("t1")
        self.assertEqual(len(first_retry), 1)
        self.assertEqual(first_retry[0].attempts, 1)
        host._replace_stale_bot_thread.assert_not_awaited()

        service.schedule_queue_drain = MagicMock()
        await service.drain_thread_queue("t1")
        await asyncio.sleep(0)

        host._replace_stale_bot_thread.assert_awaited_once()
        self.assertEqual(host._thread_queue_depth("t1"), 0)
        replacement_queue = host._thread_queue("t2")
        self.assertEqual(len(replacement_queue), 1)
        self.assertEqual(replacement_queue[0].thread_id, "t2")
        self.assertEqual(replacement_queue[0].attempts, 0)
        service.schedule_queue_drain.assert_called_once_with("t2")
        self.assertEqual(
            host.events[-1]["type"],
            "queued_turn_retargeted_after_resume_timeout",
        )


class TurnExecutionRestartRecoveryTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _active() -> ActiveThreadTurn:
        return ActiveThreadTurn(
            thread_id="t1",
            project_id="p1",
            source="web",
            started_at=100.0,
            updated_at=100.0,
        )

    async def test_resume_releases_interrupted_marker_before_start(self) -> None:
        host = _Host()
        host.active["t1"] = self._active()
        project = Project(id="p1", name="Project", path="/workspace/project")
        host._project = lambda project_id: project
        service = TurnExecutionService(host)

        async def start(thread_id, **kwargs):
            self.assertFalse(service.thread_is_active(thread_id))
            service.mark_thread_active(
                thread_id,
                turn_id="replacement-turn",
                project_id="p1",
                source=kwargs["source"],
            )
            return {"turn": {"id": "replacement-turn"}}

        service.start_thread_turn_now = start

        await service.resume_active_threads_after_startup({"t1"})

        self.assertEqual(host.active["t1"].turn_id, "replacement-turn")
        self.assertEqual(host.active["t1"].resume_attempts, 0)
        self.assertEqual(host.events[-1]["type"], "active_thread_resumed")

    async def test_failed_resume_restores_marker_with_retry_count(self) -> None:
        host = _Host()
        host.active["t1"] = self._active()
        project = Project(id="p1", name="Project", path="/workspace/project")
        host._project = lambda project_id: project
        service = TurnExecutionService(host)

        async def fail(thread_id, **kwargs):
            self.assertFalse(service.thread_is_active(thread_id))
            raise RuntimeError("provider unavailable")

        service.start_thread_turn_now = fail

        await service.resume_active_threads_after_startup({"t1"})

        restored = host.active["t1"]
        self.assertEqual(restored.resume_attempts, 1)
        self.assertIsNotNone(restored.last_resume_at)
        self.assertEqual(host.events[-1]["type"], "active_thread_resume_failed")

class _BindingService:
    def __init__(self) -> None:
        self.calls = []

    def prepare(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            assignment_id="assignment-1",
            workspace_id="workspace-1",
        )

    def prepare_bootstrap(self, **kwargs):
        self.calls.append({"kind": "bootstrap", **kwargs})
        return SimpleNamespace(
            assignment_id="assignment-1",
            workspace_id="workspace-1",
            execution_id=kwargs.get("execution_id", "bootstrap-exec"),
        )


class _CredentialMissingBindingService(_BindingService):
    def prepare(self, **kwargs):
        self.calls.append(kwargs)
        raise TurnExecutionBindingError(
            "worker credential reference configuration is unavailable for openai/codex",
            code="credential_reference_missing",
        )


class _ExpiredBootstrapBindingService(_BindingService):
    def prepare_bootstrap(self, **kwargs):
        self.calls.append({"kind": "bootstrap", **kwargs})
        raise TurnExecutionBindingError(
            "Runtime authentication for mammouth-ai/mammouth-cli is expired.",
            code="authentication_expired",
            blocker={
                "code": "authentication_expired",
                "message": (
                    "Runtime authentication for mammouth-ai/mammouth-cli "
                    "is expired."
                ),
                "retryable": False,
                "target_type": "agent_runtime",
                "target_id": "mammouth-ai/mammouth-cli",
            },
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


class _StaleSession(_Session):
    def validate_current(self):
        raise AssignmentBoundAgentSessionStaleError("expired lease")


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


class _SessionlessCliSession(_AlternateSession):
    def __init__(self, binding) -> None:
        super().__init__(binding)
        self.runtime = SimpleNamespace(native_session_id=None)


class _SessionlessCliTimeoutAdapter:
    provider_id = "openai"
    runtime_id = "codex-cli"
    runtime_type = "cli"

    async def resume_session(self, native_session_id, request):
        return AgentRuntimeResult(
            provider_native_session_id=native_session_id,
            payload={"resumed": False},
        )

    async def start_turn(self, native_session_id, request):
        raise asyncio.TimeoutError("CLI exited before acquiring a session id")


class _SessionManager:
    def __init__(self) -> None:
        self.session = _Session()
        self.started = []
        self.completed = []
        self.stopped = []

    async def start(self, assignment_id):
        self.started.append(assignment_id)
        if self.session is None:
            self.session = _Session()
        return self.session

    def get(self, assignment_id):
        return self.session if assignment_id == "assignment-1" else None

    async def complete(self, assignment_id, **kwargs):
        self.completed.append((assignment_id, kwargs))
        return SimpleNamespace(id=assignment_id)

    async def stop(self, assignment_id):
        self.stopped.append(assignment_id)
        self.session = None


class _BootstrapBindings:
    def __init__(
        self,
        thread_id: str | None = None,
        *,
        initial_turn_pending: bool = False,
    ) -> None:
        self.thread_id = thread_id
        self.initial_turn_pending = initial_turn_pending
        self.rebinds = []
        self.initial_turn_started = []

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
            initial_turn_pending=self.initial_turn_pending,
        )

    def rebind(self, **kwargs):
        self.rebinds.append(kwargs)
        return SimpleNamespace(**kwargs)

    def mark_initial_turn_started(self, thread_id, actor):
        self.initial_turn_started.append((thread_id, actor))
        self.initial_turn_pending = False


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

    async def test_supersede_bootstrap_auth_failure_is_structured_preflight(self) -> None:
        host = _Host()
        binding = _ExpiredBootstrapBindingService()
        service = TurnExecutionService(
            host,
            binding_service=binding,
            session_manager=_SessionManager(),
            bootstrap_bindings=_BootstrapBindings(),
            control_actor=SimpleNamespace(identity_id="control"),
        )
        project = Project(id="p1", name="Project", path="/workspace/project")

        with self.assertRaises(HTTPException) as caught:
            await service._supersede_thread_bootstrap(
                thread_id="t1",
                project=project,
                runtime_binding=ExecutionRuntimeBinding(
                    provider_id="mammouth-ai",
                    runtime_id="mammouth-cli",
                    capability_revision=1,
                ),
                sandbox="workspace-write",
                approval_policy="on-request",
                execution_profile_id="repository-write",
                agent_profile=None,
                explicit_repository_id="repo-1",
                previous_assignment_id=None,
            )

        self.assertEqual(caught.exception.status_code, 503)
        detail = caught.exception.detail
        self.assertEqual(detail["code"], "execution_preflight_blocked")
        self.assertEqual(
            detail["blockers"][0]["code"],
            "authentication_expired",
        )
        self.assertFalse(detail["retryable"])

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
        self.assertNotIn(
            "ASSIGNMENT CONTROL-PLANE ACCESS",
            calls[1].args[1].get("developerInstructions") or "",
        )
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
        self.assertTrue(resume["sandboxPolicy"]["networkAccess"])
        self.assertTrue(turn["sandboxPolicy"]["networkAccess"])
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

    async def test_owned_steering_handoff_can_replace_active_turn(self) -> None:
        host, _binding, sessions, service = self._service()
        project = Project(id="p1", name="Project", path="/workspace/project",
                          sandbox="workspace-write", approval_policy="on-request")
        service.mark_thread_active("t1", turn_id="old-turn")
        self.assertTrue(service.begin_thread_handoff("t1"))
        response = await service.start_thread_turn_now(
            "t1", project=project, message="steered work", sandbox="workspace-write",
            approval_policy="on-request", preserve_active_handoff=True)
        self.assertEqual(response["turn"]["id"], "turn-1")
        self.assertEqual(host.active["t1"].turn_id, "turn-1")

    async def test_fresh_bootstrap_starts_first_turn_without_resume(self) -> None:
        host = _Host()
        binding = _BindingService()
        sessions = _SessionManager()
        bootstraps = _BootstrapBindings(
            "t1",
            initial_turn_pending=True,
        )
        service = TurnExecutionService(
            host,
            binding_service=binding,
            session_manager=sessions,
            bootstrap_bindings=bootstraps,
            control_actor=SimpleNamespace(identity_id="control"),
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
            message="start replacement work",
            sandbox="workspace-write",
            approval_policy="on-request",
            source="queued:gitlab",
        )

        self.assertEqual(result["turn"]["id"], "turn-1")
        self.assertEqual(
            [method for method, _params in sessions.session.requests],
            ["turn/start"],
        )
        self.assertEqual(
            [item[0] for item in bootstraps.initial_turn_started],
            ["t1"],
        )

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

    def test_shared_broker_guidance_does_not_advertise_work_item_only_operations(self) -> None:
        guidance = assignment_control_plane_instructions()
        self.assertIn("GET /api/control-plane-broker/operations", guidance)
        self.assertNotIn("assigned_scope", guidance)
        self.assertNotIn("work_item.steer", guidance)

    async def test_null_work_item_assignment_delivers_broker_discovery(self) -> None:
        for source, bootstrap in (("slack", None), ("autonomy-watchdog", "t1")):
            with self.subTest(source=source, bootstrap=bootstrap):
                resolved_refs = []
                _host, _binding, sessions, service = self._service(
                    bootstrap_thread_id=bootstrap,
                    work_item_context_resolver=lambda ref: resolved_refs.append(ref),
                )
                project = Project(id="p1", name="Project", path="/workspace/project")
                await service.start_thread_turn_now(
                    "t1", project=project, message="inspect canonical work",
                    sandbox="workspace-write", approval_policy="on-request",
                    source=source, execution_id="exec-no-work-item",
                )
                self.assertEqual(resolved_refs, [])
                for _method, request in sessions.session.requests:
                    instructions = request["developerInstructions"]
                    self.assertEqual(instructions.count("ASSIGNMENT CONTROL-PLANE ACCESS"), 1)
                    self.assertIn("GET /api/control-plane-broker/operations", instructions)
                    self.assertIn("existing ActionIntent before retrying", instructions)
                    self.assertIn("absence of MCP tools", instructions)
                self.assertEqual(sessions.session.requests[1][0], "turn/start")

    async def test_existing_work_item_broker_guidance_is_not_duplicated(self) -> None:
        guidance = assignment_control_plane_instructions()
        _host, _binding, sessions, service = self._service()
        service._work_item_continuation_context = MagicMock(
            return_value=("Canonical work context\n" + guidance, None, None)
        )
        project = Project(id="p1", name="Project", path="/workspace/project")
        await service.start_thread_turn_now(
            "t1", project=project, message="continue issue",
            sandbox="workspace-write", approval_policy="never",
            work_item_ref="group/app#531",
        )
        instructions = sessions.session.requests[1][1]["developerInstructions"]
        self.assertIn("Canonical work context", instructions)
        self.assertEqual(instructions.count(guidance), 1)

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

    async def test_interrupt_includes_canonical_active_turn_id(self) -> None:
        host, _binding, sessions, service = self._service(bootstrap_thread_id="t1")
        service.mark_thread_active("t1", turn_id="turn-1", assignment_id="assignment-1")
        await service.request_for_thread("t1", "turn/interrupt", {"threadId": "t1"})
        self.assertEqual(sessions.session.requests[-1],
                         ("turn/interrupt", {"threadId": "t1", "turnId": "turn-1"}))

    async def test_bootstrap_binding_without_live_session_fails_without_global_fallback(self) -> None:
        host, _binding, sessions, service = self._service(
            bootstrap_thread_id="t1"
        )
        sessions.session = None

        with self.assertRaises(HTTPException) as caught:
            await service.request_for_thread(
                "t1",
                "turn/interrupt",
                {"threadId": "t1"},
            )

        self.assertEqual(caught.exception.status_code, 503)
        self.assertIn("bootstrap binding", caught.exception.detail)
        host.codex.request.assert_not_awaited()

    async def test_bootstrap_binding_read_degrades_without_global_fallback(self) -> None:
        host, _binding, sessions, service = self._service(
            bootstrap_thread_id="t1"
        )
        sessions.session = None

        response = await service.request_for_thread(
            "t1",
            "thread/read",
            {"threadId": "t1"},
        )

        self.assertFalse(response["ok"])
        self.assertEqual(
            response["thread"]["status"]["type"],
            "notLoaded",
        )
        self.assertTrue(response["thread"]["readTimedOut"])
        host.codex.request.assert_not_awaited()

    async def test_stale_bootstrap_session_read_degrades_instead_of_500(self) -> None:
        host, _binding, sessions, service = self._service(
            bootstrap_thread_id="t1"
        )
        sessions.session = _StaleSession()

        response = await service.request_for_thread(
            "t1",
            "thread/read",
            {"threadId": "t1"},
        )

        self.assertFalse(response["ok"])
        self.assertEqual(response["thread"]["status"]["type"], "notLoaded")
        host.codex.request.assert_not_awaited()

    async def test_turn_evicts_stale_bootstrap_session_before_reacquiring(self) -> None:
        host, binding, sessions, service = self._service(
            bootstrap_thread_id="t1"
        )
        sessions.session = _StaleSession()
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
            message="continue after expiry",
            sandbox="workspace-write",
            approval_policy="on-request",
            source="web",
        )

        self.assertEqual(response["turn"]["id"], "turn-1")
        self.assertEqual(sessions.stopped, ["assignment-1"])
        self.assertEqual(sessions.started, ["assignment-1"])
        self.assertEqual(binding.calls, [])
        host.codex.request.assert_not_awaited()

    async def test_missing_bootstrap_assignment_reroutes_and_preserves_profile(self) -> None:
        host, _binding, sessions, service = self._service(bootstrap_thread_id="t1")
        sessions.session = None
        service._assignment_record = MagicMock(return_value=None)
        runtime = ExecutionRuntimeBinding(provider_id="openai", runtime_id="codex", capability_revision=1)
        profile = AgentProfileExecutionBinding(
            profile_id="reviewer", profile_revision=4, profile_record_id="reviewer-r4",
            selected_provider_id="openai", selected_runtime_id="codex",
        )
        actor = SimpleNamespace(identity_id="requesting-human")
        service._select_runtime_binding = AsyncMock(return_value=(runtime, profile))
        recovered = _Session()
        assignment = recovered.validate_current()
        assignment.runtime_binding = runtime
        assignment.agent_profile = profile
        recovered.validate_current = MagicMock(return_value=assignment)
        sessions.start = AsyncMock(side_effect=[RuntimeError("assignment not found"), recovered])
        service._supersede_thread_bootstrap = AsyncMock(
            return_value=service.bootstrap_bindings.get_by_thread("t1", service.control_actor)
        )
        result = await service.start_thread_turn_now(
            "t1", project=Project(id="p1", name="Project", path="/workspace/project", model="gpt-6.1-sol"),
            message="recover", sandbox="workspace-write", approval_policy="on-request",
            actor=actor, agent_profile_id="reviewer", agent_profile_revision=4,
        )
        self.assertEqual(result["turn"]["id"], "turn-1")
        service._select_runtime_binding.assert_awaited_once_with(
            project_id="p1", sandbox="workspace-write", trusted_local_codex_session=False,
            actor=actor, agent_profile_id="reviewer", agent_profile_revision=4,
        )
        healing = service._supersede_thread_bootstrap.await_args.kwargs
        self.assertIs(healing["agent_profile"], profile)
        self.assertEqual(healing["previous_assignment_id"], "assignment-1")
        host.codex.request.assert_not_awaited()

    async def _explicit_recovery(self, *, scope_switch=False, bound_profile=None,
                                 requested_revision=6, routing_error=None,
                                 selected_runtime_id="codex"):
        host, _binding, sessions, service = self._service(bootstrap_thread_id="t1")
        runtime = ExecutionRuntimeBinding(provider_id="openai", runtime_id="codex", capability_revision=1)
        profile = AgentProfileExecutionBinding(
            profile_id="veridataops-james", profile_revision=6,
            profile_record_id="james-r6", execution_profile_id="repository-write",
            selected_provider_id="openai", selected_runtime_id="codex",
        )
        old = sessions.session.validate_current()
        old.runtime_binding = runtime
        old.agent_profile = bound_profile
        old.repository_target = SimpleNamespace(mutable_repository_id="repo-old")
        old.repository_scope = SimpleNamespace(writable_repository_ids=("repo-old",))
        sessions.session.validate_current = MagicMock(return_value=old)
        service._assignment_record = MagicMock(return_value=old)
        recovered = _Session()
        new = recovered.validate_current()
        new.runtime_binding = runtime
        new.agent_profile = profile if bound_profile is None else bound_profile
        new.repository_target = SimpleNamespace(mutable_repository_id="repo-new" if scope_switch else "repo-old")
        new.repository_scope = SimpleNamespace(writable_repository_ids=("repo-new" if scope_switch else "repo-old",))
        recovered.validate_current = MagicMock(return_value=new)
        if not scope_switch:
            sessions.session = None
            sessions.start = AsyncMock(side_effect=[RuntimeError("historical workspace unavailable"), recovered])
        else:
            sessions.start = AsyncMock(return_value=recovered)
        selected = runtime.model_copy(update={"runtime_id": selected_runtime_id, "capability_revision": 7})
        service._select_runtime_binding = AsyncMock(
            return_value=(selected, profile), side_effect=routing_error)
        service._supersede_thread_bootstrap = AsyncMock(
            return_value=service.bootstrap_bindings.get_by_thread("t1", service.control_actor))
        actor = SimpleNamespace(identity_id="real-request-actor")
        kwargs = dict(project=Project(id="p1", name="Project", path="/workspace/project"),
                      message="real repository task", sandbox="workspace-write", approval_policy="on-request",
                      actor=actor, agent_profile_id="veridataops-james", agent_profile_revision=requested_revision,
                      repository_resource_id="repo-new" if scope_switch else "repo-old",
                      writable_repository_resource_ids=("repo-new" if scope_switch else "repo-old",))
        return host, sessions, service, profile, selected, actor, kwargs

    async def test_explicit_null_profile_scope_supersession_resolves_canonical_profile(self):
        host, _sessions, service, profile, runtime, actor, kwargs = await self._explicit_recovery(scope_switch=True)
        await service.start_thread_turn_now("t1", **kwargs)
        service._select_runtime_binding.assert_awaited_once_with(
            project_id="p1", sandbox="workspace-write", trusted_local_codex_session=False,
            actor=actor, agent_profile_id="veridataops-james", agent_profile_revision=6)
        recovery = service._supersede_thread_bootstrap.await_args.kwargs
        self.assertIs(recovery["agent_profile"], profile)
        self.assertEqual(recovery["runtime_binding"], runtime)
        self.assertEqual(recovery["explicit_repository_id"], "repo-new")
        self.assertEqual(recovery["writable_repository_ids"], ("repo-new",))
        host.codex.request.assert_not_awaited()

    async def test_explicit_null_profile_failed_start_resolves_canonical_profile(self):
        _host, _sessions, service, profile, runtime, actor, kwargs = await self._explicit_recovery()
        await service.start_thread_turn_now("t1", **kwargs)
        service._select_runtime_binding.assert_awaited_once()
        self.assertIs(service._select_runtime_binding.await_args.kwargs["actor"], actor)
        recovery = service._supersede_thread_bootstrap.await_args.kwargs
        self.assertIs(recovery["agent_profile"], profile)
        self.assertEqual(recovery["runtime_binding"], runtime)
        self.assertEqual(recovery["explicit_repository_id"], "repo-old")
        self.assertEqual(recovery["writable_repository_ids"], ("repo-old",))

    async def test_matching_bound_profile_survives_failed_session_start(self):
        _host, _sessions, _initial, profile, _runtime, _actor, _kwargs = await self._explicit_recovery()
        _host, _sessions, service, _profile, _runtime, _actor, kwargs = await self._explicit_recovery(bound_profile=profile)
        await service.start_thread_turn_now("t1", **kwargs)
        service._select_runtime_binding.assert_not_awaited()
        self.assertIs(service._supersede_thread_bootstrap.await_args.kwargs["agent_profile"], profile)

    async def test_bound_profile_mismatch_rejects_before_scope_or_heal_mutations(self):
        for scope_switch in (False, True):
            with self.subTest(scope_switch=scope_switch):
                _host, _sessions, _service, profile, _runtime, _actor, _kwargs = await self._explicit_recovery()
                _host, sessions, service, _profile, _runtime, _actor, kwargs = await self._explicit_recovery(
                    scope_switch=scope_switch, bound_profile=profile, requested_revision=5)
                with self.assertRaises(HTTPException) as caught:
                    await service.start_thread_turn_now("t1", **kwargs)
                self.assertEqual(caught.exception.detail["code"], "thread_agent_profile_immutable")
                service._supersede_thread_bootstrap.assert_not_awaited()
                service._select_runtime_binding.assert_not_awaited()
                sessions.start.assert_not_awaited()

    async def test_null_profile_recovery_routing_denial_prevents_supersession(self):
        for scope_switch in (False, True):
            with self.subTest(scope_switch=scope_switch):
                _host, _sessions, service, _profile, _runtime, _actor, kwargs = await self._explicit_recovery(
                    scope_switch=scope_switch, routing_error=HTTPException(status_code=403, detail="profile denied"))
                with self.assertRaises(HTTPException) as caught:
                    await service.start_thread_turn_now("t1", **kwargs)
                self.assertEqual(caught.exception.status_code, 403)
                service._supersede_thread_bootstrap.assert_not_awaited()

    async def test_profile_recovery_does_not_silently_switch_inherited_runtime(self):
        _host, _sessions, service, _profile, _runtime, _actor, kwargs = await self._explicit_recovery(
            scope_switch=True, selected_runtime_id="different-runtime")
        with self.assertRaises(HTTPException) as caught:
            await service.start_thread_turn_now("t1", **kwargs)
        self.assertEqual(caught.exception.detail["code"], "execution_preflight_blocked")
        service._supersede_thread_bootstrap.assert_not_awaited()

    async def test_null_profile_nonrunning_session_and_explicit_model_preserve_selected_binding(self):
        _host, sessions, service, profile, selected, actor, kwargs = await self._explicit_recovery()
        old = service._assignment_record.return_value
        sessions.session = _Session()
        sessions.session.validate_current = MagicMock(return_value=old)
        sessions.session.status = MagicMock(return_value=SimpleNamespace(running=False))
        recovered = _Session()
        fresh = recovered.validate_current()
        fresh.runtime_binding = selected
        fresh.agent_profile = profile
        fresh.repository_scope = SimpleNamespace(writable_repository_ids=("repo-old",))
        fresh.repository_target = SimpleNamespace(mutable_repository_id="repo-old")
        recovered.validate_current = MagicMock(return_value=fresh)
        sessions.start = AsyncMock(return_value=recovered)
        kwargs["model"] = "codex/gpt-6.1-sol"
        await service.start_thread_turn_now("t1", **kwargs)
        service._select_runtime_binding.assert_awaited_once()
        recovery = service._supersede_thread_bootstrap.await_args.kwargs
        self.assertIs(recovery["runtime_binding"], selected)
        self.assertIs(recovery["agent_profile"], profile)

    async def test_missing_bootstrap_assignment_routing_denial_stays_fail_closed(self) -> None:
        host, _binding, sessions, service = self._service(bootstrap_thread_id="t1")
        sessions.session = None
        service._assignment_record = MagicMock(return_value=None)
        service._select_runtime_binding = AsyncMock(side_effect=HTTPException(status_code=403, detail="profile access denied"))
        sessions.start = AsyncMock()
        with self.assertRaises(HTTPException) as caught:
            await service.start_thread_turn_now(
                "t1", project=Project(id="p1", name="Project", path="/workspace/project"),
                message="recover", sandbox="workspace-write", approval_policy="on-request",
                agent_profile_id="reviewer",
            )
        self.assertEqual(caught.exception.status_code, 403)
        sessions.start.assert_not_awaited()
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

    async def test_repository_scoped_turn_supersedes_bootstrap_with_requested_target(self) -> None:
        host, _binding, sessions, service = self._service(
            bootstrap_thread_id="t1"
        )
        runtime_binding = ExecutionRuntimeBinding(
            provider_id="openai",
            runtime_id="codex",
            capability_revision=1,
        )
        original_validate = sessions.session.validate_current
        validation_calls = 0

        def validate_current():
            nonlocal validation_calls
            validation_calls += 1
            assignment = original_validate()
            assignment.runtime_binding = runtime_binding
            assignment.repository_target = SimpleNamespace(
                mutable_repository_id=(
                    "repo-old" if validation_calls == 1 else "repo-new"
                )
            )
            assignment.repository_scope = SimpleNamespace(
                writable_repository_ids=(
                    ("repo-old",)
                    if validation_calls == 1
                    else ("repo-new",)
                )
            )
            return assignment

        sessions.session.validate_current = validate_current
        service._supersede_thread_bootstrap = AsyncMock(
            return_value=SimpleNamespace(
                bootstrap_id="bootstrap-2",
                assignment_id="assignment-1",
                execution_id="bootstrap-exec",
                execution_workspace_id="workspace-1",
            )
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
            message="switch repository",
            sandbox="workspace-write",
            approval_policy="on-request",
            repository_resource_id="repo-new",
            writable_repository_resource_ids=("repo-new",),
        )

        switch = service._supersede_thread_bootstrap.await_args.kwargs
        self.assertEqual(switch["explicit_repository_id"], "repo-new")
        self.assertEqual(switch["writable_repository_ids"], ("repo-new",))


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

    async def test_sessionless_cli_timeout_releases_active_assignment(self) -> None:
        host = _Host()
        host._is_codex_timeout_error = lambda exc: isinstance(
            exc,
            asyncio.TimeoutError,
        )
        binding_service = _BindingService()
        default_manager = _SessionManager()
        default_manager.session = None
        selected = ExecutionRuntimeBinding(
            provider_id="openai",
            runtime_id="codex-cli",
            capability_revision=1,
        )
        cli_manager = _SessionManager()
        cli_manager.session = _SessionlessCliSession(selected)
        thread_history = MagicMock()
        service = TurnExecutionService(
            host,
            binding_service=binding_service,
            session_manager=default_manager,
            bootstrap_bindings=_BootstrapBindings("t1"),
            control_actor=SimpleNamespace(identity_id="control"),
            session_managers={
                ("openai", "codex"): default_manager,
                ("openai", "codex-cli"): cli_manager,
            },
            runtime_adapter_factory=(
                lambda binding, session: _SessionlessCliTimeoutAdapter()
            ),
            thread_history=thread_history,
        )
        project = Project(
            id="p1",
            name="Project",
            path="/workspace/project",
            sandbox="workspace-write",
            approval_policy="on-request",
        )

        with self.assertRaises(asyncio.TimeoutError):
            await service.start_thread_turn_now(
                "t1",
                project=project,
                message="start without a native session",
                sandbox="workspace-write",
                approval_policy="on-request",
                execution_id="ignored-for-bootstrap",
            )

        self.assertNotIn("t1", host.active)
        self.assertEqual(len(cli_manager.completed), 1)
        assignment_id, completion = cli_manager.completed[0]
        self.assertEqual(assignment_id, "assignment-1")
        self.assertFalse(completion["succeeded"])
        self.assertEqual(
            completion["failure_code"],
            "agent_turn_start_failed",
        )
        thread_history.start_turn.assert_called_once()
        projected_failure = thread_history.project_message.call_args.args[1]
        self.assertEqual(projected_failure["method"], "turn/failed")
        self.assertEqual(projected_failure["params"]["threadId"], "t1")

    async def test_sessionless_cli_read_returns_degraded_thread_not_503(self) -> None:
        host = _Host()
        default_manager = _SessionManager()
        default_manager.session = None
        selected = ExecutionRuntimeBinding(
            provider_id="openai",
            runtime_id="codex-cli",
            capability_revision=1,
        )
        cli_manager = _SessionManager()
        cli_manager.session = _SessionlessCliSession(selected)
        service = TurnExecutionService(
            host,
            session_manager=default_manager,
            bootstrap_bindings=_BootstrapBindings("t1"),
            control_actor=SimpleNamespace(identity_id="control"),
            session_managers={
                ("openai", "codex"): default_manager,
                ("openai", "codex-cli"): cli_manager,
            },
            runtime_adapter_factory=(
                lambda binding, session: _SessionlessCliTimeoutAdapter()
            ),
        )

        response = await service.request_for_thread(
            "t1",
            "thread/read",
            {"threadId": "t1", "includeTurns": True},
        )

        self.assertFalse(response["ok"])
        self.assertEqual(response["thread"]["id"], "t1")
        self.assertEqual(
            response["thread"]["status"]["type"],
            "notLoaded",
        )
        host.codex.request.assert_not_awaited()

    async def test_offloop_terminal_bootstrap_projection_clears_active_and_checkpoints(self):
        host, _binding, sessions, service = self._service(bootstrap_thread_id="t1")
        entered = asyncio.Event()
        release = asyncio.Event()
        async def checkpoint(assignment_id):
            entered.set()
            await release.wait()
            return (SimpleNamespace(),)
        sessions.checkpoint = checkpoint
        service.mark_thread_active("t1", turn_id="turn-1", assignment_id="assignment-1")
        await asyncio.to_thread(service.record_thread_activity, {
            "method": "turn/completed",
            "params": {"threadId": "t1", "turn": {"id": "turn-1"}},
        })
        await asyncio.wait_for(entered.wait(), 1)
        self.assertNotIn("t1", host.active)
        self.assertEqual(sessions.completed, [])
        release.set()
        await asyncio.gather(*list(service.assignment_completion_tasks.values()))
        self.assertEqual(host.events[-1]["repository_checkpoint_count"], 1)
        self.assertTrue(host.events[-1]["session_retained"])

    async def test_offloop_terminal_projection_completes_exact_assignment(self):
        host, _binding, sessions, service = self._service()
        service.mark_thread_active("t1", turn_id="turn-1", assignment_id="assignment-1")
        await asyncio.to_thread(service.record_thread_activity, {
            "method": "turn/failed",
            "params": {"threadId": "t1", "turn": {"id": "turn-1"}, "error": "provider failed"},
        })
        await asyncio.sleep(0)
        await asyncio.gather(*list(service.assignment_completion_tasks.values()))
        self.assertNotIn("t1", host.active)
        self.assertEqual(len(sessions.completed), 1)
        assignment_id, kwargs = sessions.completed[0]
        self.assertEqual(assignment_id, "assignment-1")
        self.assertFalse(kwargs["succeeded"])
        self.assertEqual(kwargs["failure_code"], "codex_turn_failed")

    async def test_offloop_old_terminal_event_cannot_complete_new_active_turn(self):
        host, _binding, sessions, service = self._service()
        service.mark_thread_active("t1", turn_id="new-turn", assignment_id="assignment-1")
        await asyncio.to_thread(service.record_thread_activity, {
            "method": "turn/completed",
            "params": {"threadId": "t1", "turn": {"id": "old-turn"}},
        })
        await asyncio.sleep(0)
        self.assertEqual(host.active["t1"].turn_id, "new-turn")
        self.assertEqual(sessions.completed, [])
        self.assertEqual(service.assignment_completion_tasks, {})

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

    async def test_terminal_bootstrap_turn_checkpoints_before_success_evidence(self) -> None:
        host, _binding, sessions, service = self._service(
            bootstrap_thread_id="t1"
        )
        sessions.checkpoint = AsyncMock(return_value=(SimpleNamespace(),))
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
        await asyncio.gather(*list(service.assignment_completion_tasks.values()))

        sessions.checkpoint.assert_awaited_once_with("assignment-1")
        self.assertEqual(host.events[-1]["repository_checkpoint_count"], 1)
        self.assertTrue(host.events[-1]["succeeded"])

    async def test_start_rechecks_active_state_under_thread_start_lock(self) -> None:
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

    async def test_slow_bootstrap_only_serializes_starts_for_the_same_thread(self) -> None:
        for second_thread in ("t1", "t2"):
            with self.subTest(second_thread=second_thread):
                _host, _binding, _sessions, service = self._service()
                project = Project(id="p1", name="Project", path="/workspace/project")
                entered = asyncio.Event()
                second_entered = asyncio.Event()
                release = asyncio.Event()
                calls = []

                async def convert(**kwargs):
                    calls.append(kwargs["thread_id"])
                    if len(calls) == 1:
                        entered.set()
                        await release.wait()
                    else:
                        second_entered.set()
                    raise HTTPException(status_code=503, detail="test bootstrap boundary")

                service._convert_legacy_thread_to_bootstrap = convert
                async def start(thread_id):
                    return await service.start_thread_turn_now(
                        thread_id, project=project, message="resume work",
                        sandbox="workspace-write", approval_policy="on-request",
                    )
                first = asyncio.create_task(start("t1"))
                await asyncio.wait_for(entered.wait(), 1)
                second = asyncio.create_task(start(second_thread))
                try:
                    if second_thread == "t2":
                        await asyncio.wait_for(second_entered.wait(), 1)
                    else:
                        await asyncio.sleep(0)
                        self.assertFalse(second_entered.is_set())
                finally:
                    release.set()
                    results = await asyncio.gather(first, second, return_exceptions=True)
                self.assertTrue(all(isinstance(result, HTTPException) for result in results))
                self.assertEqual(calls, ["t1", second_thread])

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

        with self.assertRaisesRegex(HTTPException, "no live agent runtime session"):
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

    async def test_cli_binding_read_without_native_session_degrades(self) -> None:
        host, _binding, sessions, service = self._service(
            bootstrap_thread_id="t1"
        )
        sessions.session.runtime = SimpleNamespace(native_session_id=None)
        sessions.session.validate_current = lambda: SimpleNamespace(
            runtime_binding=ExecutionRuntimeBinding(
                provider_id="mammouth-ai",
                runtime_id="mammouth-cli",
                capability_revision=1,
            )
        )
        adapter = SimpleNamespace(
            read_session=AsyncMock(),
        )
        service.runtime_adapter_factory = lambda binding, session: adapter

        response = await service.request_for_thread(
            "t1",
            "thread/read",
            {"threadId": "t1"},
        )

        self.assertFalse(response["ok"])
        self.assertEqual(response["thread"]["status"]["type"], "notLoaded")
        self.assertTrue(response["thread"]["readTimedOut"])
        adapter.read_session.assert_not_awaited()

    async def test_sessionless_inactive_runtime_releases_active_turn(self) -> None:
        binding = ExecutionRuntimeBinding(
            provider_id="mammouth-ai",
            runtime_id="mammouth-cli",
            capability_revision=1,
        )
        host, _binding, sessions, service = self._service(
            bootstrap_thread_id="t1"
        )
        sessions.session = _AlternateSession(binding)
        sessions.session.runtime.native_session_id = None
        adapter = SimpleNamespace(
            read_session=AsyncMock(
                return_value=AgentRuntimeResult(payload={"active": False})
            )
        )
        service.runtime_adapter_factory = lambda _binding, _session: adapter
        service.mark_thread_active(
            "t1",
            execution_id="bootstrap-exec",
            assignment_id="assignment-1",
        )

        released = await service.release_unviable_active_turn("t1")

        self.assertTrue(released)
        self.assertNotIn("t1", host.active)
        self.assertEqual(len(sessions.completed), 1)
        self.assertEqual(
            sessions.completed[0][1]["failure_code"],
            "agent_runtime_session_not_started",
        )
        self.assertEqual(host.events[-1]["type"], "sessionless_active_turn_released")

    async def test_terminal_runtime_start_timeout_does_not_orphan_active_turn(self) -> None:
        binding = ExecutionRuntimeBinding(
            provider_id="mammouth-ai",
            runtime_id="mammouth-cli",
            capability_revision=1,
        )
        host, _binding, sessions, service = self._service(
            bootstrap_thread_id="t1"
        )
        sessions.session = _AlternateSession(binding)
        sessions.session.runtime.native_session_id = None
        adapter = SimpleNamespace(
            resume_session=AsyncMock(
                return_value=AgentRuntimeResult(
                    provider_native_session_id="t1",
                    payload={"resumed": True},
                )
            ),
            start_turn=AsyncMock(
                side_effect=TimeoutError("Mammouth Code start timed out")
            ),
            read_session=AsyncMock(
                return_value=AgentRuntimeResult(payload={"active": False})
            ),
        )
        service.runtime_adapter_factory = lambda _binding, _session: adapter
        project = Project(
            id="p1",
            name="Project",
            path="/workspace/project",
            sandbox="workspace-write",
            approval_policy="on-request",
        )

        with self.assertRaisesRegex(TimeoutError, "start timed out"):
            await service.start_thread_turn_now(
                "t1",
                project=project,
                message="continue work",
                sandbox="workspace-write",
                approval_policy="on-request",
                model="mammouth-ai/gpt-6-astra",
                execution_id="queued-turn-exec",
            )

        self.assertNotIn("t1", host.active)
        self.assertEqual(len(sessions.completed), 1)
        self.assertEqual(
            sessions.completed[0][1]["failure_code"],
            "agent_runtime_session_not_started",
        )

    async def test_startup_resume_retires_persisted_active_marker_before_start(self) -> None:
        host, _binding, _sessions, service = self._service()
        project = Project(
            id="p1",
            name="Project",
            path="/workspace/project",
            sandbox="workspace-write",
            approval_policy="on-request",
        )
        host._project = lambda project_id: project if project_id == "p1" else None
        host._project_for_cwd = lambda _cwd: project
        service.mark_thread_active(
            "t1",
            turn_id="dead-turn",
            project_id="p1",
            source="web",
            execution_id="bootstrap-exec",
            assignment_id="assignment-1",
            execution_workspace_id="workspace-1",
            worker_id="worker-1",
            fence=7,
        )

        async def resumed_start(thread_id, **_kwargs):
            self.assertEqual(thread_id, "t1")
            self.assertNotIn("t1", host.active)
            return {"turn": {"id": "replacement-turn"}}

        service.start_thread_turn_now = AsyncMock(side_effect=resumed_start)

        await service.resume_active_threads_after_startup({"t1"})

        self.assertEqual(host.active["t1"].turn_id, "replacement-turn")
        self.assertEqual(host.active["t1"].source, "restart-recovery:web")
        self.assertEqual(host.events[-1]["type"], "active_thread_resumed")



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
    def test_turn_failure_text_reads_top_level_and_structured_turn_errors(self) -> None:
        self.assertEqual(
            _turn_failure_text({"params": {"error": " capacity unavailable "}}),
            "capacity unavailable",
        )
        self.assertEqual(
            _turn_failure_text(
                {
                    "params": {
                        "turn": {
                            "error": {
                                "message": "usage limit reached",
                                "codexErrorInfo": "usageLimitExceeded",
                            }
                        }
                    }
                }
            ),
            "usage limit reached",
        )

    def test_successful_terminal_turn_does_not_require_compatibility_helper(self) -> None:
        host = _Host()
        service = TurnExecutionService(host)
        service.terminal_failures["thread-1"] = deque([(1.0, "old failure")])
        service.last_inputs["thread-1"] = {"message": "retry"}

        recovery_scheduled = service.record_terminal_turn_result(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-1",
                    "turn": {"id": "turn-1", "status": "completed"},
                },
            }
        )

        self.assertFalse(recovery_scheduled)
        self.assertNotIn("thread-1", service.terminal_failures)
        self.assertNotIn("thread-1", service.last_inputs)

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




class BootstrapAuthenticationEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def _rebind(self, *, fresh=False, mode=None, provider="openai", runtime="codex", read_error=None):
        host = _Host()
        order = []
        available = [fresh]
        host.codex.authenticated_account_available = lambda: available[0]

        async def read(method, params):
            order.append(("read", method, params))
            if read_error is not None:
                raise read_error
            available[0] = True
            return {"account": {"type": "chatgpt"}}

        host.codex.request = AsyncMock(side_effect=read)
        manager = SimpleNamespace(get=lambda assignment_id: object())

        async def complete(*args, **kwargs):
            order.append(("cleanup",))

        manager.complete = AsyncMock(side_effect=complete)
        binding_service = SimpleNamespace()

        def prepare(**kwargs):
            order.append(("prepare", available[0]))
            return SimpleNamespace(execution_id="replacement", assignment_id="new", workspace_id="workspace")

        binding_service.prepare_bootstrap = prepare
        bindings = SimpleNamespace(rebind=lambda **kwargs: order.append(("rebind",)))
        service = TurnExecutionService(host, binding_service=binding_service, bootstrap_bindings=bindings,
                                       session_managers={(provider, runtime): manager})
        service._bootstrap_binding_for_thread = lambda thread_id: SimpleNamespace(assignment_id="new")
        service._assignment_record = lambda assignment_id: SimpleNamespace(runtime_binding=ExecutionRuntimeBinding(
            provider_id=provider, runtime_id=runtime, capability_revision=1,
            authentication_mode=mode or "trusted_local_session"))
        binding = ExecutionRuntimeBinding(provider_id=provider, runtime_id=runtime, capability_revision=1,
                                          authentication_mode=mode)
        kwargs = dict(thread_id="t1", project=Project(id="p1", name="P", path="/workspace/project"),
                      runtime_binding=binding, sandbox="danger-full-access", approval_policy="never",
                      execution_profile_id=None, agent_profile=None, explicit_repository_id="repository",
                      previous_assignment_id="old")
        return service, host, manager, order, available, kwargs

    async def test_expired_evidence_refreshes_after_cleanup_before_preflight(self):
        service, host, manager, order, available, kwargs = await self._rebind(mode="trusted_local_session")
        await service._supersede_thread_bootstrap(**kwargs)
        self.assertEqual(order, [("cleanup",), ("read", "account/read", {"refreshToken": False}),
                                 ("prepare", True), ("rebind",)])

    async def test_cleanup_expiration_is_checked_at_preparation_boundary(self):
        service, host, manager, order, available, kwargs = await self._rebind(fresh=True)

        async def expire(*args, **kwargs):
            order.append(("cleanup",))
            available[0] = False

        manager.complete.side_effect = expire
        await service._supersede_thread_bootstrap(**kwargs)
        host.codex.request.assert_awaited_once_with("account/read", {"refreshToken": False})
        self.assertEqual(order[-2:], [("prepare", True), ("rebind",)])

    async def test_fresh_evidence_does_not_add_rpc(self):
        service, host, manager, order, available, kwargs = await self._rebind(fresh=True)
        await service._supersede_thread_bootstrap(**kwargs)
        host.codex.request.assert_not_awaited()

    async def test_account_rpc_failure_never_prepares_or_rebinds(self):
        service, host, manager, order, available, kwargs = await self._rebind(read_error=RuntimeError("account unavailable"))
        with self.assertRaisesRegex(RuntimeError, "account unavailable"):
            await service._supersede_thread_bootstrap(**kwargs)
        self.assertFalse(available[0])
        self.assertEqual([item[0] for item in order], ["cleanup", "read"])

    async def test_other_runtime_does_not_use_operator_account(self):
        service, host, manager, order, available, kwargs = await self._rebind(provider="anthropic", runtime="claude-code")
        await service._supersede_thread_bootstrap(**kwargs)
        host.codex.request.assert_not_awaited()

    async def test_explicit_credential_modes_do_not_read_local_account(self):
        for mode in ("api_key", "delegated_worker_token"):
            with self.subTest(mode=mode):
                service, host, manager, order, available, kwargs = await self._rebind(mode=mode)
                await service._supersede_thread_bootstrap(**kwargs)
                host.codex.request.assert_not_awaited()


    async def test_mode_omitted_for_repository_drift_preserves_api_authentication(self):
        service, host, manager, order, available, kwargs = await self._rebind()
        service._assignment_record = lambda assignment_id: SimpleNamespace(runtime_binding=ExecutionRuntimeBinding(
            provider_id="openai", runtime_id="codex", capability_revision=1, authentication_mode="api_key"))
        await service._supersede_thread_bootstrap(**kwargs)
        host.codex.request.assert_not_awaited()

    async def test_missing_previous_authentication_mode_does_not_assume_operator(self):
        service, host, manager, order, available, kwargs = await self._rebind()
        service._assignment_record = lambda assignment_id: None
        await service._supersede_thread_bootstrap(**kwargs)
        host.codex.request.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
