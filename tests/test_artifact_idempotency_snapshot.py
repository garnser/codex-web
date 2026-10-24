"""A bounded idempotency snapshot retains canonical point-selection semantics."""
import os
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


class HomeArtifactSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = home_fixtures.UnownedHomeUpgradeTests()
        await self.fixture.asyncSetUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addAsyncCleanup(self.fixture.asyncTearDown)

    async def test_real_drain_preserves_home_with_one_artifact_snapshot_and_atomic_creation(self):
        f = self.fixture
        f.lose(); before = f.preserved()
        backend = f.artifacts.store.store
        with patch.object(backend, 'document_array_items_envelope',
                wraps=backend.document_array_items_envelope) as batch, \
                patch.object(backend, 'document_array_item_envelope',
                    wraps=backend.document_array_item_envelope) as point, \
                patch.object(f.artifacts, '_commit_creation', wraps=f.artifacts._commit_creation) as commit:
            self.assertTrue(f.drain().maintenance_mode)
        self.assertEqual(batch.call_count, 1)
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

    async def test_checkpoint_ambiguity_still_refuses_before_snapshot_or_creation(self):
        from codex_web.artifact_evidence import ArtifactCreate
        from codex_web.runtime.execution import ExecutionPrestartRefusal
        f = self.fixture
        f.lose(); before = f.preserved()
        f.artifacts.create_artifact(ArtifactCreate(artifact_type='report', name='Existing checkpoint',
            source='home-upgrade-checkpoint'), actor=f.actor, idempotency_key=f.plan.id)
        with patch.object(f.planned, '_dormant_snapshot',
                side_effect=AssertionError('ambiguous checkpoint must refuse first')), \
                patch.object(f.artifacts, '_commit_creation') as commit:
            with self.assertRaises(ExecutionPrestartRefusal): f.drain()
        commit.assert_not_called()
        self.assertEqual(f.preserved(), before)
        self.assertEqual(f.effects, 0)

    async def test_existing_preservation_is_reused_but_inactive_record_is_not_absence(self):
        from codex_web.runtime.execution import ExecutionPrestartRefusal
        f = self.fixture
        f.lose(); before = f.preserved()
        self.assertTrue(f.drain().maintenance_mode)
        saved = f.planned._dormant_artifact(f.plan.id, f.actor)
        f.put_plan(f.plan)  # The SAME original draft, without changing Home custody.
        with patch.object(f.artifacts, '_commit_creation') as commit:
            self.assertTrue(f.drain().maintenance_mode)
        commit.assert_not_called()
        self.assertEqual(f.planned._dormant_artifact(f.plan.id, f.actor), saved)
        f.put_plan(f.plan)
        def invalidate(state):
            state.artifacts = [a.model_copy(update={'lifecycle': 'invalidated'})
                if a.id == saved.id else a for a in state.artifacts]
            return state
        f.artifacts.store.update(invalidate)
        with patch.object(f.artifacts, '_commit_creation') as commit:
            with self.assertRaises(ExecutionPrestartRefusal): f.drain()
        commit.assert_not_called()
        self.assertEqual(f.preserved(), before)
        self.assertEqual(f.effects, 0)
