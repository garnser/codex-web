"""Private canonical interpreter fixture; registered external owner edge is controlled."""
import copy
import asyncio
import time
import unittest
from unittest.mock import patch
from pydantic import BaseModel, ConfigDict, ValidationError

from codex_web.procedure_control_plane import (
    CanonicalOperationContract, CanonicalOperationError, CanonicalOperationRegistry,
    CanonicalOperationResult,
)
from codex_web.skill_procedures import OperationalProcedure, ProcedureValueContract
from tests.test_skill_procedures import procedure
from tests import test_procedure_execution as execution_fixture


class EmptyInput(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)


OUTPUT = ProcedureValueContract(name='observed', type='boolean', description='Canonical owner observation')


class ControlledOwner:
    def __init__(self):
        self.calls = 0
        self.reconciliations = 0
        self.disposition = 'succeeded'
        self.contexts = []

    async def execute(self, context, value):
        context.validate_current()
        self.calls += 1
        self.contexts.append(context)
        return CanonicalOperationResult(disposition=self.disposition, outputs={'observed':True},
            facts={'owner_calls':self.calls}, original_references=('original-fixture-record',))

    async def reconcile(self, context, value):
        context.validate_current()
        self.reconciliations += 1
        return CanonicalOperationResult(disposition=self.disposition, outputs={'observed':True},
            facts={'original_retained':True})


def contract(effect='read'):
    return CanonicalOperationContract(operation_id='fixture.canonical.v1', contract_version='1.0',
        resource_types=('service',), required_capability='service.read' if effect == 'read' else 'service.change',
        authority_level='read' if effect == 'read' else 'execute', input_model=EmptyInput,
        output_contracts=(OUTPUT,), effect=effect, timeout_seconds=10)


def content(effect='read'):
    data = procedure()
    step = copy.deepcopy(data['steps'][0])
    step.update(id='canonical', kind='diagnostic' if effect == 'read' else 'mutation',
        inputs=[], outputs=[OUTPUT.model_dump(mode='json')],
        expected_evidence=[{'name':'observation','kind':'verification','description':'Exact canonical step observation'}],
        control_plane={'operation_id':'fixture.canonical.v1','contract_version':'1.0','resource_selector':'service'})
    step.pop('action')
    step['permissions'] = [{'capability':'service.read' if effect == 'read' else 'service.change',
        'level':'read' if effect == 'read' else 'execute','resource_selector':'service'}]
    steps = [step]
    if effect == 'mutation':
        step['impact'] = {'resource_selector':'service','change':'configuration',
            'description':'Existing canonical owner transition','max_unavailable_seconds':0,'reversible':False}
        step['verification_step_id'] = 'verify'
        verify = copy.deepcopy(step)
        verify.update(id='verify',kind='verification',preconditions=[{'source':'step','name':'canonical','predicate':'succeeded'}])
        verify.pop('impact'); verify.pop('verification_step_id')
        verify['control_plane']['operation_id'] = 'fixture.verify.v1'
        verify['permissions'] = [{'capability':'service.read','level':'read','resource_selector':'service'}]
        steps.append(verify)
    data.update(schema_version='1.4', inputs=[], service_versions=[], required_worker_capabilities=[], steps=steps,
        reviewed_at=time.time()-1,review_due_at=time.time()+1000,expires_at=time.time()+2000)
    return data


class CanonicalProcedureSchemaTests(unittest.TestCase):
    def setUp(self):
        self.owner = ControlledOwner()
        self.registry = CanonicalOperationRegistry()
        self.registry.register(contract(), self.owner)

    def test_versioned_canonical_step_is_a_real_supported_contract(self):
        refusal = None
        try:
            parsed = OperationalProcedure.model_validate(content())
        except ValidationError as exc:
            refusal = str(exc)
        self.assertIsNone(refusal, refusal)
        self.assertIs(self.registry.validate_step(parsed, parsed.steps[0]).adapter, self.owner)

    def test_old_schema_rejects_new_arm_even_explicit_null(self):
        for version in ('1.0','1.1','1.2','1.3'):
            for null in (False,True):
                with self.subTest(version=version,null=null):
                    data = content(); data['schema_version'] = version
                    if null:
                        data['steps'][0]['action'] = procedure()['steps'][0]['action']
                        data['steps'][0]['control_plane'] = None
                    with self.assertRaises(ValidationError): OperationalProcedure.model_validate(data)

    def test_ordinary_old_payload_does_not_gain_new_serialized_fields(self):
        parsed = OperationalProcedure.model_validate(procedure()).model_dump(mode='json')
        for step in parsed['steps']:
            self.assertNotIn('control_plane', step)
            self.assertNotIn('escalation_owner_policy', step['failure'])

    def test_operation_metadata_rejects_scope_permission_bound_and_output_drift(self):
        for variant in ('operation','permission','scope','widened_scope','bounds','outputs'):
            with self.subTest(variant=variant):
                data=content(); step=data['steps'][0]
                if variant=='operation': step['control_plane']['operation_id']='fixture.other.v1'
                if variant=='permission': step['permissions'][0]['capability']='arbitrary.grant'
                if variant=='scope': data['resource_selectors'][0]['resource_types']=['repository']
                if variant=='widened_scope': data['resource_selectors'][0]['resource_types']=['service','database']
                if variant=='bounds': step['timeout_seconds']=11
                if variant=='outputs': step['outputs'][0]['description']='Different contract'
                parsed=OperationalProcedure.model_validate(data)
                with self.assertRaises(CanonicalOperationError): self.registry.validate_step(parsed,parsed.steps[0])

    def test_definition_cannot_choose_actor_url_or_raw_arguments(self):
        for field in ('actor','url','arguments','provider_type'):
            with self.subTest(field=field):
                data=content(); data['steps'][0]['control_plane'][field]='caller-selected'
                with self.assertRaises(ValidationError): OperationalProcedure.model_validate(data)

    def test_policy_fallback_is_versioned_and_not_an_identity(self):
        data=content(); failure=data['steps'][0]['failure']
        failure.pop('escalation_owner_identity_id'); failure['escalation_owner_policy']='current_work_action_owner'
        parsed=OperationalProcedure.model_validate(data)
        self.assertIsNone(parsed.steps[0].failure.escalation_owner_identity_id)
        data['schema_version']='1.3'
        with self.assertRaises(ValidationError): OperationalProcedure.model_validate(data)


class CanonicalProcedureExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def build(self, effect='read', *, timeout_seconds=None, contract_timeout=None, total_timeout=None):
        env=execution_fixture.ProcedureExecutionTests()
        self.env=env
        self.owner=ControlledOwner()
        registry=CanonicalOperationRegistry()
        selected_contract = contract(effect)
        if timeout_seconds is not None:
            from dataclasses import replace
            selected_contract = replace(selected_contract, timeout_seconds=(
                timeout_seconds if contract_timeout is None else contract_timeout))
        registry.register(selected_contract,self.owner)
        if effect=='mutation':
            from dataclasses import replace
            registry.register(replace(contract(),operation_id='fixture.verify.v1'),self.owner)
        def configure(_):
            env.skills.procedure_control_plane=registry
            value = content(effect)
            if timeout_seconds is not None:
                value['schema_version'] = '1.5'
                value['max_duration_seconds'] = timeout_seconds if total_timeout is None else total_timeout
                for step in value['steps']:
                    step['timeout_seconds'] = timeout_seconds
            if self._testMethodName in {
                    'test_known_no_effect_waiting_uses_consumable_diagnostic_after_material_change',
                    'test_interrupted_reconciliation_consumes_old_no_effect_marker'}:
                for step in value['steps']:
                    step['failure'].pop('escalation_owner_identity_id')
                    step['failure']['escalation_owner_policy'] = 'current_work_action_owner'
            return value
        env.configure=configure
        def rebuild():
            execution_fixture.ProcedureExecutionTests.rebuild(env)
            env.runner.control_plane=registry
        env.rebuild=rebuild
        await env.asyncSetUp()
        self.addCleanup(env.tearDown)
        return env

    async def test_disabled_elapsed_limit_uses_new_pin_and_actual_canonical_owner(self):
        env=await self.build(timeout_seconds=0)
        first=await env.advance()
        self.assertEqual(first.schema_version,'1.2')
        self.assertIsNone(first.deadline_at)
        self.assertEqual(first.steps['canonical'].status,'succeeded',first)
        self.assertEqual((await env.advance()).status,'completed')
        self.assertEqual(self.owner.calls,1)

    async def test_disabled_step_retains_positive_registered_operation_and_total_caps(self):
        env=await self.build(timeout_seconds=0,contract_timeout=7,total_timeout=100)
        observed=[]
        original=asyncio.wait_for
        async def timed(operation, timeout):
            observed.append(timeout)
            return await original(operation,timeout=timeout)
        with patch('codex_web.services.procedure_execution.asyncio.wait_for',new=timed):
            first=await env.advance()
        self.assertEqual(first.schema_version,'1.1')
        self.assertIsNotNone(first.deadline_at)
        self.assertEqual(first.steps['canonical'].status,'succeeded',first)
        self.assertIn(7,observed)
        self.assertEqual(self.owner.calls,1)

    async def test_real_interpreter_completes_registered_read_without_provider_or_model(self):
        env=await self.build()
        first=await env.advance()
        self.assertEqual(first.steps['canonical'].status,'succeeded',first)
        self.assertEqual((await env.advance()).status,'completed')
        self.assertEqual(self.owner.calls,1)
        self.assertEqual(env.provider.calls,[])
        self.assertEqual(self.owner.contexts[0].worker_actor,env.worker_actor)
        env.runner.models.invoke.assert_not_awaited()
        evidence=env.artifacts.get_evidence(first.steps['canonical'].evidence['observation'],actor=env.actor)
        self.assertEqual(evidence.metadata['canonical_operation'],'fixture.canonical.v1')
        self.assertNotEqual(evidence.evidence_type,'provider_receipt')

    async def test_unknown_mutation_reconciles_same_checkpoint_without_reexecution(self):
        env=await self.build('mutation')
        self.owner.disposition='reconciliation_required'
        first=await env.advance()
        self.assertEqual(first.status,'reconciliation_required',first)
        second=await env.advance()
        self.assertEqual(second.status,'reconciliation_required',second)
        self.assertEqual(self.owner.calls,1)
        self.assertEqual(self.owner.reconciliations,1)
        self.assertEqual(second.steps['verify'].attempts,0)
        self.assertEqual(first.id,second.id)

    async def test_waiting_mutation_observes_resolution_without_replaying_owner(self):
        env=await self.build('mutation')
        self.owner.disposition='waiting'
        self.assertEqual((await env.advance()).status,'waiting')
        self.owner.disposition='succeeded'
        self.assertEqual((await env.advance()).steps['canonical'].status,'succeeded')
        self.assertEqual(self.owner.calls,1)
        self.assertEqual(self.owner.reconciliations,1)

    async def test_known_no_effect_waiting_uses_consumable_diagnostic_after_material_change(self):
        env = await self.build('mutation')
        self.owner.ready = False
        self.owner.effects = 0
        async def execute(context, value):
            context.validate_current()
            return CanonicalOperationResult(disposition='waiting', code='fixture_before_effect:busy',
                facts={'canonical_owner_invoked':False})
        async def reconcile(context, value):
            context.validate_current()
            previous = context.previous_step
            if (self.owner.ready and previous.status == 'waiting'
                    and previous.operation_code == 'fixture_before_effect:busy'
                    and previous.diagnostic_evidence_ids):
                self.owner.effects += 1
                return CanonicalOperationResult(disposition='succeeded', outputs={'observed':True})
            return CanonicalOperationResult(disposition='reconciliation_required', code='owner_original_unresolved')
        self.owner.execute, self.owner.reconcile = execute, reconcile
        first = await env.advance()
        self.assertEqual(first.status, 'waiting')
        self.assertEqual(first.steps['canonical'].failure_code,
            'fixture_before_effect:busy:canonical_work_owner_resolution_required')
        original_id = first.id
        self.owner.ready = True  # controlled external owner's material precondition
        settled = await env.advance()
        self.assertEqual(settled.steps['canonical'].status, 'succeeded', settled)
        self.assertEqual(settled.id, original_id)
        self.assertEqual(self.owner.effects, 1)

    async def test_interrupted_reconciliation_consumes_old_no_effect_marker(self):
        import asyncio
        env = await self.build('mutation')
        self.owner.effects = 0
        async def execute(context, value):
            return CanonicalOperationResult(disposition='waiting', code='fixture_before_effect:busy')
        async def reconcile(context, value):
            previous = context.previous_step
            if (previous.status == 'waiting' and previous.operation_code == 'fixture_before_effect:busy'
                    and previous.diagnostic_evidence_ids):
                self.owner.effects += 1
                raise asyncio.CancelledError()
            return CanonicalOperationResult(disposition='reconciliation_required', code='owner_original_unresolved')
        self.owner.execute, self.owner.reconcile = execute, reconcile
        self.assertEqual((await env.advance()).status, 'waiting')
        with self.assertRaises(asyncio.CancelledError):
            await env.advance()
        actual = env.workers.store.assignment(env.assignment.id).procedure_checkpoint.steps['canonical']
        self.assertEqual(actual.status, 'running')
        self.assertIsNone(actual.failure_code)
        self.assertEqual(actual.evidence, {})
        after = await env.advance()
        self.assertEqual(after.status, 'reconciliation_required')
        self.assertEqual(self.owner.effects, 1)
        self.assertEqual(after.steps['verify'].attempts, 0)

    async def test_postclaim_authority_loss_keeps_unknown_and_no_later_step(self):
        env=await self.build('mutation')
        self.owner.disposition='reconciliation_required'
        self.assertEqual((await env.advance()).status,'reconciliation_required')
        env.runner.clock=lambda:env.assignment.lease.expires_at+1
        result=await env.advance()
        self.assertEqual(result.steps['canonical'].status,'reconciliation_required')
        self.assertEqual(result.steps['verify'].attempts,0)
        self.assertEqual(self.owner.calls,1)
