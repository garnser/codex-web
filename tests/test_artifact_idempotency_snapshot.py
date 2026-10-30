"""A bounded idempotency snapshot retains canonical point-selection semantics."""
import os
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_web.artifact_evidence import Artifact
from codex_web.identity import AuthenticationActor, AuthenticationAssurance, MembershipRole, PrincipalKind
from codex_web.services.artifact_evidence import ArtifactEvidenceService
from codex_web.storage.artifact_evidence import ArtifactEvidenceStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from tests import test_autonomy_append_scaling as pg_fixtures
from tests import test_home_unowned_upgrade as home_fixtures


class ArtifactSnapshotContract:
    requests = (('home-upgrade-checkpoint', 'plan'), ('home-upgrade-dormant-preservation', 'plan'))

    def configure(self):
        self.actor = AuthenticationActor(identity_id='operator', principal_kind=PrincipalKind.HUMAN,
            organization_id='local', workspace_id='default', roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.MFA)
        self.service = ArtifactEvidenceService(ArtifactEvidenceStore(self.backend))
        self.ids = tuple(self.service._idempotent_evidence_id(self.actor, *request).replace(
            'evidence-', 'artifact-', 1) for request in self.requests)

    def artifact(self, index=0, **changes):
        return Artifact(id=self.ids[index], organization_id='local', workspace_id='default',
            artifact_type='report', name='First stored', producer_identity_id=self.actor.identity_id,
            source=self.requests[index][0]).model_copy(update=changes)

    def put(self, artifacts, **envelope):
        self.backend.put('artifact_evidence', {'schema_version': '1.3',
            'artifacts': artifacts, 'evidence': [], 'verifications': [], **envelope})

    def find(self, actor=None):
        return self.service.find_idempotent_artifacts(actor=actor or self.actor, requests=self.requests)

    def test_one_read_preserves_active_inactive_missing_and_order(self):
        first = self.artifact()
        second = self.artifact(1, lifecycle='invalidated')
        self.put([second.model_dump(mode='json'), first.model_dump(mode='json')])
        with patch.object(self.backend, 'document_array_items_envelope',
                wraps=self.backend.document_array_items_envelope) as batch, \
                patch.object(self.backend, 'document_array_item_envelope',
                    side_effect=AssertionError('point lookup duplicates the snapshot read')):
            self.assertEqual(self.find(), (first, second))
        self.assertEqual(batch.call_count, 1)
        self.put([])
        self.assertEqual(self.find(), (None, None))

    def test_first_scoped_duplicate_matches_point_even_when_later_duplicate_invalid(self):
        first = self.artifact()
        foreign = first.model_copy(update={'organization_id': 'foreign'})
        later = {**first.model_dump(mode='json'), 'artifact_type': 'unsupported'}
        self.put([foreign.model_dump(mode='json'), first.model_dump(mode='json'), later])
        point = self.service.find_idempotent_artifact(actor=self.actor,
            source=self.requests[0][0], idempotency_key='plan')
        self.assertEqual(self.find(), (point, None))
        # Invalid FIRST scoped member still refuses, even if a valid later one exists.
        self.put([later, first.model_dump(mode='json')])
        with self.assertRaises(ValueError): self.find()

    def test_foreign_scope_and_other_actor_do_not_supply_idempotency(self):
        second = self.artifact(1)
        self.put([self.artifact().model_copy(update={'workspace_id': 'foreign'}).model_dump(mode='json'),
                  second.model_dump(mode='json')])
        self.assertEqual(self.find(), (None, second))
        other = self.actor.model_copy(update={'identity_id': 'other-operator'})
        self.assertEqual(self.find(actor=other), (None, None))
        self.assertEqual(self.find(actor=self.actor.model_copy(update={'organization_id': 'foreign'})),
                         (None, None))

    def test_legacy_migration_and_full_fallback_retain_domain_validation(self):
        first = self.artifact()
        self.put([first.model_dump(mode='json')], schema_version='1.0')
        self.assertEqual(self.find(), (first, None))
        for extra in ({'schema_version': '99.0'}, {'unexpected': True},
                      {'evidence': 'not an array'}, {'unexpected': True, 'verifications': [{'invalid': True}]}):
            with self.subTest(extra=extra):
                self.put([first.model_dump(mode='json')], **extra)
                with self.assertRaises((ValueError, RuntimeError)): self.find()

    def test_legacy_conversion_fallback_preserves_owning_transaction(self):
        first = self.artifact(metadata={'nul': 'before\0after', 'unicode': '雪', 'number': 1.0})
        self.put([first.model_dump(mode='json')])
        self.backend.put('snapshot-sentinel', {'checked': False})
        def update(current):
            self.assertEqual(self.find(), (first, None))
            return {'checked': True}
        self.backend.update('snapshot-sentinel', update, default={})
        self.assertEqual(self.backend.get('snapshot-sentinel'), {'checked': True})

    def test_two_field_full_fallback_does_not_hide_unrelated_invalid_artifact(self):
        # Conversion fallback may return a complete legacy catalog with exactly
        # the same keys as a projected envelope. Its full decoder must still run.
        first = self.artifact()
        full = {'schema_version': '1.3', 'artifacts': [first.model_dump(mode='json'),
            {'id': 'unrelated-malformed', 'metadata': {'nul': 'before\0after'}}]}
        with patch.object(self.backend, 'document_array_item_envelope', return_value=full):
            with self.assertRaises(ValueError):
                self.service.find_idempotent_artifact(actor=self.actor,
                    source=self.requests[0][0], idempotency_key='plan')
        with patch.object(self.backend, 'document_array_items_envelope', return_value=full):
            with self.assertRaises(ValueError): self.find()

    def test_backend_without_batch_keeps_full_canonical_decoder(self):
        first = self.artifact()
        self.put([first.model_dump(mode='json')])
        with patch.object(self.backend, 'document_array_items_envelope', None), \
                patch.object(self.service.store, 'load', wraps=self.service.store.load) as load:
            self.assertEqual(self.find(), (first, None))
        self.assertEqual(load.call_count, 1)
        self.put([first.model_dump(mode='json')], evidence='malformed')
        with patch.object(self.backend, 'document_array_items_envelope', None):
            with self.assertRaises(ValueError): self.find()


class SQLiteArtifactSnapshotTests(ArtifactSnapshotContract, unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.backend = SQLiteStateStore(Path(temp.name) / 'state.db')
        self.configure()


@unittest.skipUnless(os.environ.get('CODEX_WEB_TEST_POSTGRES_DSN'), 'PostgreSQL test DSN required')
class PostgresArtifactSnapshotTests(ArtifactSnapshotContract, unittest.TestCase):
    def setUp(self):
        pg_fixtures.PostgresAutonomyAppendTests.setUp(self)
        self.configure()

    def test_actual_two_field_conversion_fallback_keeps_full_validation_and_transaction(self):
        for case, value in (('nan', float('nan')), ('nul', 'before\0after'),
                            ('surrogate', 'before\ud800after'), ('overflow', 'overflow-number')):
            for duplicate in (False, True):
                with self.subTest(case=case, duplicate=duplicate):
                    first = self.artifact().model_dump(mode='json')
                    first['metadata'] = {'legacy': value}  # Raw legacy JSON, outside typed serialization.
                    raw = json.dumps({'schema_version': '1.3', 'artifacts': [first,
                        {'id': self.ids[0] if duplicate else 'unrelated-malformed'}]})
                    if case == 'overflow': raw = raw.replace('"overflow-number"', '1e1000000')
                    self.backend.put('artifact_evidence', {})
                    with self.backend._connection() as connection:
                        connection.execute('UPDATE codex_state_documents SET payload=%s WHERE namespace=%s',
                            (raw, 'artifact_evidence'))
                    self.backend.put('snapshot-sentinel', {'checked': False})
                    def update(current):
                        with self.assertRaises(ValueError):
                            self.service.find_idempotent_artifact(actor=self.actor,
                                source=self.requests[0][0], idempotency_key='plan')
                        with self.assertRaises(ValueError): self.find()
                        return {'checked': True}
                    self.backend.update('snapshot-sentinel', update, default={})
                    self.assertEqual(self.backend.get('snapshot-sentinel'), {'checked': True})


class HomeArtifactSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = home_fixtures.UnownedHomeUpgradeTests()
        await self.fixture.asyncSetUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addAsyncCleanup(self.fixture.asyncTearDown)

    async def test_real_drain_uses_one_atomic_catalog_without_preliminary_lookup(self):
        f = self.fixture
        f.lose(); before = f.preserved()
        backend = f.artifacts.store.store
        with patch.object(backend, 'document_array_items_envelope',
                wraps=backend.document_array_items_envelope) as batch, \
                patch.object(backend, 'document_array_item_envelope',
                    wraps=backend.document_array_item_envelope) as point, \
                patch.object(f.artifacts, '_commit_creation', wraps=f.artifacts._commit_creation) as commit:
            self.assertTrue(f.drain().maintenance_mode)
        self.assertEqual(batch.call_count, 0)
        self.assertEqual(point.call_count, 0)
        self.assertEqual(commit.call_count, 1)
        self.assertEqual(f.preserved(), before)
        self.assertEqual(f.effects, 0)
        saved = f.planned._dormant_artifact(f.plan.id, f.actor)
        self.assertEqual(saved.lifecycle, 'active')
        self.assertIn('"schema_version":"1.1"', saved.metadata['dormant_preservation'])
        f.completed()
        self.assertIsNone(f.planned.resume_completed(f.plan.id, operator_actor=f.operator))
        self.assertEqual(f.preserved(), before)

    async def test_checkpoint_ambiguity_refuses_before_governance_or_catalog_writes(self):
        from codex_web.artifact_evidence import ArtifactCreate
        from codex_web.runtime.execution import ExecutionPrestartRefusal
        f = self.fixture
        f.lose(); before = f.preserved()
        f.artifacts.create_artifact(ArtifactCreate(artifact_type='report', name='Existing checkpoint',
            source='home-upgrade-checkpoint'), actor=f.actor, idempotency_key=f.plan.id)
        with patch.object(f.planned, '_dormant_snapshot', wraps=f.planned._dormant_snapshot) as snapshots, \
                patch.object(f.artifacts.governance, 'register_domain_record',
                    side_effect=AssertionError('checkpoint must refuse before governance')), \
                patch('codex_web.artifact_evidence.ArtifactEvidenceState.model_dump',
                    side_effect=AssertionError('checkpoint must refuse before catalog export')):
            # The canonical Home owner still refuses; global Upgrade merely
            # records this independent diagnostic rather than granting readiness.
            with self.assertRaises(ExecutionPrestartRefusal) as refused: f.strict_checkpoint()
        self.assertEqual(snapshots.call_count, 2)
        self.assertEqual(refused.exception.detail['code'], 'home_upgrade_preservation_ambiguous')
        with patch.object(f.artifacts.governance, 'register_domain_record',
                side_effect=AssertionError('blocked optional Home must not register governance')), \
                patch('codex_web.artifact_evidence.ArtifactEvidenceState.model_dump',
                    side_effect=AssertionError('blocked optional Home must not export catalog')):
            drained = f.drain()
        self.assertTrue(drained.maintenance_mode)
        self.assertEqual(drained.home_checkpoint_disposition.outcome, 'blocked')
        self.assertEqual(drained.home_checkpoint_disposition.reason_code, refused.exception.detail['code'])
        self.assertFalse(drained.home_checkpoint_disposition.continuation_authorized)
        self.assertIsNone(f.planned._dormant_artifact(f.plan.id, f.actor))
        self.assertEqual(f.preserved(), before)
        self.assertEqual(f.effects, 0)

    async def test_existing_preservation_is_reused_but_inactive_record_is_not_absence(self):
        from codex_web.runtime.execution import ExecutionPrestartRefusal
        f = self.fixture
        f.lose(); before = f.preserved()
        saved = f.strict_checkpoint()
        self.assertEqual(f.planned._dormant_artifact(f.plan.id, f.actor), saved)
        with patch.object(f.artifacts, '_commit_creation', wraps=f.artifacts._commit_creation) as commit:
            self.assertEqual(f.strict_checkpoint(), saved)
        self.assertEqual(commit.call_count, 1)
        self.assertEqual(f.planned._dormant_artifact(f.plan.id, f.actor), saved)
        def invalidate(state):
            state.artifacts = [a.model_copy(update={'lifecycle': 'invalidated'})
                if a.id == saved.id else a for a in state.artifacts]
            return state
        f.artifacts.store.update(invalidate)
        with patch.object(f.artifacts, '_commit_creation', wraps=f.artifacts._commit_creation) as commit:
            with self.assertRaises(ExecutionPrestartRefusal) as refused: f.strict_checkpoint()
        self.assertEqual(commit.call_count, 1)
        self.assertEqual(refused.exception.detail['code'], 'home_upgrade_dormant_preservation_changed')
        self.assertEqual(f.planned._dormant_artifact(f.plan.id, f.actor).lifecycle, 'invalidated')
        self.assertFalse(f.upgrades.get(f.plan.id, actor=f.operator).maintenance_mode)
        self.assertEqual(f.preserved(), before)
        self.assertEqual(f.effects, 0)

    def storage_digest(self):
        return hashlib.sha256(b''.join(self.fixture.artifacts.store.store.iter_storage_snapshot())).hexdigest()

    def warm_readiness(self):
        f = self.fixture
        binding = f.projects.get('home', f.actor.tenant).home_operator
        f.planned._dormant_snapshot(binding, f.actor)

    def preserve(self):
        f = self.fixture
        binding = f.projects.get('home', f.actor.tenant).home_operator
        return f.planned._preserve_dormant(f.plan.id, binding, f.actor, f.operator)

    async def test_valid_prior_without_payload_hash_reuses_before_governance_decode_and_writes(self):
        f = self.fixture
        f.lose(); first = self.preserve()
        def legacy(state):
            state.artifacts = [a.model_copy(update={'metadata': {k:v for k,v in a.metadata.items()
                if k != 'idempotency_payload_sha256'}}) if a.id == first.id else a for a in state.artifacts]
            return state
        f.artifacts.store.update(legacy)
        backend = f.artifacts.store.store
        backend.put('data_governance', {'schema_version':'99.0', 'records':[]})
        before = self.storage_digest()
        with patch.object(f.artifacts.governance.store, '_decode', side_effect=AssertionError('prior must not decode Gov')), \
                patch.object(f.artifacts.governance, 'register_domain_record', side_effect=AssertionError('prior must not register Gov')), \
                patch('codex_web.artifact_evidence.ArtifactEvidenceState.model_dump', side_effect=AssertionError('prior must not export')):
            result = self.preserve()
        self.assertEqual(result.id, first.id)
        self.assertNotIn('idempotency_payload_sha256', result.metadata)
        self.assertEqual(self.storage_digest(), before)
        self.assertEqual(f.effects, 0)

    async def test_first_scoped_prior_ignores_foreign_first_and_keeps_legacy_fallback(self):
        f = self.fixture
        f.lose(); first = self.preserve(); backend = f.artifacts.store.store
        raw = backend.get('artifact_evidence')
        raw['artifacts'].insert(0, {**first.model_dump(mode='json'), 'organization_id':'foreign'})
        raw['artifacts'].append({'id':'unrelated-invalid-artifact'})
        backend.put('artifact_evidence', raw)
        before = self.storage_digest()
        with patch.object(f.artifacts, 'find_idempotent_artifacts', wraps=f.artifacts.find_idempotent_artifacts) as fallback, \
                patch.object(f.artifacts.governance, 'register_domain_record', side_effect=AssertionError('no new Gov')):
            result = self.preserve()
        self.assertEqual((result.id,result.organization_id), (first.id,'local'))
        self.assertEqual(fallback.call_count, 1)
        self.assertEqual(self.storage_digest(), before)

    async def test_artifact_decode_failure_without_prior_rethrows_and_never_registers(self):
        f = self.fixture
        f.lose(); backend = f.artifacts.store.store
        backend.put('artifact_evidence', {'schema_version':'1.3','artifacts':[{'id':'unrelated-invalid'}]})
        self.warm_readiness()
        before = self.storage_digest()
        with patch.object(f.artifacts.governance, 'register_domain_record', side_effect=AssertionError('no Gov')):
            with self.assertRaises(ValueError): self.preserve()
        self.assertEqual(self.storage_digest(), before)

    async def test_updater_failure_is_not_reclassified_as_prewrite_legacy_lookup(self):
        f = self.fixture
        f.lose(); backend = f.artifacts.store.store
        self.warm_readiness()
        before = self.storage_digest()
        with patch.object(f.artifacts.governance, 'register_domain_record', side_effect=ValueError('updater failed')), \
                patch.object(f.artifacts, 'find_idempotent_artifacts', side_effect=AssertionError('no legacy retry')):
            with self.assertRaisesRegex(ValueError, 'updater failed'): self.preserve()
        self.assertEqual(self.storage_digest(), before)

    async def test_all_snapshots_and_session_readiness_run_outside_artifact_governance_locks(self):
        f = self.fixture
        f.lose(); backend = f.artifacts.store.store; inside = False
        original = backend.record_mutate_many
        def mutate(domains, updater):
            nonlocal inside
            is_creation = 'artifact_evidence' in domains
            if is_creation: inside = True
            try: return original(domains, updater)
            finally:
                if is_creation: inside = False
        original_snapshot = f.planned._dormant_snapshot
        def snapshot(*args, **kwargs):
            self.assertFalse(inside)
            return original_snapshot(*args, **kwargs)
        original_ready = f.planned.sessions._ensure_records
        def readiness():
            self.assertFalse(inside)
            return original_ready()
        with patch.object(backend,'record_mutate_many',side_effect=mutate), \
                patch.object(f.planned,'_dormant_snapshot',side_effect=snapshot) as snapshots, \
                patch.object(f.planned.sessions,'_ensure_records',side_effect=readiness) as ready:
            self.preserve()
        self.assertEqual(snapshots.call_count, 3)
        self.assertGreater(ready.call_count, 0)

    async def test_raw_governance_corruption_refuses_even_prior_reuse_without_fallback_or_writes(self):
        f = self.fixture
        f.lose(); self.preserve(); backend = f.artifacts.store.store
        with backend._connection() as connection:
            connection.execute('UPDATE state_documents SET payload=? WHERE namespace=?',
                ('{', 'data_governance'))
            connection.commit()
        before = self.storage_digest()
        with patch.object(f.artifacts, 'find_idempotent_artifacts', side_effect=AssertionError('raw corruption has no fallback')):
            with self.assertRaises(json.JSONDecodeError): self.preserve()
        self.assertEqual(self.storage_digest(), before)

    async def test_private_guard_rejects_composed_writer_before_any_callback(self):
        from codex_web.artifact_evidence import ArtifactCreate
        from codex_web.services.artifact_evidence import ArtifactEvidenceConflictError
        f = self.fixture
        guard = unittest.mock.Mock(); composed = unittest.mock.Mock()
        before = self.storage_digest()
        with self.assertRaises(ArtifactEvidenceConflictError):
            f.artifacts.create_artifact(ArtifactCreate(artifact_type='report', name='No composed guard'),
                actor=f.actor, _commit=composed, _catalog_guard=guard)
        guard.assert_not_called(); composed.assert_not_called()
        self.assertEqual(self.storage_digest(), before)

    async def test_checkpoint_arriving_after_snapshot_refuses_in_atomic_catalog(self):
        from codex_web.artifact_evidence import ArtifactCreate
        from codex_web.runtime.execution import ExecutionPrestartRefusal
        f = self.fixture
        f.lose(); calls = 0; snapshot = f.planned._dormant_snapshot
        def concurrent_checkpoint(*args, **kwargs):
            nonlocal calls
            value = snapshot(*args, **kwargs); calls += 1
            if calls == 2:
                f.artifacts.create_artifact(ArtifactCreate(artifact_type='report', name='Concurrent checkpoint',
                    source='home-upgrade-checkpoint'), actor=f.actor, idempotency_key=f.plan.id)
            return value
        with patch.object(f.planned, '_dormant_snapshot', side_effect=concurrent_checkpoint):
            with self.assertRaisesRegex(ExecutionPrestartRefusal, 'home_upgrade_preservation_ambiguous'):
                self.preserve()
        self.assertIsNone(f.planned._dormant_artifact(f.plan.id, f.actor))
        self.assertEqual(f.effects, 0)

    async def test_foreign_only_computed_id_with_matching_payload_hash_refuses(self):
        from codex_web.runtime.execution import ExecutionPrestartRefusal
        f = self.fixture
        f.lose(); first = self.preserve(); backend = f.artifacts.store.store
        raw = backend.get('artifact_evidence')
        original_hash = first.metadata['idempotency_payload_sha256']
        for item in raw['artifacts']:
            if item['id'] == first.id:
                item['organization_id'] = 'foreign'
                self.assertEqual(item['metadata']['idempotency_payload_sha256'], original_hash)
        backend.put('artifact_evidence', raw)
        before = self.storage_digest()
        with patch.object(f.artifacts.governance, 'register_domain_record', side_effect=AssertionError('no Gov write')):
            with self.assertRaisesRegex(ExecutionPrestartRefusal, 'home_upgrade_dormant_preservation_changed'):
                self.preserve()
        self.assertEqual(self.storage_digest(), before)
