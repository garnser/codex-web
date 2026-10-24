"""Actual domain/governance writes share failure, cancellation and idempotency."""
import concurrent.futures
import os
import hashlib
import threading
import unittest
from unittest.mock import patch

from tests import test_artifact_evidence as fixtures
from codex_web.artifact_evidence import ArtifactCreate, ArtifactType, EvidenceCreate, EvidenceType, EvidenceResult
from codex_web.data_governance import GovernedDataCreate, DataCategory, GovernanceActionRequest, GovernanceAction
from codex_web.services.artifact_evidence import ArtifactEvidenceConflictError
from codex_web.storage.state_store import StorageOperationBudget, storage_operation_budget


class ArtifactGovernanceAtomicityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ArtifactEvidenceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.service, self.actor = self.fixture.service, self.fixture.producer
        self.backend = self.service.store.store

    def snapshot(self):
        return (self.service.store.load(), self.fixture.governance.store.load())

    def create(self, kind, **kwargs):
        if kind == 'artifact':
            return self.service.create_artifact(ArtifactCreate(artifact_type=ArtifactType.REPORT,
                name='Atomic fixture'), actor=self.actor, **kwargs)
        return self.service.create_evidence(EvidenceCreate(evidence_type=EvidenceType.TEST_RESULT,
            result=EvidenceResult.INFO, source='atomic-fixture', summary='Atomic fixture'),
            actor=self.actor, **kwargs)

    def test_private_catalog_guard_reuses_before_governance_decode_without_row_changes(self):
        payload = ArtifactCreate(artifact_type=ArtifactType.REPORT, name='Prior', source='atomic-fixture')
        first = self.service.create_artifact(payload, actor=self.actor, idempotency_key='guard')
        self.backend.put('data_governance', {'schema_version':'99.0','records':[]})
        before = hashlib.sha256(b''.join(self.backend.iter_storage_snapshot())).hexdigest()
        def guard(state):
            return self.service.get_artifact(first.id, actor=self.actor, state=state)
        with patch.object(self.fixture.governance.store, '_decode', side_effect=AssertionError('no Gov decode')), \
                patch('codex_web.artifact_evidence.ArtifactEvidenceState.model_dump', side_effect=AssertionError('no export')):
            result = self.service.create_artifact(payload, actor=self.actor, idempotency_key='guard', _catalog_guard=guard)
        self.assertEqual(result.id, first.id)
        self.assertEqual(hashlib.sha256(b''.join(self.backend.iter_storage_snapshot())).hexdigest(), before)

    def test_private_catalog_guard_refusal_precedes_governance_and_exports(self):
        before = hashlib.sha256(b''.join(self.backend.iter_storage_snapshot())).hexdigest()
        def guard(state):
            raise ArtifactEvidenceConflictError('current checkpoint conflict')
        with patch.object(self.fixture.governance.store, '_decode', side_effect=AssertionError('no Gov decode')), \
                patch('codex_web.artifact_evidence.ArtifactEvidenceState.model_dump', side_effect=AssertionError('no export')):
            with self.assertRaisesRegex(ArtifactEvidenceConflictError, 'current checkpoint conflict'):
                self.service.create_artifact(ArtifactCreate(artifact_type=ArtifactType.REPORT, name='Refused'),
                    actor=self.actor, _catalog_guard=guard)
        self.assertEqual(hashlib.sha256(b''.join(self.backend.iter_storage_snapshot())).hexdigest(), before)

    def test_private_guard_wraps_only_full_artifact_domain_decode_before_writer(self):
        from codex_web.services.artifact_evidence import _ArtifactDecodeBeforeCreation
        self.backend.put('artifact_evidence', {'schema_version':'1.3','artifacts':[{'id':'bad-unrelated'}]})
        before = hashlib.sha256(b''.join(self.backend.iter_storage_snapshot())).hexdigest()
        guard = unittest.mock.Mock()
        with patch.object(self.fixture.governance.store, '_decode', side_effect=AssertionError('no Gov decode')):
            with self.assertRaises(_ArtifactDecodeBeforeCreation) as caught:
                self.service.create_artifact(ArtifactCreate(artifact_type=ArtifactType.REPORT, name='Refused'),
                    actor=self.actor, _catalog_guard=guard)
        self.assertIsInstance(caught.exception.original, ValueError)
        self.assertIs(caught.exception.__cause__, caught.exception.original)
        guard.assert_not_called()
        self.assertEqual(hashlib.sha256(b''.join(self.backend.iter_storage_snapshot())).hexdigest(), before)

    def independent_reader(self):
        from codex_web.storage.sqlite_state import SQLiteStateStore
        return SQLiteStateStore(self.backend.path)

    def test_success_and_independent_readback_link_both_objects(self):
        for kind in ('artifact', 'evidence'):
            with self.subTest(kind=kind):
                item = self.create(kind)
                reader = self.independent_reader()
                self.assertEqual(reader.get('artifact_evidence'), self.backend.get('artifact_evidence'))
                self.assertEqual(reader.get('data_governance'), self.backend.get('data_governance'))
                state = self.snapshot()
                record = next(r for r in state[1].records if r.id == item.governance_record_id)
                self.assertEqual((record.object_type, record.object_id), (kind, item.id))
                self.assertEqual((record.organization_id, record.workspace_id),
                    (item.organization_id, item.workspace_id))
                self.assertEqual(len([e for e in state[1].events if e.record_id == record.id]), 1)

    def test_artifact_creation_decodes_only_atomic_catalog_without_supersession(self):
        payload = ArtifactCreate(artifact_type=ArtifactType.REPORT,
            name='One canonical catalog read', source='atomic-fixture')
        with patch.object(self.service.store, 'load', wraps=self.service.store.load) as load, \
                patch.object(self.service.store, '_decode', wraps=self.service.store._decode) as decode:
            first = self.service.create_artifact(payload, actor=self.actor, idempotency_key='single-read')
            repeated = self.service.create_artifact(payload, actor=self.actor, idempotency_key='single-read')
        self.assertEqual(load.call_count, 0)
        self.assertEqual(decode.call_count, 2)  # Full current catalog once per atomic creation.
        self.assertEqual(first, repeated)
        state, governance = self.snapshot()
        self.assertEqual([item.id for item in state.artifacts], [first.id])
        self.assertEqual([record.object_id for record in governance.records], [first.id])
        self.assertEqual(len(governance.events), 1)

    def test_supersession_keeps_prevalidation_and_current_atomic_catalog(self):
        source = self.create('artifact')
        with patch.object(self.service.store, 'load', wraps=self.service.store.load) as load, \
                patch.object(self.service.store, '_decode', wraps=self.service.store._decode) as decode:
            replacement = self.service.create_artifact(ArtifactCreate(artifact_type=ArtifactType.REPORT,
                name='Replacement', supersedes_artifact_id=source.id), actor=self.actor)
        self.assertEqual(load.call_count, 1)
        self.assertEqual(decode.call_count, 2)
        state, governance = self.snapshot()
        original = next(item for item in state.artifacts if item.id == source.id)
        self.assertEqual((original.lifecycle, original.superseded_by_id), ('superseded', replacement.id))
        record = next(item for item in governance.records if item.object_id == replacement.id)
        self.assertIn(source.governance_record_id, record.source_record_ids)

    def test_unrelated_malformed_catalog_refuses_atomic_creation_without_writes(self):
        original = {name: self.backend.get(name) for name in ('artifact_evidence', 'data_governance')}
        for namespace, collection in (('artifact_evidence', 'evidence'), ('data_governance', 'records')):
            with self.subTest(namespace=namespace):
                import copy
                malformed = copy.deepcopy(original[namespace])
                if malformed is None:
                    malformed = (self.service.store if namespace == 'artifact_evidence'
                        else self.fixture.governance.store).load().model_dump(mode='json')
                malformed[collection].append({'id': 'unrelated-malformed-record'})
                self.backend.put(namespace, malformed)
                before = {name: self.backend.get(name) for name in original}
                try:
                    with self.assertRaises(ValueError):
                        self.create('artifact')
                    self.assertEqual({name: self.backend.get(name) for name in original}, before)
                finally:
                    self.backend.put(namespace, original[namespace])

    def test_failed_second_actual_write_rolls_back_domain_and_governance(self):
        original = self.backend._upsert
        for kind in ('artifact', 'evidence'):
            with self.subTest(kind=kind):
                before = self.snapshot()
                writes = []
                def fail_governance(connection, namespace, payload):
                    writes.append(namespace)
                    if namespace == self.fixture.governance.store.namespace:
                        raise TimeoutError('isolated second write refused')
                    return original(connection, namespace, payload)
                with patch.object(self.backend, '_upsert', side_effect=fail_governance):
                    with self.assertRaises(TimeoutError): self.create(kind)
                self.assertEqual(writes, ['artifact_evidence', 'data_governance'])
                self.assertEqual(self.snapshot(), before)

    def test_cancel_after_first_actual_write_rolls_back_both(self):
        original = self.backend._upsert
        for kind in ('artifact', 'evidence'):
            with self.subTest(kind=kind):
                before = self.snapshot()
                budget = StorageOperationBudget(10)
                def cancel_after_first(connection, namespace, payload):
                    result = original(connection, namespace, payload)
                    budget.cancelled.set()
                    return result
                token = storage_operation_budget.set(budget)
                try:
                    with patch.object(self.backend, '_upsert', side_effect=cancel_after_first):
                        with self.assertRaises(TimeoutError): self.create(kind)
                finally: storage_operation_budget.reset(token)
                self.assertEqual(self.snapshot(), before)

    def test_composed_guard_failure_after_registration_rolls_back_every_domain(self):
        self.backend.put('atomic_guard', {'value': 'retained'})
        for kind in ('artifact', 'evidence'):
            with self.subTest(kind=kind):
                before = self.snapshot()
                def commit(creation):
                    def apply(rows):
                        changes = creation.apply(rows)
                        self.assertTrue(changes['data_governance'][None]['records'])
                        raise TimeoutError('actual late composition guard refusal')
                    self.backend.record_mutate_many({**creation.domains, 'atomic_guard': (None,)}, apply)
                with self.assertRaises(TimeoutError): self.create(kind, _commit=commit)
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.backend.get('atomic_guard'), {'value': 'retained'})

    def test_composed_success_commits_guard_and_both_domain_links(self):
        def commit(creation):
            def apply(rows):
                changes = creation.apply(rows)
                changes['atomic_guard'] = {None: {'committed': True}}
                return changes
            self.backend.record_mutate_many({**creation.domains, 'atomic_guard': (None,)}, apply)
        item = self.create('artifact', _commit=commit)
        self.assertEqual(self.backend.get('atomic_guard'), {'committed': True})
        self.assertEqual(next(r for r in self.snapshot()[1].records
            if r.id == item.governance_record_id).object_id, item.id)

    def test_legacy_incomplete_composed_callback_refuses_without_partial_registration(self):
        before = self.snapshot()
        with self.assertRaises(ArtifactEvidenceConflictError):
            self.create('artifact', _commit=lambda updater: self.service.store.update(updater))
        self.assertEqual(self.snapshot(), before)

    def test_unshared_governance_backend_refuses_without_writing(self):
        from pathlib import Path
        from codex_web.storage.sqlite_state import SQLiteStateStore
        from codex_web.storage.data_governance import DataGovernanceStore
        from codex_web.services.data_governance import DataGovernanceService
        other = DataGovernanceService(DataGovernanceStore(SQLiteStateStore(Path(self.fixture.temp.name)/'other.db')))
        before = self.snapshot()
        with patch.object(self.service, 'governance', other):
            with self.assertRaises(ArtifactEvidenceConflictError): self.create('artifact')
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(other.store.load().records)

    def test_concurrent_idempotent_evidence_creates_one_governance_record(self):
        before = self.snapshot()
        barrier = threading.Barrier(2)
        original = self.service._commit_creation
        def wait_then_commit(updater, composed):
            barrier.wait(timeout=5)
            return original(updater, composed)
        with patch.object(self.service, '_commit_creation', side_effect=wait_then_commit):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(self.create, 'evidence', idempotency_key='same-original') for _ in range(2)]
                items = [f.result(timeout=10) for f in futures]
        self.assertEqual(items[0], items[1])
        current = self.snapshot()
        self.assertEqual(len(current[0].evidence)-len(before[0].evidence), 1)
        self.assertEqual(len(current[1].records)-len(before[1].records), 1)
        self.assertEqual(len(current[1].events)-len(before[1].events), 1)

    def test_late_source_invalidation_cannot_commit_stale_proof_or_supersession(self):
        original = self.service._commit_creation
        for kind in ('artifact', 'evidence'):
            with self.subTest(kind=kind):
                source = self.create('artifact')
                retained = []
                def invalidate_then_commit(updater, composed):
                    self.service.invalidate_artifact(source.id, 'concurrent source invalidation', actor=self.actor)
                    retained.append(self.snapshot())
                    return original(updater, composed)
                with patch.object(self.service, '_commit_creation', side_effect=invalidate_then_commit):
                    with self.assertRaises(ArtifactEvidenceConflictError):
                        if kind == 'artifact':
                            self.service.create_artifact(ArtifactCreate(artifact_type=ArtifactType.REPORT,
                                name='Superseding stale source', supersedes_artifact_id=source.id), actor=self.actor)
                        else:
                            self.service.create_evidence(EvidenceCreate(evidence_type=EvidenceType.TEST_RESULT,
                                result=EvidenceResult.INFO, summary='Stale proof', artifact_ids=(source.id,)), actor=self.actor)
                self.assertEqual(self.snapshot(), retained[0])

    def test_existing_canonical_delete_reconciles_absent_object_and_retains_original_audit(self):
        governance = self.fixture.governance
        record = governance.register_domain_record(GovernedDataCreate(object_type='artifact',
            object_id='artifact-absent-fixture', category=DataCategory.ARTIFACT,
            reason='isolated partial-creation fixture'), actor=self.actor)
        before = self.snapshot()
        request = governance.request_action(GovernanceActionRequest(record_id=record.id,
            action=GovernanceAction.DELETE, reason='Reconcile verified absent local payload; preserve audit'), actor=self.actor)
        result = governance.execute_request(request.id, actor=self.actor)
        self.assertEqual(result.status, 'completed')
        self.assertTrue(result.adapter_receipt_ref.endswith(':already_absent'))
        current = self.snapshot()
        self.assertEqual(current[0], before[0])
        self.assertEqual(current[1].events[:len(before[1].events)], before[1].events)
        self.assertEqual(next(r for r in current[1].records if r.id == record.id).lifecycle, 'deleted')


from tests.test_recovery_scheduled_postgres import _PostgresFixture


@unittest.skipUnless(os.environ.get('CODEX_WEB_TEST_POSTGRES_DSN'), 'isolated PostgreSQL DSN required')
class PostgresArtifactGovernanceAtomicityTests(_PostgresFixture, ArtifactGovernanceAtomicityTests):
    def setUp(self):
        with patch.object(fixtures, 'SQLiteStateStore', self.state_store_factory):
            super().setUp()

    def independent_reader(self):
        from psycopg_pool import ConnectionPool
        from codex_web.storage.postgres_state import PostgresStateStore
        dsn = os.environ['CODEX_WEB_TEST_POSTGRES_DSN']
        pool = ConnectionPool(dsn, min_size=1, max_size=1, timeout=3,
            kwargs={'options': '-c search_path=' + self._test_schema})
        self.addCleanup(pool.close)
        store = PostgresStateStore(dsn, connection_pool=pool)
        self.addCleanup(store.close)
        return store


from tests import test_recovery_storage_operator as preservation
from tests import test_action_intents as intent_fixtures


class GovernedPreservationCreationTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = preservation.StorageOperatorTests.asyncTearDown
    publish = preservation.StorageOperatorTests.publish
    insert_opaque = preservation.StorageOperatorTests.insert_opaque
    client = preservation.StorageOperatorTests.client
    body = preservation.StorageOperatorTests.body
    committed = preservation.StorageOperatorTests.committed
    capture = preservation.StorageOperatorTests.capture
    assert_original = preservation.StorageOperatorTests.assert_original

    async def asyncSetUp(self):
        from codex_web.services.data_governance import DataGovernanceService
        from codex_web.storage.data_governance import DataGovernanceStore
        await preservation.StorageOperatorTests.asyncSetUp(self)
        self.governance = DataGovernanceService(DataGovernanceStore(self.sqlite))
        self.artifacts.governance = self.governance

    def assert_current_links(self):
        state = self.artifacts.store.load()
        ids = {i.id for i in state.artifacts} | {i.id for i in state.evidence}
        records = self.governance.store.load().records
        self.assertTrue(records)
        self.assertTrue(all(row.object_id in ids for row in records))

    async def test_actual_capture_and_verify_compose_governance_with_admission(self):
        await preservation.StorageOperatorTests.test_capture_verify_same_backend_once_preserves_invalid_rows_without_health_pass(self)
        self.assert_current_links()

    async def test_actual_settlement_refusal_preserves_unknown_without_orphan_governance(self):
        await preservation.StorageOperatorTests.test_policy_change_after_ciphertext_before_settlement_keeps_unknown_custody(self)
        self.assert_current_links()


@unittest.skipUnless(os.environ.get('CODEX_WEB_TEST_POSTGRES_DSN'), 'isolated PostgreSQL DSN required')
class PostgresGovernedPreservationCreationTests(_PostgresFixture, GovernedPreservationCreationTests):
    async def asyncSetUp(self):
        with patch.object(intent_fixtures, 'SQLiteStateStore', self.state_store_factory):
            await super().asyncSetUp()
        self.addCleanup(self.sqlite.close)
