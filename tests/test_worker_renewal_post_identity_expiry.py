"""Canonical renewal must not revive a lease that expired during live identity read."""
import unittest
import time
from types import SimpleNamespace
from unittest.mock import patch

from codex_web.execution_workers import AssignmentClaimRequest, AssignmentRenewRequest, AssignmentStartRequest
from codex_web.services import execution_workers as worker_module
from codex_web.services.execution_workers import WorkerLeaseError
from tests.test_execution_workers import ExecutionWorkerServiceTests


class WorkerRenewalPostIdentityExpiryTests(ExecutionWorkerServiceTests):
    def running_assignment(self, **options):
        assignment = self._assignment(**options)
        claimed = self.service.claim(self.worker.id, AssignmentClaimRequest(lease_seconds=30),
            actor=self.worker_actor, assignment_id=assignment.id)
        self.assertIsNotNone(claimed)
        return self.service.start(self.worker.id, assignment.id,
            AssignmentStartRequest(lease_token=claimed.lease.lease_token, fence=claimed.fence),
            actor=self.worker_actor)

    def test_live_identity_read_crossing_lease_expiry_cannot_renew(self):
        assignment = self._assignment()
        claimed = self.service.claim(self.worker.id, AssignmentClaimRequest(lease_seconds=30),
            actor=self.worker_actor, assignment_id=assignment.id)
        self.assertIsNotNone(claimed)
        running = self.service.start(self.worker.id, assignment.id,
            AssignmentStartRequest(lease_token=claimed.lease.lease_token, fence=claimed.fence),
            actor=self.worker_actor)
        before = self.service.store.assignment(running.id)
        self.assertEqual(before.status.value, 'running')
        now = [before.lease.expires_at - 1]
        self.assertLess(now[0], before.lease.expires_at)
        self.assertTrue(before.deadline_at is None or before.lease.expires_at + 1 < before.deadline_at,
            'The controlled boundary crosses lease expiry, not an assignment deadline')
        reads = []
        actual_identity_read = self.identity.actor_for_identity

        def read_then_cross_expiry(*args, **kwargs):
            # Keep the real live principal/membership and actual writer path.
            # Only deterministic passage of time at this I/O boundary is controlled.
            actor = actual_identity_read(*args, **kwargs)
            reads.append(actor)
            now[0] = before.lease.expires_at + 1
            return actor

        rejected = None
        renewed = None
        with patch.object(worker_module, 'time', SimpleNamespace(time=lambda: now[0])), \
                patch.object(self.identity, 'actor_for_identity', read_then_cross_expiry):
            try:
                renewed = self.service.renew(self.worker.id, running.id,
                    AssignmentRenewRequest(lease_token=before.lease.lease_token,
                        fence=before.fence, lease_seconds=120), actor=self.worker_actor)
            except WorkerLeaseError as exc:
                rejected = exc
        self.assertEqual(len(reads), 1, 'Expected the actual current worker identity read')
        self.assertEqual(reads[0].identity_id, self.worker_actor.identity_id)
        self.assertGreater(now[0], before.lease.expires_at)
        self.assertIsNotNone(rejected,
            'Real renewal revived an expired original after live identity I/O: '
            + str({'original_expiry': before.lease.expires_at, 'current_time': now[0],
                'renewed_expiry': renewed.lease.expires_at if renewed else None}))
        self.assertEqual(self.service.store.assignment(running.id), before,
            'Refusal must preserve the original token/fence/lease and assignment bytes')

    def test_live_identity_read_crossing_assignment_deadline_cannot_renew(self):
        before = self.running_assignment(deadline_at=time.time() + 15)
        now = [before.deadline_at - 1]
        after_read = before.deadline_at + 1
        self.assertLess(after_read, before.lease.expires_at)
        reads = []
        actual = self.identity.actor_for_identity
        def read_then_cross_deadline(*args, **kwargs):
            result = actual(*args, **kwargs)
            reads.append(result.identity_id)
            now[0] = after_read
            return result
        with patch.object(worker_module, 'time', SimpleNamespace(time=lambda: now[0])), \
                patch.object(self.identity, 'actor_for_identity', read_then_cross_deadline):
            with self.assertRaisesRegex(WorkerLeaseError, 'deadline'):
                self.service.renew(self.worker.id, before.id,
                    AssignmentRenewRequest(lease_token=before.lease.lease_token,
                        fence=before.fence, lease_seconds=120), actor=self.worker_actor)
        self.assertEqual(reads, [self.worker_actor.identity_id])
        self.assertEqual(self.service.store.assignment(before.id), before)

    def test_still_live_renewal_uses_post_identity_timestamp_and_original_duration(self):
        before = self.running_assignment()
        now = [before.lease.expires_at - 10]
        after_read = before.lease.expires_at - 5
        reads = []
        actual = self.identity.actor_for_identity
        def read_then_advance(*args, **kwargs):
            result = actual(*args, **kwargs)
            reads.append(result.identity_id)
            now[0] = after_read
            return result
        with patch.object(worker_module, 'time', SimpleNamespace(time=lambda: now[0])), \
                patch.object(self.identity, 'actor_for_identity', read_then_advance):
            renewed = self.service.renew(self.worker.id, before.id,
                AssignmentRenewRequest(lease_token=before.lease.lease_token,
                    fence=before.fence, lease_seconds=120), actor=self.worker_actor)
        self.assertEqual(reads, [self.worker_actor.identity_id])
        self.assertEqual(renewed.lease.renewed_at, after_read)
        self.assertEqual(renewed.updated_at, after_read)
        self.assertEqual(renewed.lease.expires_at, after_read + 120)
        self.assertEqual(renewed.lease.lease_token, before.lease.lease_token)
        self.assertEqual(renewed.lease.acquired_at, before.lease.acquired_at)
        self.assertEqual(renewed.fence, before.fence)

    def test_start_also_refuses_lease_expiry_during_live_identity_read(self):
        assignment = self._assignment()
        before = self.service.claim(self.worker.id, AssignmentClaimRequest(lease_seconds=30),
            actor=self.worker_actor, assignment_id=assignment.id)
        now = [before.lease.expires_at - 1]
        actual = self.identity.actor_for_identity
        def read_then_cross(*args, **kwargs):
            result = actual(*args, **kwargs)
            now[0] = before.lease.expires_at + 1
            return result
        with patch.object(worker_module, 'time', SimpleNamespace(time=lambda: now[0])), \
                patch.object(self.identity, 'actor_for_identity', read_then_cross):
            with self.assertRaises(WorkerLeaseError):
                self.service.start(self.worker.id, before.id,
                    AssignmentStartRequest(lease_token=before.lease.lease_token, fence=before.fence),
                    actor=self.worker_actor)
        self.assertEqual(self.service.store.assignment(before.id), before)


def load_tests(loader, standard_tests, pattern):
    return unittest.TestSuite(WorkerRenewalPostIdentityExpiryTests(name)
        for name, method in WorkerRenewalPostIdentityExpiryTests.__dict__.items()
        if name.startswith('test_') and callable(method))
