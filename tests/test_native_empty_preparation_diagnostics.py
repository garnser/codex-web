"""Real canonical empty-scratch admission, controlled attestor edge; no native/model START."""
import unittest
from unittest.mock import patch
from codex_web.runtime.execution import ExecutionPrestartRefusal
from codex_web.services.execution_preflight import ExecutionPreflightService
import tests.test_native_provider_preparation as fixtures


class NativeEmptyPreparationDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = fixtures.NativeProviderPreparationTests()
        await self.fixture.asyncSetUp()
        await self.fixture.make_empty_scratch()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def refusal(self):
        with self.assertRaises(ExecutionPrestartRefusal) as caught:
            await self.fixture.admit()
        detail = caught.exception.detail
        self.assertNotEqual(detail['procedure_discovery']['outcome'], 'no_match')
        self.assertTrue(detail['procedure_discovery']['blockers'])
        self.assertEqual(self.fixture.fixture.ops.calls, [])
        self.assertNotIn('sentinel-private', repr(detail))
        return detail

    async def test_actual_empty_admission_retains_closed_attestor_failure(self):
        self.fixture.attestor.side_effect = TimeoutError('sentinel-private credential-shaped payload')
        detail = await self.refusal()
        expected = [{'stage': 'session_attestation', 'exception_type': 'TimeoutError'}]
        # OLD assertion uses only existing admission APIs, not a new helper import.
        self.assertEqual(detail.get('native_empty_preparation_diagnostics'), expected)
        blockers = ExecutionPreflightService._blockers(detail)
        self.assertEqual(blockers[0].model_dump(mode='json')['native_empty_preparation_diagnostics'], expected)
        self.assertFalse(blockers[0].retryable)
        self.assertNotIn('canonical:native-empty-physical-scope:', detail['procedure_discovery']['policy_source'])
        from codex_web.models import TurnCreate
        from codex_web.storage.execution_preflight import ExecutionPreflightStore
        f = self.fixture.fixture
        preflight = ExecutionPreflightService(ExecutionPreflightStore(f.sqlite))
        attempt = preflight.record_blocked(actor=f.actor, thread_id='original-empty-thread',
            project_id=f.project.id, execution_id='original-empty-execution',
            payload=TurnCreate(message='Retain this exact original'), effective={}, detail=detail)
        retained = preflight.get(attempt.id, actor=f.actor)
        public = preflight.public(retained, actor=f.actor)
        self.assertEqual(public['blockers'][0]['native_empty_preparation_diagnostics'], expected)
        self.assertIsNone(retained.started_at)
        self.assertIsNone(retained.queued_start_receipt)
        self.assertEqual(retained.message, 'Retain this exact original')

    async def test_current_owner_failure_has_distinct_closed_stage(self):
        with patch.object(self.fixture.owner, 'validate_native_preparation_owner',
                side_effect=PermissionError('sentinel-private owner detail')):
            detail = await self.refusal()
        self.assertEqual(detail['native_empty_preparation_diagnostics'],
            [{'stage': 'native_owner', 'exception_type': 'PermissionError'}])

    async def test_candidate_failure_is_observed_without_bypassing_original_refusal(self):
        with patch.object(self.fixture.owner, 'validate_native_preparation',
                side_effect=RuntimeError('sentinel-private candidate payload')):
            detail = await self.refusal()
        self.assertEqual(detail['native_empty_preparation_diagnostics'], [
            {'stage': 'candidate_validation', 'exception_type': 'RuntimeError'},
            {'stage': 'candidate_consistency', 'exception_type': 'ValueError'}])

    async def test_physical_scope_failure_is_distinct_from_attestor(self):
        f = self.fixture.fixture
        workspace = f.workspaces.get(f.native_assignment.execution_workspace_id, f.actor)
        f.workspaces.store.update(lambda state: state.model_copy(update={'leases': [
            lease.model_copy(update={'resource_ids': (f.repo.id,), 'resource_modes': {f.repo.id: 'write'}})
            if lease.id == workspace.lease_id else lease for lease in state.leases]}))
        detail = await self.refusal()
        self.assertEqual(detail['native_empty_preparation_diagnostics'],
            [{'stage': 'physical_scope', 'exception_type': 'ValueError'}])

    async def test_unknown_exception_name_is_closed_not_prose(self):
        class SentinelPrivateCredentialError(Exception):
            pass
        self.fixture.attestor.side_effect = SentinelPrivateCredentialError('sentinel-private')
        detail = await self.refusal()
        self.assertEqual(detail['native_empty_preparation_diagnostics'],
            [{'stage': 'session_attestation', 'exception_type': 'Exception'}])
        self.assertNotIn('SentinelPrivateCredentialError', repr(detail))

    async def test_success_still_seals_empty_and_rechecks_current_input(self):
        result, _ = await self.fixture.admit()
        self.assertTrue(result.admitted)
        self.assertEqual(result.outcome, 'no_match')
        self.assertEqual(result.selected, ())
        self.assertIn('canonical:native-empty-physical-scope:', result.policy_source)
        f = self.fixture.fixture
        guard = self.fixture.admission.preparation_input_guard(result, project=f.project, actor=f.actor,
            execution_id=f.native_assignment.execution_id, assignment_id=f.native_assignment.id,
            sandbox=f.native_assignment.sandbox, request_checksum='a'*64)
        self.fixture.attestor.side_effect = RuntimeError('private later drift')
        with self.assertRaises(ExecutionPrestartRefusal):
            guard()
        self.assertEqual(f.ops.calls, [])

    async def test_projection_rejects_malformed_payloads_and_bounds_duplicates(self):
        safe = {'stage': 'native_owner', 'exception_type': 'ValueError'}
        for value in (None, 'sentinel-private', [{'stage': [], 'exception_type': 'ValueError'}],
                [{'stage': 'native_owner', 'exception_type': 'sentinel-private'}],
                [{**safe, 'message': 'sentinel-private'}]):
            with self.subTest(value=value):
                detail = {'code': 'procedure_inaccessible', 'message': 'Original refusal',
                    'native_empty_preparation_diagnostics': value}
                blocker, = ExecutionPreflightService._blockers(detail)
                self.assertNotIn('native_empty_preparation_diagnostics', blocker.model_dump(mode='json'))
                self.assertEqual(blocker.code, detail['code'])
                self.assertFalse(blocker.retryable)
        blocker, = ExecutionPreflightService._blockers({'code': 'procedure_inaccessible',
            'message': 'Original refusal', 'native_empty_preparation_diagnostics': [safe] * 1000})
        self.assertEqual(blocker.model_dump(mode='json')['native_empty_preparation_diagnostics'], [safe])
