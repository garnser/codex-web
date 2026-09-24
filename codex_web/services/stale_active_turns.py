from __future__ import annotations

import asyncio
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from codex_web.execution_workers import (
    AssignmentStatus,
    ExecutionAssignment,
    ExecutionWorker,
    WorkerLifecycle,
)
from codex_web.models import ActiveThreadTurn, QueuedTurn
from codex_web.services.keyed_background_tasks import KeyedTaskCoordinator
from codex_web.storage.state_store import StateStore


RecoveryOutcome = Literal[
    "fresh",
    "live",
    "requeued",
    "terminal",
    "interrupted",
    "blocked",
]
RecoveryActionState = Literal["observed", "planned", "applied"]


class ActiveTurnRecoveryRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thread_id: str
    recovery_id: str
    source_updated_at: float
    observed_at: float
    outcome: RecoveryOutcome
    reason_code: str
    action_state: RecoveryActionState = "observed"
    actor_id: str
    original: ActiveThreadTurn
    evidence: dict[str, Any] = Field(default_factory=dict)
    resolved_at: float | None = None
    updated_at: float
    prior: tuple[dict[str, Any], ...] = ()


class ActiveTurnRecoveryMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    cursor: str | None = None
    planned_threads: tuple[str, ...] = ()
    blocked_current: int = 0
    last_started_at: float | None = None
    last_completed_at: float | None = None
    last_duration_seconds: float | None = None
    last_reason: str | None = None
    last_actor_id: str | None = None
    last_scanned: int = 0
    last_stale: int = 0
    last_fresh: int = 0
    last_live: int = 0
    last_requeued: int = 0
    last_terminal: int = 0
    last_interrupted: int = 0
    last_blocked: int = 0
    total_applied: int = 0


class ActiveTurnInspection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    thread_id: str
    stale: bool
    age_seconds: float
    outcome: RecoveryOutcome
    reason_code: str
    evidence: dict[str, Any] = Field(default_factory=dict)


class ActiveTurnRecoveryReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: str
    actor_id: str
    started_at: float
    completed_at: float
    scanned: int
    stale: int
    fresh: int
    live: int
    requeued: int
    terminal: int
    interrupted: int
    blocked: int
    planned_recovered: int
    drain_thread_ids: tuple[str, ...] = ()
    resume_thread_ids: tuple[str, ...] = ()
    legacy_backup_refs: tuple[str, ...] = ()


class ActiveTurnResolutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["retain", "interrupt", "release_to_queue"]
    reason: str = Field(min_length=1, max_length=500)


class ActiveTurnRecoveryStore:
    RECORD_NAMESPACE = "active_turn_recovery_records"
    META_NAMESPACE = "active_turn_recovery_meta"

    def __init__(self, store: StateStore) -> None:
        self.store = store
        self._lock = threading.RLock()

    def meta(self) -> ActiveTurnRecoveryMeta:
        return ActiveTurnRecoveryMeta.model_validate(
            self.store.get(self.META_NAMESPACE) or {}
        )

    def save_meta(self, meta: ActiveTurnRecoveryMeta) -> None:
        self.store.put(
            self.META_NAMESPACE,
            meta.model_dump(mode="json"),
        )

    def get(self, thread_id: str) -> ActiveTurnRecoveryRecord | None:
        raw = self.store.record_get(
            self.RECORD_NAMESPACE,
            str(thread_id),
        )
        return (
            ActiveTurnRecoveryRecord.model_validate(raw)
            if isinstance(raw, dict)
            else None
        )

    def put(self, record: ActiveTurnRecoveryRecord) -> None:
        with self._lock:
            previous = self.get(record.thread_id)
            meta = self.meta()
            if previous is not None and (
                previous.source_updated_at != record.source_updated_at
                or previous.recovery_id != record.recovery_id
            ):
                prior_item = previous.model_dump(
                    mode="json",
                    exclude={"prior"},
                )
                record = record.model_copy(
                    update={
                        "prior": tuple(
                            [
                                *previous.prior,
                                prior_item,
                            ][-20:]
                        )
                    }
                )
            previous_blocked = bool(
                previous is not None
                and previous.outcome == "blocked"
                and previous.action_state != "applied"
            )
            next_blocked = bool(
                record.outcome == "blocked"
                and record.action_state != "applied"
            )
            meta.blocked_current = max(
                0,
                meta.blocked_current
                + int(next_blocked)
                - int(previous_blocked),
            )
            self.store.record_apply(
                self.RECORD_NAMESPACE,
                upserts={
                    record.thread_id: record.model_dump(mode="json")
                },
            )
            self.save_meta(meta)

    def plan(self, record: ActiveTurnRecoveryRecord) -> None:
        with self._lock:
            self.put(
                record.model_copy(
                    update={"action_state": "planned"}
                )
            )
            meta = self.meta()
            planned = list(meta.planned_threads)
            if record.thread_id not in planned:
                planned.append(record.thread_id)
            meta.planned_threads = tuple(planned[-500:])
            self.save_meta(meta)

    def mark_applied(
        self,
        thread_id: str,
        *,
        resolved_at: float,
    ) -> ActiveTurnRecoveryRecord | None:
        with self._lock:
            current = self.get(thread_id)
            meta = self.meta()
            meta.planned_threads = tuple(
                value
                for value in meta.planned_threads
                if value != thread_id
            )
            if current is None:
                self.save_meta(meta)
                return None
            replacement = current.model_copy(
                update={
                    "action_state": "applied",
                    "resolved_at": resolved_at,
                    "updated_at": resolved_at,
                }
            )
            self.put(replacement)
            meta = self.meta()
            meta.planned_threads = tuple(
                value
                for value in meta.planned_threads
                if value != thread_id
            )
            meta.total_applied += 1
            self.save_meta(meta)
            return replacement

    def page(
        self,
        *,
        after: str | None = None,
        limit: int = 100,
    ) -> tuple[list[ActiveTurnRecoveryRecord], str | None]:
        raw, cursor = self.store.record_page(
            self.RECORD_NAMESPACE,
            after=after,
            limit=max(1, min(int(limit), 500)),
        )
        return (
            [
                ActiveTurnRecoveryRecord.model_validate(value)
                for value in raw.values()
                if isinstance(value, dict)
            ],
            cursor,
        )

    def count(self) -> int:
        return self.store.record_count(self.RECORD_NAMESPACE)


class StaleActiveTurnRecoveryService:
    def __init__(
        self,
        *,
        active_turns: Any,
        turn_queues: Any,
        worker_state_loader,
        store: ActiveTurnRecoveryStore,
        schedule_queue_drain,
        append_event,
        resume_active_threads=None,
        backup_directory: Path | None = None,
        clock=time.time,
    ) -> None:
        self.active_turns = active_turns
        self.turn_queues = turn_queues
        self.worker_state_loader = worker_state_loader
        self.store = store
        self.schedule_queue_drain = schedule_queue_drain
        self.append_event = append_event
        self.resume_active_threads = resume_active_threads
        self.backup_directory = backup_directory
        self.clock = clock
        self._mutation_lock = threading.RLock()
        self.coordinator = KeyedTaskCoordinator(
            max_concurrency=1,
            per_scope_concurrency=1,
        )

    def _backup_legacy_active_turns(self) -> str | None:
        source = getattr(self.active_turns, "legacy_path", None)
        if source is None:
            return None
        source = Path(source)
        if not source.exists():
            return None
        directory = (
            self.backup_directory
            if self.backup_directory is not None
            else source.parent / "active-turn-recovery-backups"
        )
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        path = directory / (
            f"active-turns-{int(self.clock())}-"
            f"{uuid.uuid4().hex[:12]}.json"
        )
        data = source.read_bytes()
        fd = os.open(
            path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                fd = -1
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if fd >= 0:
                os.close(fd)
        return f"file://{path.resolve()}"

    @staticmethod
    def stale_after_seconds() -> float:
        try:
            value = float(
                os.environ.get(
                    "CODEX_WEB_ACTIVE_TURN_RECONCILE_AFTER_SECONDS"
                )
                or "120"
            )
        except ValueError:
            value = 120.0
        return max(30.0, min(value, 7 * 24 * 3600.0))

    @classmethod
    def orphan_release_after_seconds(cls) -> float:
        """Return the hard expiry for ownerless active-turn markers.

        The shorter stale window starts evidence-based reconciliation.  This
        longer window prevents a marker with no queue, assignment, worker
        lease, fence, or prior resume attempt from blocking unattended work
        forever after a restart or an abruptly terminated provider turn.
        """
        try:
            value = float(
                os.environ.get(
                    "CODEX_WEB_ACTIVE_TURN_ORPHAN_RELEASE_AFTER_SECONDS"
                )
                or "900"
            )
        except ValueError:
            value = 900.0
        return max(
            cls.stale_after_seconds(),
            min(value, 7 * 24 * 3600.0),
        )

    @staticmethod
    def max_scan_records() -> int:
        try:
            value = int(
                os.environ.get(
                    "CODEX_WEB_ACTIVE_TURN_RECONCILE_MAX_RECORDS"
                )
                or "100"
            )
        except ValueError:
            value = 100
        return max(1, min(value, 1000))

    @staticmethod
    def _assignment_maps(worker_state):
        assignments_by_id: dict[str, ExecutionAssignment] = {}
        assignments_by_execution: dict[
            str,
            list[ExecutionAssignment],
        ] = {}
        workers: dict[str, ExecutionWorker] = {}
        if worker_state is None:
            return assignments_by_id, assignments_by_execution, workers
        for assignment in getattr(worker_state, "assignments", ()):
            assignments_by_id[assignment.id] = assignment
            assignments_by_execution.setdefault(
                assignment.execution_id,
                [],
            ).append(assignment)
        for worker in getattr(worker_state, "workers", ()):
            workers[worker.id] = worker
        return assignments_by_id, assignments_by_execution, workers

    @staticmethod
    def _queue_evidence(
        queued: list[QueuedTurn],
        active: ActiveThreadTurn,
    ) -> dict[str, Any]:
        matching = [
            item
            for item in queued
            if (
                active.execution_id
                and item.execution_id == active.execution_id
            )
            or (
                active.turn_id
                and item.id == active.turn_id
            )
        ]
        return {
            "queue_depth": len(queued),
            "matching_queue_ids": [item.id for item in matching[:20]],
        }

    def _assignment_for(
        self,
        active: ActiveThreadTurn,
        *,
        assignments_by_id: dict[str, ExecutionAssignment],
        assignments_by_execution: dict[
            str,
            list[ExecutionAssignment],
        ],
    ) -> tuple[ExecutionAssignment | None, bool]:
        if active.assignment_id:
            return assignments_by_id.get(active.assignment_id), False
        if not active.execution_id:
            return None, False
        matches = assignments_by_execution.get(active.execution_id, [])
        if len(matches) == 1:
            return matches[0], False
        return None, len(matches) > 1

    def _classify(
        self,
        active: ActiveThreadTurn,
        *,
        queued: list[QueuedTurn],
        assignments_by_id: dict[str, ExecutionAssignment],
        assignments_by_execution: dict[
            str,
            list[ExecutionAssignment],
        ],
        workers: dict[str, ExecutionWorker],
        now: float,
    ) -> ActiveTurnInspection:
        age = max(0.0, now - active.updated_at)
        stale = age > self.stale_after_seconds()
        queue_evidence = self._queue_evidence(queued, active)
        base_evidence: dict[str, Any] = {
            **queue_evidence,
            "source": active.source,
            "resume_attempts": active.resume_attempts,
            "last_resume_at": active.last_resume_at,
            "execution_id": active.execution_id,
            "assignment_id": active.assignment_id,
            "active_worker_id": active.worker_id,
            "active_fence": active.fence,
            "active_updated_at": active.updated_at,
        }

        assignment, ambiguous_assignment = self._assignment_for(
            active,
            assignments_by_id=assignments_by_id,
            assignments_by_execution=assignments_by_execution,
        )
        if ambiguous_assignment:
            return ActiveTurnInspection(
                thread_id=active.thread_id,
                stale=True,
                age_seconds=age,
                outcome="blocked",
                reason_code="multiple_assignments_for_execution",
                evidence=base_evidence,
            )

        if assignment is not None:
            lease = assignment.lease
            worker = (
                workers.get(assignment.assigned_worker_id or "")
                if assignment.assigned_worker_id
                else None
            )
            evidence = {
                **base_evidence,
                "resolved_assignment_id": assignment.id,
                "assignment_status": assignment.status.value,
                "assignment_fence": assignment.fence,
                "assigned_worker_id": assignment.assigned_worker_id,
                "lease_expires_at": (
                    lease.expires_at if lease is not None else None
                ),
                "worker_lifecycle": (
                    worker.lifecycle.value if worker is not None else None
                ),
                "worker_last_heartbeat_at": (
                    worker.last_heartbeat_at if worker is not None else None
                ),
            }
            if assignment.status in {
                AssignmentStatus.CLAIMED,
                AssignmentStatus.RUNNING,
            }:
                if lease is None:
                    return ActiveTurnInspection(
                        thread_id=active.thread_id,
                        stale=True,
                        age_seconds=age,
                        outcome="interrupted",
                        reason_code="active_assignment_missing_lease",
                        evidence=evidence,
                    )
                if (
                    active.worker_id
                    and active.worker_id != assignment.assigned_worker_id
                ) or (
                    active.fence is not None
                    and active.fence != assignment.fence
                ):
                    return ActiveTurnInspection(
                        thread_id=active.thread_id,
                        stale=True,
                        age_seconds=age,
                        outcome="blocked",
                        reason_code="active_marker_assignment_fence_mismatch",
                        evidence=evidence,
                    )
                worker_trusted = bool(
                    worker is not None
                    and worker.lifecycle
                    in {
                        WorkerLifecycle.ACTIVE,
                        WorkerLifecycle.DRAINING,
                    }
                )
                if lease.expires_at > now and worker_trusted:
                    return ActiveTurnInspection(
                        thread_id=active.thread_id,
                        stale=stale,
                        age_seconds=age,
                        outcome="live",
                        reason_code="valid_live_assignment_lease",
                        evidence=evidence,
                    )
                if lease.expires_at <= now:
                    return ActiveTurnInspection(
                        thread_id=active.thread_id,
                        stale=True,
                        age_seconds=age,
                        outcome="interrupted",
                        reason_code="assignment_lease_expired",
                        evidence=evidence,
                    )
                return ActiveTurnInspection(
                    thread_id=active.thread_id,
                    stale=True,
                    age_seconds=age,
                    outcome="interrupted",
                    reason_code="assignment_worker_not_trusted",
                    evidence=evidence,
                )

            if assignment.status == AssignmentStatus.PENDING:
                return ActiveTurnInspection(
                    thread_id=active.thread_id,
                    stale=stale,
                    age_seconds=age,
                    outcome="live",
                    reason_code="canonical_assignment_pending",
                    evidence=evidence,
                )

            if assignment.status == AssignmentStatus.SUCCEEDED:
                return ActiveTurnInspection(
                    thread_id=active.thread_id,
                    stale=True,
                    age_seconds=age,
                    outcome="terminal",
                    reason_code="assignment_succeeded_active_marker_stale",
                    evidence=evidence,
                )

            if assignment.status in {
                AssignmentStatus.FAILED,
                AssignmentStatus.CANCELLED,
                AssignmentStatus.LOST,
            }:
                if queued:
                    return ActiveTurnInspection(
                        thread_id=active.thread_id,
                        stale=True,
                        age_seconds=age,
                        outcome="requeued",
                        reason_code=(
                            f"assignment_{assignment.status.value}_"
                            "with_existing_queue"
                        ),
                        evidence=evidence,
                    )
                return ActiveTurnInspection(
                    thread_id=active.thread_id,
                    stale=True,
                    age_seconds=age,
                    outcome="interrupted",
                    reason_code=(
                        f"assignment_{assignment.status.value}"
                    ),
                    evidence=evidence,
                )

        if active.assignment_id:
            if queued:
                return ActiveTurnInspection(
                    thread_id=active.thread_id,
                    stale=True,
                    age_seconds=age,
                    outcome="requeued",
                    reason_code="missing_assignment_with_existing_queue",
                    evidence=base_evidence,
                )
            return ActiveTurnInspection(
                thread_id=active.thread_id,
                stale=True,
                age_seconds=age,
                outcome="interrupted",
                reason_code="assignment_record_missing",
                evidence=base_evidence,
            )

        if not stale:
            return ActiveTurnInspection(
                thread_id=active.thread_id,
                stale=False,
                age_seconds=age,
                outcome="fresh",
                reason_code="within_liveness_window_no_canonical_owner",
                evidence=base_evidence,
            )

        if queued:
            return ActiveTurnInspection(
                thread_id=active.thread_id,
                stale=True,
                age_seconds=age,
                outcome="requeued",
                reason_code="existing_queue_without_live_owner",
                evidence=base_evidence,
            )

        if active.resume_attempts > 0:
            return ActiveTurnInspection(
                thread_id=active.thread_id,
                stale=True,
                age_seconds=age,
                outcome="blocked",
                reason_code="prior_resume_without_canonical_owner",
                evidence=base_evidence,
            )

        source = (active.source or "").casefold()
        if source.startswith("queued:"):
            return ActiveTurnInspection(
                thread_id=active.thread_id,
                stale=True,
                age_seconds=age,
                outcome="interrupted",
                reason_code="orphaned_queued_turn",
                evidence=base_evidence,
            )

        if age > self.orphan_release_after_seconds():
            return ActiveTurnInspection(
                thread_id=active.thread_id,
                stale=True,
                age_seconds=age,
                outcome="interrupted",
                reason_code="ownerless_turn_hard_expired",
                evidence={
                    **base_evidence,
                    "orphan_release_after_seconds": (
                        self.orphan_release_after_seconds()
                    ),
                },
            )

        return ActiveTurnInspection(
            thread_id=active.thread_id,
            stale=True,
            age_seconds=age,
            outcome="blocked",
            reason_code="no_canonical_liveness_evidence",
            evidence=base_evidence,
        )

    @staticmethod
    def _recovery_id(active: ActiveThreadTurn) -> str:
        return (
            f"active-turn:{active.thread_id}:"
            f"{int(active.updated_at * 1_000_000)}"
        )

    def _record_from_inspection(
        self,
        active: ActiveThreadTurn,
        inspection: ActiveTurnInspection,
        *,
        actor_id: str,
        now: float,
    ) -> ActiveTurnRecoveryRecord:
        return ActiveTurnRecoveryRecord(
            thread_id=active.thread_id,
            recovery_id=self._recovery_id(active),
            source_updated_at=active.updated_at,
            observed_at=now,
            outcome=inspection.outcome,
            reason_code=inspection.reason_code,
            actor_id=actor_id,
            original=active.model_copy(deep=True),
            evidence=dict(inspection.evidence),
            updated_at=now,
        )

    def inspect(
        self,
        *,
        limit: int | None = None,
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        with self._mutation_lock:
            now = float(self.clock())
            worker_state = self.worker_state_loader()
            (
                assignments_by_id,
                assignments_by_execution,
                workers,
            ) = self._assignment_maps(worker_state)
            if thread_id is not None:
                active = self.active_turns.get(thread_id)
                values = (
                    {thread_id: active}
                    if active is not None
                    else {}
                )
                cursor = None
            else:
                values, cursor = self.active_turns.page(
                    limit=(
                        self.max_scan_records()
                        if limit is None
                        else max(1, min(int(limit), 1000))
                    )
                )
            items: list[ActiveTurnInspection] = []
            for active in values.values():
                queued = self.turn_queues.get(active.thread_id)
                items.append(
                    self._classify(
                        active,
                        queued=queued,
                        assignments_by_id=assignments_by_id,
                        assignments_by_execution=assignments_by_execution,
                        workers=workers,
                        now=now,
                    )
                )
            return {
                "generated_at": now,
                "active_turn_count": self.active_turns.count(),
                "items": [
                    item.model_dump(mode="json")
                    for item in items
                ],
                "next_cursor": cursor,
                "bounded": True,
            }

    def _finish_planned(
        self,
        *,
        now: float,
    ) -> tuple[int, list[str]]:
        recovered = 0
        drain: list[str] = []
        meta = self.store.meta()
        for thread_id in meta.planned_threads[
            : self.max_scan_records()
        ]:
            record = self.store.get(thread_id)
            if record is None or record.action_state != "planned":
                self.store.mark_applied(
                    thread_id,
                    resolved_at=now,
                )
                continue
            active = self.active_turns.get(thread_id)
            if (
                active is not None
                and active.updated_at != record.source_updated_at
            ):
                replacement = record.model_copy(
                    update={
                        "outcome": "blocked",
                        "reason_code": "active_turn_changed_after_recovery_plan",
                        "action_state": "observed",
                        "resolved_at": None,
                        "updated_at": now,
                    }
                )
                self.store.put(replacement)
                meta = self.store.meta()
                meta.planned_threads = tuple(
                    value
                    for value in meta.planned_threads
                    if value != thread_id
                )
                self.store.save_meta(meta)
                continue
            if active is not None:
                self.active_turns.delete(thread_id)
            self.store.mark_applied(
                thread_id,
                resolved_at=now,
            )
            recovered += 1
            if record.outcome == "requeued":
                drain.append(thread_id)
        return recovered, drain

    def _apply_inspection(
        self,
        active: ActiveThreadTurn,
        inspection: ActiveTurnInspection,
        *,
        actor_id: str,
        now: float,
        legacy_backups: list[str],
    ) -> tuple[ActiveTurnRecoveryRecord | None, bool]:
        if inspection.outcome == "fresh":
            return None, False

        previous = self.store.get(active.thread_id)
        if (
            previous is not None
            and previous.source_updated_at == active.updated_at
            and previous.outcome == "blocked"
            and previous.action_state == "observed"
            and inspection.outcome == "blocked"
        ):
            return previous, False

        record = self._record_from_inspection(
            active,
            inspection,
            actor_id=actor_id,
            now=now,
        )
        if inspection.outcome in {
            "requeued",
            "terminal",
            "interrupted",
        }:
            if not legacy_backups:
                backup_ref = self._backup_legacy_active_turns()
                if backup_ref:
                    legacy_backups.append(backup_ref)
            if legacy_backups:
                record = record.model_copy(
                    update={
                        "evidence": {
                            **record.evidence,
                            "legacy_backup_ref": legacy_backups[0],
                        }
                    }
                )
            self.store.plan(record)
            self.active_turns.delete(active.thread_id)
            applied = self.store.mark_applied(
                active.thread_id,
                resolved_at=now,
            )
            self.append_event(
                {
                    "type": "stale_active_turn_reconciled",
                    "thread_id": active.thread_id,
                    "outcome": inspection.outcome,
                    "reason_code": inspection.reason_code,
                    "recovery_id": record.recovery_id,
                }
            )
            return applied or record, inspection.outcome == "requeued"

        self.store.put(record)
        if inspection.outcome == "blocked":
            self.append_event(
                {
                    "type": "stale_active_turn_blocked",
                    "thread_id": active.thread_id,
                    "reason_code": inspection.reason_code,
                    "recovery_id": record.recovery_id,
                }
            )
        return record, False

    def _reconcile_sync(
        self,
        *,
        reason: str,
        actor_id: str,
        thread_id: str | None = None,
    ) -> ActiveTurnRecoveryReport:
        with self._mutation_lock:
            started = float(self.clock())
            planned_recovered, drain = self._finish_planned(
                now=started
            )
            resume: list[str] = []
            legacy_backups: list[str] = []
            worker_state = self.worker_state_loader()
            (
                assignments_by_id,
                assignments_by_execution,
                workers,
            ) = self._assignment_maps(worker_state)
            meta = self.store.meta()
            if thread_id is not None:
                active = self.active_turns.get(thread_id)
                values = (
                    {thread_id: active}
                    if active is not None
                    else {}
                )
                next_cursor = meta.cursor
            else:
                values, next_cursor = self.active_turns.page(
                    after=meta.cursor,
                    limit=self.max_scan_records(),
                )
                if not values and meta.cursor is not None:
                    values, next_cursor = self.active_turns.page(
                        after=None,
                        limit=self.max_scan_records(),
                    )

            counts = {
                "scanned": 0,
                "stale": 0,
                "fresh": 0,
                "live": 0,
                "requeued": 0,
                "terminal": 0,
                "interrupted": 0,
                "blocked": 0,
            }
            for active in values.values():
                counts["scanned"] += 1
                queued = self.turn_queues.get(active.thread_id)
                inspection = self._classify(
                    active,
                    queued=queued,
                    assignments_by_id=assignments_by_id,
                    assignments_by_execution=assignments_by_execution,
                    workers=workers,
                    now=started,
                )
                if inspection.stale:
                    counts["stale"] += 1
                counts[inspection.outcome] += 1
                if (
                    reason == "startup"
                    and inspection.outcome == "fresh"
                    and active.resume_attempts < 3
                ):
                    resume.append(active.thread_id)
                _, should_drain = self._apply_inspection(
                    active,
                    inspection,
                    actor_id=actor_id,
                    now=started,
                    legacy_backups=legacy_backups,
                )
                if should_drain:
                    drain.append(active.thread_id)

            if thread_id is None:
                meta = self.store.meta()
                meta.cursor = next_cursor
            else:
                meta = self.store.meta()
            completed = float(self.clock())
            meta.last_started_at = started
            meta.last_completed_at = completed
            meta.last_duration_seconds = max(
                0.0,
                completed - started,
            )
            meta.last_reason = reason
            meta.last_actor_id = actor_id
            meta.last_scanned = counts["scanned"]
            meta.last_stale = counts["stale"]
            meta.last_fresh = counts["fresh"]
            meta.last_live = counts["live"]
            meta.last_requeued = counts["requeued"]
            meta.last_terminal = counts["terminal"]
            meta.last_interrupted = counts["interrupted"]
            meta.last_blocked = counts["blocked"]
            self.store.save_meta(meta)
            self.active_turns.flush_legacy_mirror()

            return ActiveTurnRecoveryReport(
                reason=reason,
                actor_id=actor_id,
                started_at=started,
                completed_at=completed,
                planned_recovered=planned_recovered,
                drain_thread_ids=tuple(sorted(set(drain))),
                resume_thread_ids=tuple(sorted(set(resume))),
                legacy_backup_refs=tuple(legacy_backups),
                **counts,
            )

    async def reconcile(
        self,
        *,
        reason: str,
        actor_id: str,
        thread_id: str | None = None,
    ) -> ActiveTurnRecoveryReport:
        report = await asyncio.to_thread(
            self._reconcile_sync,
            reason=reason,
            actor_id=actor_id,
            thread_id=thread_id,
        )
        if (
            report.resume_thread_ids
            and self.resume_active_threads is not None
        ):
            await self.resume_active_threads(
                set(report.resume_thread_ids)
            )
        for value in report.drain_thread_ids:
            self.schedule_queue_drain(value)
        return report

    def schedule(self, *, reason: str = "runtime") -> bool:
        revision = f"{time.time():.6f}:{reason}"
        return self.coordinator.schedule(
            "stale-active-turns",
            lambda: self._scheduled_reconcile(reason=reason),
            revision=revision,
            scope="stale-active-turns",
            timeout_seconds=60.0,
        )

    def schedule_thread(
        self,
        thread_id: str,
        reason: str,
    ) -> bool:
        thread_id = str(thread_id or "").strip()
        if not thread_id:
            return False
        return self.coordinator.schedule(
            f"stale-active-turn:{thread_id}",
            lambda: self._scheduled_reconcile(
                reason=reason,
                thread_id=thread_id,
            ),
            revision=f"{time.time():.6f}:{reason}",
            scope="stale-active-turns",
            timeout_seconds=60.0,
        )

    async def _scheduled_reconcile(
        self,
        *,
        reason: str,
        thread_id: str | None = None,
    ) -> None:
        await self.reconcile(
            reason=reason,
            actor_id="system:stale-active-turn-recovery",
            thread_id=thread_id,
        )

    async def resolve(
        self,
        thread_id: str,
        payload: ActiveTurnResolutionRequest,
        *,
        actor_id: str,
    ) -> ActiveTurnRecoveryRecord:
        def apply() -> tuple[ActiveTurnRecoveryRecord, bool]:
            with self._mutation_lock:
                now = float(self.clock())
                active = self.active_turns.get(thread_id)
                current = self.store.get(thread_id)
                if active is None:
                    if current is None:
                        raise KeyError("active turn recovery record not found")
                    return current, False
                if current is None or current.outcome != "blocked":
                    raise ValueError(
                        "operator resolution requires a blocked recovery record"
                    )
                evidence = {
                    **current.evidence,
                    "operator_reason": payload.reason,
                    "operator_action": payload.action,
                }
                if payload.action == "retain":
                    replacement_active = active.model_copy(
                        update={"updated_at": now}
                    )
                    self.active_turns.put(
                        thread_id,
                        replacement_active,
                    )
                    replacement = current.model_copy(
                        update={
                            "outcome": "live",
                            "reason_code": "operator_retained",
                            "action_state": "applied",
                            "actor_id": actor_id,
                            "evidence": evidence,
                            "resolved_at": now,
                            "updated_at": now,
                        }
                    )
                    self.store.put(replacement)
                    self.active_turns.flush_legacy_mirror()
                    return replacement, False

                queued = self.turn_queues.get(thread_id)
                if (
                    payload.action == "release_to_queue"
                    and not queued
                ):
                    raise ValueError(
                        "release_to_queue requires an existing queued turn"
                    )
                outcome: RecoveryOutcome = (
                    "requeued"
                    if payload.action == "release_to_queue"
                    else "interrupted"
                )
                reason_code = (
                    "operator_released_to_queue"
                    if outcome == "requeued"
                    else "operator_interrupted"
                )
                backup_ref = self._backup_legacy_active_turns()
                replacement = current.model_copy(
                    update={
                        "outcome": outcome,
                        "reason_code": reason_code,
                        "action_state": "planned",
                        "actor_id": actor_id,
                        "evidence": {
                            **evidence,
                            **(
                                {"legacy_backup_ref": backup_ref}
                                if backup_ref
                                else {}
                            ),
                        },
                        "resolved_at": None,
                        "updated_at": now,
                    }
                )
                self.store.plan(replacement)
                self.active_turns.delete(thread_id)
                applied = self.store.mark_applied(
                    thread_id,
                    resolved_at=now,
                )
                self.active_turns.flush_legacy_mirror()
                return applied or replacement, outcome == "requeued"

        record, drain = await asyncio.to_thread(apply)
        if drain:
            self.schedule_queue_drain(thread_id)
        return record

    def audit_records(
        self,
        *,
        after: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        items, cursor = self.store.page(
            after=after,
            limit=max(1, min(int(limit), 500)),
        )
        return {
            "items": [
                item.model_dump(mode="json")
                for item in items
            ],
            "next_cursor": cursor,
            "count": self.store.count(),
            "bounded": True,
        }

    def status(self) -> dict[str, Any]:
        meta = self.store.meta()
        return {
            **meta.model_dump(mode="json"),
            "audit_count": self.store.count(),
            "stale_after_seconds": self.stale_after_seconds(),
            "orphan_release_after_seconds": (
                self.orphan_release_after_seconds()
            ),
            "max_scan_records": self.max_scan_records(),
            "coordinator": self.coordinator.status(),
        }

    async def stop(self) -> None:
        await self.coordinator.stop()
