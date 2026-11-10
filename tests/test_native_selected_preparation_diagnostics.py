"""Selected canonical native preparation refusals, without native/provider execution."""
import unittest
from unittest.mock import patch
from codex_web.runtime.execution import ExecutionPrestartRefusal
from codex_web.services.execution_preflight import ExecutionPreflightService
from codex_web.services.procedure_execution import ProcedureExecutionError
import tests.test_native_procedure_credential_custody as fixtures


class NativeSelectedPreparationDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture=fixtures.NativeProcedureCredentialCustodyTests()
        await self.fixture.asyncSetUp()
        self.fixture.bootstrap()
        self.f=self.fixture.f
        self.owner=self.fixture.owner

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def seal(self):
        discovery,_=await self.fixture.admit()
        self.assertTrue(discovery.admitted)
        self.assertTrue(discovery.selected)
        self.assertIsNotNone(self.fixture.current().native_credential_custody.admitted_at)
        return discovery,self.fixture.current().native_credential_custody

    async def refused(self):
        with self.assertRaises(ExecutionPrestartRefusal) as caught:
            await self.fixture.admit()
        detail=caught.exception.detail
        self.assertEqual(detail['code'],'procedure_unsupported_enforcement')
        self.assertEqual(detail['procedure_discovery']['outcome'],'unsupported_enforcement')
        self.assertTrue(detail['procedure_discovery']['blockers'][0].startswith('procedure_tool_enforcement_unsupported:'))
        self.assertNotIn('sentinel-private',repr(detail))
        self.assertNotIn('private-provider-value-must-never-enter-native',repr(detail))
        self.assertEqual(self.f.ops.calls,[])
        return detail

    async def test_sealed_selected_refusal_retains_closed_attestor_diagnostic(self):
        first,sealed=await self.seal()
        self.fixture.fixture.attestor.side_effect=TimeoutError('sentinel-private credential-shaped payload')
        detail=await self.refused()
        expected=[{'stage':'session_attestation','exception_type':'TimeoutError'}]
        # OLD uses actual existing bootstrap/seal/admission APIs and reaches
        # this positive diagnostic assertion, not a new helper/import/setup API.
        self.assertEqual(detail.get('native_preparation_diagnostics'),expected)
        self.assertNotIn('native_empty_preparation_diagnostics',detail)
        self.assertEqual(self.fixture.current().native_credential_custody,sealed)
        retained=self.fixture.current().procedure_discoveries[-1]
        self.assertEqual(retained.selected,first.selected)
        self.assertIn('native_preparation_diagnostic:session_attestation:TimeoutError',retained.blockers)
        self.assertNotIn('canonical:native-provider-preparation:',retained.policy_source)
        from codex_web.models import TurnCreate
        from codex_web.storage.execution_preflight import ExecutionPreflightStore
        service=ExecutionPreflightService(ExecutionPreflightStore(self.f.sqlite))
        attempt=service.record_blocked(actor=self.f.actor,thread_id='custody-native-thread',
            project_id=self.f.project.id,execution_id='selected-original-refusal',
            payload=TurnCreate(message='Retain this original selected request'),effective={},detail=detail)
        public=service.public(service.get(attempt.id,actor=self.f.actor),actor=self.f.actor)
        self.assertEqual(public['blockers'][0]['native_preparation_diagnostics'],expected)
        self.assertFalse(public['blockers'][0]['retryable'])
        self.assertIsNone(attempt.started_at)
        self.assertIsNone(attempt.queued_start_receipt)
        self.assertEqual(attempt.message,'Retain this original selected request')

    async def test_current_owner_refusal_retains_only_allowlisted_canonical_code(self):
        await self.seal()
        code='procedure_assignment_fence_expired_or_revoked'
        with patch.object(self.owner,'validate_native_preparation',side_effect=ProcedureExecutionError(code)):
            detail=await self.refused()
        self.assertEqual(detail['native_preparation_diagnostics'],
            [{'stage':'native_owner','exception_type':'RuntimeError','code':code}])
        self.assertIn('native_preparation_diagnostic:native_owner:RuntimeError:'+code,
            self.fixture.current().procedure_discoveries[-1].blockers)

    async def test_preparation_context_failure_has_exact_phase_without_exception_text(self):
        await self.seal()
        with patch.object(self.fixture.admission,'_preparation_context',side_effect=ValueError('sentinel-private context')):
            detail=await self.refused()
        self.assertEqual(detail['native_preparation_diagnostics'],
            [{'stage':'preparation_context','exception_type':'ValueError'}])

    async def test_credential_phase_generic_refusal_is_diagnostic_not_authority(self):
        await self.seal()
        with patch.object(self.owner,'validate_native_credential_custody',side_effect=PermissionError('sentinel-private secretref')):
            detail=await self.refused()
        self.assertEqual(detail['native_preparation_diagnostics'],
            [{'stage':'credential_custody','exception_type':'PermissionError'}])

    async def test_special_canonical_credential_refusal_keeps_exact_nonretry_contract(self):
        _,sealed=await self.seal()
        before=len(self.fixture.current().procedure_discoveries)
        code='procedure_credential_binding_not_admitted'
        with patch.object(self.owner,'validate_native_credential_custody',side_effect=ProcedureExecutionError(code)):
            with self.assertRaises(ExecutionPrestartRefusal) as caught:await self.fixture.admit()
        self.assertEqual(caught.exception.detail['code'],code)
        self.assertFalse(caught.exception.detail['retryable'])
        self.assertEqual(caught.exception.detail['remediation_route'],'/api/execution-workers/assignments')
        self.assertNotIn('native_preparation_diagnostics',caught.exception.detail)
        self.assertEqual(len(self.fixture.current().procedure_discoveries),before)
        self.assertEqual(self.fixture.current().native_credential_custody,sealed)
        self.assertEqual(self.f.ops.calls,[])

    async def test_unknown_exception_class_and_unlisted_code_are_not_serialized(self):
        await self.seal()
        class SentinelPrivateCredentialError(Exception):pass
        self.fixture.fixture.attestor.side_effect=SentinelPrivateCredentialError('sentinel-private')
        detail=await self.refused()
        self.assertEqual(detail['native_preparation_diagnostics'],
            [{'stage':'session_attestation','exception_type':'Exception'}])
        self.assertNotIn('SentinelPrivateCredentialError',repr(detail))
        self.fixture.fixture.attestor.side_effect=None
        with patch.object(self.owner,'validate_native_preparation',side_effect=ProcedureExecutionError('sentinel-private')):
            detail=await self.refused()
        self.assertEqual(detail['native_preparation_diagnostics'],
            [{'stage':'native_owner','exception_type':'RuntimeError'}])

    async def test_initial_selected_failure_does_not_seal_or_create_execution_proof(self):
        self.assertIsNone(self.fixture.current().native_credential_custody.admitted_at)
        self.fixture.fixture.attestor.side_effect=TimeoutError('sentinel-private initial attestation')
        detail=await self.refused()
        self.assertEqual(detail['native_preparation_diagnostics'],
            [{'stage':'session_attestation','exception_type':'TimeoutError'}])
        self.assertIsNone(self.fixture.current().native_credential_custody.admitted_at)

    async def test_healthy_selected_preparation_and_exact_input_guard_unchanged(self):
        first,sealed=await self.seal()
        result,_=await self.fixture.admit()
        self.assertTrue(result.admitted)
        self.assertEqual(result.selected,first.selected)
        self.assertFalse(any(x.startswith('native_preparation_diagnostic:') for x in result.blockers))
        guard=self.fixture.admission.preparation_input_guard(result,project=self.f.project,actor=self.f.actor,
            execution_id=self.fixture.assignment.execution_id,assignment_id=self.fixture.assignment.id,
            sandbox=self.fixture.assignment.sandbox,request_checksum='a'*64)
        guard()
        self.assertEqual(self.fixture.current().native_credential_custody,sealed)
        self.assertEqual(self.f.ops.calls,[])

    async def test_selected_discovery_failure_retains_discovery_phase(self):
        await self.seal()
        real=self.f.discovery.discover
        calls=[]
        def discovery(*args,**kwargs):
            calls.append(True)
            if len(calls)==2:raise RuntimeError('sentinel-private discovery')
            return real(*args,**kwargs)
        with patch.object(self.f.discovery,'discover',side_effect=discovery):detail=await self.refused()
        self.assertEqual(detail['native_preparation_diagnostics'],
            [{'stage':'discovery','exception_type':'RuntimeError'}])

    async def test_generic_projection_uses_one_closed_field_and_keeps_legacy_compatibility(self):
        safe={'stage':'native_owner','exception_type':'Exception',
            'code':'procedure_assignment_fence_expired_or_revoked'}
        generic={'code':'procedure_unsupported_enforcement','message':'Original refusal',
            'native_preparation_diagnostics':[safe]*1000,
            'native_empty_preparation_diagnostics':[{'stage':'fallback','exception_type':'ValueError'}]}
        blocker,=ExecutionPreflightService._blockers(generic)
        raw=blocker.model_dump(mode='json')
        self.assertEqual(raw['native_preparation_diagnostics'],[safe])
        self.assertNotIn('native_empty_preparation_diagnostics',raw)
        self.assertFalse(blocker.retryable)
        legacy={'code':'procedure_inaccessible','message':'Original empty refusal',
            'native_empty_preparation_diagnostics':[{'stage':'session_attestation','exception_type':'TimeoutError'}]}
        old,=ExecutionPreflightService._blockers(legacy)
        self.assertEqual(old.model_dump(mode='json')['native_empty_preparation_diagnostics'],legacy['native_empty_preparation_diagnostics'])
        for value in ([{**safe,'message':'sentinel-private'}], [{'stage':[],'exception_type':'Exception'}], 'sentinel-private'):
            b,=ExecutionPreflightService._blockers({**generic,'native_preparation_diagnostics':value})
            self.assertNotIn('native_preparation_diagnostics',b.model_dump(mode='json'))
            self.assertNotIn('native_empty_preparation_diagnostics',b.model_dump(mode='json'))
            self.assertNotIn('sentinel-private',repr(b.model_dump(mode='json')))
