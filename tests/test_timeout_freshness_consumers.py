"""Real late-readiness and watchdog consumers keep independent freshness semantics."""
import asyncio
import os
import threading
import time
import unittest
from unittest.mock import patch

from codex_web.models import WorkItemHandoff, WorkItemHandoffDelivery, WorkItemState
from codex_web.services.work_item_watchdog_candidates import WorkItemWatchdogCandidatePolicy
from tests import test_late_ready_completion as late


class DisabledHandoffCandidateTests(unittest.TestCase):
    def setUp(self):
        self.duration = 0
        self.findings = []
        self.policy = WorkItemWatchdogCandidatePolicy(
            load_states=lambda: {}, split_brain_findings=lambda _: self.findings,
            handoff_timeout_seconds=lambda: self.duration,
            handoff_delivery_timeout_seconds=lambda: self.duration,
            coerce_owner=lambda value: value, sla_threshold_seconds=lambda _: 10)

    def state(self, delivery):
        return WorkItemState(ref='project!33', project_id='project', current_owner='quinn',
            current_stage='implementation_active', created_at=1, updated_at=1,
            last_meaningful_update_at=1,
            handoff=WorkItemHandoff(from_agent='james', to_agent='quinn', requested_at=1,
                delivery=delivery))

    def deliveries(self):
        return [None, WorkItemHandoffDelivery(), WorkItemHandoffDelivery(status='delivered',
            started_at=2, thread_id='thread', turn_id='actual-turn', execution_id='execution')]

    def test_disabled_windows_never_emit_time_based_pending_or_stale_candidates(self):
        for self.duration in (0, -1):
            for delivery in self.deliveries():
                with self.subTest(duration=self.duration, delivery=delivery):
                    state = self.state(delivery)
                    original = state.model_dump(mode='json')
                    self.assertIsNone(self.policy.orchestrator_reason(state, now=10000))
                    self.assertEqual(state.model_dump(mode='json'), original)

    def test_disabled_windows_keep_non_time_based_split_brain_and_blocked_reasons(self):
        for delivery in self.deliveries():
            state = self.state(delivery)
            state.current_stage = 'failed_with_action_owner'
            self.assertEqual(self.policy.orchestrator_reason(state, now=10000)[0], 'blocked_lane')
            self.findings = ['actual owner mismatch']
            self.assertEqual(self.policy.orchestrator_reason(state, now=10000)[0], 'split_brain')
            self.findings = []

    def test_positive_windows_still_emit_delivery_and_ack_candidates(self):
        self.duration = 600
        for delivery, expected in zip(self.deliveries(),
                ['pending_handoff', 'handoff_delivery_expired', 'pending_handoff']):
            self.assertEqual(self.policy.orchestrator_reason(self.state(delivery), now=10000)[0], expected)
        # The preexisting early legacy warning retains its independent300s cap.
        self.assertEqual(self.policy.orchestrator_reason(self.state(None), now=350)[0], 'pending_handoff')


class DisabledLateReadyFreshnessTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = late.LateReadyCompletionTests.asyncSetUp
    asyncTearDown = late.LateReadyCompletionTests.asyncTearDown
    _assignment = staticmethod(late.LateReadyCompletionTests._assignment)
    _publish_worker_authority = late.LateReadyCompletionTests._publish_worker_authority
    _publish_profile = late.LateReadyCompletionTests._publish_profile
    _request = late.LateReadyCompletionTests._request
    change_assignment = late.LateReadyCompletionTests.change_assignment
    submit = late.LateReadyCompletionTests.submit
    respond = late.LateReadyCompletionTests.respond
    prepare_target = late.LateReadyCompletionTests.prepare_target
    build = late.LateReadyCompletionTests.build
    prepare = late.LateReadyCompletionTests.prepare
    new_callback = late.LateReadyCompletionTests.new_callback
    terminal_runtime = late.LateReadyCompletionTests.terminal_runtime
    configured_runtime = late.LateReadyCompletionTests.configured_runtime
    mark_inflight = late.LateReadyCompletionTests.mark_inflight
    terminal_before_result = late.LateReadyCompletionTests.terminal_before_result
    late_result = late.LateReadyCompletionTests.late_result
    join_followups = late.LateReadyCompletionTests.join_followups
    assert_originals = late.LateReadyCompletionTests.assert_originals

    def set_duration(self, duration):
        def update(state):
            state.intents = [row.model_copy(update={'timeout_seconds': duration})
                if row.id == self.intent_id else row for row in state.intents]
            return state
        self.runtime.intents.store.update(update, intent_ids=(self.intent_id,))

    async def assert_fresh_disabled_settles(self, duration):
        runtime, _, _ = await self.terminal_before_result()
        self.set_duration(duration)
        with patch.dict(os.environ, {'CODEX_WEB_LOCAL_PREPARATION_TIMEOUT_SECONDS': '0'}):
            await self.late_result(runtime)
        history = self.assert_originals()
        self.assertEqual(history['intent']['status'], 'succeeded')
        self.assertTrue(history['verifications'][-1]['verified'])
        self.assertTrue(all(method == 'GET' for method, _ in self.calls[self.original_calls:]))

    async def test_zero_action_timeout_allows_fresh_original_completion(self):
        await self.assert_fresh_disabled_settles(0)

    async def test_minus_one_action_timeout_allows_fresh_original_completion(self):
        await self.assert_fresh_disabled_settles(-1)

    async def test_disabled_action_timeout_still_refuses_older_than_independent300s_freshness(self):
        runtime, _, _ = await self.terminal_before_result()
        self.set_duration(0)
        future_now = time.time() + 301
        with patch('codex_web.runtime.readiness_completion.time.time', return_value=future_now), \
                patch.object(runtime, '_session_for_assignment', wraps=runtime._session_for_assignment) as custody:
            await self.late_result(runtime)
            custody.assert_not_called()  # Freshness refuses before native authority reads.
        self.assertEqual(len(self.calls), self.original_calls)
        self.assertEqual(self.assert_originals()['intent']['status'], 'requires_reconciliation')

    async def test_cancelled_terminal_event_read_joins_actual_producer(self):
        runtime, _, journal = await self.terminal_before_result()
        self.set_duration(0)
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        release, finished = threading.Event(), threading.Event()
        def read(**kwargs):
            loop.call_soon_threadsafe(entered.set)
            try:
                release.wait()
                return journal.recent(**kwargs)
            finally:
                finished.set()
        runtime.readiness_terminal_events = read
        follow = None
        try:
            with patch.dict(os.environ, {'CODEX_WEB_LOCAL_PREPARATION_TIMEOUT_SECONDS': '0'}):
                await asyncio.to_thread(self.runtime.intents._ready_reconciliation,
                    self.intent_id, 'Private late result while owned event read is held')
                await entered.wait()
                follow = next(iter(runtime.terminal_recovery_tasks.values()))
                follow.cancel()
                await asyncio.sleep(0)
                self.assertFalse(follow.done())
                self.assertFalse(finished.is_set())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await follow
                self.assertTrue(finished.is_set())
            self.assertEqual(len(self.calls), self.original_calls)
            self.assertEqual(self.assert_originals()['intent']['status'], 'requires_reconciliation')
        finally:
            release.set()
            if follow is not None:
                await asyncio.gather(follow, return_exceptions=True)
