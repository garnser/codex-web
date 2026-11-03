"""Canonical target-only recipient discovery over the real private Unix broker.

Source sharing owners are the qualified #2006/#2008 fixture. Runtime observation
edges remain controlled; no live runtime/provider is invoked by this module.
"""
import base64
import hashlib
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from codex_web.agent_profiles import AgentProfileCreate,AgentProfileExecutionBinding
from codex_web.definitions import DefinitionDraftCreate,DefinitionPublishRequest
from codex_web.authority import AUTHORITY_ROLE_CATALOG_ID,AUTHORITY_ROLE_CATALOG_KIND
from codex_web.execution_workers import (ExecutionWorkerRegister,ExecutionAssignmentCreate,
    AssignmentClaimRequest,AssignmentStartRequest)
from codex_web.execution_workspaces import ExecutionWorkspaceAcquire
from codex_web.resources import RepositoryExecutionScope
from codex_web.services.control_plane_broker import AssignmentBoundControlPlaneBroker
from tests import test_artifact_sharing as sharing


class WorkerSharedInputsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.s=sharing.GovernedArtifactSharingTests()
        self.s.addCleanup,self.s.addAsyncCleanup=self.addCleanup,self.addAsyncCleanup
        await self.s.asyncSetUp()
        self.f=self.s.f
        # Source capture/share is a real canonical governed pair, not sender-
        # provided IDs manufactured for the recipient. The receiver discovers
        # only from its exact Work reference over HTTP.
        self.delivered=await self.s.share()
        await self.receiver()

    def publish_receiver_reads(self, *, evidence=True):
        f=self.f
        active=f.registry.resolve(definition_id=AUTHORITY_ROLE_CATALOG_ID,kind=AUTHORITY_ROLE_CATALOG_KIND)
        payload=json.loads(json.dumps(active.payload))
        payload['roles']=[r for r in payload['roles'] if r['id']!='shared-input-recipient']
        capabilities=['api.artifacts.read']+(['api.evidence.read'] if evidence else [])
        payload['roles'].append({'id':'shared-input-recipient','name':'Private target reader',
            'description':'Exact receiving Work resource reads; no Source or data-sharing authority',
            'grants':[{'id':'recipient-'+cap,'capability':cap,'level':'read','project_ids':['project-a'],
                'resource_ids':[self.s.target.id]} for cap in capabilities]})
        payload['bindings']=[b for b in payload.get('bindings',[]) if b['id']!='shared-input-recipient-binding']
        payload['bindings'].append({'id':'shared-input-recipient-binding','role_id':'shared-input-recipient',
            'subject_kind':'identity','subject_id':'sharing-recipient-worker','organization_id':'local',
            'workspace_id':'default','project_ids':['project-a']})
        draft=f.registry.create_draft(DefinitionDraftCreate(definition_id=AUTHORITY_ROLE_CATALOG_ID,
            kind=AUTHORITY_ROLE_CATALOG_KIND,definition_schema_version='1.0',payload=payload,actor='private-publisher'))
        f.registry.approve_publication(draft.record_id,actor='private-reviewer',reference='private-target-read',
            reason='Controlled exact target metadata/content fixture')
        f.registry.publish(draft.record_id,DefinitionPublishRequest(actor='private-publisher',expected_active_revision=active.revision))

    async def receiver(self):
        f=self.f
        self.publish_receiver_reads()
        self.target=self.s.target
        self.target_work=self.s.target_work
        receiver = f.identity.bootstrap_service_actor(identity_id='sharing-recipient-worker',name='Private recipient',
            scope=f.scope,service_scopes=('execution-worker:run','action-intent:worker','secret:use'))
        registered=f.workers.register(ExecutionWorkerRegister(service_identity_id=receiver.identity_id,
            pool='private-receiver',version='1.0',capabilities=f.worker.capabilities,
            supported_execution_contract_versions=('thread-bootstrap/1.0',),max_concurrency=1),actor=f.admin)
        workspace=f.workspaces.acquire(ExecutionWorkspaceAcquire(work_item_ref=self.target_work.ref,
            execution_id='private-recipient-execution',project_id='project-a',resource_ids=(self.target.id,),
            repository_resource_id=self.target.id),actor=f.admin)
        profile=f.agent_profiles.create(AgentProfileCreate(profile_id='quinn-sharing-recipient',name='Private receiver',
            role_id='quinn',authority_role_id='local-admin',execution_profile_id='repository-write'),actor=f.admin)
        pin=AgentProfileExecutionBinding(profile_id=profile.profile_id,profile_revision=profile.revision,
            profile_record_id=profile.record_id,role_id=profile.role_id,authority_role_id=profile.authority_role_id,
            execution_profile_id=profile.execution_profile_id,authority_definition_ref=profile.authority_definition_ref)
        _,definition=f.profiles.resolve('repository-write',project_id='project-a')
        assignment=f.workers.create_assignment(ExecutionAssignmentCreate(work_item_ref=self.target_work.ref,
            execution_id=workspace.execution_id,project_id='project-a',resource_ids=(self.target.id,),
            execution_workspace_id=workspace.id,repository_scope=RepositoryExecutionScope(organization_id='local',
                workspace_id='default',project_id='project-a',writable_repository_ids=(self.target.id,),source='work_item'),
            execution_contract_version='thread-bootstrap/1.0',required_capabilities=f.assignment.required_capabilities,
            sandbox='workspace-write',approval_policy='on-request',execution_profile_id='repository-write',
            execution_profile_definition=definition,agent_profile=pin),actor=f.admin)
        claimed=f.workers.claim(registered.id,AssignmentClaimRequest(),actor=receiver,assignment_id=assignment.id)
        actual=f.workers.start(registered.id,claimed.id,AssignmentStartRequest(fence=claimed.fence,
            lease_token=claimed.lease.lease_token),actor=receiver)
        self.actual,self.actor=actual,receiver
        self.broker=AssignmentBoundControlPlaneBroker(f.service,assignment=actual,worker_id=registered.id,
            worker_actor=receiver,fence=actual.fence,
            validator=lambda:f.workers.assignment_for_execution(actual.execution_id,actor=f.admin))
        await self.broker.start()
        self.addAsyncCleanup(self.broker.stop)
        self.transport=SimpleNamespace(broker=self.broker,broker_response_timeout=10)

    async def request(self,path,*,method='GET',payload=None):
        return await type(self.f)._request(self.transport,method,path,payload=payload)

    def lookup_path(self,**query):
        return '/api/artifacts/shared-inputs?'+urlencode({'work_item_ref':self.target_work.ref,**query})

    async def lookup(self,**query):
        return await self.request(self.lookup_path(**query))

    async def test_receiver_discovers_then_reads_shared_input_without_source_execution_scope(self):
        status,_,result=await self.lookup()
        self.assertEqual(status,200,result)  # OLD missing route must reach this assertion.
        self.assertTrue(result['complete'])
        self.assertFalse(result['truncated'])
        self.assertIsNone(result['next_cursor'])
        self.assertTrue(result['source_information_only'])
        self.assertFalse(result['independent_qa_or_acceptance'])
        self.assertFalse(result['repository_execution_scope_changed'])
        self.assertEqual(len(result['items']),1)
        item=result['items'][0]  # IDs come ONLY from the recipient's lookup.
        self.assertEqual(set(item),{'artifact_id','evidence_id','sha256','size_bytes','result'})
        self.assertEqual(item['result'],'info')
        status,_,metadata=await self.request('/api/artifacts/'+item['artifact_id'])
        self.assertEqual(status,200,metadata)
        self.assertEqual(metadata['item']['work_item_ref'],self.target_work.ref)
        status,_,content=await self.request('/api/artifacts/'+item['artifact_id']+'/content')
        self.assertEqual(status,200,content)
        raw=base64.b64decode(content['content_base64'])
        self.assertEqual(hashlib.sha256(raw).hexdigest(),item['sha256'])
        self.assertEqual(raw,self.s.body)
        status,_,proof=await self.request('/api/evidence/'+item['evidence_id'])
        self.assertEqual(status,200,proof)
        self.assertEqual(proof['item']['result'],'info')
        self.assertEqual(self.actual.resource_ids,(self.target.id,))
        self.assertNotIn(self.f.target.id,self.actual.resource_ids)
        self.assertNotIn('content_base64',json.dumps(result))

    async def test_lookup_requires_both_target_read_capabilities(self):
        self.publish_receiver_reads(evidence=False)
        status,_,_=await self.lookup()
        self.assertEqual(status,403)

    async def test_foreign_or_missing_work_refuses_without_content_or_ids(self):
        for work in (self.f.ref,'missing/repository!99'):
            with self.subTest(work=work):
                status,_,result=await self.request('/api/artifacts/shared-inputs?'+urlencode({'work_item_ref':work}))
                self.assertEqual(status,403,result)
                self.assertNotIn(self.delivered['artifact_id'],json.dumps(result))

    async def test_foreign_project_and_nonowner_work_refuse(self):
        original=self.f.items.get(self.target_work.ref)
        for update in ({'project_id':'foreign'},{'current_owner':'james'},{'resource_ids':[self.f.target.id]}):
            with self.subTest(update=update):
                self.f.items.put(original.ref,original.model_copy(update=update))
                status,_,_=await self.lookup()
                self.assertEqual(status,403)
        self.f.items.put(original.ref,original)

    async def test_query_and_body_bounds_are_explicit(self):
        for query in ({'limit':0},{'limit':17},{'limit':'not-int'},{'cursor':'not-json'}, {'repository_resource_id':self.f.target.id}):
            with self.subTest(query=query):
                status,_,_=await self.lookup(**query)
                self.assertEqual(status,400)
        status,_,_=await self.request(self.lookup_path(),payload={'override':True})
        self.assertEqual(status,400)
        status,_,_=await self.request(self.lookup_path(),method='POST')
        self.assertEqual(status,400)

    async def test_current_policy_and_parent_visibility_are_applied_before_discovery(self):
        self.s.publish_policy(target=False)
        status,_,result=await self.lookup()
        self.assertEqual(status,200,result)
        self.assertEqual(result['items'],[])
        self.s.publish_policy()
        status,_,result=await self.lookup()
        self.assertEqual(status,200,result)
        self.assertEqual(len(result['items']),1)  # compatible current policy remains readable.
        self.f.evidence.invalidate_evidence(self.s.proof.id,'Private original source withdrawn',actor=self.f.worker_actor)
        status,_,result=await self.lookup()
        self.assertEqual(status,200,result)
        self.assertEqual(result['items'],[])
        self.assertNotIn(self.delivered['artifact_id'],json.dumps(result))

    async def test_parent_context_denial_hides_pair(self):
        def deny(state):
            next(r for r in state.records if r.id==self.s.artifact.governance_record_id).deny_model_context=True
            return state
        self.f.evidence.governance.store.update(deny)
        status,_,result=await self.lookup()
        self.assertEqual(status,200,result)
        self.assertEqual(result['items'],[])

    async def test_current_reader_fence_change_is_not_historical_authority(self):
        assignment=self.f.workers.store.assignment(self.actual.id)
        def drift(state):
            state.assignments=[a.model_copy(update={'fence':a.fence+1}) for a in state.assignments]
            return state
        self.f.workers.store.update_assignment(assignment.assigned_worker_id,assignment.id,drift)
        status,_,_=await self.lookup()
        self.assertIn(status,(403,409))

    async def test_catalog_declares_bounded_exact_work_info_operation(self):
        status,_,result=await self.request('/api/control-plane-broker/operations')
        self.assertEqual(status,200,result)
        # Catalog's actual envelope is shared with the established broker tests.
        operations=result['operations']
        item=next(o for o in operations if o['id']=='verification.shared-inputs.read')
        self.assertEqual(item['required_query_parameters'],['work_item_ref'])
        self.assertEqual(item['bounds']['returned_items'],16)
        self.assertIn('not acceptance',item['effect'])

    async def test_exact_work_query_ignores_later_unrelated_history_before_limit(self):
        # Canonical owner is deliberately surrounded with more than one raw page
        # of unrelated target/provider/tenant metadata. No full tenant load is
        # available to this read; SQL scope precedes its bounded LIMIT.
        original=self.f.evidence.store.load()
        artifact=next(a for a in original.artifacts if a.id==self.delivered['artifact_id'])
        unrelated=tuple(artifact.model_copy(update={'id':'unrelated-'+str(i),'work_item_ref':'other/work!1'}) for i in range(70))
        self.f.evidence.store.update(lambda state:state.model_copy(update={'artifacts':state.artifacts+list(unrelated)}))
        with patch.object(self.f.evidence.store,'load',side_effect=AssertionError('unbounded Artifact history read')):
            status,_,result=await self.lookup()
        self.assertEqual(status,200,result)
        self.assertEqual([i['artifact_id'] for i in result['items']],[artifact.id])
        self.assertTrue(result['complete'])

    async def test_empty_filtered_page_has_honest_continuation(self):
        # Thirty-three historical revoked derivatives are real candidates but
        # never visible. Their raw count must not become a false absence claim.
        original=self.f.evidence.store.load()
        artifact=next(a for a in original.artifacts if a.id==self.delivered['artifact_id'])
        hidden=tuple(artifact.model_copy(update={'id':'revoked-'+str(i),'lifecycle':'invalidated'}) for i in range(33))
        self.f.evidence.store.update(lambda state:state.model_copy(update={'artifacts':state.artifacts+list(hidden)}))
        status,_,result=await self.lookup()
        self.assertEqual(status,200,result)
        self.assertEqual(result['items'],[])
        self.assertTrue(result['truncated'])
        self.assertFalse(result['complete'])
        self.assertIsNotNone(result['next_cursor'])
        status,_,last=await self.lookup(cursor=result['next_cursor'])
        self.assertEqual(status,200,last)
        self.assertTrue(last['complete'])
        self.assertEqual([i['artifact_id'] for i in last['items']],[artifact.id])

    async def test_duplicate_candidate_is_not_silently_first_matched(self):
        artifact=self.f.evidence.get_artifact(self.delivered['artifact_id'],actor=self.actor)
        self.f.evidence.store.update(lambda state:state.model_copy(update={'artifacts':state.artifacts+[artifact]}))
        status,_,result=await self.lookup()
        self.assertEqual(status,409,result)
        self.assertNotIn(artifact.id,json.dumps(result))

    async def test_missing_policy_owner_is_not_empty_success(self):
        self.f.service.artifact_sharing_policies=None
        status,_,result=await self.lookup()
        self.assertEqual(status,403,result)

    async def test_profile_changed_after_lookup_refuses_stale_results(self):
        from codex_web.agent_profiles import AgentProfileUpdate
        original=self.f.evidence.shared_input_candidates
        def changed(*args,**kwargs):
            page=original(*args,**kwargs)
            self.f.agent_profiles.update(self.actual.agent_profile.profile_id,
                AgentProfileUpdate(description='Changed during metadata observation',reason='Private current Profile race'),actor=self.f.admin)
            return page
        with patch.object(self.f.evidence,'shared_input_candidates',side_effect=changed):
            status,_,result=await self.lookup()
        self.assertEqual(status,403,result)
        self.assertNotIn(self.delivered['artifact_id'],json.dumps(result))

    async def test_policy_ref_changed_during_lookup_refuses_stale_observation(self):
        original=self.f.evidence.shared_input_candidates
        def changed(*args,**kwargs):
            page=original(*args,**kwargs)
            self.s.publish_policy()
            return page
        with patch.object(self.f.evidence,'shared_input_candidates',side_effect=changed):
            status,_,result=await self.lookup()
        self.assertEqual(status,403,result)
        self.assertNotIn(self.delivered['artifact_id'],json.dumps(result))

    async def test_oversized_pair_is_not_disclosed(self):
        artifact=self.f.evidence.get_artifact(self.delivered['artifact_id'],actor=self.actor)
        changed=artifact.model_copy(update={'content':artifact.content.model_copy(update={'size_bytes':262145})})
        self.f.evidence.store.update(lambda state:state.model_copy(update={'artifacts':[
            changed if item.id==artifact.id else item for item in state.artifacts]}))
        status,_,result=await self.lookup()
        self.assertEqual(status,200,result)
        self.assertEqual(result['items'],[])
        self.assertTrue(result['complete'])
