from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codex_web.execution_workers import (
    AssignmentStatus,
    WorkerLifecycle,
)
from codex_web.models import ActiveThreadTurn, QueuedTurn
from codex_web.services.stale_active_turns import (
    ActiveTurnRecoveryStore,
    ActiveTurnResolutionRequest,
    StaleActiveTurnRecoveryService,
)
from codex_web.storage.runtime_state import ModelMapRepository
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.turn_queue import TurnQueueRepository


NOW = 1_800_000_000.0


def _active(
    thread_id: str,
    *,
    age: float = 14 * 24 * 3600,
    source: str = "queued:web",
    execution_id: str | None = None,
    assignment_id: str | None = None,
    worker_id: str | None = None,
    fence: int | None = None,
    resume_attempts: int = 0,
) -> ActiveThreadTurn:
    return ActiveThreadTurn(
        thread_id=thread_id,
        project_id="home",
        source=source,
        execution_id=execution_id,
        assignment_id=assignment_id,
        worker_id=worker_id,
        fence=fence,
        started_at=NOW - age,
        updated_at=NOW - age,
        resume_attempts=resume_attempts,
    )


def _queued(
    thread_id: str,
    *,
    execution_id: str | None = None,
) -> QueuedTurn:
    return QueuedTurn(
        id=f"queued-{thread_id}",
        thread_id=thread_id,
        project_id="home",
        message="continue work",
        execution_id=execution_id,
        source="web",
        created_at=NOW - 300,
    )


def _assignment(
    *,
    assignment_id: str,
    execution_id: str,
    status: AssignmentStatus,
    worker_id: str | None = None,
    fence: int = 1,
    lease_expires_at: float | None = None,
):
    lease = (
        SimpleNamespace(
            worker_id=worker_id,
            fence=fence,
            expires_at=lease_expires_at,
        )
        if worker_id and lease_expires_at is not None
        else None
    )
    return SimpleNamespace(
        id=assignment_id,
        execution_id=execution_id,
        status=status,
        assigned_worker_id=worker_id,
        fence=fence,
        lease=lease,
    )


def _worker(
    worker_id: str,
    lifecycle: WorkerLifecycle,
):
    return SimpleNamespace(
        id=worker_id,
        lifecycle=lifecycle,
        last_heartbeat_at=NOW - 10,
    )


class StaleActiveTurnRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = SQLiteStateStore(self.root / "state.sqlite3")
        self.active = ModelMapRepository(
            self.state,
            namespace="active_turns",
            legacy_path=self.root / "active_turns.json",
            model=ActiveThreadTurn,
        )
        self.queues = TurnQueueRepository(
            self.state,
            self.root / "queued_turns.json",
        )
        self.recovery_store = ActiveTurnRecoveryStore(self.state)
        self.worker_state = SimpleNamespace(
            assignments=[],
            workers=[],
        )
        self.events: list[dict] = []
        self.drained: list[str] = []
        self.resumed: list[set[str]] = []

        async def resume(values: set[str]) -> None:
            self.resumed.append(set(values))

        self.service = StaleActiveTurnRecoveryService(
            active_turns=self.active,
            turn_queues=self.queues,
            worker_state_loader=lambda: self.worker_state,
            store=self.recovery_store,
            schedule_queue_drain=self.drained.append,
            append_event=lambda event: self.events.append(
                dict(event)
            ),
            resume_active_threads=resume,
            backup_directory=self.root / "recovery-backups",
            clock=lambda: NOW,
        )

    def _put_active(self, value: ActiveThreadTurn) -> None:
        self.active.put(value.thread_id, value)
        self.active.flush_legacy_mirror()

    async def test_reported_old_queued_turn_with_empty_queue_is_interrupted(self) -> None:
        active = _active("thread-old")
        self._put_active(active)

        report = await self.service.reconcile(
            reason="startup",
            actor_id="system:test",
        )

        self.assertEqual(report.scanned, 1)
        self.assertEqual(report.stale, 1)
        self.assertEqual(report.interrupted, 1)
        self.assertIsNone(self.active.get("thread-old"))
        record = self.recovery_store.get("thread-old")
        assert record is not None
        self.assertEqual(record.outcome, "interrupted")
        self.assertEqual(
            record.reason_code,
            "orphaned_queued_turn",
        )
        self.assertEqual(record.action_state, "applied")
        self.assertEqual(
            record.original.model_dump(mode="json"),
            active.model_dump(mode="json"),
        )
        self.assertEqual(len(report.legacy_backup_refs), 1)
        backup = Path(
            report.legacy_backup_refs[0].removeprefix("file://")
        )
        self.assertTrue(backup.exists())
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            backup.parent.stat().st_mode & 0o777,
            0o700,
        )

        repeated = await self.service.reconcile(
            reason="startup",
            actor_id="system:test",
        )
        self.assertEqual(repeated.interrupted, 0)
        self.assertEqual(
            self.recovery_store.meta().total_applied,
            1,
        )

    async def test_old_turn_with_valid_live_assignment_lease_is_retained(self) -> None:
        active = _active(
            "thread-live",
            execution_id="exec-live",
            assignment_id="assignment-live",
            worker_id="worker-1",
            fence=3,
        )
        self._put_active(active)
        self.worker_state = SimpleNamespace(
            assignments=[
                _assignment(
                    assignment_id="assignment-live",
                    execution_id="exec-live",
                    status=AssignmentStatus.RUNNING,
                    worker_id="worker-1",
                    fence=3,
                    lease_expires_at=NOW + 120,
                )
            ],
            workers=[
                _worker(
                    "worker-1",
                    WorkerLifecycle.ACTIVE,
                )
            ],
        )

        report = await self.service.reconcile(
            reason="startup",
            actor_id="system:test",
        )

        self.assertEqual(report.live, 1)
        self.assertEqual(report.stale, 1)
        self.assertIsNotNone(self.active.get("thread-live"))
        record = self.recovery_store.get("thread-live")
        assert record is not None
        self.assertEqual(
            record.reason_code,
            "valid_live_assignment_lease",
        )
        self.assertEqual(record.action_state, "observed")
        self.assertEqual(report.legacy_backup_refs, ())

    async def test_lost_worker_with_unexpired_lease_is_interrupted(self) -> None:
        active = _active(
            "thread-offline",
            execution_id="exec-offline",
            assignment_id="assignment-offline",
            worker_id="worker-offline",
            fence=2,
        )
        self._put_active(active)
        self.worker_state = SimpleNamespace(
            assignments=[
                _assignment(
                    assignment_id="assignment-offline",
                    execution_id="exec-offline",
                    status=AssignmentStatus.RUNNING,
                    worker_id="worker-offline",
                    fence=2,
                    lease_expires_at=NOW + 300,
                )
            ],
            workers=[
                _worker(
                    "worker-offline",
                    WorkerLifecycle.OFFLINE,
                )
            ],
        )

        report = await self.service.reconcile(
            reason="startup",
            actor_id="system:test",
        )

        self.assertEqual(report.interrupted, 1)
        self.assertIsNone(self.active.get("thread-offline"))
        record = self.recovery_store.get("thread-offline")
        assert record is not None
        self.assertEqual(
            record.reason_code,
            "assignment_worker_not_trusted",
        )

    async def test_stale_turn_with_queue_is_released_to_existing_queue(self) -> None:
        active = _active("thread-queued")
        self._put_active(active)
        self.queues.put(
            "thread-queued",
            [_queued("thread-queued")],
        )

        report = await self.service.reconcile(
            reason="queue-recovery",
            actor_id="system:test",
        )

        self.assertEqual(report.requeued, 1)
        self.assertIsNone(self.active.get("thread-queued"))
        self.assertEqual(self.drained, ["thread-queued"])
        self.assertEqual(
            len(self.queues.get("thread-queued")),
            1,
        )

    async def test_prior_resume_attempt_without_owner_is_blocked(self) -> None:
        active = _active(
            "thread-ambiguous",
            source="web",
            resume_attempts=1,
        )
        self._put_active(active)

        first = await self.service.reconcile(
            reason="startup",
            actor_id="system:test",
        )
        second = await self.service.reconcile(
            reason="startup",
            actor_id="system:test",
            thread_id="thread-ambiguous",
        )

        self.assertEqual(first.blocked, 1)
        self.assertEqual(second.blocked, 1)
        self.assertIsNotNone(
            self.active.get("thread-ambiguous")
        )
        self.assertEqual(
            self.recovery_store.meta().blocked_current,
            1,
        )
        record = self.recovery_store.get("thread-ambiguous")
        assert record is not None
        self.assertEqual(
            record.reason_code,
            "prior_resume_without_canonical_owner",
        )

    async def test_ownerless_turn_is_blocked_during_hard_expiry_grace(self) -> None:
        active = _active(
            "thread-ownerless-grace",
            age=10 * 60,
            source="web",
        )
        self._put_active(active)

        report = await self.service.reconcile(
            reason="runtime",
            actor_id="system:test",
        )

        self.assertEqual(report.blocked, 1)
        self.assertIsNotNone(
            self.active.get("thread-ownerless-grace")
        )
        record = self.recovery_store.get(
            "thread-ownerless-grace"
        )
        assert record is not None
        self.assertEqual(
            record.reason_code,
            "no_canonical_liveness_evidence",
        )

    async def test_ownerless_turn_is_interrupted_after_hard_expiry(self) -> None:
        active = _active(
            "thread-ownerless-expired",
            age=16 * 60,
            source="web",
        )
        self._put_active(active)

        report = await self.service.reconcile(
            reason="runtime",
            actor_id="system:test",
        )

        self.assertEqual(report.interrupted, 1)
        self.assertIsNone(
            self.active.get("thread-ownerless-expired")
        )
        record = self.recovery_store.get(
            "thread-ownerless-expired"
        )
        assert record is not None
        self.assertEqual(
            record.reason_code,
            "ownerless_turn_hard_expired",
        )
        self.assertEqual(
            record.evidence["orphan_release_after_seconds"],
            900.0,
        )
        self.assertEqual(record.action_state, "applied")

    async def test_ownerless_hard_expiry_is_configurable(self) -> None:
        active = _active(
            "thread-ownerless-configured",
            age=6 * 60,
            source="web",
        )
        self._put_active(active)

        with patch.dict(
            os.environ,
            {
                "CODEX_WEB_ACTIVE_TURN_ORPHAN_RELEASE_AFTER_SECONDS": "300",
            },
            clear=False,
        ):
            report = await self.service.reconcile(
                reason="runtime",
                actor_id="system:test",
            )

        self.assertEqual(report.interrupted, 1)
        self.assertIsNone(
            self.active.get("thread-ownerless-configured")
        )

    async def test_multiple_assignments_for_execution_are_blocked(self) -> None:
        active = _active(
            "thread-multiple",
            source="web",
            execution_id="exec-shared",
        )
        self._put_active(active)
        self.worker_state = SimpleNamespace(
            assignments=[
                _assignment(
                    assignment_id="assignment-1",
                    execution_id="exec-shared",
                    status=AssignmentStatus.PENDING,
                ),
                _assignment(
                    assignment_id="assignment-2",
                    execution_id="exec-shared",
                    status=AssignmentStatus.PENDING,
                ),
            ],
            workers=[],
        )

        report = await self.service.reconcile(
            reason="startup",
            actor_id="system:test",
        )

        self.assertEqual(report.blocked, 1)
        self.assertIsNotNone(
            self.active.get("thread-multiple")
        )
        record = self.recovery_store.get("thread-multiple")
        assert record is not None
        self.assertEqual(
            record.reason_code,
            "multiple_assignments_for_execution",
        )

    async def test_crash_after_plan_is_finished_idempotently_on_next_run(self) -> None:
        active = _active("thread-crash")
        self._put_active(active)
        real_delete = self.active.delete

        with patch.object(
            self.active,
            "delete",
            side_effect=RuntimeError("injected crash"),
        ):
            with self.assertRaises(RuntimeError):
                await self.service.reconcile(
                    reason="startup",
                    actor_id="system:test",
                )

        planned = self.recovery_store.get("thread-crash")
        assert planned is not None
        self.assertEqual(planned.action_state, "planned")
        self.assertIsNotNone(self.active.get("thread-crash"))
        self.assertIn(
            "legacy_backup_ref",
            planned.evidence,
        )

        with patch.object(
            self.active,
            "delete",
            side_effect=real_delete,
        ):
            report = await self.service.reconcile(
                reason="startup",
                actor_id="system:test",
            )

        self.assertEqual(report.planned_recovered, 1)
        self.assertIsNone(self.active.get("thread-crash"))
        applied = self.recovery_store.get("thread-crash")
        assert applied is not None
        self.assertEqual(applied.action_state, "applied")
        self.assertEqual(
            self.recovery_store.meta().total_applied,
            1,
        )

    async def test_fresh_ownerless_turn_is_only_startup_resume_candidate(self) -> None:
        active = _active(
            "thread-fresh",
            age=10,
            source="web",
        )
        self._put_active(active)

        report = await self.service.reconcile(
            reason="startup",
            actor_id="system:test",
        )

        self.assertEqual(report.fresh, 1)
        self.assertEqual(
            report.resume_thread_ids,
            ("thread-fresh",),
        )
        self.assertEqual(
            self.resumed,
            [{"thread-fresh"}],
        )
        self.assertIsNotNone(self.active.get("thread-fresh"))

    async def test_operator_can_retain_blocked_turn_with_audit_provenance(self) -> None:
        active = _active(
            "thread-operator",
            source="web",
            resume_attempts=1,
        )
        self._put_active(active)
        await self.service.reconcile(
            reason="startup",
            actor_id="system:test",
        )

        record = await self.service.resolve(
            "thread-operator",
            ActiveTurnResolutionRequest(
                action="retain",
                reason="provider confirms execution is still live",
            ),
            actor_id="admin-1",
        )

        self.assertEqual(record.outcome, "live")
        self.assertEqual(record.reason_code, "operator_retained")
        self.assertEqual(record.actor_id, "admin-1")
        self.assertEqual(
            record.evidence["operator_action"],
            "retain",
        )
        self.assertEqual(
            self.recovery_store.meta().blocked_current,
            0,
        )
        retained = self.active.get("thread-operator")
        assert retained is not None
        self.assertEqual(retained.updated_at, NOW)

    def test_inspection_is_bounded_and_does_not_load_full_active_map(self) -> None:
        for index in range(5):
            self._put_active(
                _active(
                    f"thread-{index}",
                    source="web",
                )
            )

        with patch.dict(
            os.environ,
            {
                "CODEX_WEB_ACTIVE_TURN_RECONCILE_MAX_RECORDS": "2",
            },
            clear=False,
        ):
            report = self.service.inspect()

        self.assertEqual(report["active_turn_count"], 5)
        self.assertEqual(len(report["items"]), 2)
        self.assertTrue(report["bounded"])


if __name__ == "__main__":
    unittest.main()
