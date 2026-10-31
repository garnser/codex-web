"""Operator derivation uses real Artifact, Resource and Governance owners."""
import threading
import time
import unittest

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from codex_web.api.artifact_evidence import build_artifact_evidence_router
from codex_web.artifact_evidence import EvidenceCreate
from codex_web.data_governance import ContextFilterRequest
from codex_web.identity import AuthenticationAssurance, MembershipRole, PrincipalKind
from codex_web.resources import ResourceCreate
from codex_web.services.resources import ResourceCatalogService
from codex_web.storage.resource_catalog import ResourceCatalogStore
from tests import test_artifact_evidence as fixtures


class DerivedArtifactAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ArtifactEvidenceTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.addCleanup(self.f.tearDown)
        self.actor = self.f.producer
        self.resources = ResourceCatalogService(ResourceCatalogStore(self.f.artifact_store.store))
        self.resource = self.resources.create(ResourceCreate(resource_type='repository', name='Private target'), actor=self.actor)
        self.f.service.resources = self.resources
        self.f.work_item.resource_ids = [self.resource.id]
        self.f.host.states[self.f.work_item.ref] = self.f.work_item
        self.parent = self.f.service.create_evidence(EvidenceCreate(
            project_id='home', evidence_type='approval_record', result='pass',
            metadata={'document_sha256': 'a' * 64}), actor=self.actor)
        self.request_thread_ids = []
        app = FastAPI()
        @app.middleware('http')
        async def identity(request: Request, call_next):
            self.request_thread_ids.append(threading.get_ident())
            request.state.identity_actor = self.actor
            return await call_next(request)
        app.include_router(build_artifact_evidence_router(self.f.service))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def body(self):
        return {'artifact': {'project_id': 'home', 'work_item_ref': self.f.work_item.ref,
            'resource_ids': [self.resource.id], 'artifact_type': 'report', 'name': 'Approved input',
            'source': 'operator-derived-input',
            'digest': {'algorithm': 'sha256', 'value': 'a' * 64}},
            'source_evidence_ids': [self.parent.id], 'idempotency_key': 'approved-input-1'}

    def post(self, body=None):
        return self.client.post('/api/artifacts/derived', json=body or self.body())

    def test_real_operator_derives_scoped_input_with_parent_lineage_and_idempotency(self):
        original = self.f.service.get_evidence(self.parent.id, actor=self.actor)
        response = self.post()
        self.assertEqual(response.status_code, 200, response.text)
        item = response.json()['item']
        self.assertEqual(item['work_item_ref'], self.f.work_item.ref)
        self.assertEqual(item['resource_ids'], [self.resource.id])
        record = self.f.governance.get_record(item['governance_record_id'], self.actor)
        self.assertEqual(record.source_record_ids, (self.parent.governance_record_id,))
        before = self.f.artifact_store.load().model_dump(mode='json')
        self.assertEqual(self.post().json()['item']['id'], item['id'])
        self.assertEqual(self.f.artifact_store.load().model_dump(mode='json'), before)
        self.assertEqual(self.f.service.get_evidence(self.parent.id, actor=self.actor), original)
        changed = self.body(); changed['artifact']['digest']['value'] = 'b' * 64
        self.assertEqual(self.post(changed).status_code, 409)

    def test_canonical_creation_runs_once_off_the_request_event_loop_thread(self):
        original = self.f.service.create_derived_artifact
        calls = []
        def create(payload, *, actor, source_evidence_ids, idempotency_key):
            calls.append((threading.get_ident(), actor.identity_id,
                source_evidence_ids, idempotency_key))
            return original(payload, actor=actor, source_evidence_ids=source_evidence_ids,
                idempotency_key=idempotency_key)
        self.f.service.create_derived_artifact = create
        response = self.post()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(calls), 1)
        self.assertNotEqual(calls[0][0], self.request_thread_ids[-1])
        self.assertEqual(calls[0][1:], (self.actor.identity_id, (self.parent.id,), 'approved-input-1'))
        self.assertEqual(response.json()['item']['work_item_ref'], self.f.work_item.ref)

    def test_member_primary_admin_and_unscoped_service_cannot_admit(self):
        original = self.actor
        cases = [original.model_copy(update={'roles': (MembershipRole.MEMBER,)}),
            original.model_copy(update={'assurance': AuthenticationAssurance.PRIMARY}),
            original.model_copy(update={'principal_kind': PrincipalKind.SERVICE, 'service_scopes': ()})]
        for actor in cases:
            self.actor = actor
            self.assertEqual(self.post().status_code, 403)
        self.assertEqual(self.f.artifact_store.load().artifacts, [])

    def test_admin_service_scope_retains_existing_supported_authority(self):
        self.actor = self.actor.model_copy(update={'principal_kind': PrincipalKind.SERVICE,
            'assurance': AuthenticationAssurance.SERVICE_TOKEN, 'service_scopes': ('artifact-evidence:admin',)})
        self.assertEqual(self.post().status_code, 200)

    def test_request_bounds_and_private_hooks_are_rejected_before_write(self):
        for change in ({'source_evidence_ids': []}, {'source_evidence_ids': ['x'] * 65},
                       {'source_evidence_ids': ['']}, {'idempotency_key': ''}, {'idempotency_key': 'x' * 201}, {'_commit': 'forbidden'}):
            body = self.body(); body.update(change)
            self.assertEqual(self.post(body).status_code, 422)
        self.assertEqual(self.f.artifact_store.load().artifacts, [])

    def test_current_target_scope_is_required_and_revalidated_on_retry(self):
        for change in ({'project_id': None}, {'work_item_ref': None}, {'resource_ids': []}, {'digest': None},
                       {'project_id': 'foreign'}, {'work_item_ref': 'missing'}, {'resource_ids': ['foreign']}):
            body = self.body(); body['artifact'].update(change)
            self.assertEqual(self.post(body).status_code, 409)
        self.assertEqual(self.post().status_code, 200)
        self.f.host.states[self.f.work_item.ref].resource_ids = ['changed']
        self.assertEqual(self.post().status_code, 409)

    def test_cross_project_parent_including_null_project_cannot_be_laundered(self):
        for project in (None, 'foreign'):
            parent = self.f.service.create_evidence(EvidenceCreate(project_id=project,
                evidence_type='runtime_tool_summary', result='pass'), actor=self.actor)
            body = self.body(); body['source_evidence_ids'] = [parent.id]
            self.assertEqual(self.post(body).status_code, 409)
        self.assertEqual(self.f.artifact_store.load().artifacts, [])

    def test_invalidated_missing_or_foreign_tenant_parent_refuses_without_orphans(self):
        body = self.body(); body['source_evidence_ids'] = ['missing']
        self.assertEqual(self.post(body).status_code, 404)
        foreign = self.actor.model_copy(update={'organization_id': 'other'})
        parent = self.f.service.create_evidence(EvidenceCreate(project_id='home', evidence_type='approval_record', result='pass'), actor=foreign)
        body['source_evidence_ids'] = [parent.id]
        self.assertEqual(self.post(body).status_code, 404)
        self.f.service.invalidate_evidence(self.parent.id, 'Private negative control', actor=self.actor)
        self.assertEqual(self.post().status_code, 409)
        self.assertEqual(self.f.artifact_store.load().artifacts, [])

    def test_source_governance_restrictions_and_expiry_are_inherited(self):
        expiry = time.time() + 600
        parent = self.f.service.create_evidence(EvidenceCreate(project_id='home',
            evidence_type='approval_record', result='pass', classification='restricted',
            retention_expires_at=expiry, residency_tags=('eu',), deny_model_context=True), actor=self.actor)
        body = self.body(); body['source_evidence_ids'] = [parent.id]
        response = self.post(body)
        self.assertEqual(response.status_code, 200, response.text)
        item = response.json()['item']; record = self.f.governance.get_record(item['governance_record_id'], self.actor)
        self.assertEqual(record.classification, 'restricted')
        self.assertEqual(record.retention_expires_at, expiry)
        self.assertEqual(record.residency_tags, ('eu',))
        self.assertTrue(record.deny_model_context)
        filtered = self.f.governance.filter_context(ContextFilterRequest(record_ids=(record.id,)), actor=self.f.verifier)
        self.assertIn(record.id, filtered.denied_record_ids)
