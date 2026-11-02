"""Actual original receipt -> interpreter scalar/Evidence boundary, no provider I/O.

Reuse real private SQLite/Identity/Resource/ActionIntent/Evidence owners. Only
GitLab transport and the prerequisite validator's current fact result are fixture
edges. A PASS observation receipt never means readiness or merge authority.
"""
import copy
import unittest
from types import SimpleNamespace as NS

from codex_web.action_intents import ActionIntentCreate, ActionIntentClaimRequest, ActionDecisionSnapshot
from codex_web.procedure_execution import ProcedureStepCheckpoint
from codex_web.services.code_host_ci_observation import CI_OBSERVATION_ACTION_ID, PrerequisiteResult
from codex_web.services.procedure_execution import ProcedureExecutionError
from codex_web.services.procedure_provider_results import CodeHostProcedureResultAdapter
from tests.test_code_host_ci_observation import policy_ref
from tests.test_procedure_code_host_results import ProcedureCodeHostResultTests


HEAD = 'a' * 40


class CIProcedureProjectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = ProcedureCodeHostResultTests(methodName='test_canonical_original_receipt_and_actual_verification_become_distinct_evidence')
        await self.f.asyncSetUp()
        self.addAsyncCleanup(self.f.asyncTearDown)
        f = self.f
        f.step.id = 'observe-ci'
        f.step.action.action_id = CI_OBSERVATION_ACTION_ID
        fields = (('head_sha','string'),('pull_request_number','integer'),('current_pipeline_id','string'),
            ('ci_disposition','string'),('readiness','string'),('next_owner_identity_id','string'),
            ('prerequisite_reasons_json','string'),('pipeline_ids_json','string'),
            ('privileged_operation_authorized','boolean'),('execution_target_kind','string'),
            ('external_ci_runtime_attested','boolean'),('execution_target_requirement_ref_json','string'))
        f.step.outputs = [NS(name=name,type=kind,required=name!='execution_target_kind') for name,kind in fields]
        async def read(base, path, *, token, params=None):
            if '/merge_requests/' in path:
                return dict(iid=57,sha=HEAD,project_id=9,head_pipeline=dict(id=7,sha=HEAD))
            if path == 'projects/group%2Fplatform%2Fcodex-web':
                return dict(id=9)
            if path == 'projects/9/pipelines':
                return [dict(id=7,sha=HEAD)]
            if path.endswith('/bridges'):
                return []
            if path.endswith('/jobs'):
                return [dict(id=11,name='unit',status='success',commit=dict(id=HEAD),
                    pipeline=dict(id=7,sha=HEAD,project_id=9))]
            return dict(id=7,project_id=9,sha=HEAD,source='merge_request_event',status='success',ref='feature')
        f.client.read_json_bounded = read

    async def original(self, prerequisite=None):
        f = self.f
        if prerequisite is not None:
            f.provider.ci_prerequisite_validator = lambda request,binding:prerequisite
        req = f._request(CI_OBSERVATION_ACTION_ID,dict(pull_request_number=57,expected_head_sha=HEAD,
            environment='development',change_classification='shared-surface',
            work_item_ref='group/platform/codex-web!57',target_resource_id='actual-target-unavailable'))
        req.idempotency_key = f.runner._step_origin(f.assignment,f.step.id,intent=True)
        req.project_id = f.assignment.project_id
        intent = f.intents.create(ActionIntentCreate(binding_id=f.binding.id,request=req,
            execution_id=f.assignment.execution_id,
            policy_decision=ActionDecisionSnapshot(outcome='allow',source='private-ci-read-only-fixture')),
            actor=f.actor)
        f.intents.claim(ActionIntentClaimRequest(worker_id='worker-1',lease_seconds=30),intent_id=intent.id,actor=f.worker)
        intent = await f.intents.execute_claimed(intent.id,'worker-1',actor=f.worker)
        self.assertEqual(intent.status,'succeeded',intent.last_error)
        history = f.intents.store.load(intent_id=intent.id)
        receipt = next(r for r in history.receipts if r.id==intent.last_receipt_id)
        return intent,receipt,history

    def project(self, intent, receipt, history):
        f = self.f
        return f.runner._provider_result(f.assignment,f.step,intent,receipt,history,f.actor)

    async def test_missing_target_needs_input_is_valid_optional_scalar_without_ready_or_grant(self):
        f = self.f
        intent,receipt,history = await self.original()
        original = copy.deepcopy(receipt.result.output)
        self.assertIsNone(original['prerequisites']['execution_target_kind'])
        error = None
        try:
            result = self.project(intent,receipt,history)
        except ProcedureExecutionError as exc:
            error = str(exc)
        self.assertIsNone(error,error)
        self.assertNotIn('execution_target_kind',result['outputs'])
        self.assertEqual(result['outputs']['readiness'],'needs_input')
        self.assertFalse(result['outputs']['privileged_operation_authorized'])
        self.assertFalse(result['outputs']['external_ci_runtime_attested'])
        self.assertEqual(result['outputs']['execution_target_requirement_ref_json'],'null')
        # These PASS records prove only the SAME successful read and provider
        # verification. The typed outputs explicitly preserve missing readiness.
        for reference in result['evidence'].values():
            proof = f.evidence.get_evidence(reference,actor=f.actor)
            self.assertEqual(proof.source,'action-intent:'+intent.id)
            self.assertEqual(proof.metadata['action_id'],CI_OBSERVATION_ACTION_ID)
        state = ProcedureStepCheckpoint(attempts=1,action_intent_id=intent.id,
            started_at=receipt.result.started_at,evidence=result['evidence'])
        f.runner._record_outputs(f.assignment,f.step,state,result['outputs'],f.actor)
        f.runner._proof(f.assignment,f.step,state,f.actor)
        self.assertEqual(f.runner._output_values(f.assignment,state,f.actor),result['outputs'])
        self.assertEqual(receipt.result.output,original)
        current = f.intents.store.load(intent_id=intent.id)
        self.assertEqual(len(current.receipts),1)
        self.assertEqual(current.receipts[0].result.output,original)
        self.assertEqual(f.client.merge_request_merges,0)

    async def test_blocked_prerequisite_without_target_remains_blocked_not_malformed(self):
        intent,receipt,history = await self.original(PrerequisiteResult(disposition='blocked',
            reasons=('canonical_dependency_incompatible',),next_owner_identity_id=self.f.actor.identity_id))
        result = self.project(intent,receipt,history)
        self.assertEqual(result['outputs']['readiness'],'blocked')
        self.assertNotIn('execution_target_kind',result['outputs'])
        self.assertFalse(result['outputs']['privileged_operation_authorized'])
        self.assertIsNone(receipt.result.output['prerequisites']['execution_target_kind'])

    async def test_verified_explicit_target_remains_present_with_no_external_runtime_claim(self):
        prerequisite = PrerequisiteResult(disposition='verified',
            verification_ids=tuple('verification-'+x*32 for x in 'abc'),
            next_owner_identity_id=self.f.actor.identity_id,execution_target_kind='canonical-native-assignment',
            execution_target_requirement_ref=policy_ref())
        intent,receipt,history = await self.original(prerequisite)
        result = self.project(intent,receipt,history)
        self.assertEqual(result['outputs']['execution_target_kind'],'canonical-native-assignment')
        self.assertEqual(result['outputs']['readiness'],'ready_for_validation')
        self.assertFalse(result['outputs']['privileged_operation_authorized'])
        self.assertFalse(result['outputs']['external_ci_runtime_attested'])

    async def test_malformed_present_target_type_still_refuses_projection(self):
        intent,receipt,history = await self.original()
        raw=copy.deepcopy(receipt.result.output)
        for value in (True,7,{},[], 'unregistered-target'):
            output=copy.deepcopy(raw)
            output['prerequisites']['execution_target_kind']=value
            result=receipt.result.model_copy(update={'output':output})
            with self.subTest(value=value),self.assertRaises(ValueError):
                CodeHostProcedureResultAdapter(provider_instance=intent.provider_instance).project(intent.request,result)

    async def test_present_null_is_not_a_new_nullable_engine_type(self):
        with self.assertRaisesRegex(ProcedureExecutionError,'procedure_value_type_mismatch'):
            self.f.runner._values(self.f.step.outputs,{
                **{v.name:False if v.type=='boolean' else 1 if v.type=='integer' else 'fixture' for v in self.f.step.outputs},
                'execution_target_kind':None},self.f.actor,self.f.assignment)
