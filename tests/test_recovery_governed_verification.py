"""Real Recovery composition retains Evidence/governance and final-write guards."""
import os
import unittest
from unittest.mock import patch

from tests import test_recovery as fixtures
from tests import test_recovery_final_settlement as settlement
from tests import test_recovery_manual_api as manual
from tests import test_recovery_operator_api as operator_api
from tests.test_recovery_scheduled_postgres import _PostgresFixture
from codex_web.services.data_governance import DataGovernanceService
from codex_web.storage.data_governance import DataGovernanceStore
from codex_web.storage.artifact_evidence import ArtifactEvidenceStore
from codex_web.storage.recovery import RecoveryStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.services.recovery import RecoveryConflictError
from codex_web.storage.state_store import StorageOperationBudget, storage_operation_budget


def govern(service):
    governance = DataGovernanceService(DataGovernanceStore(service.store.store))
    service.evidence.governance = governance
    return governance


class GovernedManualVerificationTests(unittest.IsolatedAsyncioTestCase):
    client = manual.ManualRecoveryApiTests.client
    url = operator_api.RecoveryOperatorApiTests.url
    body = operator_api.RecoveryOperatorApiTests.body
    asyncTearDown = manual.ManualRecoveryApiTests.asyncTearDown

    async def asyncSetUp(self):
        await manual.ManualRecoveryApiTests.asyncSetUp(self)
        self.governance = govern(self.recovery)

    async def test_actual_http_verification_commits_all_three_links(self):
        async with self.client() as client:
            backup = await client.post('/api/recovery/backups')
            self.assertEqual(backup.status_code, 200, backup.text)
            response = await client.post('/api/recovery/backups/' + backup.json()['item']['id'] + '/verify')
            self.assertEqual(response.status_code, 200, response.text)
            verification = response.json()['item']
        self.assertEqual(verification['status'], 'pass')
        reader = SQLiteStateStore(self.recovery.store.store.path)
        persisted = RecoveryStore(reader).load().verifications[verification['id']]
        evidence = next(e for e in ArtifactEvidenceStore(reader).load().evidence if e.id == persisted.evidence_id)
        record = next(r for r in DataGovernanceStore(reader).load().records if r.id == evidence.governance_record_id)
        self.assertEqual(evidence.metadata['verification_id'], persisted.id)
        self.assertEqual(record.object_id, evidence.id)
        self.assertEqual((record.organization_id, record.workspace_id), (self.actor.organization_id, self.actor.workspace_id))
        self.assertEqual(self.recovery._scheduled_executor.status()['waiting_or_running'], 0)

    async def test_rotation_before_final_commit_refuses_without_any_partial_record(self):
        async with self.client() as client:
            backup = await client.post('/api/recovery/backups')
            self.assertEqual(backup.status_code, 200, backup.text)
            before = (self.recovery.store.load(), self.recovery.evidence.store.load(), self.governance.store.load())
            original = self.recovery.evidence.create_evidence
            def rotate(payload, **kwargs):
                self.identity.refresh_session(self.credentials.session_id, self.credentials.refresh_token)
                return original(payload, **kwargs)
            with patch.object(self.recovery.evidence, 'create_evidence', side_effect=rotate):
                response = await client.post('/api/recovery/backups/' + backup.json()['item']['id'] + '/verify')
            self.assertEqual(response.status_code, 401, response.text)
            self.assertEqual((self.recovery.store.load(), self.recovery.evidence.store.load(), self.governance.store.load()), before)
            self.assertEqual(self.recovery._scheduled_executor.status()['waiting_or_running'], 0)

    async def test_governed_reconciliation_keeps_plain_locked_evidence_callback(self):
        await operator_api.RecoveryOperatorApiTests.test_real_operator_route_commits_once_preserving_unknown_and_current_preview(self)


class GovernedScheduledVerificationTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.ScheduledRecoveryTests.setUp
    tearDown = fixtures.ScheduledRecoveryTests.tearDown
    asyncTearDown = fixtures.ScheduledRecoveryTests.asyncTearDown
    verify = settlement.ScheduledFinalSettlementTests.verify

    async def asyncSetUp(self):
        self.governance = govern(self.service)
        await settlement.ScheduledFinalSettlementTests.asyncSetUp(self)

    def reader(self):
        return SQLiteStateStore(self.state.path)

    def snapshot(self, reader=None):
        reader = reader or self.state
        return (RecoveryStore(reader).load(), ArtifactEvidenceStore(reader).load(), DataGovernanceStore(reader).load())

    def assert_link(self, verification):
        recovery, artifacts, governance = self.snapshot(self.reader())
        self.assertEqual(recovery.verifications[verification.id], verification)
        self.assertEqual(recovery.scheduled_firings[self.event.event_id].status, 'succeeded')
        evidence = next(e for e in artifacts.evidence if e.id == verification.evidence_id)
        self.assertEqual(evidence.metadata['verification_id'], verification.id)
        self.assertEqual(evidence.metadata['canonical_event_id'], self.event.event_id)
        record = next(r for r in governance.records if r.id == evidence.governance_record_id)
        self.assertEqual(record.object_id, evidence.id)
        self.assertEqual(len([e for e in governance.events if e.record_id == record.id]), 1)

    async def test_success_commits_governance_evidence_verification_and_terminal_firing(self):
        verification = self.verify()
        self.assert_link(verification)
        self.admission.finish(self.firing, succeeded=True)
        self.assertIsNone(self.admission.admit(self.event))

    async def test_late_sql_failure_rolls_back_all_domains_and_firing(self):
        before = self.snapshot()
        original = self.state._upsert
        writes = []
        def fail(connection, namespace, payload):
            writes.append(namespace)
            result = original(connection, namespace, payload)
            if namespace == self.governance.store.namespace:
                raise TimeoutError('synthetic failure after actual governed SQL write')
            return result
        with patch.object(self.state, '_upsert', side_effect=fail):
            with self.assertRaises(TimeoutError):
                self.verify()
        self.assertIn(self.evidence.store.namespace, writes)
        self.assertIn(self.service.store.namespace, writes)
        self.assertIn(self.governance.store.namespace, writes)
        self.assertEqual(self.snapshot(self.reader()), before)

    async def test_cancellation_after_governance_sql_rolls_back_every_domain(self):
        before = self.snapshot()
        original = self.state._upsert
        budget = StorageOperationBudget(10)
        def cancel(connection, namespace, payload):
            result = original(connection, namespace, payload)
            if namespace == self.governance.store.namespace:
                budget.cancelled.set()
            return result
        token = storage_operation_budget.set(budget)
        try:
            with patch.object(self.state, '_upsert', side_effect=cancel):
                with self.assertRaises(TimeoutError):
                    self.verify()
        finally:
            storage_operation_budget.reset(token)
        self.assertTrue(budget.cancelled.is_set())
        self.assertEqual(self.snapshot(self.reader()), before)

    async def test_paused_schedule_before_commit_refuses_without_orphans(self):
        original = self.evidence.create_evidence
        def paused(payload, **kwargs):
            self.scheduler.pause(self.firing.schedule_id, actor_id=self.actor.identity_id)
            return original(payload, **kwargs)
        before = self.snapshot()
        with patch.object(self.evidence, 'create_evidence', side_effect=paused):
            with self.assertRaises(RecoveryConflictError):
                self.verify()
        self.assertEqual(self.snapshot(self.reader()), before)
        self.admission.finish(self.firing, succeeded=False, error_code='RecoveryConflictError')
        self.assertEqual(self.service.store.load().scheduled_firings[self.event.event_id].status, 'unknown')
        self.assertIsNone(self.admission.admit(self.event))

    async def test_required_key_cannot_be_revoked_before_governed_settlement(self):
        from codex_web.services.crypto_keys import CryptoKeyConflictError
        original = self.evidence.create_evidence
        attempts = []
        def revoke(payload, **kwargs):
            with self.assertRaises(CryptoKeyConflictError):
                self.crypto.revoke_key(self.key.id, 'synthetic concurrent revocation', actor=self.actor)
            attempts.append(self.key.id)
            return original(payload, **kwargs)
        with patch.object(self.evidence, 'create_evidence', side_effect=revoke):
            self.assert_link(self.verify())
        self.assertEqual(attempts, [self.key.id])


@unittest.skipUnless(os.environ.get('CODEX_WEB_TEST_POSTGRES_DSN'), 'isolated PostgreSQL test DSN required')
class PostgresGovernedScheduledVerificationTests(_PostgresFixture, GovernedScheduledVerificationTests):
    def reader(self):
        from psycopg_pool import ConnectionPool
        from codex_web.storage.postgres_state import PostgresStateStore
        dsn = os.environ['CODEX_WEB_TEST_POSTGRES_DSN']
        pool = ConnectionPool(dsn, min_size=1, max_size=1, timeout=3,
                              kwargs={'options': f'-c search_path={self._test_schema}'})
        self.addCleanup(pool.close)
        return PostgresStateStore(dsn, connection_pool=pool)
