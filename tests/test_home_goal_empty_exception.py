"""Empty exception messages cannot invalidate canonical Home Goal metadata."""
import asyncio
import unittest

from codex_web.goal_execution_bindings import GoalExecutionBindingStatus
from tests import test_home_goal_continuation as fixture


class HomeGoalEmptyExceptionTests(fixture.HomeGoalContinuationTests):
    def claim(self):
        return self.goal_bindings.claim_continuation(self.goal_binding.id,
            scope=self.actor.tenant, owner_id=self.goal_continuation.owner_id, lease_seconds=60.)

    def readable(self):
        # Decode the actual persisted envelope, including its events, rather
        # than inspecting a model_copy which has not crossed validation.
        state = self.goal_bindings.store.load()
        return next(row for row in state.bindings if row.id == self.goal_binding.id), state

    def no_input(self):
        self.assertEqual(self.queue_store.get('operator-thread'), [])
        self.assertEqual(self.starts(), [])
        self.transport.request.assert_not_awaited()

    async def refused_before_reservation(self, error):
        claimed = self.claim()
        def refuse(binding):
            raise error
        self.home_goal.context = refuse
        if isinstance(error, asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await self.home_goal._dispatch(claimed, scope=self.actor.tenant)
        else:
            result = await self.home_goal._dispatch(claimed, scope=self.actor.tenant)
            self.assertEqual((result.outcome, result.reason), ('blocked', type(error).__name__))
        current, state = self.readable()
        self.assertEqual(current.status, 'blocked')
        self.assertEqual(current.stop_reason, type(error).__name__)
        self.assertEqual(current.change_reason, type(error).__name__)
        self.assertEqual(state.events[-1].reason, type(error).__name__)
        self.assertIsNone(current.home_continuation)
        self.no_input()

    async def test_empty_runtime_error_keeps_canonical_binding_readable(self):
        await self.refused_before_reservation(RuntimeError())

    async def test_whitespace_runtime_error_keeps_canonical_binding_readable(self):
        await self.refused_before_reservation(RuntimeError(' \n\t'))

    async def test_empty_cancelled_error_is_retained_then_reraised(self):
        await self.refused_before_reservation(asyncio.CancelledError())

    async def test_empty_failure_preserves_new_operator_pause(self):
        claimed = self.claim()
        after_pause = []
        def refuse(binding):
            self.goal_bindings.operator_stop(binding.id, scope=self.actor.tenant,
                actor_id=self.actor.identity_id, cancelled=False, reason='Current operator pause')
            after_pause.append(self.goal_bindings.store.load())
            raise RuntimeError()
        self.home_goal.context = refuse
        result = await self.home_goal._dispatch(claimed, scope=self.actor.tenant)
        self.assertEqual((result.outcome, result.reason), ('not_claimed', 'canonical_binding_changed'))
        self.assertEqual(self.readable()[1], after_pause[0])
        self.no_input()

    async def test_empty_failure_preserves_new_lease_owner(self):
        claimed = self.claim()
        after_claim = []
        def refuse(binding):
            self.goal_bindings.release_continuation(binding.id, scope=self.actor.tenant,
                owner_id=self.goal_continuation.owner_id, status=GoalExecutionBindingStatus.IDLE,
                reason='Original owner releases')
            renewed = self.goal_bindings.claim_continuation(binding.id, scope=self.actor.tenant,
                owner_id='other-current-owner', lease_seconds=60.)
            self.assertIsNotNone(renewed)
            after_claim.append(self.goal_bindings.store.load())
            raise RuntimeError()
        self.home_goal.context = refuse
        result = await self.home_goal._dispatch(claimed, scope=self.actor.tenant)
        self.assertEqual(result.outcome, 'not_claimed')
        current, state = self.readable()
        self.assertEqual(current.lease_owner_id, 'other-current-owner')
        self.assertEqual(state, after_claim[0])
        self.no_input()

    async def uncertain_after_real_queue(self, error):
        claimed = self.claim()
        real = self.turns.start
        async def accepted_then_lost(*args, **kwargs):
            await real(*args, **kwargs)
            raise error
        self.turns.start = accepted_then_lost
        if isinstance(error, asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await self.home_goal._dispatch(claimed, scope=self.actor.tenant)
        else:
            result = await self.home_goal._dispatch(claimed, scope=self.actor.tenant)
            self.assertEqual((result.outcome, result.reason), ('unknown', 'RuntimeError'))
        originals = self.queue_store.get('operator-thread')
        self.assertEqual(len(originals), 1)
        original = originals[0].model_dump(mode='json')
        current, _ = self.readable()
        self.assertEqual(current.status, 'unknown')
        self.assertEqual(current.stop_reason, 'Home Goal input outcome requires original reconciliation')
        self.assertEqual(current.home_continuation.delivery.phase, 'unknown')
        self.assertEqual(current.home_continuation.reserved_calls, 1)
        await self.goal_continuation.dispatch_once(current.id, scope=self.actor.tenant)
        self.assertEqual([row.model_dump(mode='json') for row in self.queue_store.get('operator-thread')], [original])
        self.assertEqual(self.starts(), [])
        self.transport.request.assert_not_awaited()

    async def test_empty_failure_after_queue_retains_one_unknown_original(self):
        await self.uncertain_after_real_queue(RuntimeError())

    async def test_empty_cancellation_after_queue_retains_original_and_reraises(self):
        await self.uncertain_after_real_queue(asyncio.CancelledError())


def load_tests(loader, standard_tests, pattern):
    return unittest.TestSuite(HomeGoalEmptyExceptionTests(name)
        for name in HomeGoalEmptyExceptionTests.__dict__ if name.startswith('test_'))
