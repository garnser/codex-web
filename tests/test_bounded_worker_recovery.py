"""Bounded recovery with actual canonical rows and concurrent facade races."""
import unittest
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

from codex_web.execution_subjects import ExecutionSubject
from codex_web.execution_workers import (
    AssignmentCancelRequest, AssignmentClaimRequest, AssignmentRenewRequest,
    AssignmentStatus, WorkerHeartbeatRequest,
)
from codex_web.services.execution_workers import ExecutionWorkerService, WorkerConflictError
from codex_web.services.identity import AuthorizationError
from codex_web.storage.execution_workers import ExecutionWorkerStore
from tests import test_execution_workers as worker_fixtures
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.state_store import StorageOperationBudget, storage_operation_budget


class BoundedWorkerRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.f = worker_fixtures.ExecutionWorkerServiceTests(); self.f.setUp()
        self.addCleanup(self.f.tearDown)

    def other_facade(self):
        return ExecutionWorkerService(ExecutionWorkerStore(SQLiteStateStore(self.f.service.store.store.path)), identity=self.f.identity)

    def bootstrap(self):
        return self.f._assignment(work_item_ref=None,
            subject=ExecutionSubject(kind='thread_bootstrap', ref='bootstrap-bounded'))

    def claim(self, assignment):
        return self.f.service.claim(self.f.worker.id, AssignmentClaimRequest(lease_seconds=30),
            actor=self.f.worker_actor, assignment_id=assignment.id)

    def observation(self, assignment, **kwargs):
        self.f.service.record_runtime_observation(self.f.worker.id, assignment.id,
            observed_fence=assignment.fence, failure_code='execution_timeout',
            timings={'validation_authority_seconds': .1, 'renewal_authority_wait_seconds': .2},
            evidence_id=None, actor=self.f.worker_actor, phase='validation_authority', **kwargs)

    def test_cancellation_is_targeted_idempotent_and_preserves_other_rows(self):
        a = self.bootstrap(); other = self.f._assignment(execution_id='unrelated')
        before = self.f.service.store.load()
        request = AssignmentCancelRequest(expected_fence=0, reason='qualified idle recovery')
        with patch.object(self.f.service.store, '_load_unlocked', side_effect=AssertionError('history monopolized mutation lock')):
            cancelled = self.f.service.cancel_bootstrap(a.id, request, actor=self.f.admin)
            repeated = self.f.service.cancel_bootstrap(a.id, request, actor=self.f.admin)
            self.f.service.heartbeat(self.f.worker.id, WorkerHeartbeatRequest(), actor=self.f.worker_actor)
        after = self.f.service.store.load()
        self.assertEqual(cancelled, repeated)
        self.assertEqual((cancelled.status, cancelled.fence, cancelled.lease), (AssignmentStatus.CANCELLED, 1, None))
        self.assertEqual(self.f.service.store.assignment(other.id), other)
        self.assertEqual(after.events[:-1], before.events)
        self.assertEqual(after.events[-1].event_type, 'assignment_cancelled')

    def test_cancellation_rechecks_fence_after_another_facade_claims(self):
        a = self.bootstrap(); original = self.f.service.store.transition_assignment
        other = self.other_facade()
        def raced(worker, target, updater, **kwargs):
            claimed = other.claim(self.f.worker.id, AssignmentClaimRequest(),
                actor=self.f.worker_actor, assignment_id=a.id)
            self.assertIsNotNone(claimed)
            return original(worker, target, updater, **kwargs)
        with patch.object(self.f.service.store, 'transition_assignment', side_effect=raced):
            with self.assertRaises(WorkerConflictError):
                self.f.service.cancel_bootstrap(a.id,
                    AssignmentCancelRequest(expected_fence=0, reason='stale operator observation'), actor=self.f.admin)
        current = self.f.service.store.assignment(a.id)
        self.assertEqual((current.status, current.fence), (AssignmentStatus.CLAIMED, 1))
        self.assertIsNotNone(current.lease)

    def test_expiry_uses_point_rows_and_releases_workspace_only_after_commit(self):
        a = self.f._assignment()
        def attach_release_reference(state):
            state.assignments[0] = state.assignments[0].model_copy(update={'execution_workspace_id': 'retained-workspace'})
            return state
        self.f.service.store.update_assignment(self.f.worker.id, a.id, attach_release_reference)
        claimed = self.claim(a); other = self.f._assignment(execution_id='unrelated')
        released = []
        def release(workspace_id, payload, **kwargs):
            # Another facade can write only after the expiration writer commits.
            self.other_facade().heartbeat(self.f.worker.id, WorkerHeartbeatRequest(), actor=self.f.worker_actor)
            self.assertEqual(self.f.service.store.assignment(a.id).status, AssignmentStatus.LOST)
            self.assertFalse(payload.discard); self.assertTrue(kwargs['preserve_files'])
            released.append(workspace_id)
        self.f.service.workspaces = SimpleNamespace(release=release)
        with patch.object(self.f.service.store, '_load_unlocked', side_effect=AssertionError('bulk expiry mutation')):
            lost = self.f.service.recover_expired(actor=self.f.admin, now=claimed.lease.expires_at + 1)
        self.assertEqual(lost, [a.id]); self.assertEqual(released, ['retained-workspace'])
        self.assertEqual(self.f.service.store.assignment(other.id), other)

    def test_expiry_cannot_overwrite_a_lease_renewed_after_candidate_selection(self):
        a = self.f._assignment(); claimed = self.claim(a)
        original = self.f.service.store.transition_assignment; other = self.other_facade(); renewed = []
        def raced(worker, target, updater, **kwargs):
            renewed.append(other.renew(self.f.worker.id, a.id, AssignmentRenewRequest(
                lease_token=claimed.lease.lease_token, fence=claimed.fence, lease_seconds=120), actor=self.f.worker_actor))
            return original(worker, target, updater, **kwargs)
        with patch.object(self.f.service.store, 'transition_assignment', side_effect=raced):
            lost = self.f.service.recover_expired(actor=self.f.admin, now=claimed.lease.expires_at + 1)
        self.assertEqual(lost, [])
        self.assertEqual(self.f.service.store.assignment(a.id), renewed[0])
        self.assertFalse(any(e.event_type == 'assignment_lost' for e in self.f.service.events(self.f.admin)))

    def test_failure_observation_rechecks_retained_claim_without_catalog_mutation(self):
        a = self.f._assignment(); claimed = self.claim(a); before = self.f.service.store.load()
        with patch.object(self.f.service.store, '_load_unlocked', side_effect=AssertionError('diagnostic history lock')):
            self.observation(claimed)
        after = self.f.service.store.load()
        self.assertEqual(after.assignments, before.assignments)
        self.assertEqual(after.events[:-1], before.events)
        event = after.events[-1]
        self.assertEqual(event.event_type, 'agent_runtime_supervision_failed')
        self.assertFalse(event.details['completion_asserted'])
        self.assertEqual(event.details['phase'], 'validation_authority')
        self.assertEqual(event.details['validation_authority_seconds'], .1)

    def test_pruned_original_claim_cannot_authorize_a_late_failure_observation(self):
        a = self.f._assignment(); claimed = self.claim(a)
        original = self.f.service.store.transition_assignment
        def prune(worker, target, updater, **kwargs):
            keys = kwargs['retained_event_ids']; self.assertEqual(len(keys), 1)
            self.f.service.store.store.record_apply(self.f.service.store.event_namespace, upserts={}, deletes=keys)
            return original(worker, target, updater, **kwargs)
        with patch.object(self.f.service.store, 'transition_assignment', side_effect=prune):
            with self.assertRaises(AuthorizationError):
                self.observation(claimed)
        self.assertEqual(self.f.service.store.assignment(a.id), claimed)
        self.assertFalse(any(e.event_type == 'agent_runtime_supervision_failed' for e in self.f.service.events(self.f.admin)))

    def test_transition_cannot_rewrite_retained_claim_evidence(self):
        a = self.f._assignment(); claimed = self.claim(a)
        claim = self.f.service.store.original_claim_event(self.f.worker.id, a.id, claimed.fence)
        def rewrite(state):
            state.events[0].details['fence'] = claimed.fence + 1
            return state
        with self.assertRaisesRegex(ValueError, 'preserve retained evidence'):
            self.f.service.store.transition_assignment(self.f.worker.id, a.id, rewrite,
                retained_event_ids=(claim.id,))
        retained = self.f.service.store.original_claim_event(self.f.worker.id, a.id, claimed.fence)
        self.assertEqual(retained, claim)
        self.assertEqual(self.f.service.store.assignment(a.id), claimed)

    def test_inspection_does_not_lock_and_decode_unrelated_catalogs(self):
        a = self.f._assignment(); self.claim(a)
        before = self.f.service.store.load()
        with patch.object(self.f.service.store, '_load_unlocked', side_effect=AssertionError('inspection held mutation lock')):
            assignments = self.f.service.list_assignments(self.f.admin, worker_id=self.f.worker.id)
            events = self.f.service.events(self.f.admin)
            workers = self.f.service.list_workers(self.f.admin)
            enrollments = self.f.service.list_enrollments(self.f.admin)
            resolved = self.f.service.assignment_for_execution(a.execution_id, actor=self.f.admin)
        self.assertEqual(workers, before.workers)
        self.assertEqual(enrollments, before.enrollments)
        self.assertEqual(resolved.id, a.id)
        self.assertEqual(assignments, before.assignments)
        self.assertEqual(events, sorted(before.events, key=lambda e: (e.occurred_at, e.id), reverse=True))

    def test_slow_retained_snapshot_cannot_starve_current_worker_heartbeat(self):
        self.f._assignment()
        entered = threading.Event(); release = threading.Event()
        original = self.f.service.store._load_unlocked
        def slow_history(**kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError('snapshot did not release')
            return original(**kwargs)
        with patch.object(self.f.service.store, '_load_unlocked', side_effect=slow_history):
            with ThreadPoolExecutor(max_workers=1) as pool:
                reader = pool.submit(self.f.service.store.load)
                try:
                    self.assertTrue(entered.wait(2))
                    token = storage_operation_budget.set(StorageOperationBudget(1))
                    try:
                        current = self.f.service.heartbeat(self.f.worker.id,
                            WorkerHeartbeatRequest(version='renewal-probe'), actor=self.f.worker_actor)
                    finally:
                        storage_operation_budget.reset(token)
                    self.assertEqual(current.version, 'renewal-probe')
                    self.assertFalse(reader.done(), 'heartbeat must run while history is still busy')
                finally:
                    release.set(); reader.result(timeout=5)
