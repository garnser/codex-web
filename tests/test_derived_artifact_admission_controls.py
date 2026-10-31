"""Additional real-owner derivation atomicity and direct-authority controls."""
import unittest

from codex_web.artifact_evidence import ArtifactCreate, EvidenceCreate
from codex_web.identity import AuthenticationAssurance, MembershipRole, PrincipalKind
from codex_web.services.identity import AuthorizationError
from tests import test_derived_artifact_admission as fixtures


class DerivedArtifactAdmissionControlsTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.DerivedArtifactAdmissionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def snapshot(self):
        f = self.fixture
        return (f.f.artifact_store.load().model_dump(mode='json'),
                f.f.governance.store.load().model_dump(mode='json'),
                {key: value.model_dump(mode='json') for key, value in f.f.host.states.items()})

    def test_valid_first_parent_does_not_leave_artifact_or_governance_on_invalid_second(self):
        f = self.fixture
        before = self.snapshot()
        body = f.body()
        body['source_evidence_ids'].append('missing-second-parent')
        response = f.post(body)
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(self.snapshot(), before)
        f.f.service.invalidate_evidence(f.parent.id, 'Private lifecycle control', actor=f.actor)
        before = self.snapshot()
        body['source_evidence_ids'] = [f.parent.id]
        self.assertEqual(f.post(body).status_code, 409)
        self.assertEqual(self.snapshot(), before)

    def test_same_key_cannot_switch_or_expand_parent_set(self):
        f = self.fixture
        second = f.f.service.create_evidence(EvidenceCreate(project_id='home',
            evidence_type='approval_record', result='pass'), actor=f.actor)
        first = f.post()
        self.assertEqual(first.status_code, 200, first.text)
        before = self.snapshot()
        for parents in ([second.id], [f.parent.id, second.id]):
            body = f.body()
            body['source_evidence_ids'] = parents
            response = f.post(body)
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(self.snapshot(), before)
        self.assertEqual(f.post().json()['item']['id'], first.json()['item']['id'])
        self.assertEqual(self.snapshot(), before)

    def test_direct_domain_entry_cannot_bypass_operator_assurance_or_service_scope(self):
        f = self.fixture
        body = f.body()
        payload = ArtifactCreate.model_validate(body['artifact'])
        before = self.snapshot()
        for actor in (
            f.actor.model_copy(update={'roles': (MembershipRole.MEMBER,)}),
            f.actor.model_copy(update={'assurance': AuthenticationAssurance.PRIMARY}),
            f.actor.model_copy(update={'principal_kind': PrincipalKind.SERVICE,
                'service_scopes': (), 'assurance': AuthenticationAssurance.SERVICE_TOKEN}),
        ):
            with self.subTest(kind=actor.principal_kind, assurance=actor.assurance):
                with self.assertRaises(AuthorizationError):
                    f.f.service.create_derived_artifact(payload, actor=actor,
                        source_evidence_ids=tuple(body['source_evidence_ids']),
                        idempotency_key=body['idempotency_key'])
                self.assertEqual(self.snapshot(), before)
