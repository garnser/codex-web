"""Canonical tool qualification gate using real records and bounded point getters."""
from dataclasses import replace
import hashlib
from pathlib import Path
import unittest

from codex_web.artifact_evidence import (
    Artifact, ArtifactDigest, ArtifactLifecycle, ArtifactType,
    Evidence, EvidenceLifecycle, EvidenceResult, EvidenceType,
)
from codex_web.canonical_json import canonical_json_bytes
from codex_web.identity import AuthenticationActor, AuthenticationAssurance, MembershipRole, PrincipalKind
from codex_web.preinstall_backup_cli import validate_source_compatible_tool_evidence
from codex_web.recovery_tool_compatibility import (
    ChangedFile, RecoveryToolCompatibilityError, ValidatedRecoveryTool,
)


class PointRecords:
    def __init__(self, artifact, records):
        self.artifact, self.records = artifact, records
        self.calls = []

    def get_artifact(self, identifier, *, actor):
        self.calls.append(('artifact', identifier, actor.identity_id))
        if identifier != self.artifact.id:
            raise LookupError('missing fixture Artifact')
        return self.artifact

    def get_evidence(self, identifier, *, actor):
        self.calls.append(('evidence', identifier, actor.identity_id))
        return self.records[identifier]


class RecoveryToolQualificationTests(unittest.TestCase):
    def setUp(self):
        self.actor = AuthenticationActor(identity_id='operator', principal_kind=PrincipalKind.HUMAN,
        organization_id='org', workspace_id='ws', roles=(MembershipRole.ADMIN,),
        assurance=AuthenticationAssurance.LOCAL_TRUSTED)
        self.facts = ValidatedRecoveryTool(Path('/private/proof.json'), 'f' * 64,
            (1, 2, 3, 4, 5, 6), Path('/private/complete-tool'), 'a' * 40, 'b' * 40,
            'c' * 40, 'd' * 40, 'e' * 40, '1' * 64, 'artifact-' + '1' * 32,
            'evidence-' + '2' * 32, 'evidence-' + '3' * 32,
            (ChangedFile('codex_web/services/crypto_keys.py', '4' * 64, '5' * 64),))
        self.ci_id = 'evidence-' + '4' * 32
        self.now = 2000000000.
        rows = [{'path': row.path, 'base_sha256': row.base_sha256, 'tool_sha256': row.tool_sha256}
                for row in self.facts.changed_files]
        self.bindings = {'base_revision': self.facts.base_revision,
            'tool_revision': self.facts.tool_revision,
            'upgrade_target_revision': self.facts.upgrade_target_revision,
            'base_tree_sha1': self.facts.base_tree_sha1, 'tool_tree_sha1': self.facts.tool_tree_sha1,
            'tool_archive_sha256': self.facts.tool_archive_sha256,
            'changed_files_sha256': hashlib.sha256(canonical_json_bytes(rows)).hexdigest()}
        self.artifact = Artifact(id=self.facts.tool_artifact_id, organization_id='org', workspace_id='ws',
            project_id='project', artifact_type=ArtifactType.PACKAGE, name='qualified tool',
            producer_identity_id='operator', revision=self.facts.tool_revision,
            digest=ArtifactDigest(value=self.facts.tool_archive_sha256))
        common = dict(organization_id='org', workspace_id='ws', project_id='project',
            artifact_ids=(self.artifact.id,), producer_identity_id='operator',
            provider='observed-provider', result=EvidenceResult.PASS)
        self.compat = Evidence(id=self.facts.compatibility_evidence_id, **common,
            evidence_type=EvidenceType.TEST_RESULT, source='tool-compatibility-fixture',
            external_id='fixture-original-run-1', deep_link='https://proof.invalid/fixture/1',
            metadata={**self.bindings, 'review_evidence_id': self.facts.review_evidence_id,
                'ci_evidence_id': self.ci_id, 'downstream_checks_unchanged': True,
                'measured_memory_qualification': True, 'contracts_unchanged': True})
        self.review = Evidence(id=self.facts.review_evidence_id, **common,
            evidence_type=EvidenceType.REVIEW, source='independent-reviewer',
            external_id='review-original-1', deep_link='https://proof.invalid/review/1',
            metadata={**self.bindings, 'compatibility_evidence_id': self.compat.id,
                'ci_evidence_id': self.ci_id, 'reviewer_source': 'independent-reviewer',
                'review_reference': 'review-original-1'})
        self.ci = Evidence(id=self.ci_id, **common, evidence_type=EvidenceType.CI_CHECK,
            source='canonical-ci-observation', external_id='external-ci-run-1',
            deep_link='https://ci.invalid/run/1', metadata={
                'tool_revision': self.facts.tool_revision, 'tool_tree_sha1': self.facts.tool_tree_sha1,
                'tool_archive_sha256': self.facts.tool_archive_sha256,
                'all_applicable_checks_passed': True, 'ci_run_id': 'actual-run-1'})
        self.service = PointRecords(self.artifact,
            {record.id: record for record in (self.compat, self.review, self.ci)})

    def validate(self, **kwargs):
        return validate_source_compatible_tool_evidence(kwargs.pop('facts', self.facts),
            self.service, actor=kwargs.pop('actor', self.actor), now=kwargs.pop('now', self.now), **kwargs)

    def test_qualified_same_producer_distinct_external_review_returns_closed_receipt(self):
        receipt = self.validate()
        self.assertEqual(receipt['tool_revision'], self.facts.tool_revision)
        self.assertEqual(receipt['ci_evidence_id'], self.ci.id)
        self.assertEqual(receipt['review_evidence_id'], self.review.id)
        self.assertEqual(len(self.service.calls), 4)
        self.assertNotIn('summary', receipt)
        self.assertNotIn('deep_link', receipt)
        self.assertEqual({item.producer_identity_id for item in (self.compat, self.review, self.ci)}, {'operator'})

    def test_artifact_type_revision_digest_and_lifecycle_each_rechecked(self):
        for changes in ({'artifact_type': ArtifactType.REPORT}, {'revision': '0' * 40},
                        {'digest': None}, {'digest': ArtifactDigest(value='0' * 64)},
                        {'lifecycle': ArtifactLifecycle.SUPERSEDED}, {'invalidated_at': self.now - 1},
                        {'invalidation_reason': 'revoked'}, {'retention_expires_at': self.now}):
            with self.subTest(changes=changes):
                self.service.artifact = self.artifact.model_copy(update=changes)
                with self.assertRaises(RecoveryToolCompatibilityError):
                    self.validate()
        self.service.artifact = self.artifact
        self.assertEqual(self.validate()['tool_artifact_id'], self.artifact.id)

    def test_foreign_tenant_or_project_refused_even_if_getter_returns_it(self):
        for selected in ('artifact', self.compat.id, self.review.id, self.ci.id):
            original = self.artifact if selected == 'artifact' else self.service.records[selected]
            fields = ('organization_id', 'workspace_id') if selected == 'artifact' else ('organization_id', 'workspace_id', 'project_id')
            for field in fields:
                with self.subTest(record=selected, field=field):
                    changed = original.model_copy(update={field: 'foreign'})
                    if selected == 'artifact': self.service.artifact = changed
                    else: self.service.records[selected] = changed
                    with self.assertRaises(RecoveryToolCompatibilityError): self.validate()
                    self.service.artifact = self.artifact
                    if selected != 'artifact': self.service.records[selected] = original

    def test_every_evidence_requires_current_pass_exact_type_and_artifact_link(self):
        for original in (self.compat, self.review, self.ci):
            for changes in ({'result': EvidenceResult.FAIL}, {'lifecycle': EvidenceLifecycle.INVALIDATED},
                            {'invalidated_at': self.now - 1}, {'invalidation_reason': 'retired'},
                            {'retention_expires_at': self.now}, {'artifact_ids': ()},
                            {'evidence_type': EvidenceType.USER_OUTCOME}, {'external_id': None}):
                with self.subTest(identifier=original.id, changes=changes):
                    self.service.records[original.id] = original.model_copy(update=changes)
                    with self.assertRaises(RecoveryToolCompatibilityError): self.validate()
            self.service.records[original.id] = original

    def test_all_compatibility_and_review_fingerprints_are_exact(self):
        for original in (self.compat, self.review):
            for field in self.bindings:
                with self.subTest(record=original.id, field=field):
                    self.service.records[original.id] = original.model_copy(
                        update={'metadata': {**original.metadata, field: '0' * 64}})
                    with self.assertRaises(RecoveryToolCompatibilityError): self.validate()
            self.service.records[original.id] = original
        with self.assertRaises(RecoveryToolCompatibilityError):
            self.validate(facts=replace(self.facts, changed_files=(
                ChangedFile('codex_web/services/crypto_keys.py', '4' * 64, '6' * 64),)))

    def test_booleans_alone_no_real_qualification_or_current_ci_do_not_pass(self):
        for field in ('downstream_checks_unchanged', 'measured_memory_qualification', 'contracts_unchanged'):
            for value in (False, None, 'true', 1):
                self.service.records[self.compat.id] = self.compat.model_copy(
                    update={'metadata': {**self.compat.metadata, field: value}})
                with self.assertRaises(RecoveryToolCompatibilityError): self.validate()
        self.service.records[self.compat.id] = self.compat
        for field, value in (('all_applicable_checks_passed', False), ('all_applicable_checks_passed', 1),
                             ('ci_run_id', ''), ('tool_revision', self.facts.upgrade_target_revision),
                             ('tool_tree_sha1', '0' * 40), ('tool_archive_sha256', '0' * 64)):
            self.service.records[self.ci.id] = self.ci.model_copy(update={'metadata': {**self.ci.metadata, field: value}})
            with self.assertRaises(RecoveryToolCompatibilityError): self.validate()

    def test_mutual_references_and_independent_review_provenance_required(self):
        for original, changes in ((self.compat, {'review_evidence_id': self.ci.id}),
                                  (self.compat, {'ci_evidence_id': self.review.id}),
                                  (self.review, {'compatibility_evidence_id': self.ci.id}),
                                  (self.review, {'ci_evidence_id': self.compat.id}),
                                  (self.review, {'reviewer_source': ''}),
                                  (self.review, {'review_reference': 'another-review'})):
            self.service.records[original.id] = original.model_copy(update={'metadata': {**original.metadata, **changes}})
            with self.assertRaises(RecoveryToolCompatibilityError): self.validate()
            self.service.records[original.id] = original
        self.service.records[self.review.id] = self.review.model_copy(update={
            'external_id': self.ci.external_id, 'metadata': {**self.review.metadata, 'review_reference': self.ci.external_id}})
        with self.assertRaises(RecoveryToolCompatibilityError): self.validate()

    def test_new_call_reads_revocations_instead_of_caching_prior_success(self):
        self.validate()
        self.service.records[self.ci.id] = self.ci.model_copy(update={'lifecycle': EvidenceLifecycle.INVALIDATED})
        with self.assertRaises(RecoveryToolCompatibilityError): self.validate()
        self.assertEqual(len(self.service.calls), 8)
        self.service.records[self.ci.id] = self.ci
        self.service.records[self.review.id] = self.review.model_copy(update={'retention_expires_at': self.now + 1})
        self.validate()
        with self.assertRaises(RecoveryToolCompatibilityError): self.validate(now=self.now + 1)

    def test_missing_canonical_record_and_invalid_time_cannot_qualify(self):
        del self.service.records[self.ci.id]
        with self.assertRaises(KeyError): self.validate()
        for value in (True, float('nan'), float('inf'), None):
            with self.assertRaises(RecoveryToolCompatibilityError): self.validate(now=value)
