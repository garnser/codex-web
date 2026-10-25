"""Real assignment broker/provider/governed append; private provider transport."""
import json
import time
from types import SimpleNamespace
from unittest.mock import patch

from tests import test_worker_mr_verifications as fixtures
from codex_web.mr_continuation_input import METHOD
from codex_web.services.mr_procedure_inputs import validate_mr_procedure_input
from codex_web.services.procedure_execution import ProcedureExecutionService


class MRContinuationInputTests(fixtures.WorkerMRVerificationTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        from pathlib import Path
        from codex_web.services.artifact_content import ArtifactContentRegistry, ArtifactContentService
        from codex_web.services.local_artifact_content import LocalArtifactContentStore
        registry=ArtifactContentRegistry()
        registry.register(LocalArtifactContentStore(Path(self.temp.name)/'private-continuation-content'))
        self.evidence.content=ArtifactContentService(registry)

    def owner(self):
        owner = object.__new__(ProcedureExecutionService)
        owner.workers = self.workers
        owner.identity = self.identity
        owner.control_actor = self.admin
        owner.evidence = self.evidence
        owner.intents = self.intents
        owner.authority = self.service.authority
        owner.clock = time.time
        owner.discovery = self.service.procedure_admission.discovery
        return owner

    def content(self, artifact_id):
        _, stream = self.evidence.open_artifact_content(artifact_id, actor=self.admin)
        return b''.join(stream)

    async def publish(self, request_id='b'):
        status, _, body = await self.post({**self.request, 'method':METHOD,'request_id':request_id*32})
        self.assertEqual(status,200,body)
        return body

    async def test_actual_publisher_produces_bound_input_and_repeated_facts_same_content(self):
        first = await self.publish()
        artifact = first['procedure_input']
        assignment = self.workers.store.assignment(self.assignment.id)
        data = self.content(artifact['id'])
        validate_mr_procedure_input(self.owner(), artifact['id'], data,self.admin,assignment)
        repeated = await self.publish('c')
        self.assertNotEqual(artifact['id'],repeated['procedure_input']['id'])
        self.assertEqual(data,self.content(repeated['procedure_input']['id']))
        self.assertEqual(len(json.loads(data)['observed_requirements_fingerprint']),64)
        self.assertEqual(len(self.evidence.store.load().verifications),2)

    async def test_publisher_lookup_never_refreshes_or_repeats_provider_read(self):
        first = await self.publish()
        before = len(self.server.calls)
        repeated = await self.publish()
        self.assertTrue(repeated['deduplicated'])
        self.assertEqual(first['procedure_input']['id'],repeated['procedure_input']['id'])
        self.assertEqual(len(self.server.calls),before)
        expiry=next(iter(first['evidence'].values()))['metadata']['observation_expires_at']
        owner=self.owner()
        with patch.object(owner,'clock',return_value=expiry):
            with self.assertRaises(ValueError):
                validate_mr_procedure_input(owner,first['procedure_input']['id'],self.content(first['procedure_input']['id']),self.admin,self.workers.store.assignment(self.assignment.id))

    async def test_arbitrary_fingerprint_and_changed_proof_or_fence_refuse(self):
        result=await self.publish()
        artifact=result['procedure_input'];data=self.content(artifact['id'])
        owner=self.owner();assignment=self.workers.store.assignment(self.assignment.id)
        forged=json.loads(data);forged['observed_requirements_fingerprint']='f'*64
        with self.assertRaises(ValueError):
            validate_mr_procedure_input(owner,artifact['id'],json.dumps(forged).encode(),self.admin,assignment)
        with self.assertRaises(ValueError):
            validate_mr_procedure_input(owner,artifact['id'],data,self.admin,assignment.model_copy(update={'created_by':'foreign'}))
        def alter(state):
            item=next(e for e in state.evidence if e.id==next(iter(result['evidence'].values()))['id'])
            item.metadata['provider_snapshot_sha256']='0'*64
            return state
        self.evidence.store.update(alter)
        with self.assertRaises(ValueError):validate_mr_procedure_input(owner,artifact['id'],data,self.admin,assignment)

    async def test_distinct_resource_context_requester_is_preserved_and_rechecked(self):
        from codex_web.identity import HumanUserCreate
        from codex_web.resource_context import ReadOnlyResourceContext
        human,_=self.identity.create_human_user(HumanUserCreate(id='original-context-requester',
            display_name='Original context requester',roles=['admin']),actor=self.admin)
        from codex_web.authority import AUTHORITY_ROLE_CATALOG_ID, AUTHORITY_ROLE_CATALOG_KIND
        from codex_web.definitions import DefinitionDraftCreate, DefinitionPublishRequest
        active=self.registry.resolve(definition_id=AUTHORITY_ROLE_CATALOG_ID,kind=AUTHORITY_ROLE_CATALOG_KIND)
        payload=active.payload
        payload['bindings'].append({'id':'private-context-requester-role','role_id':'local-admin',
            'subject_kind':'identity','subject_id':human.id,'organization_id':self.admin.organization_id,
            'workspace_id':self.admin.workspace_id,'project_ids':['project-a']})
        draft=self.registry.create_draft(DefinitionDraftCreate(definition_id=AUTHORITY_ROLE_CATALOG_ID,
            kind=AUTHORITY_ROLE_CATALOG_KIND,definition_schema_version=active.definition_schema_version,
            payload=payload,actor='private-fixture'))
        self.registry.approve_publication(draft.record_id,actor='private-fixture-reviewer',
            reference='PRIVATE-REQUESTER-ROLE',reason='Independent private original requester authority')
        self.registry.publish(draft.record_id,DefinitionPublishRequest(actor='private-fixture',expected_active_revision=active.revision))
        context=ReadOnlyResourceContext(schema_version='1.0',read_only_resource_ids=(self.target.id,),
            requester_identity_id=human.id,work_item_ref=self.ref)
        def bind(state):
            state.assignments[0].resource_context=context
            return state
        self.workers.store.update_assignment(self.worker.id,self.assignment.id,bind)
        # A Resource context is immutable for a broker incarnation. Construct
        # its transport with the original scoped pin rather than mutating a
        # previously authenticated broker's assignment scope.
        from codex_web.services.control_plane_broker import AssignmentBoundControlPlaneBroker
        await self.broker.stop()
        current=self.workers.store.assignment(self.assignment.id)
        self.broker=AssignmentBoundControlPlaneBroker(self.service,assignment=current,
            worker_id=self.worker.id,worker_actor=self.worker_actor,fence=current.fence,
            validator=lambda:self.workers.assignment_for_execution(current.execution_id,actor=self.admin))
        await self.broker.start()
        result=await self.publish()
        artifact=result['procedure_input'];data=self.content(artifact['id'])
        assignment=self.workers.store.assignment(self.assignment.id)
        self.assertEqual(artifact['metadata']['requester_identity_id'],human.id)
        self.assertNotEqual(human.id,assignment.created_by)
        validate_mr_procedure_input(self.owner(),artifact['id'],data,self.admin,assignment)
        def revoke(state):
            for membership in state.memberships:
                if membership.identity_id==human.id:membership.revoked_at=time.time()
            return state
        self.identity.store.update(revoke)
        with self.assertRaises(ValueError):
            validate_mr_procedure_input(self.owner(),artifact['id'],data,self.admin,assignment)

    async def test_late_expiry_after_attachment_commits_no_pointer_proofs_or_governance(self):
        original=self.evidence.attach_artifact_content
        before=self.evidence.store.load().model_dump(mode='json')
        governed=self.evidence.governance.store.load().model_dump(mode='json')
        attached=[]
        clock=self.service._clock
        def attach(*args,**kwargs):
            artifact=original(*args,**kwargs)
            attached.append(artifact)
            return artifact
        def now():return time.time()+31 if attached else clock()
        with patch.object(self.evidence,'attach_artifact_content',side_effect=attach), patch.object(self.service,'_clock',side_effect=now):
            status,_,_=await self.post({**self.request,'method':METHOD,'request_id':'b'*32})
        self.assertNotEqual(status,200)
        self.assertEqual(len(attached),1)
        self.assertIsNotNone(attached[0].content)
        self.assertEqual(self.evidence.store.load().model_dump(mode='json'),before)
        self.assertEqual(self.evidence.governance.store.load().model_dump(mode='json'),governed)
        # Existing content-addressed blobs may remain; do not delete a digest
        # shared by another retained Artifact. Only canonical pointers/proofs
        # and Governance are transactionally atomic.

    async def test_real_api_policy_denial_is_typed_input_refusal_not_unexpected_error(self):
        from codex_web.api.authorization import ApiAuthorizationPolicy, ApiAccessMode, ApiAuthorizationError
        from codex_web.services.procedure_execution import ProcedureExecutionError
        result=await self.publish();artifact=result['procedure_input'];data=self.content(artifact['id'])
        owner=self.owner();assignment=self.workers.store.assignment(self.assignment.id)
        protocol=SimpleNamespace(input_protocol='code-host-mr-procedure-input.v1')
        # Exercise the actual API authorization service and actual role denial,
        # with a stricter private policy. No grant or production policy changes.
        policy=ApiAuthorizationPolicy(capability='private.ungranted.observation.read',level='read',access=ApiAccessMode.AUTHORITY)
        with patch('codex_web.api.authorization.policy_for_operation',return_value=policy):
            with self.assertRaises(ProcedureExecutionError) as refused:
                owner._input_protocol(protocol,artifact['id'],data,self.admin,assignment)
        self.assertEqual(refused.exception.code,'procedure_governed_input_invalid')
        self.assertIsInstance(refused.exception.__cause__,ApiAuthorizationError)

    async def test_cached_input_governance_denial_does_not_leak_or_repeat_read(self):
        from urllib.parse import quote
        result=await self.publish();artifact=result['procedure_input']
        original=self.evidence.get_artifact(artifact['id'],actor=self.admin).model_dump(mode='json')
        def deny(state):
            state.records=[r.model_copy(update={'deny_model_context':True}) if r.id==artifact['governance_record_id'] else r for r in state.records]
            return state
        self.evidence.governance.store.update(deny)
        calls=len(self.server.calls)
        status,_,body=await self._request('GET','/api/verifications?method='+METHOD+'&source='+quote(result['item']['source'],safe=''))
        self.assertEqual(status,403,body)
        self.assertEqual(len(self.server.calls),calls)
        self.assertEqual(self.evidence.get_artifact(artifact['id'],actor=self.admin).model_dump(mode='json'),original)

    async def test_atomic_artifact_content_failure_retains_no_partial_proofs(self):
        before=self.evidence.store.load().model_dump(mode='json')
        with patch.object(self.evidence,'attach_artifact_content',side_effect=OSError('private injected content refusal')):
            status,_,_=await self.post({**self.request,'method':METHOD,'request_id':'b'*32})
        self.assertNotEqual(status,200)
        self.assertEqual(self.evidence.store.load().model_dump(mode='json'),before)


def load_tests(loader,tests,pattern):
    import unittest
    return unittest.TestSuite(MRContinuationInputTests(name) for name in MRContinuationInputTests.__dict__ if name.startswith('test_'))
