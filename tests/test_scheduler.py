from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from codex_web.scheduler import (
    MisfirePolicy,
    RecurrenceKind,
    ScheduleCreate,
    ScheduleRecurrence,
    ScheduleStatus,
)
from codex_web.services.canonical_events import CanonicalEventBus, CanonicalEventIngestionService
from codex_web.services.scheduler import SchedulerService
from codex_web.storage.canonical_events import CanonicalEventStore
from codex_web.storage.scheduler import SchedulerStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class MutableClock:
    def __init__(self, value: float) -> None:
        self.value = float(value)

    def __call__(self) -> float:
        return self.value


class DurableSchedulerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.clock = MutableClock(100.0)
        self.state = SQLiteStateStore(self.path)
        self.schedule_store = SchedulerStore(self.state)
        self.event_store = CanonicalEventStore(self.state)
        self.event_bus = CanonicalEventBus(self.event_store)
        self.ingestion = CanonicalEventIngestionService(self.event_bus)
        self.service = SchedulerService(
            self.schedule_store,
            self.ingestion,
            clock=self.clock,
            owner_id="scheduler-a",
            lease_seconds=5.0,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def create(
        self,
        *,
        due_at: float,
        recurrence: ScheduleRecurrence | None = None,
        policy: MisfirePolicy = MisfirePolicy.FIRE_ONCE,
        grace: float = 60.0,
        catch_up_limit: int = 10,
        name: str = "test schedule",
    ):
        return self.service.create(
            ScheduleCreate(
                name=name,
                tenant_id="org-a",
                workspace_id="ws-a",
                trigger_type="test.trigger",
                payload={"fixture": True},
                due_at=due_at,
                recurrence=recurrence,
                misfire_policy=policy,
                misfire_grace_seconds=grace,
                catch_up_limit=catch_up_limit,
            ),
            actor_id="human-admin",
        )

    async def test_one_shot_fires_once_and_restart_does_not_repeat(self) -> None:
        schedule = self.create(due_at=100.0)

        first = await self.service.run_due()

        self.assertEqual(first.claimed, 1)
        self.assertEqual(first.processed, 1)
        self.assertEqual(first.emitted, 1)
        stored = self.schedule_store.get(schedule.id)
        self.assertEqual(stored.status, ScheduleStatus.COMPLETED)
        self.assertIsNone(stored.next_run_at)
        self.assertEqual(stored.firing_count, 1)
        events = self.event_store.recent()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_type, "schedule.due")
        self.assertEqual(events[0].tenant_id, "org-a")
        self.assertEqual(events[0].workspace_id, "ws-a")
        self.assertEqual(events[0].payload["trigger_type"], "test.trigger")

        restarted_state = SQLiteStateStore(self.path)
        restarted = SchedulerService(
            SchedulerStore(restarted_state),
            CanonicalEventIngestionService(
                CanonicalEventBus(CanonicalEventStore(restarted_state))
            ),
            clock=self.clock,
            owner_id="scheduler-restarted",
        )
        second = await restarted.run_due()

        self.assertEqual(second.claimed, 0)
        self.assertEqual(second.emitted, 0)
        self.assertEqual(len(CanonicalEventStore(restarted_state).recent()), 1)

    async def test_crash_after_event_publish_replays_idempotently_after_lease_expiry(self) -> None:
        schedule = self.create(due_at=100.0)
        claimed = self.schedule_store.claim_due(
            now=100.0,
            owner_id="crashed-worker",
            lease_seconds=1.0,
            limit=1,
        )
        self.assertEqual(len(claimed), 1)

        original = SchedulerService(
            self.schedule_store,
            self.ingestion,
            clock=self.clock,
            owner_id="crashed-worker",
            lease_seconds=1.0,
        )
        first_delivery = await original._publish_firing(
            claimed[0],
            scheduled_for=100.0,
            fired_at=100.0,
        )
        self.assertTrue(first_delivery.inserted)
        self.assertEqual(len(self.event_store.recent()), 1)

        self.clock.value = 102.0
        restarted_state = SQLiteStateStore(self.path)
        restarted_store = SchedulerStore(restarted_state)
        restarted_event_store = CanonicalEventStore(restarted_state)
        restarted = SchedulerService(
            restarted_store,
            CanonicalEventIngestionService(
                CanonicalEventBus(restarted_event_store)
            ),
            clock=self.clock,
            owner_id="scheduler-after-crash",
            lease_seconds=5.0,
        )

        result = await restarted.run_due()

        self.assertEqual(result.claimed, 1)
        self.assertEqual(result.emitted, 1)
        self.assertEqual(len(restarted_event_store.recent()), 1)
        completed = restarted_store.get(schedule.id)
        self.assertEqual(completed.status, ScheduleStatus.COMPLETED)
        self.assertEqual(completed.firing_count, 1)
        self.assertEqual(completed.last_fired_at, 100.0)

    async def test_misfire_policies_skip_fire_once_and_bound_catch_up(self) -> None:
        recurrence = ScheduleRecurrence(
            kind=RecurrenceKind.INTERVAL,
            interval_seconds=10.0,
        )
        skipped = self.create(
            due_at=100.0,
            recurrence=recurrence,
            policy=MisfirePolicy.SKIP,
            grace=0.0,
            name="skip",
        )
        once = self.create(
            due_at=100.0,
            recurrence=recurrence,
            policy=MisfirePolicy.FIRE_ONCE,
            grace=0.0,
            name="once",
        )
        bounded = self.create(
            due_at=100.0,
            recurrence=recurrence,
            policy=MisfirePolicy.BOUNDED_CATCH_UP,
            grace=0.0,
            catch_up_limit=3,
            name="bounded",
        )
        self.clock.value = 145.0

        result = await self.service.run_due(max_events=100)

        self.assertEqual(result.claimed, 3)
        self.assertEqual(result.emitted, 4)
        self.assertEqual(self.schedule_store.get(skipped.id).next_run_at, 150.0)
        self.assertEqual(self.schedule_store.get(skipped.id).firing_count, 0)
        self.assertEqual(self.schedule_store.get(once.id).next_run_at, 150.0)
        self.assertEqual(self.schedule_store.get(once.id).firing_count, 1)
        self.assertEqual(self.schedule_store.get(bounded.id).next_run_at, 150.0)
        self.assertEqual(self.schedule_store.get(bounded.id).firing_count, 3)

        by_schedule: dict[str, list[float]] = {}
        for event in self.event_store.recent(limit=20):
            by_schedule.setdefault(event.payload["schedule_id"], []).append(
                event.payload["scheduled_for"]
            )
        self.assertNotIn(skipped.id, by_schedule)
        self.assertEqual(by_schedule[once.id], [140.0])
        self.assertEqual(sorted(by_schedule[bounded.id]), [120.0, 130.0, 140.0])

    async def test_daily_wall_clock_recurrence_preserves_local_time_across_dst(self) -> None:
        zone = ZoneInfo("Europe/Stockholm")
        first = datetime(2026, 3, 28, 9, 0, tzinfo=zone).timestamp()
        self.clock.value = first
        schedule = self.create(
            due_at=first,
            recurrence=ScheduleRecurrence(
                kind=RecurrenceKind.DAILY,
                local_time="09:00",
                timezone="Europe/Stockholm",
            ),
            name="daily-local",
        )

        result = await self.service.run_due()
        updated = self.schedule_store.get(schedule.id)

        self.assertEqual(result.emitted, 1)
        self.assertIsNotNone(updated.next_run_at)
        next_local = datetime.fromtimestamp(float(updated.next_run_at), zone)
        self.assertEqual(
            (next_local.year, next_local.month, next_local.day, next_local.hour),
            (2026, 3, 29, 9),
        )
        self.assertEqual(float(updated.next_run_at) - first, 23 * 60 * 60)

    async def test_pause_resume_and_cancel_control_due_processing(self) -> None:
        paused = self.create(due_at=110.0, name="paused")
        cancelled = self.create(due_at=110.0, name="cancelled")
        self.service.pause(paused.id, actor_id="human-admin")
        self.service.cancel(cancelled.id, actor_id="human-admin")
        self.clock.value = 120.0

        initial = await self.service.run_due()

        self.assertEqual(initial.claimed, 0)
        self.assertEqual(initial.emitted, 0)
        self.assertEqual(self.schedule_store.get(paused.id).status, ScheduleStatus.PAUSED)
        self.assertEqual(
            self.schedule_store.get(cancelled.id).status,
            ScheduleStatus.CANCELLED,
        )

        self.service.resume(paused.id, actor_id="human-admin")
        resumed = await self.service.run_due()

        self.assertEqual(resumed.claimed, 1)
        self.assertEqual(resumed.emitted, 1)
        self.assertEqual(
            self.schedule_store.get(paused.id).status,
            ScheduleStatus.COMPLETED,
        )

    async def test_large_clock_jump_is_bounded_and_advances_past_backlog(self) -> None:
        schedule = self.create(
            due_at=100.0,
            recurrence=ScheduleRecurrence(
                kind=RecurrenceKind.INTERVAL,
                interval_seconds=1.0,
            ),
            policy=MisfirePolicy.BOUNDED_CATCH_UP,
            grace=0.0,
            catch_up_limit=4,
            name="clock-jump",
        )
        self.clock.value = 1_000_000.0

        result = await self.service.run_due()
        updated = self.schedule_store.get(schedule.id)

        self.assertEqual(result.emitted, 4)
        self.assertEqual(updated.firing_count, 4)
        self.assertEqual(updated.next_run_at, 1_000_001.0)
        self.assertEqual(len(self.event_store.recent(limit=20)), 4)

    async def test_idle_not_due_schedule_emits_no_event(self) -> None:
        self.create(due_at=200.0)

        result = await self.service.run_due()

        self.assertEqual(result.claimed, 0)
        self.assertEqual(result.emitted, 0)
        self.assertEqual(self.event_store.recent(), [])

    async def test_leases_fence_other_scheduler_until_expiry(self) -> None:
        schedule = self.create(due_at=100.0)

        first = self.schedule_store.claim_due(
            now=100.0,
            owner_id="scheduler-a",
            lease_seconds=5.0,
            limit=1,
        )
        blocked = self.schedule_store.claim_due(
            now=101.0,
            owner_id="scheduler-b",
            lease_seconds=5.0,
            limit=1,
        )
        recovered = self.schedule_store.claim_due(
            now=106.0,
            owner_id="scheduler-b",
            lease_seconds=5.0,
            limit=1,
        )

        self.assertEqual([item.id for item in first], [schedule.id])
        self.assertEqual(blocked, [])
        self.assertEqual([item.id for item in recovered], [schedule.id])
        self.assertGreater(recovered[0].revision, first[0].revision)


if __name__ == "__main__":
    unittest.main()
