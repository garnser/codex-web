"""One fresh scoped Evidence snapshot per governed-input validation callback."""
import unittest
from unittest.mock import patch

from codex_web.services.mr_procedure_inputs import validate_mr_procedure_input
from tests import test_mr_input_verification_point_read as fixtures


class GovernedInputEvidenceBatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = fixtures.MRInputVerificationPointReadTests(
            methodName='test_actual_published_input_validation_avoids_full_catalog_decode')
        await self.f.asyncSetUp()

    async def asyncTearDown(self):
        await self.f.asyncTearDown()
        self.f.doCleanups()

    async def test_actual_three_proofs_share_one_read_and_next_callback_rechecks_revocation(self):
        f = self.f.f
        published = await f.publish()
        artifact = published['procedure_input']
        data = f.content(artifact['id'])
        assignment = f.workers.store.assignment(f.assignment.id)
        owner = f.owner()
        backend = f.evidence.store.store
        original = f.evidence.store.load()
        provider_reads = len(f.server.calls)
        with patch.object(backend, 'document_array_item_envelope',
                wraps=backend.document_array_item_envelope) as points, \
             patch.object(backend, 'document_array_items_envelope',
                wraps=backend.document_array_items_envelope) as batches:
            validate_mr_procedure_input(owner, artifact['id'], data, f.admin, assignment)
        evidence_points = [call for call in points.call_args_list
            if call.args[:2] == ('artifact_evidence', 'evidence')]
        evidence_batches = [call for call in batches.call_args_list
            if call.args[:2] == ('artifact_evidence', 'evidence')]
        self.assertEqual(len(evidence_points), 0, 'three independent SQL point reads remain')
        self.assertEqual(len(evidence_batches), 1)
        self.assertEqual(len(evidence_batches[0].args[2]), 3)
        from codex_web.artifact_evidence import EvidenceLifecycle
        proof_id = original.verifications[0].evidence_ids[0]
        def revoke(state):
            state.evidence = [e.model_copy(update={'lifecycle': EvidenceLifecycle.INVALIDATED})
                if e.id == proof_id else e for e in state.evidence]
            return state
        f.evidence.store.update(revoke)
        with self.assertRaisesRegex(ValueError, 'mr_input_evidence_identity_or_expired'):
            validate_mr_procedure_input(owner, artifact['id'], data, f.admin, assignment)
        f.evidence.store.update(lambda _: original)
        validate_mr_procedure_input(owner, artifact['id'], data, f.admin, assignment)
        self.assertEqual(len(f.server.calls), provider_reads)

    async def test_custom_incomplete_or_reordered_batch_never_bypasses_identity(self):
        f = self.f.f
        published = await f.publish()
        artifact = published['procedure_input']
        data = f.content(artifact['id'])
        assignment = f.workers.store.assignment(f.assignment.id)
        record = f.evidence.store.load().verifications[0]
        proofs = tuple(f.evidence.get_evidence(i, actor=f.admin) for i in record.evidence_ids)
        for invalid in ((), proofs[:2], tuple(reversed(proofs))):
            with self.subTest(count=len(invalid)), patch.object(f.evidence, 'get_evidence_batch',
                    create=True, return_value=invalid):
                with self.assertRaisesRegex(ValueError, 'mr_input_evidence_identity_or_expired'):
                    validate_mr_procedure_input(f.owner(), artifact['id'], data, f.admin, assignment)


class EvidenceBatchOwnerTests(unittest.TestCase):
    def setUp(self):
        from codex_web.artifact_evidence import Evidence
        self.f = fixtures.VerificationPointEnvelopeTests(
            methodName='test_scoped_duplicates_and_work_filter_match_existing_list_semantics')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.records = tuple(Evidence(id='evidence-'+format(i,'032x'),
            organization_id='local', workspace_id='default', producer_identity_id='worker',
            evidence_type='test_result', result='info') for i in range(3))

    def put(self, records=None, **extra):
        self.f.backend.put('artifact_evidence', {'schema_version': '1.5',
            'artifacts': [], 'verifications': [], 'evidence': [r.model_dump(mode='json')
                for r in (self.records if records is None else records)], **extra})

    def get(self, ids):
        return self.f.service.get_evidence_batch(ids, actor=self.f.actor)

    def test_request_order_duplicates_and_first_scoped_match_equal_point_reads(self):
        first = self.records[0]
        foreign = first.model_copy(update={'organization_id': 'foreign'})
        later = first.model_copy(update={'producer_identity_id': 'later'})
        self.put((foreign, first, later, *self.records[1:]))
        ids = (self.records[2].id, first.id, first.id, self.records[1].id)
        expected = tuple(self.f.service.get_evidence(i, actor=self.f.actor) for i in ids)
        with patch.object(self.f.service.store, 'load', side_effect=AssertionError('full catalog')):
            self.assertEqual(self.get(ids), expected)
        self.assertEqual(self.get(ids)[1].producer_identity_id, 'worker')

    def test_missing_foreign_and_empty_keep_original_closed_semantics(self):
        from codex_web.services.artifact_evidence import EvidenceNotFoundError
        self.put()
        with patch.object(self.f.service.store, 'evidence_states', side_effect=AssertionError('empty IO')):
            self.assertEqual(self.get(()), ())
        with self.assertRaises(EvidenceNotFoundError): self.get((self.records[0].id, 'absent'))
        foreign = self.records[0].model_copy(update={'workspace_id': 'foreign'})
        self.put((foreign,))
        with self.assertRaises(EvidenceNotFoundError): self.get((foreign.id,))

    def test_selected_first_record_and_legacy_envelope_are_fully_validated(self):
        self.put()
        raw = self.f.backend.get('artifact_evidence')
        bad = {**raw['evidence'][0], 'evidence_type': 'unsupported'}
        # Later duplicate is not selected by the existing POINT contract.
        raw['evidence'].insert(1, bad)
        self.f.backend.put('artifact_evidence', raw)
        self.assertEqual(self.get((self.records[0].id,)), (self.records[0],))
        raw['evidence'] = [bad, *raw['evidence']]
        self.f.backend.put('artifact_evidence', raw)
        with self.assertRaises(ValueError): self.get((self.records[0].id,))
        self.put(schema_version='99.0')
        with self.assertRaises(ValueError): self.get((self.records[0].id,))
        self.put(unexpected_header=True)
        with self.assertRaises(ValueError): self.get((self.records[0].id,))

    def test_custom_getter_and_legacy_backend_keep_their_own_validation(self):
        from codex_web.services.artifact_evidence import ArtifactEvidenceService
        from codex_web.storage.artifact_evidence import ArtifactEvidenceStore
        self.put(schema_version='1.4')
        ids = tuple(r.id for r in self.records)
        with patch.object(self.f.service, 'get_evidence', side_effect=self.records) as custom:
            self.assertEqual(self.get(ids), self.records)
            self.assertEqual([call.args[0] for call in custom.call_args_list], list(ids))
        class Legacy:
            def __init__(self, backend): self.get = backend.get
        self.f.service = ArtifactEvidenceService(ArtifactEvidenceStore(Legacy(self.f.backend)))
        self.assertEqual(self.get(ids), self.records)
        raw = self.f.backend.get('artifact_evidence')
        raw['artifacts'] = [{'invalid': True}]
        self.f.backend.put('artifact_evidence', raw)
        with self.assertRaises(ValueError): self.get(ids)

    def test_getter_rebound_from_another_owner_keeps_that_owners_fresh_store(self):
        other = fixtures.VerificationPointEnvelopeTests(
            methodName='test_scoped_duplicates_and_work_filter_match_existing_list_semantics')
        other.setUp()
        self.addCleanup(other.doCleanups)
        self.put()
        theirs = tuple(record.model_copy(update={'producer_identity_id': 'other-owner'})
                       for record in self.records)
        other.backend.put('artifact_evidence', {'schema_version': '1.5',
            'artifacts': [], 'verifications': [],
            'evidence': [record.model_dump(mode='json') for record in theirs]})
        ids = tuple(record.id for record in self.records)
        with patch.object(self.f.service, 'get_evidence', new=other.service.get_evidence):
            self.assertEqual(self.get(ids), theirs)
        # The custom owner's next canonical mutation must be read on next call.
        theirs = tuple(record.model_copy(update={'producer_identity_id': 'changed-owner'})
                       for record in theirs)
        other.backend.put('artifact_evidence', {'schema_version': '1.5',
            'artifacts': [], 'verifications': [],
            'evidence': [record.model_dump(mode='json') for record in theirs]})
        with patch.object(self.f.service, 'get_evidence', new=other.service.get_evidence):
            self.assertEqual(self.get(ids), theirs)

    def test_class_override_retains_ordinary_getter_checks_without_internal_state(self):
        from codex_web.services.artifact_evidence import ArtifactEvidenceService
        self.put()
        original = ArtifactEvidenceService.get_evidence
        supplied_states = []
        def custom(owner, evidence_id, *, actor, state=None):
            supplied_states.append(state)
            return original(owner, evidence_id, actor=actor, state=state)
        ids = tuple(record.id for record in self.records)
        with patch.object(ArtifactEvidenceService, 'get_evidence', new=custom):
            self.assertEqual(self.get(ids), self.records)
        self.assertEqual(len(supplied_states), len(ids))
        self.assertTrue(all(state is None for state in supplied_states),
                        'Class override received an internal optimized snapshot')
