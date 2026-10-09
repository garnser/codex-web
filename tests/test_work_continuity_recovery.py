from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codex_web.models import WorkItemState, WorkItemHandoff
from codex_web.services.native_recovery import NativeRecoveryService
from codex_web.services.runtime_policy import RuntimePolicy
from codex_web.services.work_item_continuity import WorkItemContinuityService


class RuntimePolicyTests(unittest.TestCase):
    def test_local_worker_capacity_configuration_is_bounded(self):
        policy = RuntimePolicy(Path("/tmp"))
        for value, expected in [("", 8), ("16", 16), ("0", 1), ("999", 128), ("invalid", 8)]:
            with patch.dict(os.environ, {"CODEX_WEB_LOCAL_WORKER_MAX_CONCURRENCY": value}):
                self.assertEqual(policy.local_worker_max_concurrency(), expected)

    def test_autonomy_disable_file_and_interval_clamps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            policy = RuntimePolicy(Path(directory))
            with patch.dict(
                os.environ,
                {
                    "CODEX_WEB_AUTONOMY_ENABLED": "1",
                    "CODEX_WEB_OWNER_WORK_WATCHDOG_SECONDS": "5",
                    "CODEX_WEB_ACTIONABLE_OWNER_CONTINUITY_DELAY_SECONDS": "1",
                },
                clear=False,
            ):
                self.assertTrue(policy.autonomy_enabled())
                self.assertEqual(
                    policy.owner_work_watchdog_interval(),
                    10.0,
                )
                self.assertEqual(
                    policy.actionable_owner_continuity_delay(),
                    5.0,
                )
                (Path(directory) / "AUTONOMY_DISABLED").write_text("1")
                self.assertFalse(policy.autonomy_enabled())
                self.assertEqual(policy.owner_work_watchdog_interval(), 0.0)


class NativeRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_schedule_is_cooldown_idempotent_and_stoppable(self) -> None:
        calls: list[str] = []
        events: list[dict[str, object]] = []

        class Policy:
            @staticmethod
            def autonomy_enabled() -> bool:
                return True

            @staticmethod
            def native_recovery_cooldown() -> float:
                return 60.0

        async def cycle() -> None:
            calls.append("cycle")

        service = NativeRecoveryService(
            policy=Policy(),
            cycles=(cycle,),
            append_event=events.append,
        )

        self.assertTrue(service.schedule(reason="test"))
        self.assertFalse(service.schedule(reason="duplicate"))
        await asyncio.sleep(0)

        self.assertEqual(calls, ["cycle"])
        self.assertEqual(events[0]["type"], "native_recovery_scheduled")
        self.assertEqual(events[0]["reason"], "test")

        await service.stop()
        self.assertFalse(service.schedule(reason="after-stop"))


class WorkItemContinuityTests(unittest.IsolatedAsyncioTestCase):
    def _service(
        self,
        *,
        states: dict[str, object],
        events: list[dict[str, object]],
        deliveries: list[tuple[str, str]],
    ) -> WorkItemContinuityService:
        class Policy:
            @staticmethod
            def actionable_owner_continuity_delay() -> float:
                return 0.0

            @staticmethod
            def handoff_continuity_delay() -> float:
                return 0.0

        async def dispatch(binding, text, source, **_kwargs):
            deliveries.append((binding.thread_id, source))
            return {"ok": True, "text": text}

        async def replace(binding, _reason):
            return binding

        return WorkItemContinuityService(
            policy=Policy(),
            get_state=lambda ref: states[ref],
            coerce_owner=lambda owner: owner.strip().lower() if owner else None,
            binding_for_agent=lambda owner, project_id, **_kwargs: SimpleNamespace(
                thread_id=f"{project_id}:{owner}",
                project_id=project_id,
            ),
            replace_nonperforming_thread=replace,
            dispatch_event=dispatch,
            dispatch_text=lambda state: f"dispatch:{state.ref}",
            append_event=events.append,
            truncate_text=lambda value, limit: value[:limit],
            thread_is_active=lambda _thread_id: False,
            thread_queue_depth=lambda _thread_id: 0,
            thread_recently_active=lambda _thread_id: False,
            watchdog_dispatch_allowed=lambda _key: True,
            record_watchdog_dispatch=lambda _key: None,
            coordination_channel="coordination",
        )

    @staticmethod
    def _blocked_mr():
        return WorkItemState(ref="example/gtm!16", project_id="project-a",
            current_owner="Sally", next_owner="Carl", implementation_owner="Sally",
            validation_owner="Quinn", current_stage="failed_with_action_owner",
            blocker="renderer prerequisite", created_at=1.0, updated_at=2.0,
            last_meaningful_update_at=2.0)

    async def test_blocked_next_owner_and_normal_current_owner_are_distinct(self):
        for stage, next_owner, expected in [
            ("failed_with_action_owner", "Carl", "carl"),
            ("failed_with_action_owner", None, "sally"),
            ("implementation_active", "Carl", "sally"),
            ("validation_running", "Carl", "sally"),
        ]:
            with self.subTest(stage=stage, next_owner=next_owner):
                state = self._blocked_mr().model_copy(update={
                    "current_stage": stage, "next_owner": next_owner})
                before = state.model_dump()
                deliveries, events = [], []
                service = self._service(states={state.ref: state},
                    events=events, deliveries=deliveries)
                await service.dispatch_actionable_owner(state, source="regression")
                self.assertEqual(deliveries, [("project-a:" + expected, "regression")])
                self.assertEqual(state.model_dump(), before)
                self.assertIn(state.ref, events[-1].values())
                await service.stop()

    async def test_unavailable_next_owner_keeps_existing_coercion_and_binding_guards(self):
        from codex_web.services.work_item_state import WorkItemStateMachine
        for value, expected in [("none", "owner_unavailable"),
                                ("null", "owner_unavailable"),
                                ("   ", "owner_unavailable"),
                                ("Unregistered", "binding_unavailable")]:
            with self.subTest(next_owner=value):
                state = self._blocked_mr().model_copy(update={"next_owner": value})
                deliveries, events = [], []
                service = self._service(states={state.ref: state},
                    events=events, deliveries=deliveries)
                service.coerce_owner = lambda owner: WorkItemStateMachine._coerce_owner(None, owner)
                def binding(owner, project, **_kwargs):
                    if owner in {"sally", "carl"}:
                        return SimpleNamespace(thread_id=project + ":" + owner)
                    return None
                service.binding_for_agent = binding
                result = await service.dispatch_actionable_owner(state, source="regression")
                self.assertEqual(result["status"], expected)
                self.assertEqual(deliveries, [])
                self.assertEqual(state.current_owner, "Sally")
                self.assertEqual(state.next_owner, value)
                await service.stop()

    async def test_next_action_owner_change_during_replacement_prevents_stale_dispatch(self):
        state = self._blocked_mr()
        states = {state.ref: state}
        deliveries, events = [], []
        service = self._service(states=states, events=events, deliveries=deliveries)
        async def replace(binding, _source):
            states[state.ref] = state.model_copy(update={"next_owner": "Orchestrator"})
            await asyncio.sleep(0)
            return binding
        service.replace_nonperforming_thread = replace
        result = await service.dispatch_actionable_owner(state, source="regression")
        self.assertEqual(result["status"], "stale_state")
        self.assertFalse(result["dispatched"])
        self.assertEqual(deliveries, [])
        await service.stop()

    async def test_pending_handoff_blocks_next_owner_dispatch(self):
        state = self._blocked_mr().model_copy(update={"handoff": WorkItemHandoff(
            from_agent="Sally", to_agent="Carl", requested_at=1.0)})
        deliveries, events = [], []
        service = self._service(states={state.ref: state}, events=events, deliveries=deliveries)
        result = await service.dispatch_actionable_owner(state, source="regression")
        self.assertEqual(result["status"], "handoff_pending")
        self.assertEqual(deliveries, [])
        await service.stop()

    async def test_queued_dispatch_revalidates_next_owner_before_delivery(self):
        state = self._blocked_mr()
        states = {state.ref: state}
        deliveries, events = [], []
        service = self._service(states=states, events=events, deliveries=deliveries)
        service.schedule_actionable_owner_dispatch(state, source="regression")
        states[state.ref] = state.model_copy(update={"next_owner": "Orchestrator"})
        for _ in range(10):
            await asyncio.sleep(0)
        self.assertEqual(deliveries, [])
        await service.stop()

    async def test_actionable_owner_continuity_dispatches_once(self) -> None:
        state = SimpleNamespace(
            ref="WI-1",
            project_id="project-a",
            current_owner="Dana",
            next_owner=None,
            current_stage="implementation_active",
            closed_at=None,
            handoff=None,
        )
        states = {"WI-1": state}
        events: list[dict[str, object]] = []
        deliveries: list[tuple[str, str]] = []
        service = self._service(
            states=states,
            events=events,
            deliveries=deliveries,
        )

        service.schedule_actionable_owner_continuity_check(
            state,
            source="continuity-test",
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        self.assertEqual(
            deliveries,
            [("project-a:dana", "continuity-test")],
        )
        self.assertNotIn("WI-1", service.actionable_owner_tasks)
        await service.stop()

    async def test_handoff_continuity_stops_when_handoff_changes(self) -> None:
        handoff = SimpleNamespace(
            status="pending",
            to_agent="Dana",
            requested_at=10.0,
        )
        state = SimpleNamespace(
            ref="WI-2",
            project_id="project-a",
            current_owner=None,
            next_owner="Dana",
            current_stage="implementation_active",
            closed_at=None,
            handoff=handoff,
        )
        states = {"WI-2": state}
        events: list[dict[str, object]] = []
        deliveries: list[tuple[str, str]] = []
        service = self._service(
            states=states,
            events=events,
            deliveries=deliveries,
        )

        service.schedule_handoff_continuity_check(
            state,
            source="handoff-test",
        )
        handoff.requested_at = 11.0
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        self.assertEqual(deliveries, [])
        await service.stop()


if __name__ == "__main__":
    unittest.main()
