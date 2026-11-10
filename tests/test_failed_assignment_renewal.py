"""Real retained admission/Retry owners; native input and renewal are external seams."""
import unittest
from unittest.mock import AsyncMock

from fastapi import HTTPException

from codex_web.models import ExecutionPreflightBlocker, QueuedNativeStartReceipt
from codex_web.runtime.execution import ExecutionPrestartRefusal
from tests import test_immediate_guarded_turn_admission as fixtures


class FailedAssignmentRenewalTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.ImmediateGuardedTurnAdmissionTests.setUp
    accept = fixtures.ImmediateGuardedTurnAdmissionTests.accept
    exhausted = fixtures.ImmediateGuardedTurnAdmissionTests.exhausted

    def renewal_refusal(self):
        return ExecutionPrestartRefusal(status_code=409, detail={
            'code': 'execution_preflight_blocked', 'message': 'Renewal held',
            'blockers': [{'code': 'procedure_assignment_renewal_blocked',
                'message': 'The runner has pending work or unavailable custody; inspect it before renewal.',
                'retryable': True, 'target_type': 'execution_assignment',
                'target_id': 'old-assignment', 'remediation_route': '/api/execution-workers'}]})

    async def blocked_renewal(self):
        result = await self.accept()
        self.service.preflight = self.runtime.queued_preflight
        original = self.queue.queue('thread')[0].model_dump(mode='json')
        self.runtime.start_thread_turn_now.side_effect = self.exhausted()
        self.runtime.renew_exhausted_assignment = AsyncMock(side_effect=self.renewal_refusal())
        await self.runtime.drain_thread_queue('thread')
        return 'preflight-queued-' + result['queuedId'], original

    async def test_failed_renewal_keeps_typed_target_and_same_original_retry(self):
        attempt_id, original = await self.blocked_renewal()
        saved = self.service.preflight.get(attempt_id, actor=self.actor)
        self.assertEqual(saved.status, 'blocked')
        self.assertEqual(saved.attempt_number, 2)
        self.assertEqual([(b.code, b.target_type, b.target_id) for b in saved.blockers], [
            ('procedure_assignment_exhausted', 'execution_assignment', 'old-assignment'),
            ('procedure_assignment_renewal_blocked', 'execution_assignment', 'old-assignment')])
        self.assertEqual(saved.blockers[-1].remediation_route, '/api/execution-workers')
        self.assertIn('pending work', saved.blockers[-1].message)
        # The unchanged normal renewal guard can refuse again; no new claim,
        # queue entry, native input or attempt-number change is permitted.
        with self.assertRaises(ExecutionPrestartRefusal):
            await self.service.retry_preflight('thread', attempt_id, actor=self.actor)
        self.assertEqual(self.service.preflight.get(attempt_id, actor=self.actor), saved)
        self.assertEqual(self.queue.queue('thread'), [])
        # Once that real owner reports success, ordinary Retry must invoke it
        # with the retained exact assignment before requeueing this original.
        self.runtime.renew_exhausted_assignment.side_effect = None
        response = await self.service.retry_preflight('thread', attempt_id, actor=self.actor)
        self.runtime.renew_exhausted_assignment.assert_awaited_with('thread', project=self.project,
            actor=self.actor, expected_assignment_id='old-assignment')
        self.assertEqual(response['queuedId'], original['id'])
        self.assertEqual(self.queue.queue('thread')[0].model_dump(mode='json'), original)
        self.assertEqual(self.service.preflight.get(attempt_id, actor=self.actor).attempt_number, 3)
        self.runtime.start_thread_turn_now.side_effect = None
        await self.runtime.drain_thread_queue('thread')
        saved = self.service.preflight.get(attempt_id, actor=self.actor)
        self.assertEqual(saved.status, 'started')
        self.assertEqual(saved.queued_turn_snapshot, original)
        self.assertEqual(self.runtime.start_thread_turn_now.await_count, 2)

    async def test_legacy_wrapper_uses_fresh_admission_without_inventing_history(self):
        attempt_id, original = await self.blocked_renewal()
        preflight = self.service.preflight
        preflight.store.update(attempt_id, lambda item: item.model_copy(update={
            'blockers': (ExecutionPreflightBlocker(code='execution_preflight_blocked',
                message='Historical generic wrapper', retryable=True),), 'blocker_history': ()}))
        self.runtime.renew_exhausted_assignment.reset_mock()
        await self.service.retry_preflight('thread', attempt_id, actor=self.actor)
        self.runtime.renew_exhausted_assignment.assert_not_awaited()
        await self.runtime.drain_thread_queue('thread')
        saved = preflight.get(attempt_id, actor=self.actor)
        self.assertEqual((saved.status, saved.attempt_number), ('blocked', 3))
        self.assertEqual(saved.blockers[0].code, 'procedure_assignment_exhausted')
        self.assertEqual(saved.blocker_history, ())
        self.runtime.renew_exhausted_assignment.assert_not_awaited()
        self.runtime.renew_exhausted_assignment.side_effect = None
        await self.service.retry_preflight('thread', attempt_id, actor=self.actor)
        self.runtime.renew_exhausted_assignment.assert_awaited_once()
        self.assertEqual(self.queue.queue('thread')[0].model_dump(mode='json'), original)

    async def test_uncertain_error_cannot_keep_retryable_exhaustion(self):
        attempt_id, _ = await self.blocked_renewal()
        preflight = self.service.preflight
        _, claim, acquired = preflight.claim_retry(attempt_id, actor=self.actor)
        self.assertTrue(acquired)
        saved = preflight.hold_renewal_retry(attempt_id, actor=self.actor, claim_id=claim,
            error=RuntimeError('transport outcome unknown'))
        self.assertEqual(saved.status, 'reconciliation_required')
        self.assertEqual([b.code for b in saved.blockers], ['native_start_outcome_unknown'])
        before = self.runtime.renew_exhausted_assignment.await_count
        with self.assertRaises(HTTPException):
            await self.service.retry_preflight('thread', attempt_id, actor=self.actor)
        self.assertEqual(self.runtime.renew_exhausted_assignment.await_count, before)

    async def test_changed_claim_or_positive_start_cannot_be_overwritten(self):
        attempt_id, _ = await self.blocked_renewal()
        preflight = self.service.preflight
        current, claim, _ = preflight.claim_retry(attempt_id, actor=self.actor)
        self.assertEqual(preflight.hold_renewal_retry(attempt_id, actor=self.actor,
            claim_id='foreign', error=self.renewal_refusal()), current)
        receipt = QueuedNativeStartReceipt(admission_claim_id='actual-claim',
            requested_execution_id=current.execution_id, execution_id=current.execution_id,
            thread_id='thread', provider_native_session_id='thread', provider_native_turn_id='actual',
            provider_id='openai', runtime_id='codex', observed_at=100)
        started = preflight.store.update(attempt_id, lambda item: item.model_copy(update={
            'status': 'started', 'started_at': 100, 'queued_start_receipt': receipt}))
        self.assertEqual(preflight.hold_renewal_retry(attempt_id, actor=self.actor,
            claim_id=claim, error=self.renewal_refusal()), started)

    async def test_possible_native_observation_keeps_unknown_even_for_local_refusal(self):
        attempt_id, _ = await self.blocked_renewal()
        preflight = self.service.preflight
        _, claim, _ = preflight.claim_retry(attempt_id, actor=self.actor)
        preflight.store.update(attempt_id, lambda item: item.model_copy(update={'started_at': 100}))
        saved = preflight.hold_renewal_retry(attempt_id, actor=self.actor,
            claim_id=claim, error=self.renewal_refusal())
        self.assertEqual(saved.status, 'reconciliation_required')
        self.assertEqual(saved.started_at, 100)
        self.assertNotIn('procedure_assignment_exhausted', [b.code for b in saved.blockers])
