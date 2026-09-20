from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from codex_web.models import WorkItemState
from codex_web.services.keyed_background_tasks import (
    KeyedTaskCoordinator,
)
from codex_web.services.native_recovery import NativeRecoveryService
from codex_web.services.work_item_continuity import (
    WorkItemContinuityService,
)


class _Policy:
    def autonomy_enabled(self) -> bool:
        return True

    def native_recovery_cooldown(self) -> float:
        return 0.0

    def native_recovery_cycle_timeout(self) -> float:
        return 2.0

    def background_task_max_concurrency(self) -> int:
        return 3

    def continuity_per_project_concurrency(self) -> int:
        return 2

    def continuity_dispatch_timeout(self) -> float:
        return 2.0

    def handoff_continuity_delay(self) -> float:
        return 0.0

    def actionable_owner_continuity_delay(self) -> float:
        return 0.0


def _state(
    *,
    ref: str = "work-1",
    project_id: str = "project-a",
    owner: str = "james",
    stage: str = "implementation_active",
    updated_at: float = 10.0,
) -> WorkItemState:
    return WorkItemState(
        ref=ref,
        project_id=project_id,
        current_owner=owner,
        current_stage=stage,
        last_meaningful_update_at=updated_at,
        updated_at=updated_at,
        created_at=1.0,
    )


class KeyedTaskCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_latest_pending_wins_for_100_same_key_triggers(self) -> None:
        coordinator = KeyedTaskCoordinator(
            max_concurrency=4,
            per_scope_concurrency=2,
        )
        observed: list[int] = []

        for value in range(100):
            async def run(current=value) -> None:
                observed.append(current)

            coordinator.schedule(
                "same",
                run,
                revision=str(value),
                scope="project-a",
            )

        await asyncio.gather(*list(coordinator._tasks.values()))

        self.assertEqual(observed, [99])
        status = coordinator.status()
        self.assertEqual(status["scheduled"], 100)
        self.assertEqual(status["coalesced"], 99)
        self.assertEqual(status["completed"], 1)
        self.assertEqual(
            status["lastCompletedRevision"]["same"],
            "99",
        )

    async def test_global_and_per_scope_concurrency_are_bounded(self) -> None:
        coordinator = KeyedTaskCoordinator(
            max_concurrency=3,
            per_scope_concurrency=2,
        )
        release = asyncio.Event()
        running = 0
        scope_running = {"a": 0, "b": 0}
        max_running = 0
        max_scope = {"a": 0, "b": 0}

        def factory(scope: str):
            async def run() -> None:
                nonlocal running, max_running
                running += 1
                scope_running[scope] += 1
                max_running = max(max_running, running)
                max_scope[scope] = max(
                    max_scope[scope],
                    scope_running[scope],
                )
                try:
                    await release.wait()
                finally:
                    scope_running[scope] -= 1
                    running -= 1

            return run

        for index in range(12):
            scope = "a" if index < 8 else "b"
            coordinator.schedule(
                f"{scope}-{index}",
                factory(scope),
                scope=scope,
            )

        for _ in range(20):
            if coordinator.status()["running"] >= 3:
                break
            await asyncio.sleep(0)

        self.assertLessEqual(max_running, 3)
        self.assertLessEqual(max_scope["a"], 2)
        self.assertLessEqual(max_scope["b"], 2)
        release.set()
        await asyncio.gather(*list(coordinator._tasks.values()))

    async def test_timeout_exception_and_shutdown_are_observable(self) -> None:
        coordinator = KeyedTaskCoordinator(max_concurrency=2)

        async def slow() -> None:
            await asyncio.sleep(1)

        async def broken() -> None:
            raise RuntimeError("boom")

        coordinator.schedule(
            "timeout",
            slow,
            timeout_seconds=0.01,
        )
        coordinator.schedule("broken", broken)
        await asyncio.gather(*list(coordinator._tasks.values()))

        status = coordinator.status()
        self.assertEqual(status["timedOut"], 1)
        self.assertEqual(status["failed"], 1)

        started = asyncio.Event()

        async def forever() -> None:
            started.set()
            await asyncio.Event().wait()

        coordinator.schedule("forever", forever)
        await started.wait()
        await coordinator.stop()

        status = coordinator.status()
        self.assertEqual(status["activeTasks"], 0)
        self.assertGreaterEqual(status["cancelled"], 1)


class NativeRecoveryCoalescingTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_cycle_never_overlaps_and_repeated_triggers_coalesce(self) -> None:
        policy = _Policy()
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = 0
        running = 0
        max_running = 0
        events: list[dict] = []

        async def cycle() -> None:
            nonlocal calls, running, max_running
            calls += 1
            running += 1
            max_running = max(max_running, running)
            entered.set()
            try:
                await release.wait()
            finally:
                running -= 1

        service = NativeRecoveryService(
            policy=policy,
            cycles=(cycle,),
            append_event=events.append,
        )

        self.assertTrue(service.schedule(reason="first"))
        await entered.wait()

        for index in range(100):
            self.assertTrue(
                service.schedule(reason=f"repeat-{index}")
            )

        status = service.status()
        self.assertEqual(status["running"], 1)
        self.assertEqual(status["queueDepth"], 1)
        self.assertEqual(status["coalesced"], 100)

        release.set()
        await asyncio.gather(*list(service.tasks))

        self.assertEqual(calls, 2)
        self.assertEqual(max_running, 1)
        self.assertTrue(
            any(
                event.get("type") == "native_recovery_coalesced"
                for event in events
            )
        )


class ContinuityCoalescingTests(unittest.IsolatedAsyncioTestCase):
    def _service(
        self,
        states: dict[str, WorkItemState],
        *,
        replace=None,
        dispatches: list[tuple[str, str, str]] | None = None,
    ) -> WorkItemContinuityService:
        dispatches = dispatches if dispatches is not None else []
        recorded: set[str] = set()

        async def replace_default(binding, _source):
            return binding

        async def dispatch(binding, text, source):
            dispatches.append((binding.thread_id, text, source))
            return {"ok": True}

        return WorkItemContinuityService(
            policy=_Policy(),
            get_state=lambda ref: states[ref].model_copy(deep=True),
            coerce_owner=lambda owner: (
                str(owner).strip().lower() if owner else None
            ),
            binding_for_agent=lambda owner, project_id, **_kwargs: (
                SimpleNamespace(
                    thread_id=f"thread-{project_id}-{owner}"
                )
            ),
            replace_nonperforming_thread=(
                replace or replace_default
            ),
            dispatch_event=dispatch,
            dispatch_text=lambda state: (
                f"{state.ref}:{state.current_owner}:"
                f"{state.current_stage}"
            ),
            append_event=lambda _event: None,
            truncate_text=lambda value, limit: str(value)[:limit],
            thread_is_active=lambda _thread_id: False,
            thread_queue_depth=lambda _thread_id: 0,
            thread_recently_active=lambda _thread_id: False,
            watchdog_dispatch_allowed=lambda key: key not in recorded,
            record_watchdog_dispatch=recorded.add,
            coordination_channel="coord",
        )

    async def test_100_owner_updates_coalesce_to_latest_dispatch(self) -> None:
        initial = _state()
        states = {initial.ref: initial.model_copy(deep=True)}
        dispatches: list[tuple[str, str, str]] = []
        service = self._service(states, dispatches=dispatches)

        for index in range(100):
            latest = _state(
                owner=f"owner-{index}",
                updated_at=20.0 + index,
            )
            states[latest.ref] = latest.model_copy(deep=True)
            service.schedule_actionable_owner_dispatch(
                latest,
                source="test",
            )

        await asyncio.gather(
            *list(service.dispatch_coordinator._tasks.values())
        )

        self.assertEqual(len(dispatches), 1)
        self.assertIn("owner-99", dispatches[0][1])
        status = service.status()["dispatch"]
        self.assertEqual(status["scheduled"], 100)
        self.assertEqual(status["coalesced"], 99)

    async def test_owner_change_during_thread_replacement_discards_stale_dispatch(self) -> None:
        old = _state(owner="old", updated_at=10.0)
        states = {old.ref: old.model_copy(deep=True)}
        dispatches: list[tuple[str, str, str]] = []
        replacement_started = asyncio.Event()
        release = asyncio.Event()

        async def replace(binding, _source):
            replacement_started.set()
            await release.wait()
            return binding

        service = self._service(
            states,
            replace=replace,
            dispatches=dispatches,
        )
        service.schedule_actionable_owner_dispatch(
            old,
            source="test",
        )
        await replacement_started.wait()

        states[old.ref] = _state(
            owner="new",
            stage="ready_for_validation",
            updated_at=20.0,
        )
        release.set()
        await asyncio.gather(
            *list(service.dispatch_coordinator._tasks.values())
        )

        self.assertEqual(dispatches, [])

    async def test_stop_cancels_managed_immediate_dispatch(self) -> None:
        state = _state()
        states = {state.ref: state.model_copy(deep=True)}
        replacement_started = asyncio.Event()

        async def replace(binding, _source):
            replacement_started.set()
            await asyncio.Event().wait()
            return binding

        service = self._service(states, replace=replace)
        service.schedule_actionable_owner_dispatch(
            state,
            source="test",
        )
        await replacement_started.wait()

        await service.stop()

        status = service.status()["dispatch"]
        self.assertEqual(status["activeTasks"], 0)
        self.assertGreaterEqual(status["cancelled"], 1)


if __name__ == "__main__":
    unittest.main()
