from __future__ import annotations

import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI

from codex_web.action_providers import ActionRequest
from codex_web.autonomy import AutonomyCycleOutcome
from codex_web.models import WorkItemHandoff, WorkItemState
from codex_web.services.autonomy import AutonomyService, install_autonomy_service


class _Host:
    def __init__(self) -> None:
        self.saved_states: dict[str, WorkItemState] | None = None
        self.events: list[dict] = []
        self.OWNER_QUEUE_AGENTS = ()
        self.HANDOFF_COORDINATION_CHANNEL = None

    def _load_work_item_states(self) -> dict[str, WorkItemState]:
        return self.states

    def _save_work_item_states(self, states: dict[str, WorkItemState]) -> None:
        self.saved_states = states

    @staticmethod
    def _coerce_owner(value: str | None) -> str | None:
        value = (value or "").strip().lower()
        return value or None

    @staticmethod
    def _work_item_handoff_timeout_seconds() -> float:
        return 10.0

    @staticmethod
    def _archive_active_handoff(
        state: WorkItemState,
        *,
        now: float,
        status: str,
        reason_code: str | None = None,
    ) -> WorkItemState:
        assert state.handoff is not None
        archived = state.handoff.model_copy(
            update={
                "status": status,
                "acknowledged_at": now,
                "reason_code": reason_code,
            }
        )
        # Validate the archived value through the typed model before persisting.
        archived = WorkItemHandoff.model_validate(archived.model_dump())
        state.handoff_history = [*state.handoff_history, archived]
        state.handoff = None
        return state

    @staticmethod
    def _work_item_event(ref: str, event_type: str, **kwargs) -> dict:
        return {"ref": ref, "event_type": event_type, **kwargs}

    def _append_work_item_event(self, event: dict) -> None:
        self.events.append(event)


class AutonomyInstallationTests(unittest.TestCase):
    def test_install_rebinds_all_cycle_entrypoints_and_is_idempotent(self) -> None:
        app = FastAPI()
        host = _Host()

        first = install_autonomy_service(app, host)
        second = install_autonomy_service(app, host)

        self.assertIs(first, second)
        self.assertIs(app.state.autonomy_service, first)
        self.assertIs(host._run_owner_work_watchdog_cycle.__self__, first)
        self.assertIs(host._run_release_gate_watchdog_cycle.__self__, first)
        self.assertIs(host._run_work_item_sla_cycle.__self__, first)
        self.assertIs(host._run_orchestrator_watchdog_cycle.__self__, first)
        self.assertIs(host._run_split_brain_watchdog_cycle.__self__, first)
        self.assertIs(host._prepare_external_action.__self__, first)
        self.assertIs(host._execute_external_action.__self__, first)
        self.assertIs(host._verify_external_action.__self__, first)
        self.assertIs(host._rollback_external_action.__self__, first)


class AutonomyActionDelegationTests(unittest.IsolatedAsyncioTestCase):
    async def test_external_action_execution_queues_durable_intent(self) -> None:
        class Intents:
            def __init__(self) -> None:
                self.calls = []

            def create(self, payload, *, actor):
                self.calls.append((payload, actor))
                return "queued-intent"

        intents = Intents()
        service = AutonomyService(_Host(), action_intents=intents)
        request = ActionRequest(
            action_id="reference.set",
            organization_id="local",
            workspace_id="default",
        )
        actor = object()

        result = await service.execute_external_action(
            "binding-1",
            request,
            actor=actor,
        )

        self.assertEqual(result, "queued-intent")
        self.assertEqual(len(intents.calls), 1)
        self.assertEqual(intents.calls[0][0].binding_id, "binding-1")
        self.assertEqual(intents.calls[0][0].request, request)
        self.assertIs(intents.calls[0][1], actor)


class AutonomyBoundedDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_identical_idle_owner_payload_has_bounded_dedup_window(self):
        events = SimpleNamespace(ingest=AsyncMock(return_value=SimpleNamespace(
            inserted=False, event=SimpleNamespace(event_id="event-1"))))
        service = AutonomyService(
            runtime=SimpleNamespace(project_scope=lambda _project: ("org", "workspace")),
            controller=object(), canonical_events=events,
        )
        keys = []
        with patch("codex_web.services.autonomy.WatchdogDispatchPolicy.cooldown_seconds", return_value=120):
            for now in [1200, 1259, 1320]:
                with patch("codex_web.services.autonomy.time.time", return_value=now):
                    await service._bounded_reasoning_dispatch(
                        SimpleNamespace(thread_id="owner"), "resume", "owner-work-watchdog",
                        cycle_key="owner-work:owner", payload={"project_id": "project-a"},
                    )
                keys.append(events.ingest.await_args.kwargs["idempotency_key"])
        self.assertEqual(keys[0], keys[1])
        self.assertNotEqual(keys[1], keys[2])

    async def test_canonical_event_uses_project_scope(self) -> None:
        runtime = SimpleNamespace(
            project_scope=lambda project_id: (
                ("org-a", "workspace-a")
                if project_id == "project-a"
                else (None, None)
            ),
            dispatch_event=AsyncMock(return_value={"ok": True}),
        )
        canonical_events = SimpleNamespace(
            ingest=AsyncMock(
                return_value=SimpleNamespace(
                    inserted=True,
                    event=SimpleNamespace(event_id="event-1"),
                )
            )
        )

        async def process(_event, _observation, *, reasoner, **_kwargs):
            await reasoner()
            return SimpleNamespace(
                outcome=AutonomyCycleOutcome.COMPLETED,
                id="cycle-1",
                reason="completed",
            )

        service = AutonomyService(
            runtime=runtime,
            controller=SimpleNamespace(process=process),
            canonical_events=canonical_events,
        )

        result = await service._bounded_reasoning_dispatch(
            SimpleNamespace(thread_id="thread-james"),
            "pursue the issue",
            "owner-work-watchdog",
            cycle_key="owner-work:project-a:thread-james:james",
            payload={"project_id": "project-a", "agent": "james"},
        )

        self.assertTrue(result["ok"])
        self.assertEqual(
            canonical_events.ingest.await_args.kwargs["tenant_id"],
            "org-a",
        )
        self.assertEqual(
            canonical_events.ingest.await_args.kwargs["workspace_id"],
            "workspace-a",
        )
        first_key = canonical_events.ingest.await_args.kwargs[
            "idempotency_key"
        ]

        runtime.project_scope = lambda _project_id: (
            "org-b",
            "workspace-b",
        )
        await service._bounded_reasoning_dispatch(
            SimpleNamespace(thread_id="thread-james"),
            "pursue the issue",
            "owner-work-watchdog",
            cycle_key="owner-work:project-a:thread-james:james",
            payload={"project_id": "project-a", "agent": "james"},
        )

        self.assertNotEqual(
            first_key,
            canonical_events.ingest.await_args.kwargs["idempotency_key"],
        )


class AutonomyStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_expired_pending_handoff_is_archived_with_typed_status(self) -> None:
        host = _Host()
        now = time.time()
        state = WorkItemState(
            ref="example/project#17",
            project_id="project-a",
            current_owner="alice",
            current_stage="implementation_active",
            handoff=WorkItemHandoff(
                from_agent="alice",
                to_agent="bob",
                requested_at=now - 60,
                status="pending",
            ),
            last_meaningful_update_at=now - 60,
            created_at=now - 120,
            updated_at=now - 60,
        )
        host.states = {state.ref: state}
        service = AutonomyService(host)

        await service.run_work_item_sla_cycle()

        saved = host.saved_states
        self.assertIsNotNone(saved)
        current = saved[state.ref]
        self.assertIsNone(current.handoff)
        self.assertEqual(current.current_owner, "alice")
        self.assertEqual(current.next_owner, "alice")
        self.assertEqual(current.current_stage, "implementation_active")
        self.assertEqual(current.blocker, "Structured handoff expired without acknowledgement.")
        self.assertEqual(current.handoff_history[-1].status, "superseded")
        self.assertEqual(current.handoff_history[-1].reason_code, "handoff_expired")
        # Re-validate the whole state to prove the watchdog cannot write a value
        # rejected by the typed state-machine model on the next read.
        WorkItemState.model_validate(current.model_dump())
        self.assertEqual(host.events[-1]["event_type"], "handoff_expired")


class AutonomyOwnerWorkTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _runtime(
        states: dict[str, WorkItemState],
        *,
        active: bool = False,
        recently_active: bool = False,
    ) -> SimpleNamespace:
        binding = SimpleNamespace(
            thread_id="thread-james",
            thread_name="James",
        )
        dispatch = AsyncMock(return_value={"ok": True})

        async def replace(current, _reason):
            return current

        gitlab_group_issues = AsyncMock(return_value=[])
        return SimpleNamespace(
            load_gitlab_routing_settings=lambda: SimpleNamespace(
                enabled=True,
                projects={
                    "project-a": SimpleNamespace(enabled=True),
                },
            ),
            load_work_item_states=lambda: states,
            gitlab_token_for_project=lambda _project_id: "token",
            gitlab_group_path=lambda _settings: "example",
            gitlab_group_issues=gitlab_group_issues,
            append_bot_event=lambda event: None,
            coerce_owner=lambda value: (
                str(value).strip().lower() if value else None
            ),
            owner_queue_agents=("james",),
            handoff_coordination_channel=None,
            binding_for_agent=lambda *_args, **_kwargs: binding,
            replace_nonperforming_thread=replace,
            watchdog_dispatch_allowed=lambda _key: True,
            release_stale_active_turn=lambda *_args: None,
            thread_is_active=lambda _thread_id: active,
            thread_queue_depth=lambda _thread_id: 0,
            thread_recently_active=lambda _thread_id: recently_active,
            binding_prefix=lambda _binding: "James",
            record_watchdog_dispatch=lambda _key: None,
            dispatch_event=dispatch,
            work_item_dispatch_text=(
                lambda state: f"dispatch:{state.ref}"
            ),
            owner_activity_timestamp=lambda state: (
                state.last_meaningful_update_at
            ),
        )

    async def test_idle_owner_with_canonical_actionable_work_is_woken(self) -> None:
        implementation = WorkItemState(
            ref="example/project#1",
            project_id="project-a",
            current_owner="james",
            current_stage="implementation_active",
            last_meaningful_update_at=10.0,
            created_at=1.0,
            updated_at=10.0,
        )
        failed = WorkItemState(
            ref="example/project#2",
            project_id="project-a",
            current_owner="james",
            current_stage="failed_with_action_owner",
            last_meaningful_update_at=20.0,
            created_at=1.0,
            updated_at=20.0,
        )
        runtime = self._runtime(
            {
                implementation.ref: implementation,
                failed.ref: failed,
            }
        )

        await AutonomyService(runtime=runtime).run_owner_work_cycle()

        runtime.dispatch_event.assert_awaited_once_with(
            SimpleNamespace(
                thread_id="thread-james",
                thread_name="James",
            ),
            "dispatch:example/project#2",
            "owner-work-watchdog",
        )
        runtime.gitlab_group_issues.assert_not_awaited()

    async def test_active_owner_is_not_dispatched_duplicate_work(self) -> None:
        state = WorkItemState(
            ref="example/project#1",
            project_id="project-a",
            current_owner="james",
            current_stage="implementation_active",
            last_meaningful_update_at=10.0,
            created_at=1.0,
            updated_at=10.0,
        )
        runtime = self._runtime({state.ref: state}, active=True)

        await AutonomyService(runtime=runtime).run_owner_work_cycle()

        runtime.dispatch_event.assert_not_awaited()

    async def test_recent_activity_does_not_mask_idle_actionable_owner(self) -> None:
        state = WorkItemState(
            ref="example/project#1",
            project_id="project-a",
            current_owner="james",
            current_stage="implementation_active",
            last_meaningful_update_at=10.0,
            created_at=1.0,
            updated_at=10.0,
        )
        runtime = self._runtime({state.ref: state}, recently_active=True)

        await AutonomyService(runtime=runtime).run_owner_work_cycle()

        runtime.dispatch_event.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
