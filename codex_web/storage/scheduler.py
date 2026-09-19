from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from codex_web.compatibility import MigrationRegistry
from codex_web.scheduler import (
    SCHEDULER_STATE_CONTRACT,
    ScheduleRecord,
    ScheduleStatus,
    SchedulerState,
)
from codex_web.storage.sqlite_state import SQLiteStateStore


SCHEDULER_STATE_MIGRATIONS = MigrationRegistry("scheduler-state")
SCHEDULER_STATE_MIGRATIONS.register(
    "0.0",
    "1.0",
    lambda payload: {
        "schema_version": SCHEDULER_STATE_CONTRACT.current,
        "schedules": dict(payload.get("schedules") or {}),
    },
)


class ScheduleNotFoundError(KeyError):
    pass


class ScheduleConflictError(RuntimeError):
    pass


class SchedulerStore:
    """Transactional durable state for canonical schedules and scheduler leases."""

    namespace = "scheduler"

    def __init__(self, store: SQLiteStateStore) -> None:
        self.store = store

    def _decode(self, payload: Any) -> SchedulerState:
        if payload is None:
            return SchedulerState()
        if not isinstance(payload, dict):
            raise ValueError("scheduler state must be an object")
        version = str(payload.get("schema_version") or "0.0")
        if version != SCHEDULER_STATE_CONTRACT.current:
            payload = SCHEDULER_STATE_MIGRATIONS.migrate(
                payload,
                from_version=version,
                to_version=SCHEDULER_STATE_CONTRACT.current,
            )
        SCHEDULER_STATE_CONTRACT.require(payload.get("schema_version", ""))
        return SchedulerState.model_validate(payload)

    def load(self) -> SchedulerState:
        return self._decode(self.store.get(self.namespace))

    def list(self) -> list[ScheduleRecord]:
        return sorted(
            self.load().schedules.values(),
            key=lambda item: (item.created_at, item.id),
        )

    def get(self, schedule_id: str) -> ScheduleRecord:
        schedule = self.load().schedules.get(schedule_id)
        if schedule is None:
            raise ScheduleNotFoundError(schedule_id)
        return schedule

    def _update(
        self,
        updater: Callable[[SchedulerState], SchedulerState],
    ) -> SchedulerState:
        payload = self.store.update(
            self.namespace,
            lambda raw: updater(self._decode(raw)).model_dump(mode="json"),
            default=SchedulerState().model_dump(mode="json"),
        )
        return self._decode(payload)

    def create(self, schedule: ScheduleRecord) -> ScheduleRecord:
        created: dict[str, ScheduleRecord] = {}

        def apply(state: SchedulerState) -> SchedulerState:
            if schedule.id in state.schedules:
                raise ScheduleConflictError(f"schedule already exists: {schedule.id}")
            state.schedules[schedule.id] = schedule
            created["value"] = schedule
            return state

        self._update(apply)
        return created["value"]

    def _transition(
        self,
        schedule_id: str,
        *,
        actor_id: str,
        transition: Callable[[ScheduleRecord], ScheduleRecord],
    ) -> ScheduleRecord:
        result: dict[str, ScheduleRecord] = {}

        def apply(state: SchedulerState) -> SchedulerState:
            current = state.schedules.get(schedule_id)
            if current is None:
                raise ScheduleNotFoundError(schedule_id)
            updated = transition(current)
            updated = updated.model_copy(
                update={
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "revision": current.revision + 1,
                    "updated_at": time.time(),
                    "updated_by": actor_id,
                }
            )
            state.schedules[schedule_id] = updated
            result["value"] = updated
            return state

        self._update(apply)
        return result["value"]

    def pause(self, schedule_id: str, *, actor_id: str) -> ScheduleRecord:
        def transition(current: ScheduleRecord) -> ScheduleRecord:
            if current.status == ScheduleStatus.CANCELLED:
                raise ScheduleConflictError("cancelled schedule cannot be paused")
            if current.status == ScheduleStatus.COMPLETED:
                raise ScheduleConflictError("completed schedule cannot be paused")
            return current.model_copy(update={"status": ScheduleStatus.PAUSED})

        return self._transition(schedule_id, actor_id=actor_id, transition=transition)

    def resume(self, schedule_id: str, *, actor_id: str) -> ScheduleRecord:
        def transition(current: ScheduleRecord) -> ScheduleRecord:
            if current.status == ScheduleStatus.CANCELLED:
                raise ScheduleConflictError("cancelled schedule cannot be resumed")
            if current.status == ScheduleStatus.COMPLETED:
                raise ScheduleConflictError("completed schedule cannot be resumed")
            return current.model_copy(update={"status": ScheduleStatus.ACTIVE})

        return self._transition(schedule_id, actor_id=actor_id, transition=transition)

    def cancel(self, schedule_id: str, *, actor_id: str) -> ScheduleRecord:
        return self._transition(
            schedule_id,
            actor_id=actor_id,
            transition=lambda current: current.model_copy(
                update={
                    "status": ScheduleStatus.CANCELLED,
                    "next_run_at": None,
                }
            ),
        )

    def claim_due(
        self,
        *,
        now: float,
        owner_id: str,
        lease_seconds: float,
        limit: int,
    ) -> list[ScheduleRecord]:
        if not owner_id.strip():
            raise ValueError("scheduler lease owner must not be empty")
        lease_seconds = max(1.0, float(lease_seconds))
        limit = max(0, int(limit))
        if limit == 0:
            return []

        claimed: list[ScheduleRecord] = []

        def apply(state: SchedulerState) -> SchedulerState:
            candidates = sorted(
                (
                    item
                    for item in state.schedules.values()
                    if item.status == ScheduleStatus.ACTIVE
                    and item.next_run_at is not None
                    and item.next_run_at <= now
                    and (
                        item.lease_owner is None
                        or item.lease_expires_at is None
                        or item.lease_expires_at <= now
                        or item.lease_owner == owner_id
                    )
                ),
                key=lambda item: (float(item.next_run_at or 0.0), item.id),
            )
            for current in candidates[:limit]:
                updated = current.model_copy(
                    update={
                        "lease_owner": owner_id,
                        "lease_expires_at": now + lease_seconds,
                        "revision": current.revision + 1,
                        "updated_at": now,
                        "updated_by": owner_id,
                    }
                )
                state.schedules[current.id] = updated
                claimed.append(updated)
            return state

        self._update(apply)
        return claimed

    def complete_claim(
        self,
        schedule_id: str,
        *,
        owner_id: str,
        expected_revision: int,
        next_run_at: float | None,
        status: ScheduleStatus,
        last_fired_at: float | None,
        last_scheduled_for: float | None,
        firing_count_increment: int,
        now: float,
    ) -> ScheduleRecord:
        if firing_count_increment < 0:
            raise ValueError("firing_count_increment cannot be negative")
        result: dict[str, ScheduleRecord] = {}

        def apply(state: SchedulerState) -> SchedulerState:
            current = state.schedules.get(schedule_id)
            if current is None:
                raise ScheduleNotFoundError(schedule_id)
            if (
                current.lease_owner != owner_id
                or current.revision != expected_revision
                or current.status != ScheduleStatus.ACTIVE
            ):
                raise ScheduleConflictError(
                    "scheduler claim is stale or no longer owned by this worker"
                )
            updated = current.model_copy(
                update={
                    "next_run_at": next_run_at,
                    "status": status,
                    "last_fired_at": last_fired_at,
                    "last_scheduled_for": last_scheduled_for,
                    "firing_count": current.firing_count + firing_count_increment,
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "revision": current.revision + 1,
                    "updated_at": now,
                    "updated_by": owner_id,
                }
            )
            state.schedules[schedule_id] = updated
            result["value"] = updated
            return state

        self._update(apply)
        return result["value"]

    def release_claim(
        self,
        schedule_id: str,
        *,
        owner_id: str,
        expected_revision: int,
        now: float,
    ) -> bool:
        released = False

        def apply(state: SchedulerState) -> SchedulerState:
            nonlocal released
            current = state.schedules.get(schedule_id)
            if current is None:
                return state
            if (
                current.lease_owner != owner_id
                or current.revision != expected_revision
            ):
                return state
            state.schedules[schedule_id] = current.model_copy(
                update={
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "revision": current.revision + 1,
                    "updated_at": now,
                    "updated_by": owner_id,
                }
            )
            released = True
            return state

        self._update(apply)
        return released

    def next_due_at(self, *, now: float | None = None) -> float | None:
        timestamp = time.time() if now is None else float(now)
        candidates = [
            item.next_run_at
            for item in self.load().schedules.values()
            if item.status == ScheduleStatus.ACTIVE
            and item.next_run_at is not None
            and (
                item.lease_owner is None
                or item.lease_expires_at is None
                or item.lease_expires_at <= timestamp
            )
        ]
        return min(candidates) if candidates else None
