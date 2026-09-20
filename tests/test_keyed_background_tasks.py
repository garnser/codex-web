from __future__ import annotations

import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from codex_web.models import WorkItemHandoff, WorkItemState
from codex_web.services.keyed_tasks import KeyedTaskCoordinator
from codex_web.services.native_recovery import NativeRecoveryService
from codex_web.services.work_item_continuity import WorkItemContinuityService


def _state(
    *,
    ref: str = "group/project#1",
    project_id: str = "project",
    owner: str = "alice",
    stage: str = "implementation_active",
    updated_at: float = 1.0,
    handoff: WorkItemHandoff | None = None,
) -> WorkItemState:
    return WorkItemState(
        ref=ref,
        project_id=project_id,
        project_path=f"group/{project_id}",
        title="Task",
        url="https://example.invalid/task",
        kind="issue",
        priority=None,
        current_owner=owner,
        current_stage=stage,
        handoff=handoff,
        last_meaningful_update_at=updated_at,
        last_gitlab_event_at=None,
        blocker=None,
        next_action="next",
        next_owner=None,
        release_gate=False,
        status_label=None,
        labels=[],
        mr_refs=[],
        notes=[],
        closed_at=None,
        updated_at=updated_at,
        created_at=1.0,
    )


class KeyedTaskCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_latest_pending_operation_coalesces_per_key(self) -> None:
        coordinator = KeyedTaskCoordinator(
            max_concurrency=2,
            timeout_seconds=1,
            name="test",
        )
        gate = asyncio.Event()
        started = asyncio.Event()
        values: list[int] = []

        async def first():
            started.set()
            await gate.wait()
            values.append(0)

        coordinator.submit("same", first)
        await started.wait()
        for value in range(1, 101):
            async def latest(item=value):
                values.append(item)
            coordinator.submit("same", latest)

        self.assertEqual(coordinator.status()["taskCount"], 1)
        gate.set()
        await asyncio.gather(*list(coordinator._tasks.values()))

        self.assertEqual(values, [0, 100])
        self.assertGreaterEqual(coordinator.status()["coalesced"], 99)

    async def test_group_and_global_concurrency_are_bounded(self) -> None:
        coordinator = KeyedTaskCoordinator(
            max_concurrency=3,
            per_group_limit=2,
            timeout_seconds=1,
            name="bounded",
        )
        gate = asyncio.Event()
        active = 0
        max_global = 0
        group_active: dict[str, int] = {}
        max_group: dict[str, int] = {}

        def operation(group: str):
            async def run():
                nonlocal active, max_global
                active += 1
                group_active[group] = group_active.get(group, 0) + 1
                max_global = max(max_global, active)
                max_group[group] = max(
                    max_group.get(group, 0),
                    group_active[group],
                )
                try:
                    await gate.wait()
                finally:
                    active -= 1
                    group_active[group] -= 1
            return run

        for index in range(20):
            group = "project-a" if index < 10 else "project-b"
            coordinator.submit(
                f"key-{index}",
                operation(group),
                group=group,
            )
        await asyncio.sleep(0.02)

        self.assertLessEqual(max_global, 3)
        self.assertLessEqual(max_group.get("project-a", 0), 2)
        self.assertLessEqual(max_group.get("project-b", 0), 2)
        self.assertEqual(coordinator.status()["taskCount"], 20)
        gate.set()
        await asyncio.gather(*list(coordinator._tasks.values()))

    async def test_timeout_exception_and_shutdown_are_observable(self) -> None:
        events: list[dict] = []
        coordinator = KeyedTaskCoordinator(
            max_concurrency=2,
            timeout_seconds=0.01,
            name="observable",
            event_sink=events.append,
        )

        async def slow():
            await asyncio.sleep(10)

        async def fail():
            raise RuntimeError("boom")

        coordinator.submit("slow", slow)
        coordinator.submit("fail", fail)
        await asyncio.gather(
            *list(coordinator._tasks.values()),
            return_exceptions=True,
        )

        status = coordinator.status()
        self.assertEqual(status["timedOut"], 1)
        self.assertEqual(status["failed"], 1)
        self.assertTrue(
            any(
                item.get("type") == "background_task_timed_out"
                for item in events
            )
        )
        self.assertTrue(
            any(
                item.get("type") == "background_task_failed"
                for item in events
            )
        )

        blocking = KeyedTaskCoordinator(
            timeout_seconds=60,
            name="shutdown",
        )
        gate = asyncio.Event()
        blocking.submit("blocked", lambda: gate.wait())
        await asyncio.sleep(0)
        await blocking.stop(drain_timeout=0)
        self.assertEqual(blocking.status()["taskCount"], 0)
        self.assertGreaterEqual(blocking.status()["cancelled"], 1)


class NativeRecoveryCoalescingTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_schedules_never_overlap_same_cycle(self) -> None:
        gate = asyncio.Event()
        started = asyncio.Event()
        runs = 0
        active = 0
        max_active = 0

        async def cycle():
            nonlocal runs, active, max_active
            runs += 1
            active += 1
            max_active = max(max_active, active)
            started.set()
            try:
                await gate.wait()
            finally:
                active -= 1

        policy = SimpleNamespace(
            autonomy_enabled=lambda: True,
            native_recovery_cooldown=lambda: 0.0,
        )
        service = NativeRecoveryService(
            policy=policy,
            cycles=(cycle,),
            append_event=lambda _event: None,
        )

        self.assertTrue(service.schedule(reason="first"))
        await started.wait()
        for index in range(100):
            self.assertTrue(service.schedule(reason=f"repeat-{index}"))

        self.assertEqual(len(service.tasks), 1)
        gate.set()
        await asyncio.gather(*list(service.tasks.values()))

        self.assertEqual(max_active, 1)
        self.assertEqual(runs, 2)
        self.assertGreaterEqual(service.status()["coalesced"], 99)
        await service.stop()


class WorkItemContinuityCoalescingTests(unittest.IsolatedAsyncioTestCase):
    def _service(
        self,
        states: dict[str, WorkItemState],
        dispatches: list[tuple[str, str]],
        *,
        replace_gate: asyncio.Event | None = None,
        events: list[dict] | None = None,
    ) -> WorkItemContinuityService:
        seen_keys: set[str] = set()
        event_rows = events if events is not None else []

        async def replace(binding, _source):
            if replace_gate is not None:
                await replace_gate.wait()
            return binding

        async def dispatch(_binding, text, _source):
            dispatches.append((_binding.thread_id, text))
            return {"sent": True}

        return WorkItemContinuityService(
            policy=SimpleNamespace(
                handoff_continuity_delay=lambda: 0.0,
                actionable_owner_continuity_delay=lambda: 0.0,
            ),
            get_state=lambda ref: states[ref].model_copy(deep=True),
            coerce_owner=lambda value: (
                str(value).strip().lower() if value else None
            ),
            binding_for_agent=lambda owner, project_id, **_kwargs: (
                SimpleNamespace(
                    thread_id=f"{project_id}:{owner}",
                    project_id=project_id,
                )
            ),
            replace_nonperforming_thread=replace,
            dispatch_event=dispatch,
            dispatch_text=lambda state: (
                f"{state.current_owner}:{state.current_stage}:"
                f"{state.updated_at}"
            ),
            append_event=event_rows.append,
            truncate_text=lambda value, limit: str(value)[:limit],
            thread_is_active=lambda _thread_id: False,
            thread_queue_depth=lambda _thread_id: 0,
            thread_recently_active=lambda _thread_id: False,
            watchdog_dispatch_allowed=lambda key: key not in seen_keys,
            record_watchdog_dispatch=seen_keys.add,
            coordination_channel="coordination",
        )

    async def test_100_owner_updates_coalesce_to_latest_dispatch(self) -> None:
        states: dict[str, WorkItemState] = {}
        dispatches: list[tuple[str, str]] = []
        latest = _state()
        states[latest.ref] = latest
        with patch.dict(
            os.environ,
            {
                "CODEX_WEB_CONTINUITY_DISPATCH_CONCURRENCY": "4",
                "CODEX_WEB_CONTINUITY_DISPATCH_PROJECT_CONCURRENCY": "2",
            },
            clear=False,
        ):
            service = self._service(states, dispatches)
            for index in range(100):
                latest = _state(
                    owner=f"owner-{index}",
                    updated_at=float(index + 1),
                )
                states[latest.ref] = latest
                service.schedule_actionable_owner_dispatch(
                    latest,
                    source="burst",
                )

            self.assertEqual(
                service.status()["immediateDispatch"]["taskCount"],
                1,
            )
            await asyncio.gather(
                *list(service.dispatch_coordinator._tasks.values())
            )

        self.assertEqual(len(dispatches), 1)
        self.assertIn("owner-99", dispatches[0][1])
        self.assertGreaterEqual(
            service.status()["immediateDispatch"]["coalesced"],
            99,
        )
        await service.stop()

    async def test_superseded_owner_is_rechecked_before_external_dispatch(self) -> None:
        old = _state(owner="old", updated_at=1)
        states = {old.ref: old}
        dispatches: list[tuple[str, str]] = []
        events: list[dict] = []
        gate = asyncio.Event()
        service = self._service(
            states,
            dispatches,
            replace_gate=gate,
            events=events,
        )

        service.schedule_actionable_owner_dispatch(
            old,
            source="old",
        )
        await asyncio.sleep(0)
        new = _state(owner="new", updated_at=2)
        states[new.ref] = new
        service.schedule_actionable_owner_dispatch(
            new,
            source="new",
        )
        gate.set()
        await asyncio.gather(
            *list(service.dispatch_coordinator._tasks.values())
        )

        self.assertEqual(len(dispatches), 1)
        self.assertIn("new", dispatches[0][1])
        self.assertTrue(
            any(
                event.get("type")
                == "work_item_owner_dispatch_superseded"
                for event in events
            )
        )
        await service.stop()

    async def test_handoff_dispatch_is_keyed_and_idempotent(self) -> None:
        handoff = WorkItemHandoff(
            from_agent="old",
            to_agent="reviewer",
            requested_at=10.0,
            status="pending",
        )
        state = _state(
            owner="old",
            stage="ready_for_validation",
            updated_at=10,
            handoff=handoff,
        )
        states = {state.ref: state}
        dispatches: list[tuple[str, str]] = []
        service = self._service(states, dispatches)

        for _ in range(100):
            service.schedule_structured_handoff_dispatch(
                state,
                source="handoff",
            )
        await asyncio.gather(
            *list(service.dispatch_coordinator._tasks.values())
        )
        service.schedule_structured_handoff_dispatch(
            state,
            source="handoff-retry",
        )
        await asyncio.gather(
            *list(service.dispatch_coordinator._tasks.values())
        )

        self.assertEqual(len(dispatches), 1)
        await service.stop()


if __name__ == "__main__":
    unittest.main()
