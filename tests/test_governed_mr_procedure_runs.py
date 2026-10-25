"""Actual broker-derived input and published procedure on one private assignment.

Only the external provider HTTP transport is synthetic. No model/native/provider
mutation or human startup is used to continue the assignment.
"""
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from tests import test_worker_mr_continuation_inputs as inputs
from tests import test_premerge_readiness_observation as readiness
from codex_web.action_providers import ActionProviderBindingCreate
from codex_web.resources import ResourceAlias, ResourceUpdate
from codex_web.secrets import SecretCreate
from codex_web.services.code_hosts import CodeHostRegistry, CodeHostService
from codex_web.services.gitlab_code_host import GitLabCodeHostProvider
from codex_web.services.gitlab_action_provider import GitLabActionProvider
from codex_web.integrations.gitlab_client import GitLabClient
from codex_web.services.code_host_action_contract import CODE_HOST_PULL_REQUEST_OBSERVE_READINESS_ACTION_ID as ACTION
from codex_web.services.procedure_execution import ProcedureExecutionService, ProcedureExecutionError
from codex_web.services.procedure_discovery import ProcedureDiscoveryService, ProcedureExecutionCapabilityService
from codex_web.services.skills import SkillService
from codex_web.services.skill_scanners import SkillScannerRegistry
from codex_web.services.skill_security import SkillSecurityService
from codex_web.storage.skill_security import SkillSecurityStore
from codex_web.skills import SkillCreate, SkillPublish
from codex_web.definitions import DefinitionReference
from tests.test_skill_security import FakeScanner


class GovernedMRProcedureRunTests(inputs.MRContinuationInputTests):
    respond = readiness.PremergeReadinessProcedureTests.respond

    def _publish_profile(self,payload,version,project=None):
        for profile in payload['profiles']:
            if profile['id']=='repository-write':
                profile['control_plane_operations']=list(dict.fromkeys([*profile['control_plane_operations'],
                    'procedure.read','procedure.start','procedure.advance']))
        super()._publish_profile(payload,version,project)

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.broker_response_timeout=20  # Private socket client, below 30s step bound.
        self.service.limits=self.service.limits.model_copy(update={'max_requests_per_minute':24})  # Default production bound is60.
        self.target=self.resources.update(self.target.id,ResourceUpdate(
            aliases=(ResourceAlias(namespace='gitlab-project',provider='gitlab',value='group/private'),),
            operational_context={'environment':'staging'}),actor=self.admin)
        self.observed_calls=[];self.hook=None;self.approvals_status=None
        self.expected_fixture_credential='private-governed-read-credential'
        self.mr=dict(iid=33,project_id=42,sha='a'*40,target_branch='main',state='opened',draft=False,
            detailed_merge_status='mergeable',author={'id':1},blocking_discussions_resolved=True,
            head_pipeline={'id':9,'sha':'a'*40,'status':'success'})
        self.approvals_fact=dict(approved=False,approved_by=[],approvals_required=1,approvals_left=1)
        client=GitLabClient(transport=httpx.MockTransport(self.respond))
        hosts=CodeHostRegistry();hosts.register_provider(GitLabCodeHostProvider(client))
        self.service.code_hosts=CodeHostService(hosts,self.resources,secrets=self.secret,identity=self.identity)
        registry=self.intents.execution.registry
        for binding in registry.list_bindings(self.admin):registry.set_enabled(binding.id,False,actor=self.admin)
        registry.register(GitLabActionProvider(self.resources,client,provider_instance='gitlab.example',
            api_base='https://gitlab.example/api/v4'),tenant_scope=self.admin.tenant)
        credential=self.secret.create(SecretCreate(name='Private governed procedure read',
            value=self.expected_fixture_credential,provider='gitlab',purpose='code-host',
            allowed_identity_ids=[self.worker_actor.identity_id,self.admin.identity_id]),actor=self.admin)
        registry.bind(ActionProviderBindingCreate(provider_type='gitlab',provider_instance='gitlab.example',
            project_id='project-a',resource_ids=(self.target.id,),credential_ref=credential.id,
            security_policy={'network':{'enabled':True,'allowed_schemes':['https'],
                'allowed_hosts':['gitlab.example'],'allowed_ports':[443]}}),actor=self.admin,resources=self.resources)
        self.request.update(pull_request_number=33,expected_head_sha='a'*40)
        # Reuse the fixture's already-bound canonical Skill owner.
        self.skills.bind_procedure_contracts(actions=registry,resources=self.resources,scope_actor=lambda record:self.admin)
        scanners=SkillScannerRegistry();scanners.register(FakeScanner())
        self.skills.bind_security(SkillSecurityService(SkillSecurityStore(self.sqlite),self.registry,scanners))
        def field(name,kind):return {'name':name,'type':kind,'description':'Canonical provider observation, not consent'}
        action_inputs=[field('pull_request_number','integer'),field('expected_head_sha','string')]
        outputs=[field(n,k) for n,k in [('pull_request_number','integer'),('head_sha','string'),
            ('observation_succeeded','boolean'),('readiness','string'),('independent_review','string'),
            ('exact_head_ci','string'),('merge_policy_snapshot','string'),('blockers_json','string'),
            ('pull_request_state','string'),('draft','boolean'),('mergeable','string'),
            ('merge_state','string'),('observed_at','number'),('expires_at','number')]]
        procedure={'schema_version':'1.2','input_protocol':'code-host-mr-procedure-input.v1',
            'owner_identity_id':self.admin.identity_id,'task_triggers':['private-governed-observation'],
            'operation_triggers':[ACTION],'resource_selectors':[{'id':'repository','resource_types':['repository'],
                'resource_ids':[self.target.id]}],'environments':['staging'],
            'inputs':[*action_inputs,field('observed_requirements_fingerprint','string')],
            'reviewed_at':1,'review_due_at':4102444800,'expires_at':4102444801,
            'required_provider_capabilities':[],'required_worker_capabilities':[],
            'conflict_group':'private-governed-observation','precedence':1,'max_duration_seconds':120,
            'steps':[{'id':'observe','kind':'diagnostic','description':'Read actual MR with honest pending facts',
                'inputs':action_inputs,'outputs':outputs,'preconditions':[],
                'expected_evidence':[{'name':n,'kind':k,'description':'Original provider proof'}
                    for n,k in [('receipt','provider_receipt'),('verification','verification')]],
                'timeout_seconds':30,'max_attempts':1,'retry_backoff_seconds':0,
                'permissions':[{'capability':'repository.read.metadata','level':'read','resource_selector':'repository'}],
                'failure':{'outcome':'escalate','escalation_owner_identity_id':self.admin.identity_id,
                    'attention_reason':'Preserve original observations; no invented QA'},
                'action':{'provider_type':'gitlab','provider_instance':'gitlab.example','action_id':ACTION,
                    'provider_contract_version':'1.2','resource_selector':'repository'}}]}
        draft=self.skills.create(SkillCreate(skill_id='private-governed-readiness',name='Private governed readiness',
            instructions='Use actual governed observations; preserve unknown policy and current authority.',
            operational_procedure=procedure),actor=self.admin)
        published=self.skills.publish(draft['skillId'],draft['recordId'],SkillPublish(),actor=self.admin)
        self.pin=DefinitionReference.model_validate(published['definitionReference'])
        from codex_web.services.codex_auth_delegation import CODEX_AUTH_PURPOSE, CodexAuthDelegationService
        codex=self.secret.create(SecretCreate(name='Private never-launched session credential',
            value='private-no-provider-process-token',provider='codex',purpose=CODEX_AUTH_PURPOSE,
            allowed_identity_ids=[self.worker_actor.identity_id,self.admin.identity_id],
            expires_at=time.time()+60),actor=self.admin)
        def bind(state):
            state.assignments[0].skill_refs=(self.pin,)
            state.assignments[0].secret_refs=(credential.id,codex.id)
            return state
        self.workers.store.update_assignment(self.worker.id,self.assignment.id,bind)
        from codex_web.services.control_plane_broker import AssignmentBoundControlPlaneBroker
        await self.broker.stop()
        current=self.workers.store.assignment(self.assignment.id)
        from codex_web.services.agent_process_session import AssignmentBoundAgentProcessSession
        from codex_web.services.local_execution_worker import LocalExecutionWorkerRuntime
        delegated=CodexAuthDelegationService(self.secret)
        local=LocalExecutionWorkerRuntime(self.workers,self.workspaces,None,worker=self.worker,
            worker_actor=self.worker_actor,control_actor=self.admin,codex_auth_delegation=delegated)
        def never_launch(*args,**kwargs):raise AssertionError('No native/model process may start')
        session=AssignmentBoundAgentProcessSession(local,None,current.id,runtime_factory=never_launch,
            credential_provider=delegated)
        self.addCleanup(session._supervision_executor.shutdown,wait=True)
        self.addCleanup(session._diagnostic_executor.shutdown,wait=True)
        session.fence=current.fence
        session.delegation=delegated.issue(current,worker_id=self.worker.id,fence=current.fence,actor=self.worker_actor)
        self.broker=AssignmentBoundControlPlaneBroker(self.service,assignment=current,worker_id=self.worker.id,
            worker_actor=self.worker_actor,fence=current.fence,validator=session._validate_current_authority)
        await self.broker.start()
        self.discovery=ProcedureDiscoveryService(self.skills,self.resources)
        self.discovery.execution_capabilities=ProcedureExecutionCapabilityService(self.workers,None,None)
        from codex_web.services.canonical_events import CanonicalEventBus, CanonicalEventIngestionService
        from codex_web.storage.canonical_events import CanonicalEventStore
        from codex_web.services.approval_requests import ApprovalRequestService
        from codex_web.storage.approval_requests import ApprovalRequestStore
        from codex_web.services.attention import AttentionService
        from codex_web.storage.attention import AttentionStore
        events=CanonicalEventIngestionService(CanonicalEventBus(CanonicalEventStore(self.sqlite)))
        approvals=ApprovalRequestService(ApprovalRequestStore(self.sqlite),self.identity,events,authority_roles=self.authority)
        attention=AttentionService(AttentionStore(self.sqlite),events,identity=self.identity)
        self.runner=ProcedureExecutionService(workers=self.workers,skills=self.skills,discovery=self.discovery,
            intents=self.intents,evidence=self.evidence,identity=self.identity,authority=self.authority,
            approvals=approvals,attention=attention,control_actor=self.admin,worker_actor=self.worker_actor,
            models=SimpleNamespace(invoke=AsyncMock(side_effect=AssertionError('zero model calls'))))
        # Attribute to the real existing keyed Work state through the same
        # narrow host read contract; no Work-success/update callback is used.
        self.intents.work_item_host=SimpleNamespace(_load_work_item_states=self.items.load)
        self.intents.execution_workers=self.workers
        self.intents.procedure_discovery=self.discovery
        self.intents.procedure_execution=self.runner
        self.intents.execution.procedure_admission=self.intents.procedure_provider_admission
        from codex_web.services.assignment_reasoning_broker import AssignmentReasoningBroker
        self.service.assignment_reasoning=AssignmentReasoningBroker(self.service,procedures=self.runner,executives=None)

    async def start_snapshot(self,artifact_id):
        status,_,body=await self._request('POST',f'/api/execution-workers/assignments/{self.assignment.id}/procedure/start',
            payload={'definition':self.pin.model_dump(mode='json'),'input_snapshot_id':artifact_id})
        self.assertEqual(status,200,body)
        return self.workers.store.assignment(self.assignment.id)

    async def advance_run(self,run_id,*,witness=None):
        status,_,body=await self._request('POST',f'/api/execution-workers/assignments/{self.assignment.id}/procedure/advance',
            payload={'run_id':run_id,**({'current_input_snapshot_id':witness} if witness else {})})
        self.assertEqual(status,200,body)
        return self.workers.store.assignment(self.assignment.id)

    async def test_invalid_governed_start_is_409_without_checkpoint_or_provider_effect(self):
        result=await self.publish('b');artifact=result['procedure_input']
        before=self.workers.store.assignment(self.assignment.id).model_dump(mode='json')
        calls=len(self.observed_calls)
        def invalidate(state):
            proof=next(p for p in state.evidence if p.id==next(iter(result['evidence'].values()))['id'])
            proof.metadata['provider_snapshot_sha256']='0'*64
            return state
        self.evidence.store.update(invalidate)
        from codex_web.services.assignment_reasoning_broker import resolve
        path=f'/api/execution-workers/assignments/{self.assignment.id}/procedure/start'
        payload={'definition':self.pin.model_dump(mode='json'),'input_snapshot_id':artifact['id']}
        expected=self.service.procedure_admission.inspect_current(
            assignment=self.workers.store.assignment(self.assignment.id),actor=self.admin,
            operation=resolve('POST',path)[0],resource_ids=self.assignment.resource_ids,payload=payload)
        status,_,body=await self._request('POST',path,payload=payload)
        self.assertEqual(status,409,body)
        self.assertIn('procedure_governed_input_invalid',json.dumps(body))
        after=self.workers.store.assignment(self.assignment.id).model_dump(mode='json')
        # Preserve the exact append-only admission audit and its touch time;
        # every other assignment field must remain byte-for-byte equivalent.
        self.assertEqual(after['procedure_discoveries'][:-1],before['procedure_discoveries'])
        self.assertEqual(len(after['procedure_discoveries']),len(before['procedure_discoveries'])+1)
        discovery=after['procedure_discoveries'][-1]
        self.assertEqual({k:v for k,v in discovery.items() if k!='evaluated_at'},
            expected.model_dump(mode='json',exclude={'evaluated_at'}))
        self.assertGreaterEqual(after['updated_at'],before['updated_at'])
        self.assertGreaterEqual(after['updated_at'],discovery['evaluated_at'])
        audit_only={'procedure_discoveries','updated_at'}
        self.assertEqual({k:v for k,v in after.items() if k not in audit_only},
            {k:v for k,v in before.items() if k not in audit_only})
        self.assertEqual(len(self.observed_calls),calls)
        self.runner.models.invoke.assert_not_called()

    async def test_same_assignment_actual_publisher_start_advance_and_changed_same_head_run(self):
        first=await self.publish('b')
        artifact=first['procedure_input']
        started=await self.start_snapshot(artifact['id'])
        run_id=started.procedure_checkpoint.id
        for _ in range(2):
            current=await self.advance_run(run_id)
        self.assertEqual(current.procedure_checkpoint.status,'completed',current.procedure_checkpoint.failure_code)
        original=current.procedure_checkpoint.model_dump(mode='json')
        repeated=await self.publish('c')
        same=await self.start_snapshot(repeated['procedure_input']['id'])
        self.assertEqual(same.procedure_checkpoint.id,run_id)
        # Actual changed CI fact, same exact repository/head; not a new delivery
        # nonce or observation time masquerading as changed operational state.
        self.mr['head_pipeline']['status']='failed'
        changed=await self.publish('d')
        self.assertNotEqual(self.content(changed['procedure_input']['id']),self.content(artifact['id']))
        second=await self.start_snapshot(changed['procedure_input']['id'])
        self.assertNotEqual(second.procedure_checkpoint.id,run_id)
        self.assertEqual(second.procedure_history[0].model_dump(mode='json'),original)
        for field in ('id','execution_id','assigned_worker_id','fence','lease','limits','deadline_at'):
            self.assertEqual(getattr(second,field),getattr(started,field))
        for _ in range(2):
            final=await self.advance_run(second.procedure_checkpoint.id)
        self.assertEqual(final.procedure_checkpoint.status,'completed',final.procedure_checkpoint.failure_code)
        self.runner.models.invoke.assert_not_called()
        self.assertEqual(self.mr['state'],'opened')
        self.assertTrue(all('/merge' not in path or '/merge_requests/' in path for path in self.observed_calls))

    async def test_expired_and_changed_witness_refuse_then_fresh_equal_witness_advances_with_provenance(self):
        first=await self.publish('b');artifact=first['procedure_input']
        started=await self.start_snapshot(artifact['id']);pin=started.procedure_checkpoint
        original=pin.model_dump(mode='json')
        original_work=self.items.get(self.ref).model_dump(mode='json')
        self.mr['head_pipeline']['status']='failed'
        changed=await self.publish('c')
        calls=len(self.observed_calls)
        status,_,_=await self._request('POST',f'/api/execution-workers/assignments/{self.assignment.id}/procedure/advance',
            payload={'run_id':pin.id,'current_input_snapshot_id':changed['procedure_input']['id']})
        self.assertEqual(status,409)
        self.assertEqual(self.workers.store.assignment(self.assignment.id).procedure_checkpoint.model_dump(mode='json'),original)
        self.assertEqual(len(self.observed_calls),calls)
        self.mr['head_pipeline']['status']='success'
        expiry=next(iter(first['evidence'].values()))['metadata']['observation_expires_at']
        future=expiry+1
        from codex_web.services.gitlab_merge_requirements import GitLabMergeRequirementsReader
        constructor=GitLabMergeRequirementsReader.__init__
        def fresh_clock(reader,*args,**kwargs):
            return constructor(reader,*args,**{**kwargs,'clock':lambda:future})
        from codex_web.action_intents import ActionIntentReceipt, ActionIntentVerificationReceipt
        # Pydantic captured these two default factories before the private
        # clock patch. Inject only coherent timestamps into the REAL models;
        # actual provider execution, verification and persisted proofs stay real.
        def receipt_clock(**values):return ActionIntentReceipt(received_at=future,**values)
        def verification_clock(**values):return ActionIntentVerificationReceipt(verified_at=future,**values)
        with patch('time.time',return_value=future),patch.object(self.service,'_clock',return_value=future),patch.object(self.runner,'clock',return_value=future),patch.object(GitLabMergeRequirementsReader,'__init__',fresh_clock),patch('codex_web.services.action_intents.ActionIntentReceipt',receipt_clock),patch('codex_web.services.action_intents.ActionIntentVerificationReceipt',verification_clock):
            fresh=await self.publish('d');witness=fresh['procedure_input']
            self.assertEqual(self.content(witness['id']),self.content(artifact['id']))
            calls=len(self.observed_calls)
            status,_,_=await self._request('POST',f'/api/execution-workers/assignments/{self.assignment.id}/procedure/advance',
                payload={'run_id':pin.id,'current_input_snapshot_id':artifact['id']})
            self.assertEqual(status,409)
            self.assertEqual(self.workers.store.assignment(self.assignment.id).procedure_checkpoint.model_dump(mode='json'),original)
            self.assertEqual(len(self.observed_calls),calls)
            advanced=await self.advance_run(pin.id,witness=witness['id'])
            self.assertEqual(advanced.procedure_checkpoint.steps['observe'].status,'succeeded',advanced.procedure_checkpoint.failure_code)
            self.assertEqual(advanced.procedure_checkpoint.input_snapshot_id,artifact['id'])
            self.assertEqual(advanced.procedure_checkpoint.input_snapshot_checksum,pin.input_snapshot_checksum)
            state=advanced.procedure_checkpoint.steps['observe']
            intent=self.intents.get(state.action_intent_id,self.admin)
            self.assertIn('Current-use input witness Artifact: '+witness['id'],intent.policy_decision.reasons)
            output=self.evidence.get_artifact(state.output_snapshot_id,actor=self.admin)
            self.assertEqual(output.metadata['current_input_witness_id'],witness['id'])
            governed=self.evidence.governance.get_record(output.governance_record_id,self.admin,context_only=True)
            originals=[self.evidence.get_artifact(i,actor=self.admin).governance_record_id for i in (artifact['id'],witness['id'])]
            self.assertTrue(set(originals).issubset(governed.source_record_ids))
            completed=await self.advance_run(pin.id,witness=witness['id'])
            self.assertEqual(completed.procedure_checkpoint.status,'completed',completed.procedure_checkpoint.failure_code)
            terminal=completed.procedure_checkpoint.model_dump(mode='json')
            calls=len(self.observed_calls)
            repeated=await self.advance_run(pin.id,witness=changed['procedure_input']['id'])
            self.assertEqual(repeated.procedure_checkpoint.model_dump(mode='json'),terminal)
            self.assertEqual(len(self.observed_calls),calls)
        self.assertEqual(self.items.get(self.ref).model_dump(mode='json'),original_work)
        self.assertEqual(completed.procedure_checkpoint.id,pin.id)
        self.assertEqual(completed.procedure_checkpoint.input_snapshot_id,artifact['id'])
        self.assertEqual(completed.procedure_history,())
        self.runner.models.invoke.assert_not_called()


def load_tests(loader,tests,pattern):
    import unittest
    return unittest.TestSuite(GovernedMRProcedureRunTests(name) for name in GovernedMRProcedureRunTests.__dict__ if name.startswith('test_'))
