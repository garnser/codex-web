"""Tool-free reasoning still uses canonical Skill discovery and final admission."""
import dataclasses
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock

from codex_web.definitions import DefinitionReference
from codex_web.execution_workers import ExecutionRuntimeBinding
from codex_web.runtime.execution import ExecutionPrestartRefusal
from codex_web.services.procedure_discovery import NativeProcedureAdmissionService,OperationContext
from tests import test_procedure_native_admission as procedure_fixtures


class NativeReasoningAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture=procedure_fixtures.ProductionNativeProcedureAdmissionTests()
        await self.fixture.asyncSetUp()
        f=self.fixture
        current=f.native_assignment
        def update(state):
            rows=[r.model_copy(update={'runtime_binding':ExecutionRuntimeBinding(provider_id='openai',
                runtime_id='codex-reasoning',capability_revision=1),'resource_ids':(),
                'sandbox':'read-only'}) if r.id==current.id else r for r in state.assignments]
            return state.model_copy(update={'assignments':rows})
        f.native_assignment=f.workers.store.update_assignment(f.procedure_worker.id,current.id,update).assignments[0]
        self.admission=NativeProcedureAdmissionService(f.discovery,f.workers,f.history,f.actor,f.profiles)
        self.attested_fence=f.native_assignment.fence
        def preparation(**kwargs):
            if kwargs['assignment'].fence != self.attested_fence:
                raise ValueError('actual session fence changed')
            if kwargs['request_checksum']!='a'*64:
                raise ValueError('original request changed')
            return OperationContext(actor=kwargs['actor'],project_id=f.project.id,resource_ids=(),
                operation='model.reasoning',provider_capabilities=(),worker_capabilities=(),operation_capabilities=(),
                policy_source='canonical:private-actual-session-proof',policy_allows=True,
                enforcement='bounded_read_only',request_checksum=kwargs['request_checksum'],
                required_skill_refs=tuple(f.native_assignment.skill_refs))
        self.admission.reasoning_preparation=preparation

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def admit(self,**changes):
        f=self.fixture
        values=dict(project=f.project,actor=f.actor,thread_id='reasoning-thread',
            execution_id=f.native_assignment.execution_id,assignment_id=f.native_assignment.id,
            sandbox='read-only',source='queued:web',request_checksum='a'*64)
        values.update(changes)
        return await self.admission(**values)

    async def test_existing_operational_procedure_is_unmatched_by_exact_reasoning_operation(self):
        result,_=await self.admit()
        self.assertTrue(result.admitted,result)
        self.assertEqual(result.operation,'model.reasoning')
        self.assertEqual(result.outcome,'no_match')
        self.assertTrue(any('operation_does_not_match' in r.reasons for r in result.candidates))
        retained=self.fixture.workers.assignment_for_execution(self.fixture.native_assignment.execution_id,
            actor=self.fixture.actor)
        self.assertEqual(retained.procedure_discoveries[-1].context_checksum,result.context_checksum)

    async def test_missing_private_preparation_and_changed_request_refuse(self):
        preparation=self.admission.reasoning_preparation
        self.admission.reasoning_preparation=None
        with self.assertRaises(ExecutionPrestartRefusal):await self.admit()
        self.admission.reasoning_preparation=preparation
        with self.assertRaises(ExecutionPrestartRefusal):await self.admit(request_checksum='b'*64)

    async def test_actual_preparation_cause_is_closed_stage_and_exception_not_prose(self):
        from codex_web.services.native_model_provider import NativeReasoningPreparationError
        for stage in ('invocation_context', 'original_input_binding', 'session_attestation'):
            def unavailable(**kwargs):
                try:
                    raise RuntimeError('private fixture detail must never be projected')
                except RuntimeError as exc:
                    raise NativeReasoningPreparationError(stage) from exc
            self.admission.reasoning_preparation = unavailable
            with self.subTest(stage=stage), self.assertRaises(ExecutionPrestartRefusal) as caught:
                await self.admit()
            detail = caught.exception.detail
            self.assertEqual(detail['native_preparation_stage'], stage)
            self.assertEqual(detail['native_exception_type'], 'RuntimeError')
            self.assertNotIn('private fixture', str(detail))

    async def test_final_native_guard_rechecks_current_attested_fence(self):
        f=self.fixture
        result,_=await self.admit()
        guard=self.admission.preparation_input_guard(result,project=f.project,actor=f.actor,
            execution_id=f.native_assignment.execution_id,assignment_id=f.native_assignment.id,
            sandbox='read-only',request_checksum='a'*64)
        guard()
        self.attested_fence+=1
        with self.assertRaises(ExecutionPrestartRefusal):guard()

    async def test_required_missing_skill_pin_is_not_erased_by_reasoning_callback(self):
        f=self.fixture
        reference=DefinitionReference(record_id='missing',definition_id='missing',revision=1,
            kind='skill',checksum='a'*64,definition_schema_version='1.0')
        f.native_assignment=f.native_assignment.model_copy(update={'skill_refs':(reference,)})
        with self.assertRaises(ExecutionPrestartRefusal) as caught:await self.admit()
        self.assertEqual(caught.exception.detail['code'],'procedure_inaccessible')

    async def test_current_input_authority_denial_precedes_reasoning_preparation(self):
        from codex_web.services.identity import AuthorizationError
        self.admission.input_authorizer=Mock(side_effect=AuthorizationError('revoked'))
        preparation=self.admission.reasoning_preparation=Mock()
        with self.assertRaises(ExecutionPrestartRefusal):await self.admit()
        preparation.assert_not_called()
