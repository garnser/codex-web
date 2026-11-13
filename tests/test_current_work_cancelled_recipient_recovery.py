"""Explicit current-Work replacement after real owned idle cancellation.

The borrowed routing fixture has no credential seal. Real sealed attribution
and cancellation need separate coverage; this case proves the cancellation
lifecycle boundary without inventing credential custody or a native outcome.
"""
import unittest

from fastapi import HTTPException

from codex_web.execution_workers import AssignmentStatus
from tests import test_current_work_recipient_recovery as recovery_fixture


class CurrentWorkCancelledRecipientRecoveryTests(
    recovery_fixture.CurrentWorkRecipientRecoveryTests
):
    async def test_owned_idle_cancel_preserves_original_and_allows_explicit_current_work_replacement(self):
        physical = await self.owned_replacement_service()
        started = await self.started()
        assignment_id = self.pin.recipient.assignment_id
        old_thread = self.pin.recipient.thread_id
        original = physical.store.assignment(assignment_id)
        observed = original.bootstrap_create_observation
        self.assertEqual(observed.outcome, 'created')
        self.assertEqual(observed.fence, original.fence)
        self.assertIsNone(original.native_credential_custody)
        self.assertEqual(started.status, 'started')
        completed = [turn for turn in self.history.get(old_thread)['turns']
            if turn['id'] == started.execution_id
            and turn['providerTurnId'] == started.queued_start_receipt.provider_native_turn_id]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]['status'], 'completed')
        self.assertIsNotNone(completed[0]['completedAt'])
        retained_start = started.model_dump(mode='json')
        retained_work = self.f.f.env.states.get(self.state.ref).model_dump(mode='json')

        # The reused transport harness records thread/start directly through
        # the real capture writer/reader. Enable its normal session cleanup
        # recorder only after that actual created observation exists. This flag
        # is not cleanup proof: manager.cancel_bootstrap must join the real
        # adopted process and executors and append its own durable observation.
        physical.session._bootstrap_create_capture_enabled = True
        await physical.manager.cancel_bootstrap(assignment_id,
            reason='Private owned idle retirement for upgrade; preserve Work and originals')
        cancelled = physical.store.assignment(assignment_id)
        self.n.reserved = cancelled
        self.assertEqual(cancelled.status, AssignmentStatus.CANCELLED)
        self.assertEqual(cancelled.fence, original.fence + 1)
        self.assertIsNone(cancelled.lease)
        self.assertEqual(cancelled.failure_code, 'thread_bootstrap_cancelled')
        self.assertNotIn(assignment_id, physical.manager.sessions)
        self.assertEqual(physical.process.poll(), 0)
        joined = cancelled.bootstrap_create_observation
        self.assertEqual(joined.fence, original.fence)
        self.assertEqual(joined.model_dump(exclude={'cleanup_joined_at', 'process_exit_code'}),
            observed.model_dump(exclude={'cleanup_joined_at', 'process_exit_code'}))
        self.assertIsNotNone(joined.cleanup_joined_at)
        self.assertEqual(joined.process_exit_code, 0)
        before = cancelled.model_dump(mode='json')
        self.assertIsNone(self.state.handoff)
        self.assertIsNone(self.state.closed_at)

        # A NEW explicit MFA operator request concerns still-actionable Work;
        # it neither replays the completed input nor uncancels its assignment.
        try:
            result = await self.service.replace(old_thread, work_item_ref=self.state.ref,
                actor=self.f.f.actor, project=self.project)
        except HTTPException as refused:
            self.assertEqual(self.new_calls, [])
            self.assertEqual(physical.store.assignment(assignment_id).model_dump(mode='json'), before)
            self.assertEqual(self.f.f.preflight.get(started.id).model_dump(mode='json'), retained_start)
            self.fail('Explicit current-Work replacement rejected positively joined canonical idle '
                f'cancellation: HTTP{refused.status_code} {refused.detail}')
        self.assertTrue(result['ok'])
        self.assertEqual(result['oldThreadId'], old_thread)
        self.assertEqual(result['newThreadId'], 'successor-thread-1')
        self.assertEqual(len(self.new_calls), 1)
        self.assertEqual(physical.store.assignment(assignment_id).model_dump(mode='json'), before)
        self.assertEqual(self.f.f.preflight.get(started.id).model_dump(mode='json'), retained_start)
        self.assertEqual(self.f.f.env.states.get(self.state.ref).model_dump(mode='json'), retained_work)


def load_tests(loader, standard_tests, pattern):
    # Inherit fixture lifecycle/helpers, not a second collection of its tests.
    names = sorted(name for name in CurrentWorkCancelledRecipientRecoveryTests.__dict__
        if name.startswith('test_'))
    return unittest.TestSuite(CurrentWorkCancelledRecipientRecoveryTests(name) for name in names)
