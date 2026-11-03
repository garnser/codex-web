"""Real private worker/identity/Work/Evidence owners; no provider or native input."""
import hashlib
import json
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

from codex_web.artifact_evidence import ArtifactCreate, EvidenceCreate, sha256_digest
from codex_web.agent_profiles import AgentProfileUpdate
from codex_web.resources import ResourceUpdate, ResourceOperationalContext
from codex_web.services.artifact_content import ArtifactContentRegistry, ArtifactContentService
from codex_web.services.local_artifact_content import LocalArtifactContentStore
from tests import test_worker_verifications as fixtures


class WorkerCIReviewTests(unittest.IsolatedAsyncioTestCase):
    _assignment = staticmethod(fixtures.WorkerVerificationTests._assignment)
    _publish_worker_authority = fixtures.WorkerVerificationTests._publish_worker_authority
    _request = fixtures.WorkerVerificationTests._request
    _publish_profile = fixtures.WorkerVerificationTests._publish_profile
    verification_resources = fixtures.WorkerVerificationTests.verification_resources
    _stop_services = fixtures.WorkerVerificationTests._stop_services

    async def asyncSetUp(self):
        await fixtures.WorkerVerificationTests.asyncSetUp(self)
        self.resources.update(self.target.id,ResourceUpdate(
            operational_context=ResourceOperationalContext(environment='development')),actor=self.admin)
        registry=ArtifactContentRegistry()
        registry.register(LocalArtifactContentStore(Path(self.temp.name)/'ci-content',backend_id='local'))
        self.evidence.content=ArtifactContentService(registry)
        self.evidence.content_backend_resolver=lambda actor,project:'local'
        self.body=dict(schema_version='1.0',kind='dependency',repository_resource_id=self.target.id,
            head_sha='a'*40,target_resource_id=self.target.id,environment='development',work_item_ref=self.ref,
            expires_at=time.time()+3600,facts=dict(manifest_sha256='b'*64,private_core_provenance_sha256='c'*64,
                python_abi='cp314',platform='linux_x86_64'))
        self.payload=self.admit_report(self.body,self.admin)

    def admit_report(self,body,producer):
        data=json.dumps(body,sort_keys=True).encode()
        artifact=self.evidence.create_artifact(ArtifactCreate(project_id='project-a',work_item_ref=self.ref,
            execution_id=self.assignment.execution_id,execution_workspace_id=self.assignment.execution_workspace_id,
            resource_ids=(self.target.id,),artifact_type='report',name='Original bounded CI prerequisite observations',
            revision=body['head_sha'],digest=sha256_digest(data)),actor=producer)
        self.evidence.attach_artifact_content(artifact.id,(data,),actor=producer,media_type='application/json')
        proof=self.evidence.create_evidence(EvidenceCreate(project_id='project-a',work_item_ref=self.ref,
            execution_id=self.assignment.execution_id,execution_workspace_id=self.assignment.execution_workspace_id,
            resource_ids=(self.target.id,),evidence_type='test_result',result='pass',artifact_ids=(artifact.id,),
            digest=sha256_digest(data)),actor=producer)
        return dict(method='ci-dependency-readiness.v1',kind=body['kind'],work_item_ref=self.ref,
            artifact_id=artifact.id,evidence_id=proof.id,expected_manifest_sha256=hashlib.sha256(data).hexdigest(),
            expected_head_sha=body['head_sha'],target_resource_id=body['target_resource_id'],request_id='d'*32)

    async def post(self,payload=None):
        return await self._request('POST','/api/verifications',payload=payload or self.payload)

    async def test_actual_distinct_worker_reviews_root_facts_once_then_lookup(self):
        status,_,result=await self.post()
        self.assertEqual(status,200,result)
        record=result['item']
        self.assertEqual(record['verifier_identity_id'],self.worker_actor.identity_id)
        self.assertNotEqual(self.worker_actor.identity_id,self.admin.identity_id)
        self.assertTrue(record['independent'])
        self.assertIn('ci_readiness_manifest_sha256:'+self.payload['expected_manifest_sha256'],record['findings'])
        self.assertTrue(result['no_privileged_operation_authorization'])
        status,_,again=await self.post()
        self.assertEqual(status,200,again)
        self.assertEqual(again['item']['id'],record['id'])
        self.assertEqual(len(self.evidence.store.load().verifications),1)
        path='/api/verifications?method=ci-dependency-readiness.v1&work_item_ref='+quote(self.ref,safe='')+'&source='+quote(record['source'],safe='')
        status,_,lookup=await self._request('GET',path)
        self.assertEqual(status,200,lookup)
        self.assertEqual(lookup['outcome'],'unique')
        self.assertEqual(lookup['original']['item']['id'],record['id'])

    async def test_same_principal_two_roles_is_not_independent(self):
        payload=self.admit_report(self.body,self.worker_actor)
        status,_,body=await self.post(payload)
        self.assertEqual(status,403,body)
        self.assertEqual(self.evidence.store.load().verifications,[])

    async def test_caller_result_actor_and_digest_overrides_refused(self):
        for update in ({'result':'verified'},{'independent':True},{'verifier_identity_id':self.admin.identity_id},
                       {'facts':self.body['facts']},{'expected_manifest_sha256':'f'*64}):
            with self.subTest(update=update):
                status,_,body=await self.post({**self.payload,**update})
                self.assertEqual(status,403,body)
        self.assertEqual(self.evidence.store.load().verifications,[])

    async def test_wrong_head_or_unadmitted_target_refused_without_append(self):
        for update in ({'expected_head_sha':'f'*40},{'target_resource_id':'foreign-resource'}):
            with self.subTest(update=update):
                status,_,body=await self.post({**self.payload,**update})
                self.assertEqual(status,403,body)
        self.assertEqual(self.evidence.store.load().verifications,[])

    async def test_expired_or_malformed_root_facts_cannot_be_verified(self):
        for change in ({'expires_at':1.0},{'facts':{'manifest_sha256':'b'*64}}):
            with self.subTest(change=change):
                payload=self.admit_report({**self.body,**change},self.admin)
                status,_,body=await self.post(payload)
                self.assertEqual(status,403,body)
        self.assertEqual(self.evidence.store.load().verifications,[])

    async def test_profile_revoked_at_append_rolls_back_review(self):
        original=self.evidence.create_verification
        def revoke(payload,*,actor,_commit=None):
            def guarded(updater):
                self.agent_profiles.update('developer',AgentProfileUpdate(description='Changed before final CI review',
                    reason='Private canonical revocation race'),actor=self.admin)
                return _commit(updater)
            return original(payload,actor=actor,_commit=guarded)
        with patch.object(self.evidence,'create_verification',side_effect=revoke):
            status,_,body=await self.post()
        self.assertEqual(status,403,body)
        self.assertEqual(self.evidence.store.load().verifications,[])

    async def test_lookup_unknown_does_not_grant_replay(self):
        path='/api/verifications?method=ci-dependency-readiness.v1&work_item_ref='+quote(self.ref,safe='')+'&source=sha256:'+('0'*64)
        status,_,body=await self._request('GET',path)
        self.assertEqual(status,200,body)
        self.assertEqual(body['outcome'],'unknown')
        self.assertTrue(body['absence_is_not_permission_to_replay'])
        self.assertEqual(self.evidence.store.load().verifications,[])

    async def test_native_runtime_review_does_not_substitute_for_external_ci_image(self):
        a=self.assignment
        facts=dict(assignment_id=a.id,execution_id=a.execution_id,execution_workspace_id=a.execution_workspace_id,
            worker_id=a.assigned_worker_id,assignment_fence=str(a.fence),
            profile_reference_sha256=hashlib.sha256(json.dumps(a.execution_profile_definition.model_dump(mode='json'),
                sort_keys=True,separators=(',',':')).encode()).hexdigest(),runtime_contract=a.execution_contract_version,
            python_abi='cp314',platform='linux_x86_64',execution_target_kind='canonical-native-assignment')
        payload=self.admit_report({**self.body,'kind':'runtime','facts':facts},self.admin)
        status,_,body=await self.post(payload)
        self.assertEqual(status,200,body)
        external=self.admit_report({**self.body,'kind':'runtime','facts':{
            **facts,'execution_target_kind':'external-ci-job'}},self.admin)
        status,_,body=await self.post(external)
        self.assertEqual(status,403,body)
        self.assertEqual(len(self.evidence.store.load().verifications),1)
