"""Cold verification must not promote live envelopes before source restoration."""
from copy import deepcopy
from types import SimpleNamespace
import unittest

from codex_web import preinstall_backup_cli as cli
from codex_web.artifact_evidence import ArtifactEvidenceState, Evidence
from codex_web.compatibility import ContractSpec, ContractCompatibilityError
from codex_web.services.artifact_evidence import ArtifactEvidenceService
from codex_web.storage.artifact_evidence import ArtifactEvidenceStore
from tests import test_recovery as recovery_fixture


class ColdVerificationSchemaTests(unittest.TestCase):
    # Reuse only disposable canonical Recovery setup, not its test methods.
    setUp = recovery_fixture.RecoveryTests.setUp
    tearDown = recovery_fixture.RecoveryTests.tearDown

    def source_state(self):
        raw = ArtifactEvidenceState(evidence=[Evidence(
            organization_id='org-a', workspace_id='ws-a',
            producer_identity_id='historical-source', evidence_type='review',
            result='warn', observed_at=100, metadata={'original': 'retained'})]).model_dump(mode='json')
        raw['schema_version'] = '1.3'
        self.state.put(ArtifactEvidenceStore.namespace, raw)
        self.service.evidence = ArtifactEvidenceService(ArtifactEvidenceStore(self.state))
        return deepcopy(raw)

    def test_actual_target_recovery_publication_would_break_source_reader_contract(self):
        original = self.source_state()
        # Target reads alone must not be mistaken for persisted migration.
        self.service.evidence.store.load()
        self.assertEqual(self.state.get(ArtifactEvidenceStore.namespace), original)
        manifest = self.service.create_backup(actor=self.actor)
        self.assertEqual(self.state.get(ArtifactEvidenceStore.namespace), original)
        result = self.service.verify_restore(manifest.id, actor=self.actor, publish_evidence=True)
        self.assertEqual(result.status, 'pass')
        self.assertTrue(result.isolated)
        self.assertFalse(result.side_effects_enabled)
        self.assertIsNotNone(result.evidence_id)
        after = self.state.get(ArtifactEvidenceStore.namespace)
        self.assertEqual(after['schema_version'], '1.4')
        self.assertEqual(after['evidence'][0], original['evidence'][0])
        source = ContractSpec('artifact-evidence-state', '1.3', ('1.0','1.1','1.2','1.3'))
        with self.assertRaises(ContractCompatibilityError):
            source.require(after['schema_version'])

    def test_target_tool_is_refused_before_its_canonical_publication(self):
        original = self.source_state()
        options = SimpleNamespace(expected_runtime_revision='a'*40,
            expected_producer_revision='b'*40, expected_upgrade_target_revision='b'*40)
        with self.assertRaisesRegex(ValueError, 'installed source revision'):
            cli.source_compatible_tool_target(options)
            # If admission regresses, this actual writer changes live state.
            manifest = self.service.create_backup(actor=self.actor)
            self.service.verify_restore(manifest.id, actor=self.actor, publish_evidence=True)
        self.assertEqual(self.state.get(ArtifactEvidenceStore.namespace), original)
