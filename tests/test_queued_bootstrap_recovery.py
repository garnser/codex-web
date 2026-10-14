from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import HTTPException
from codex_web.identity import AuthenticationAssurance

from codex_web.models import QueuedTurn
from codex_web.services.execution_preflight import ExecutionPreflightService
from codex_web.storage.execution_preflight import ExecutionPreflightStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from tests.test_execution_preflight import actor


class QueuedBootstrapRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'state.db'
        self.now = 1800000000.0
        self.service = self.reopen()
        self.admin = actor(admin=True)
        self.queued = QueuedTurn(id='original', thread_id='archived', project_id='project',
            message='Complete original message', source='web', created_at=100,
            execution_id='original-turn', agent_profile_id='james', agent_profile_revision=6,
            agent_profile_actor_id='admin', execution_profile_id='repository-write',
            repository_resource_id='repo', writable_repository_resource_ids=('repo',),
            read_only_repository_resource_ids=('reference',), sandbox='workspace-write',
            approval_policy='on-request')
        attempt = self.service.retain_queued(self.queued, organization_id='org', workspace_id='ws')
        self.attempt = self.service.queued_disposition(attempt.id, status='blocked',
            detail={'code':'upgrade_maintenance', 'threadId':'archived'})
        self.digest = self.service.queued_snapshot_digest(self.attempt.queued_turn_snapshot)

    def reopen(self):
        return ExecutionPreflightService(ExecutionPreflightStore(SQLiteStateStore(self.path)),
            clock=lambda: self.now)

    def reserve(self, service=None, **changes):
        values = dict(actor=self.admin, expected_snapshot_sha256=self.digest,
            reason='Fresh exact-scope bootstrap; historical evidence is incomplete')
        values.update(changes)
        return (service or self.service).reserve_bootstrap_recovery(self.attempt.id, **values)

    def test_reservation_preserves_original_outcome_request_and_attempt(self):
        before = self.attempt.model_dump(mode='json')
        after, reserved = self.reserve()
        self.assertTrue(reserved)
        for key, value in before.items():
            if key != 'updated_at':
                self.assertEqual(after.model_dump(mode='json')[key], value, key)
        self.assertEqual(after.bootstrap_recovery.queued_snapshot_sha256, self.digest)
        self.assertEqual(after.bootstrap_recovery.requested_by, 'admin')
        self.assertEqual(after.bootstrap_recovery.state, 'pending')
        self.assertIsNone(after.bootstrap_recovery.destination_thread_id)
        self.assertNotIn('bootstrap_recovery', before)

    def test_restart_and_expired_retry_cannot_repeat_unresolved_bootstrap(self):
        original, _ = self.reserve()
        self.now += 100000
        restarted = self.reopen()
        repeated, reserved = self.reserve(restarted)
        self.assertFalse(reserved)
        self.assertEqual(repeated.bootstrap_recovery, original.bootstrap_recovery)
        with self.assertRaises(HTTPException) as caught:
            restarted.claim_retry(original.id, actor=self.admin)
        self.assertEqual(caught.exception.detail['code'], 'bootstrap_recovery_pending')
        current, claimed = restarted.store.claim_retry(original.id, claim_id='stale-retry',
            now=self.now, stale_after_seconds=1)
        self.assertFalse(claimed)
        self.assertEqual(current.attempt_number, 1)
        self.assertEqual(current.status, 'blocked')
        self.assertFalse(restarted.public(current, actor=self.admin)['can_retry'])

    def test_concurrent_reservations_share_one_durable_identity(self):
        services = [self.reopen(), self.reopen()]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda service: self.reserve(service), services))
        self.assertEqual(sum(int(reserved) for _, reserved in results), 1)
        self.assertEqual(results[0][0].bootstrap_recovery, results[1][0].bootstrap_recovery)

    def test_denied_actor_or_stale_preview_cannot_reserve(self):
        for changes in ({'actor':actor(admin=False)},
                        {'actor':self.admin.model_copy(update={'assurance':AuthenticationAssurance.PRIMARY})},
                        {'actor':self.admin.model_copy(update={'organization_id':'another-tenant'})},
                        {'expected_snapshot_sha256':'f' * 64},
                        {'reason':'   '}):
            with self.subTest(changes=changes), self.assertRaises(HTTPException):
                self.reserve(**changes)
            self.assertIsNone(self.service.store.get(self.attempt.id).bootstrap_recovery)

    def test_unknown_start_cannot_be_reclassified_by_stale_refusal(self):
        self.service.store.update(self.attempt.id,
            lambda value: value.model_copy(update={'status':'reconciliation_required'}))
        with self.assertRaises(HTTPException):
            self.reserve()
        self.assertIsNone(self.service.store.get(self.attempt.id).bootstrap_recovery)

    def test_changed_original_scope_refuses_old_preview(self):
        self.service.store.update(self.attempt.id, lambda value: value.model_copy(update={
            'queued_turn_snapshot':dict(value.queued_turn_snapshot, agent_profile_revision=7)}))
        with self.assertRaises(HTTPException) as caught:
            self.reserve()
        self.assertEqual(caught.exception.detail['code'], 'bootstrap_recovery_snapshot_changed')
        self.assertIsNone(self.service.store.get(self.attempt.id).bootstrap_recovery)


if __name__ == '__main__':
    unittest.main()
