"""Known governed verification lookup must not decode unrelated catalog bodies."""
import unittest
from unittest.mock import patch

from codex_web.services.mr_procedure_inputs import validate_mr_procedure_input
from tests import test_worker_mr_continuation_inputs as fixtures


class MRInputVerificationPointReadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = fixtures.MRContinuationInputTests(
            methodName='test_actual_publisher_produces_bound_input_and_repeated_facts_same_content')
        await self.f.asyncSetUp()

    async def asyncTearDown(self):
        await self.f.asyncTearDown()
        self.f.doCleanups()

    async def test_actual_published_input_validation_avoids_full_catalog_decode(self):
        f = self.f
        published = await f.publish()
        artifact = published['procedure_input']
        data = f.content(artifact['id'])
        assignment = f.workers.store.assignment(f.assignment.id)
        owner = f.owner()
        # Valid synthetic neighbors, not copied live content. Real canonical
        # publisher/input/governance remain the authoritative selected records.
        def populate(state):
            source = state.artifacts[0]
            state.artifacts.extend(source.model_copy(update={
                'id': 'artifact-' + format(i, '032x'),
                'name': 'Synthetic neighbor ' + str(i) + 'x' * 4096,
            }) for i in range(32))
            return state
        f.evidence.store.update(populate)
        decoded = []
        decode = f.evidence.store._decode
        def counted(payload):
            result = decode(payload)
            decoded.append((len(result.artifacts), len(result.evidence), len(result.verifications)))
            return result
        before = len(f.server.calls)
        with patch.object(f.evidence.store, 'load', wraps=f.evidence.store.load) as full, \
                patch.object(f.evidence.store, '_decode', side_effect=counted):
            validate_mr_procedure_input(owner, artifact['id'], data, f.admin, assignment)
        self.assertEqual(len(f.server.calls), before, 'validation must not reread provider')
        self.assertEqual(full.call_count, 0, f'Known ID decoded whole catalog; decoded sizes={decoded}')
        self.assertTrue(decoded)
        self.assertTrue(all(artifacts <= 1 and evidence <= 3 and verifications <= 1
            for artifacts, evidence, verifications in decoded), decoded)

    async def test_each_validation_rechecks_verification_and_original_evidence(self):
        f = self.f
        result = await f.publish()
        artifact = result['procedure_input']
        data = f.content(artifact['id'])
        assignment = f.workers.store.assignment(f.assignment.id)
        owner = f.owner()
        original = f.evidence.store.load()
        validate_mr_procedure_input(owner, artifact['id'], data, f.admin, assignment)
        def change_verification(field, value):
            state = original.model_copy(deep=True)
            state.verifications[0] = state.verifications[0].model_copy(update={field: value})
            return state
        variants = [
            ('invalidated', change_verification('result', 'invalidated'), 'mr_input_verification_identity'),
            ('wrong work', change_verification('work_item_ref', 'other-work'), 'mr_input_verification_unavailable'),
            ('foreign tenant', change_verification('organization_id', 'foreign'), 'mr_input_verification_unavailable'),
            ('wrong source', change_verification('source', 'other-source'), 'mr_input_verification_identity'),
            ('wrong verifier', change_verification('verifier_identity_id', 'other-worker'), 'mr_input_verification_identity'),
        ]
        missing = original.model_copy(deep=True); missing.verifications = []
        variants.append(('missing', missing, 'mr_input_verification_unavailable'))
        proof_index = next(i for i, item in enumerate(original.evidence)
            if item.id == original.verifications[0].evidence_ids[0])
        foreign = original.model_copy(deep=True)
        foreign.evidence[proof_index] = foreign.evidence[proof_index].model_copy(update={'project_id': 'foreign-project'})
        variants.append(('foreign evidence project', foreign, 'mr_input_evidence_identity_or_expired'))
        revoked = original.model_copy(deep=True)
        revoked.evidence[proof_index] = revoked.evidence[proof_index].model_copy(update={'lifecycle': 'invalidated'})
        variants.append(('revoked evidence', revoked, 'mr_input_evidence_identity_or_expired'))
        before = len(f.server.calls)
        for name, state, code in variants:
            with self.subTest(name=name):
                f.evidence.store.update(lambda _, replacement=state: replacement)
                with self.assertRaisesRegex(ValueError, code):
                    validate_mr_procedure_input(owner, artifact['id'], data, f.admin, assignment)
        f.evidence.store.update(lambda _: original)
        validate_mr_procedure_input(owner, artifact['id'], data, f.admin, assignment)
        self.assertEqual(len(f.server.calls), before)


    async def test_managed_input_open_is_scoped_without_catalog_decode(self):
        from codex_web.artifact_content import ArtifactContentRange
        from codex_web.services.artifact_evidence import ArtifactNotFoundError
        f = self.f
        published = await f.publish()
        artifact_id = published['procedure_input']['id']
        expected = f.content(artifact_id)
        with patch.object(f.evidence.store, 'load', side_effect=AssertionError('full content catalog load')):
            pointer, stream = f.evidence.open_artifact_content(artifact_id, actor=f.admin, chunk_size=7)
            self.assertEqual(b''.join(stream), expected)
            self.assertEqual(pointer.size_bytes, len(expected))
            _, stream = f.evidence.open_artifact_content(artifact_id, actor=f.admin,
                byte_range=ArtifactContentRange(start=1, end_exclusive=5))
            self.assertEqual(b''.join(stream), expected[1:5])
            with self.assertRaises(ArtifactNotFoundError):
                f.evidence.open_artifact_content(artifact_id,
                    actor=f.admin.model_copy(update={'organization_id': 'foreign'}))

    async def test_managed_input_open_keeps_first_scoped_duplicate_and_integrity_guards(self):
        from codex_web.artifact_content import ArtifactContentError, ArtifactContentIntegrityError
        f = self.f
        published = await f.publish()
        artifact_id = published['procedure_input']['id']
        original = f.evidence.store.load()
        selected = next(a for a in original.artifacts if a.id == artifact_id)
        # A foreign duplicate must not shadow the first actual tenant member.
        foreign = selected.model_copy(update={'organization_id': 'foreign'})
        state = original.model_copy(deep=True)
        state.artifacts.insert(0, foreign)
        f.evidence.store.update(lambda _: state)
        _, stream = f.evidence.open_artifact_content(artifact_id, actor=f.admin)
        self.assertTrue(b''.join(stream))
        tombstone = selected.model_copy(update={'content': selected.content.model_copy(
            update={'state': 'tombstoned'})})
        state.artifacts = [foreign, tombstone, selected]
        f.evidence.store.update(lambda _: state)
        with self.assertRaises(ArtifactContentError):
            f.evidence.open_artifact_content(artifact_id, actor=f.admin)
        corrupt = selected.model_copy(update={'digest': selected.digest.model_copy(update={'value': '0'*64})})
        state.artifacts = [corrupt]
        f.evidence.store.update(lambda _: state)
        with self.assertRaises(ArtifactContentIntegrityError):
            f.evidence.open_artifact_content(artifact_id, actor=f.admin)
        # A later canonical restore is read anew, without cached denial/bytes.
        f.evidence.store.update(lambda _: original)
        _, stream = f.evidence.open_artifact_content(artifact_id, actor=f.admin)
        self.assertTrue(b''.join(stream))

    async def test_incomplete_custom_governance_batch_cannot_skip_principal_checks(self):
        f = self.f
        published = await f.publish()
        artifact = published['procedure_input']
        data = f.content(artifact['id'])
        assignment = f.workers.store.assignment(f.assignment.id)
        with patch.object(f.evidence.governance, 'filter_context_for_actors',
                          create=True, return_value=()):
            with self.assertRaisesRegex(ValueError, 'mr_input_current_governance_denied'):
                validate_mr_procedure_input(f.owner(), artifact['id'], data, f.admin, assignment)


class VerificationPointEnvelopeTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        from codex_web.artifact_evidence import Verification
        from codex_web.identity import AuthenticationActor, AuthenticationAssurance, MembershipRole, PrincipalKind
        from codex_web.services.artifact_evidence import ArtifactEvidenceService
        from codex_web.storage.artifact_evidence import ArtifactEvidenceStore
        from codex_web.storage.sqlite_state import SQLiteStateStore
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.backend = SQLiteStateStore(Path(self.temp.name) / 'private.sqlite3')
        self.service = ArtifactEvidenceService(ArtifactEvidenceStore(self.backend))
        self.actor = AuthenticationActor(identity_id='operator', principal_kind=PrincipalKind.HUMAN,
            organization_id='local', workspace_id='default', roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.LOCAL_TRUSTED)
        self.record = Verification(id='verification-'+'a'*32, organization_id='local', workspace_id='default',
            work_item_ref='work', verifier_identity_id='worker', independent=False,
            method='controlled-observation', result='inconclusive', evidence_ids=('evidence-'+'b'*32,))

    def put(self, rows, **extra):
        self.backend.put('artifact_evidence', {'schema_version': '1.5', 'artifacts': [],
            'evidence': [], 'verifications': [row.model_dump(mode='json') for row in rows], **extra})

    def get(self, **kwargs):
        return self.service.get_verification(self.record.id, actor=self.actor, **kwargs)

    def test_scoped_duplicates_and_work_filter_match_existing_list_semantics(self):
        from codex_web.services.artifact_evidence import VerificationNotFoundError
        foreign = self.record.model_copy(update={'organization_id': 'foreign'})
        other_work = self.record.model_copy(update={'work_item_ref': 'other'})
        self.put([foreign, other_work, self.record])
        with patch.object(self.service.store, 'load', side_effect=AssertionError('full load')):
            self.assertEqual(self.get(work_item_ref='work'), self.record)
            with self.assertRaises(VerificationNotFoundError): self.get()
            with self.assertRaises(VerificationNotFoundError): self.get(work_item_ref='absent')
        self.put([self.record, self.record])
        with self.assertRaises(VerificationNotFoundError): self.get(work_item_ref='work')

    def test_missing_and_foreign_are_closed_and_next_read_is_fresh(self):
        from codex_web.services.artifact_evidence import VerificationNotFoundError
        self.put([self.record])
        self.assertEqual(self.get(), self.record)
        invalidated = self.record.model_copy(update={'result': 'invalidated'})
        self.put([invalidated])
        self.assertEqual(self.get().result, 'invalidated')
        self.put([self.record.model_copy(update={'workspace_id': 'foreign'})])
        with self.assertRaises(VerificationNotFoundError): self.get()
        with self.assertRaises(VerificationNotFoundError):
            self.service.get_verification(None, actor=self.actor)
        self.put([])
        with self.assertRaises(VerificationNotFoundError): self.get()

    def test_selected_record_and_envelope_are_fully_validated(self):
        self.put([self.record])
        raw = self.backend.get('artifact_evidence')
        raw['verifications'][0]['method'] = ''
        self.backend.put('artifact_evidence', raw)
        with self.assertRaises(ValueError): self.get()
        self.put([self.record], schema_version='99.0')
        with self.assertRaises(ValueError): self.get()
        self.put([self.record], unexpected_top_level=True)
        with self.assertRaises(ValueError): self.get()

    def test_backend_without_projection_preserves_full_legacy_validation(self):
        from codex_web.storage.artifact_evidence import ArtifactEvidenceStore
        from codex_web.services.artifact_evidence import ArtifactEvidenceService
        class LegacyBackend:
            def __init__(self, backend): self.get = backend.get
        self.put([self.record], schema_version='1.4')
        self.service = ArtifactEvidenceService(ArtifactEvidenceStore(LegacyBackend(self.backend)))
        self.assertEqual(self.get(), self.record)
        raw = self.backend.get('artifact_evidence')
        raw['artifacts'] = [{'malformed': True}]
        self.backend.put('artifact_evidence', raw)
        with self.assertRaises(ValueError): self.get()
