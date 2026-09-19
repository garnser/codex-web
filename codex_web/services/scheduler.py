from __future__ import annotations

import asyncio
import logging
import math
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, time as wall_time
from typing import Callable
from zoneinfo import ZoneInfo

from codex_web.canonical_events import CanonicalEventType
from codex_web.scheduler import (
    MisfirePolicy,
    RecurrenceKind,
    ScheduleCreate,
    ScheduleRecord,
    ScheduleStatus,
)
from codex_web.services.canonical_events import (
    CanonicalEventDelivery,
    CanonicalEventIngestionService,
)
from codex_web.storage.scheduler import SchedulerStore


Clock = Callable[[], float]
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SchedulerRunResult:
    claimed: int
    processed: int
    emitted: int


@dataclass(frozen=True, slots=True)
class _OccurrenceWindow:
    first: float
    total_due: int
    latest_due: float
    next_future: float | None


@dataclass(frozen=True, slots=True)
class _FiringPlan:
    occurrences: tuple[float, ...]
    next_run_at: float | None
    status: ScheduleStatus
    processed_through: float


class SchedulerService:
    """Durable deterministic timer engine.

    The scheduler never invokes a model or privileged provider directly. Every
    firing is normalized into one canonical schedule.due event; normal
    orchestration policy then decides whether anything else should happen.
    """

    def __init__(
        self,
        store: SchedulerStore,
        canonical_events: CanonicalEventIngestionService,
        *,
        clock: Clock = time.time,
        owner_id: str | None = None,
        lease_seconds: float = 30.0,
        max_claims_per_tick: int = 100,
        max_events_per_tick: int = 100,
        max_idle_sleep_seconds: float = 30.0,
    ) -> None:
        self.store = store
        self.canonical_events = canonical_events
        self.clock = clock
        self.owner_id = owner_id or f"scheduler-{uuid.uuid4().hex}"
        self.lease_seconds = max(1.0, float(lease_seconds))
        self.max_claims_per_tick = max(1, int(max_claims_per_tick))
        self.max_events_per_tick = max(1, int(max_events_per_tick))
        self.max_idle_sleep_seconds = max(0.05, float(max_idle_sleep_seconds))
        self._wake = asyncio.Event()

    def notify_state_changed(self) -> None:
        """Wake the runtime after another canonical transaction changed schedules."""

        self._wake.set()

    def create(self, payload: ScheduleCreate, *, actor_id: str) -> ScheduleRecord:
        schedule = ScheduleRecord.from_create(
            payload,
            actor_id=actor_id,
            now=self.clock(),
        )
        result = self.store.create(schedule)
        self._wake.set()
        return result

    def list(self) -> list[ScheduleRecord]:
        return self.store.list()

    def get(self, schedule_id: str) -> ScheduleRecord:
        return self.store.get(schedule_id)

    def pause(self, schedule_id: str, *, actor_id: str) -> ScheduleRecord:
        result = self.store.pause(schedule_id, actor_id=actor_id)
        self._wake.set()
        return result

    def resume(self, schedule_id: str, *, actor_id: str) -> ScheduleRecord:
        result = self.store.resume(schedule_id, actor_id=actor_id)
        self._wake.set()
        return result

    def cancel(self, schedule_id: str, *, actor_id: str) -> ScheduleRecord:
        result = self.store.cancel(schedule_id, actor_id=actor_id)
        self._wake.set()
        return result

    @staticmethod
    def _daily_timestamp(recurrence, target_date: date) -> float:
        zone = ZoneInfo(str(recurrence.timezone))
        local_clock = wall_time.fromisoformat(str(recurrence.local_time))
        local = datetime.combine(target_date, local_clock, tzinfo=zone).replace(fold=0)
        return local.timestamp()

    def _next_occurrence(self, schedule: ScheduleRecord, after: float) -> float | None:
        recurrence = schedule.recurrence
        if recurrence is None:
            return None
        if recurrence.kind == RecurrenceKind.INTERVAL:
            return after + float(recurrence.interval_seconds or 0.0)

        zone = ZoneInfo(str(recurrence.timezone))
        local_after = datetime.fromtimestamp(after, zone)
        candidate = self._daily_timestamp(recurrence, local_after.date())
        if candidate > after + 1e-9:
            return candidate
        return self._daily_timestamp(
            recurrence,
            local_after.date() + timedelta(days=1),
        )

    def _occurrence_window(
        self,
        schedule: ScheduleRecord,
        *,
        now: float,
    ) -> _OccurrenceWindow:
        first = float(schedule.next_run_at or 0.0)
        if schedule.recurrence is None:
            return _OccurrenceWindow(
                first=first,
                total_due=1,
                latest_due=first,
                next_future=None,
            )

        recurrence = schedule.recurrence
        if recurrence.kind == RecurrenceKind.INTERVAL:
            interval = float(recurrence.interval_seconds or 0.0)
            total = int(math.floor(max(0.0, now - first) / interval)) + 1
            latest = first + (total - 1) * interval
            return _OccurrenceWindow(
                first=first,
                total_due=total,
                latest_due=latest,
                next_future=first + total * interval,
            )

        second = self._next_occurrence(schedule, first)
        if second is None or second > now:
            return _OccurrenceWindow(
                first=first,
                total_due=1,
                latest_due=first,
                next_future=second,
            )

        zone = ZoneInfo(str(recurrence.timezone))
        first_daily_date = datetime.fromtimestamp(second, zone).date()
        now_local = datetime.fromtimestamp(now, zone)
        latest_date = now_local.date()
        latest = self._daily_timestamp(recurrence, latest_date)
        if latest > now:
            latest_date -= timedelta(days=1)
            latest = self._daily_timestamp(recurrence, latest_date)

        if latest < second:
            return _OccurrenceWindow(
                first=first,
                total_due=1,
                latest_due=first,
                next_future=second,
            )

        daily_count = (latest_date - first_daily_date).days + 1
        return _OccurrenceWindow(
            first=first,
            total_due=1 + daily_count,
            latest_due=latest,
            next_future=self._daily_timestamp(
                recurrence,
                latest_date + timedelta(days=1),
            ),
        )

    def _occurrence_at(
        self,
        schedule: ScheduleRecord,
        window: _OccurrenceWindow,
        index: int,
    ) -> float:
        if index < 0 or index >= window.total_due:
            raise IndexError("schedule occurrence index out of range")
        if index == 0:
            return window.first

        recurrence = schedule.recurrence
        if recurrence is None:
            raise IndexError("one-shot schedule has only one occurrence")
        if recurrence.kind == RecurrenceKind.INTERVAL:
            return window.first + index * float(recurrence.interval_seconds or 0.0)

        second = self._next_occurrence(schedule, window.first)
        if second is None:
            raise RuntimeError("daily recurrence has no next occurrence")
        zone = ZoneInfo(str(recurrence.timezone))
        first_daily_date = datetime.fromtimestamp(second, zone).date()
        return self._daily_timestamp(
            recurrence,
            first_daily_date + timedelta(days=index - 1),
        )

    def _plan(
        self,
        schedule: ScheduleRecord,
        *,
        now: float,
        event_budget: int,
    ) -> _FiringPlan:
        window = self._occurrence_window(schedule, now=now)
        late_by = max(0.0, now - window.first)
        misfired = (
            window.total_due > 1
            or late_by > schedule.misfire_grace_seconds
        )

        emit_indexes: list[int]
        if not misfired:
            emit_indexes = [0]
        elif schedule.misfire_policy == MisfirePolicy.SKIP:
            emit_indexes = []
        elif schedule.misfire_policy == MisfirePolicy.FIRE_ONCE:
            emit_indexes = [window.total_due - 1]
        else:
            bounded = min(
                window.total_due,
                schedule.catch_up_limit,
                max(0, event_budget),
            )
            emit_indexes = list(range(window.total_due - bounded, window.total_due))

        if schedule.recurrence is None:
            return _FiringPlan(
                occurrences=tuple(
                    self._occurrence_at(schedule, window, index)
                    for index in emit_indexes[: max(0, event_budget)]
                ),
                next_run_at=None,
                status=ScheduleStatus.COMPLETED,
                processed_through=window.latest_due,
            )

        occurrences = tuple(
            self._occurrence_at(schedule, window, index)
            for index in emit_indexes[: max(0, event_budget)]
        )
        return _FiringPlan(
            occurrences=occurrences,
            next_run_at=window.next_future,
            status=ScheduleStatus.ACTIVE,
            processed_through=window.latest_due,
        )

    async def _publish_firing(
        self,
        schedule: ScheduleRecord,
        *,
        scheduled_for: float,
        fired_at: float,
    ) -> CanonicalEventDelivery:
        idempotency_key = f"{schedule.id}:{float(scheduled_for)!r}"
        return await self.canonical_events.ingest(
            event_type=CanonicalEventType.SCHEDULE,
            source=f"scheduler:{schedule.id}",
            idempotency_key=idempotency_key,
            payload={
                "schedule_id": schedule.id,
                "schedule_name": schedule.name,
                "trigger_type": schedule.trigger_type,
                "scheduled_for": scheduled_for,
                "misfire_policy": schedule.misfire_policy.value,
                "payload": dict(schedule.payload),
            },
            occurred_at=fired_at,
            tenant_id=schedule.tenant_id,
            workspace_id=schedule.workspace_id,
        )

    async def run_due(
        self,
        *,
        max_claims: int | None = None,
        max_events: int | None = None,
    ) -> SchedulerRunResult:
        now = float(self.clock())
        claim_limit = self.max_claims_per_tick if max_claims is None else max(0, int(max_claims))
        event_limit = self.max_events_per_tick if max_events is None else max(0, int(max_events))
        if claim_limit == 0 or event_limit == 0:
            return SchedulerRunResult(claimed=0, processed=0, emitted=0)

        claimed = self.store.claim_due(
            now=now,
            owner_id=self.owner_id,
            lease_seconds=self.lease_seconds,
            limit=claim_limit,
        )
        processed = 0
        emitted = 0

        for index, schedule in enumerate(claimed):
            remaining = max(0, event_limit - emitted)
            plan = self._plan(schedule, now=now, event_budget=remaining)
            last_fired_at = schedule.last_fired_at
            try:
                for scheduled_for in plan.occurrences:
                    delivery = await self._publish_firing(
                        schedule,
                        scheduled_for=scheduled_for,
                        fired_at=now,
                    )
                    last_fired_at = delivery.event.occurred_at
                    emitted += 1

                self.store.complete_claim(
                    schedule.id,
                    owner_id=self.owner_id,
                    expected_revision=schedule.revision,
                    next_run_at=plan.next_run_at,
                    status=plan.status,
                    last_fired_at=last_fired_at,
                    last_scheduled_for=plan.processed_through,
                    firing_count_increment=len(plan.occurrences),
                    now=now,
                )
                processed += 1
            except Exception:
                self.store.release_claim(
                    schedule.id,
                    owner_id=self.owner_id,
                    expected_revision=schedule.revision,
                    now=float(self.clock()),
                )
                for pending in claimed[index + 1 :]:
                    self.store.release_claim(
                        pending.id,
                        owner_id=self.owner_id,
                        expected_revision=pending.revision,
                        now=float(self.clock()),
                    )
                raise

            if emitted >= event_limit:
                for pending in claimed[index + 1 :]:
                    self.store.release_claim(
                        pending.id,
                        owner_id=self.owner_id,
                        expected_revision=pending.revision,
                        now=float(self.clock()),
                    )
                break

        return SchedulerRunResult(
            claimed=len(claimed),
            processed=processed,
            emitted=emitted,
        )

    async def run_forever(self) -> None:
        while True:
            try:
                await self.run_due()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("durable scheduler tick failed")
            now = float(self.clock())
            next_due = self.store.next_due_at(now=now)
            timeout = self.max_idle_sleep_seconds
            if next_due is not None:
                timeout = min(timeout, max(0.05, next_due - now))
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=timeout)
            except TimeoutError:
                pass
