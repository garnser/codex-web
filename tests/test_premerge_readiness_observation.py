"""Published procedure + canonical intent/receipt/Evidence; only private mock GETs."""
import copy
import json
import unittest
from unittest.mock import patch

import httpx

from codex_web.action_providers import ActionProviderBindingCreate
from codex_web.resources import ResourceAlias, ResourceUpdate
from codex_web.integrations.gitlab_client import GitLabClient
from codex_web.services.gitlab_action_provider import GitLabActionProvider
from codex_web.services.code_host_action_contract import CODE_HOST_PULL_REQUEST_OBSERVE_READINESS_ACTION_ID as ACTION
from codex_web.services.code_host_readiness_observation import VERSION, observation_outputs
from tests import test_procedure_execution as canonical
from tests.test_code_host_mr_observation import fact


class PremergeObservationProtocolTests(unittest.TestCase):
    def observe(self, value):
        raw = {'observation_version': VERSION, 'credential_metadata_sha256':'d'*64, 'credential_rotation':0, 'pull_request_number': 7,
            'expected_head_sha': 'a'*40, 'merge_requirements': value.model_dump(mode='json')}
        return observation_outputs(raw, binding_id='binding', resource_id='repo',
            provider_instance='example', parameters={'pull_request_number':7,'expected_head_sha':'a'*40}, observed_at=1001)

    def test_complete_three_passes_do_not_erase_actual_blockers_or_draft_state(self):
        from codex_web.code_host_merge_requirements import MergeFactSource
        positive = fact(provider_type='gitlab', pull_request_state='OPENED', complete=True, mergeable='MERGEABLE', merge_state='mergeable',
            sources=[dict(source=x.value, availability='complete', provider_source=x.value, observed_at=1000) for x in MergeFactSource],
            requirements_fingerprint='c'*64, strict_checks_required=False,
            reviews=dict(approving_count=1, code_owner_required=False, last_push_approval_required=False, dismiss_stale_approvals=False),
            conversation_resolution_required=True, conversation_count=0, unresolved_conversation_count=0)
        self.assertEqual(self.observe(positive)['readiness'], 'requirements_observed_satisfied')
        for changes in ({'blockers':('unresolved_conversations',)}, {'draft':True},
                        {'pull_request_state':'CLOSED'}, {'merge_state':'blocked'}):
            with self.subTest(changes=changes):
                result = self.observe(positive.model_copy(update=changes))
                self.assertNotEqual(result['readiness'], 'requirements_observed_satisfied')
                self.assertEqual(result['merge_policy_snapshot'], 'pass')

    def test_only_bounded_exact_mr_head_parameters_are_accepted(self):
        from types import SimpleNamespace
        from codex_web.services.code_host_action_contract import CodeHostActionContract
        valid={'pull_request_number':7,'expected_head_sha':'a'*40}
        for value in ({**valid,'url':'https://caller.invalid'}, {**valid,'independent':True},
                      {**valid,'producer_id':'caller'}, {**valid,'query':'caller'},
                      {**valid,'pull_request_number':True}, {**valid,'pull_request_number':0},
                      {**valid,'pull_request_number':2**31}, {**valid,'expected_head_sha':'A'*40}):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    CodeHostActionContract.pull_request_observe_readiness(SimpleNamespace(parameters=value))
        self.assertEqual(CodeHostActionContract.pull_request_observe_readiness(SimpleNamespace(parameters=valid)),valid)

    def test_scope_head_changed_and_expiry_fail_closed(self):
        for changes in ({'resource_id':'other'}, {'binding_id':'other'}, {'provider_instance':'other'},
                        {'head_revision':'b'*40}, {'changed':True}, {'expires_at':1001}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):self.observe(fact(provider_type='gitlab', **changes))


class PremergeReadinessProcedureTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = canonical.ProcedureExecutionTests.asyncSetUp
    tearDown = canonical.ProcedureExecutionTests.tearDown
    artifact = canonical.ProcedureExecutionTests.artifact
    proof = canonical.ProcedureExecutionTests.proof
    rebuild = canonical.ProcedureExecutionTests.rebuild
    advance = canonical.ProcedureExecutionTests.advance
    configure_project = canonical.ProcedureExecutionTests.configure_project
    skill_create_options = canonical.ProcedureExecutionTests.skill_create_options

    def assignment_options(self):
        return {'repository_scope':{'organization_id':self.actor.organization_id,
            'workspace_id':self.actor.workspace_id,'project_id':self.project.id,
            'writable_repository_ids':[self.repo.id],'source':'explicit'}}

    def configure(self, ignored):
        self.repo = self.resources.update(self.repo.id,ResourceUpdate(aliases=[*self.repo.aliases,
            ResourceAlias(namespace='gitlab-project',provider='gitlab',value='group/private')]),actor=self.actor)
        self.observed_calls = []; self.hook = None; self.approvals_status = None
        self.mr = dict(iid=33,project_id=42,sha='a'*40,target_branch='main',state='opened',draft=False,
            detailed_merge_status='mergeable',author={'id':1},blocking_discussions_resolved=True,
            head_pipeline={'id':9,'sha':'a'*40,'status':'success'})
        self.approvals_fact = dict(approved=False, approved_by=[],approvals_required=1,approvals_left=1)
        self.client = GitLabClient(transport=httpx.MockTransport(self.respond))
        self.provider = GitLabActionProvider(self.resources,self.client,
            provider_instance='gitlab.example',api_base='https://gitlab.example/api/v4')
        self.registry.register(self.provider,tenant_scope=self.actor.tenant)
        self.binding=self.registry.bind(ActionProviderBindingCreate(provider_type='gitlab',provider_instance='gitlab.example',
            project_id=self.project.id,resource_ids=self.assignment.resource_ids,credential_ref=self.secret_fixture_ref,
            security_policy={'network':{'enabled':True,'allowed_schemes':['https'],
                'allowed_hosts':['gitlab.example'],'allowed_ports':[443]}}), actor=self.actor,resources=self.resources)
        self.snapshot=self.artifact(json.dumps({'pull_request_number':33,'expected_head_sha':'a'*40}).encode(),'exact-premerge-input')
        def field(n,t):return {'name':n,'type':t,'description':'Exact typed provider observation, not consent'}
        inputs=[field('pull_request_number','integer'),field('expected_head_sha','string')]
        outputs=[field(n,t)for n,t in [('pull_request_number','integer'),('head_sha','string'),('observation_succeeded','boolean'),
            ('readiness','string'),('independent_review','string'),('exact_head_ci','string'),('merge_policy_snapshot','string'),
            ('blockers_json','string'),('pull_request_state','string'),('draft','boolean'),('mergeable','string'),
            ('merge_state','string'),('observed_at','number'),('expires_at','number')]]
        return {'schema_version':'1.0','owner_identity_id':self.actor.identity_id,
            'task_triggers':['exact-premerge-observation'],'operation_triggers':[ACTION],
            'resource_selectors':[{'id':'repository','resource_types':['repository'],'resource_ids':[self.repo.id]}],
            'environments':['staging'],'inputs':inputs,'reviewed_at':1,'review_due_at':4102444800,'expires_at':4102444801,
            'required_provider_capabilities':[],'required_worker_capabilities':[],'conflict_group':'private-premerge',
            'precedence':1,'max_duration_seconds':120,'steps':[{'id':'observe','kind':'diagnostic',
                'description':'Observe unmerged exact MR with honest pending facts','inputs':inputs,'outputs':outputs,
                'preconditions':[],'expected_evidence':[{'name':n,'kind':k,'description':'Original canonical proof'}
                    for n,k in [('receipt','provider_receipt'),('verification','verification')]],
                'timeout_seconds':30,'max_attempts':1,'retry_backoff_seconds':0,
                'permissions':[{'capability':'repository.read.metadata','level':'read','resource_selector':'repository'}],
                'failure':{'outcome':'escalate','escalation_owner_identity_id':self.actor.identity_id,
                    'attention_reason':'Inspect original observation; never fabricate QA or replay unknown result'},
                'action':{'provider_type':'gitlab','provider_instance':'gitlab.example','action_id':ACTION,
                    'provider_contract_version':'1.2','resource_selector':'repository'}}]}

    def respond(self, request):
        self.assertEqual(request.method,'GET')
        self.assertEqual(request.url.host,'gitlab.example')
        self.assertEqual(request.headers['PRIVATE-TOKEN'],self.expected_fixture_credential)
        self.observed_calls.append(request.url.path)
        path=request.url.path
        if path.endswith('/approvals'):
            if self.approvals_status:return httpx.Response(self.approvals_status,json={'message':'PRIVATE-UNTRUSTED'})
            value=self.approvals_fact
        elif path.endswith('/merge_requests/33'):value=self.mr
        elif path.endswith('/repository/branches/main'):value={'name':'main','commit':{'id':'b'*40}}
        elif path.endswith('/projects/group/private') or path.endswith('/projects/group%2Fprivate'):
            value={'id':42,'path_with_namespace':'group/private'}
        else:raise AssertionError('unexpected fixed observation route: '+path)
        value=copy.deepcopy(value)
        if self.hook:self.hook()
        return httpx.Response(200,json=value)

    async def test_secret_reference_unmerged_pending_procedure_retains_original_receipt_and_verification(self):
        result=await self.advance()
        step=result.steps['observe']
        self.assertEqual(step.status,'succeeded', (result.failure_code, self.intents.get(step.action_intent_id,self.actor).last_error if step.action_intent_id else None))
        outputs=self.runner._output_values(self.assignment,step,self.actor)
        self.assertIs(outputs['observation_succeeded'],True)
        self.assertEqual(outputs['readiness'],'blocked')
        self.assertEqual(outputs['independent_review'],'fail')
        self.assertEqual(outputs['exact_head_ci'],'pass')
        self.assertEqual(outputs['merge_policy_snapshot'],'warn')
        self.assertEqual(outputs['pull_request_state'],'OPENED')
        self.assertIn('provider_review_gate_not_approved',json.loads(outputs['blockers_json']))
        intent=self.intents.get(step.action_intent_id,self.actor);history=self.intents.store.load(intent_id=intent.id)
        self.assertEqual(len(history.receipts),1)
        self.assertTrue(intent.last_verification_id)
        for name in ('receipt','verification'):
            evidence=self.artifacts.get_evidence(step.evidence[name],actor=self.actor)
            self.assertEqual(evidence.source,'action-intent:'+intent.id)
            self.assertEqual(evidence.external_id,intent.last_receipt_id)
            self.assertEqual(evidence.metadata['verification_method'],'code-host-readiness-observation.v1')
            self.assertEqual(evidence.metadata['observation_expires_at'],outputs['expires_at'])
            self.assertIn(evidence.evidence_type,('provider_receipt','artifact_verification'))
        self.assertEqual(len(self.observed_calls),14)
        self.assertEqual(self.mr['state'],'opened')
        self.assertFalse(hasattr(self.client,'mutations'))
        finished=await self.advance();self.assertEqual(finished.status,'completed')
        self.runner.models.invoke.assert_not_called()

    async def test_secret_reference_changed_head_does_not_complete_or_retry(self):
        self.mr['sha']='c'*40
        result=await self.advance();self.assertNotEqual(result.steps['observe'].status,'succeeded')
        count=len(self.observed_calls)
        await self.advance();self.assertEqual(len(self.observed_calls),count)
        self.assertEqual(self.mr['state'],'opened')

    async def test_secret_reference_original_receipt_kinds_retained_but_expired_observation_not_current(self):
        from codex_web.services.procedure_execution import ProcedureExecutionError
        result = await self.advance()
        step = result.steps['observe']
        self.assertEqual(step.status, 'succeeded', result.failure_code)
        original = {name: self.artifacts.get_evidence(step.evidence[name], actor=self.actor)
            for name in ('receipt', 'verification')}
        self.assertEqual(original['receipt'].evidence_type, 'provider_receipt')
        self.assertEqual(original['verification'].evidence_type, 'artifact_verification')
        expiry = original['receipt'].metadata['observation_expires_at']
        for proof in original.values():
            self.assertEqual(proof.metadata['verification_method'], 'code-host-readiness-observation.v1')
            self.assertEqual(proof.metadata['observation_expires_at'], expiry)
            self.assertIsNone(proof.retention_expires_at)
            self.runner._evidence(proof.id, self.actor, self.assignment)
        count = len(self.observed_calls)
        with patch.object(self.runner, 'clock', return_value=expiry):
            for proof in original.values():
                with self.assertRaises(ProcedureExecutionError):
                    self.runner._evidence(proof.id, self.actor, self.assignment)
        self.assertEqual(len(self.observed_calls), count)
        for name, proof in original.items():
            self.assertEqual(self.artifacts.get_evidence(proof.id, actor=self.actor), proof)

    async def test_secret_reference_unknown_review_is_observed_not_passed(self):
        self.approvals_status=403
        result=await self.advance();step=result.steps['observe']
        self.assertEqual(step.status,'succeeded', (result.failure_code, self.intents.get(step.action_intent_id,self.actor).last_error if step.action_intent_id else None))
        outputs=self.runner._output_values(self.assignment,step,self.actor)
        self.assertEqual(outputs['independent_review'],'warn')
        self.assertEqual(outputs['readiness'],'blocked')
        self.assertNotIn('PRIVATE-UNTRUSTED',json.dumps(outputs))

    async def test_secret_reference_worker_revocation_stops_at_first_get_boundary(self):
        def revoke():
            self.hook=None
            self.workers.set_lifecycle(self.worker.id,'revoked',actor=self.actor,reason='Private authority negative')
        self.hook=revoke
        result=await self.advance();self.assertNotEqual(result.steps['observe'].status,'succeeded')
        self.assertEqual(len(self.observed_calls),1)

    async def test_secret_reference_malformed_scope_cannot_make_evidence(self):
        self.mr['project_id']=999
        result=await self.advance();step=result.steps['observe']
        self.assertNotEqual(step.status,'succeeded')
        self.assertFalse(step.evidence)

    async def test_secret_reference_response_loss_never_replays_an_original_attempt(self):
        def lost():
            if len(self.observed_calls)==2:raise httpx.ReadError('PRIVATE-response-loss')
        self.hook=lost
        result=await self.advance();self.assertNotEqual(result.steps['observe'].status,'succeeded')
        count=len(self.observed_calls)
        await self.advance();self.assertEqual(len(self.observed_calls),count)
        self.assertEqual(self.mr['state'],'opened')

    async def test_secret_reference_revocation_expiry_and_rotation_stop_subsequent_gets(self):
        from codex_web.secrets import SecretRotate
        # Each method invocation is a separate real published procedure fixture.
        def revoke():
            self.hook=None
            self.secret_broker.revoke(self.secret_fixture_ref,actor=self.actor,reason='Private lifecycle negative')
        self.hook=revoke
        result=await self.advance()
        self.assertNotEqual(result.steps['observe'].status,'succeeded')
        self.assertEqual(len(self.observed_calls),1)
        from codex_web.services.secrets import current_async_use_reference
        self.assertIsNone(current_async_use_reference())

    async def test_secret_reference_expiry_at_get_boundary_is_not_an_active_metadata_read(self):
        import time
        def expire():
            self.hook=None
            self.secret_broker.store.update(lambda state:state.model_copy(update={
                'references':[r.model_copy(update={'expires_at':time.time()-1}) if r.id==self.secret_fixture_ref else r
                    for r in state.references]}))
        self.hook=expire
        result=await self.advance()
        self.assertNotEqual(result.steps['observe'].status,'succeeded')
        self.assertEqual(len(self.observed_calls),1)

    async def test_secret_reference_rotation_at_get_boundary_stops_old_borrow(self):
        from codex_web.secrets import SecretRotate
        def rotate():
            self.hook=None
            self.secret_broker.rotate(self.secret_fixture_ref,SecretRotate(value='private-new-value'),actor=self.actor)
        self.hook=rotate
        result=await self.advance()
        self.assertNotEqual(result.steps['observe'].status,'succeeded')
        self.assertEqual(len(self.observed_calls),1)

    async def test_secret_reference_rotation_before_verification_cannot_attest_original_receipt(self):
        from codex_web.secrets import SecretRotate
        original=self.execution.verify
        async def rotated(*args,**kwargs):
            self.secret_broker.rotate(self.secret_fixture_ref,SecretRotate(value='private-new-value'),actor=self.actor)
            return await original(*args,**kwargs)
        with patch.object(self.execution,'verify',side_effect=rotated):
            result=await self.advance()
        step=result.steps['observe']
        self.assertNotEqual(step.status,'succeeded')
        self.assertEqual(len(self.observed_calls),7) # Only original observation; no GET with changed credential.
        intent=self.intents.get(step.action_intent_id,self.actor)
        history=self.intents.store.load(intent_id=intent.id)
        self.assertEqual(len(history.receipts),1)
        self.assertEqual(history.receipts[0].outcome,'completed')
        self.assertFalse(step.evidence)

    async def test_secret_reference_binding_owned_credential_is_required_before_any_get(self):
        from codex_web.action_providers import ActionRequest
        request=ActionRequest(action_id=ACTION, organization_id=self.actor.organization_id,
            workspace_id=self.actor.workspace_id, project_id=self.project.id,
            resource_ids=(self.repo.id,), parameters={'pull_request_number':33,'expected_head_sha':'a'*40},
            credential_ref=self.secret_fixture_ref)
        with self.assertRaises(ValueError):
            await self.provider.prepare(request,binding=self.binding.model_copy(update={'credential_ref':None}))
        self.assertEqual(self.observed_calls,[])

    async def test_secret_reference_rotation_before_consumer_rejects_actual_old_borrow(self):
        from codex_web.secrets import SecretRotate
        reference=self.secret_broker.metadata(self.secret_fixture_ref,actor=self.actor)
        backend=self.secret_broker._backend(reference)
        original=backend.get
        def rotated(identity):
            value=original(identity)
            self.secret_broker.rotate(identity,SecretRotate(value='private-rotated-before-consumer'),actor=self.actor)
            return value
        with patch.object(backend,'get',side_effect=rotated) as used:
            result=await self.advance()
        used.assert_called_once_with(self.secret_fixture_ref)
        self.assertNotEqual(result.steps['observe'].status,'succeeded')
        self.assertEqual(self.observed_calls,[])


class BorrowedSecretReferenceScopeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from tests import test_secrets_broker as fixtures
        from codex_web.secrets import SecretCreate
        self.f=fixtures.SecretBrokerTests()
        self.f.setUp();self.addCleanup(self.f.tearDown)
        self.broker,self.actor=self.f.broker,self.f.admin
        self.one=self.broker.create(SecretCreate(name='one',value='private-one'),actor=self.actor)
        self.two=self.broker.create(SecretCreate(name='two',value='private-two'),actor=self.actor)

    async def test_nested_parallel_consumers_keep_copies_and_reset_on_cancellation(self):
        import asyncio
        from codex_web.services.secrets import current_async_use_reference
        def observed():
            return current_async_use_reference().id
        async def nested(_):
            self.assertEqual(observed(),self.one.id)
            copy=current_async_use_reference();copy.rotation=999
            self.assertEqual(current_async_use_reference().rotation,0)
            async def inner(_):
                self.assertEqual(observed(),self.two.id)
                return True
            self.assertTrue(await self.broker.use_async(self.two.id,actor=self.actor,operation='private',consumer=inner))
            self.assertEqual(observed(),self.one.id)
            return True
        self.assertIsNone(current_async_use_reference())
        self.assertTrue(await self.broker.use_async(self.one.id,actor=self.actor,operation='private',consumer=nested))
        self.assertIsNone(current_async_use_reference())
        barrier=asyncio.Barrier(2)
        async def parallel(reference):
            async def consume(_):
                await barrier.wait()
                self.assertEqual(observed(),reference.id)
                return True
            return await self.broker.use_async(reference.id,actor=self.actor,operation='private',consumer=consume)
        self.assertEqual(await asyncio.gather(parallel(self.one),parallel(self.two)),[True,True])
        self.assertIsNone(current_async_use_reference())
        failure=asyncio.CancelledError('private cancellation')
        async def cancelled(_):
            self.assertEqual(observed(),self.one.id)
            raise failure
        with self.assertRaises(asyncio.CancelledError) as caught:
            await self.broker.use_async(self.one.id,actor=self.actor,operation='private',consumer=cancelled)
        self.assertIs(caught.exception,failure)
        self.assertIsNone(current_async_use_reference())
