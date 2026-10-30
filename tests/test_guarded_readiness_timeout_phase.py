"""Real canonical timeout owners retain closed phase metadata, never output."""
import math
import unittest
from unittest.mock import patch

from codex_web.action_intents import ActionIntentStatus
from tests import test_mr_guarded_readonly_budget as guarded
from tests import test_action_intents as legacy


class GuardedReadinessTimeoutPhaseTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = guarded.GuardedReadinessCompositionTests.asyncSetUp
    asyncTearDown = guarded.GuardedReadinessCompositionTests.asyncTearDown

    async def start(self):
        self.f.configure(180)
        await guarded.publish_guarded_skill(self.f)
        publication = await self.f.publish_v2()
        return await self.f.start_snapshot(publication['procedure_input']['id'])

    def assert_unknown(self, assignment, phase, *, resolved=True):
        f = self.f
        step = assignment.procedure_checkpoint.steps['observe']
        intent = f.intents.get(step.action_intent_id, f.admin)
        history = f.intents.store.load(intent_id=intent.id)
        self.assertEqual(intent.status, ActionIntentStatus.UNCERTAIN)
        self.assertEqual(assignment.procedure_checkpoint.status, 'reconciliation_required')
        self.assertEqual(len(history.receipts), 1)
        receipt = history.receipts[0]
        self.assertEqual(receipt.outcome, 'unknown')
        self.assertIsNone(receipt.result)
        self.assertEqual(history.verifications, [])
        self.assertEqual(intent.failure.source_native_code, 'TimeoutError')
        self.assertEqual(intent.failure.reason_code.value, 'action_timeout_unknown_outcome')
        self.assertEqual(receipt.details['timeout_phase'], phase)
        expected = {'timeout_phase', 'phase_elapsed_seconds', 'guarded_deadline_at',
                    'shared_remaining_at_phase_entry', 'shared_remaining_at_timeout'}
        self.assertEqual(set(receipt.details), {'reason', *expected})
        self.assertEqual(receipt.details['reason'], 'timeout')
        for key in expected:
            self.assertEqual(intent.failure.details[key], receipt.details[key])
            if key != 'timeout_phase' and receipt.details[key] is not None:
                self.assertIsInstance(receipt.details[key], (int, float))
                self.assertTrue(math.isfinite(receipt.details[key]))
                self.assertGreaterEqual(receipt.details[key], 0)
        if resolved:
            self.assertEqual(receipt.details['guarded_deadline_at'],
                             assignment.procedure_checkpoint.deadline_at)
        else:
            self.assertIsNone(receipt.details['guarded_deadline_at'])
            self.assertIsNone(receipt.details['shared_remaining_at_timeout'])
        self.assertNotIn('SYNTHETIC_EXCEPTION_SECRET', receipt.model_dump_json())
        self.assertNotIn('SYNTHETIC_EXCEPTION_SECRET', intent.failure.model_dump_json())
        self.assertNotIn(f.expected_fixture_credential, receipt.model_dump_json())
        self.assertEqual(step.outputs, {})
        self.assertEqual(step.evidence, {})
        f.runner.models.invoke.assert_not_called()
        return receipt

    async def test_provider_timeout_is_not_result_admission_timeout(self):
        started = await self.start()
        async def timeout(*args, **kwargs):
            raise TimeoutError('SYNTHETIC_EXCEPTION_SECRET https://not-retained.invalid/private')
        before = len(self.f.observed_calls)
        with patch.object(self.f.intents, '_execute_provider', side_effect=timeout):
            current = await self.f.advance_run(started.procedure_checkpoint.id)
        self.assert_unknown(current, 'provider_execution')
        self.assertEqual(len(self.f.observed_calls), before)

    async def test_actual_provider_return_then_result_admission_timeout_is_distinct(self):
        started = await self.start()
        before = len(self.f.observed_calls)
        observed = []
        def timeout(intent, result):
            observed.append(result.status)
            raise TimeoutError('SYNTHETIC_EXCEPTION_SECRET')
        with patch.object(self.f.runner, 'result_admission', side_effect=timeout):
            current = await self.f.advance_run(started.procedure_checkpoint.id)
        self.assertEqual(observed, ['succeeded'])
        self.assertGreater(len(self.f.observed_calls), before)
        self.assert_unknown(current, 'result_admission')
        # The existing failure semantics are intentionally retained: a result
        # returned to the local executor is not asserted to be a durable receipt.

    async def test_deadline_lookup_timeout_before_provider_has_no_resolved_budget(self):
        started = await self.start()
        original = self.f.intents._mark_executing
        resolver = patch.object(self.f.runner, 'readonly_operation_deadline',
                                side_effect=TimeoutError('SYNTHETIC_EXCEPTION_SECRET'))
        def mark(*args, **kwargs):
            result = original(*args, **kwargs)
            resolver.start()
            return result
        before = len(self.f.observed_calls)
        try:
            with patch.object(self.f.intents, '_mark_executing', side_effect=mark):
                current = await self.f.advance_run(started.procedure_checkpoint.id)
        finally:
            resolver.stop()
        self.assert_unknown(current, 'execute_admission', resolved=False)
        self.assertEqual(len(self.f.observed_calls), before)

    async def test_deadline_recheck_after_provider_return_is_not_actual_result_admission(self):
        started = await self.start()
        original = self.f.intents._execute_provider
        resolver = patch.object(self.f.runner, 'readonly_operation_deadline',
                                side_effect=TimeoutError('SYNTHETIC_EXCEPTION_SECRET'))
        observed = []
        async def execute(*args, **kwargs):
            result = await original(*args, **kwargs)
            observed.append(result.status)
            resolver.start()
            return result
        try:
            with patch.object(self.f.intents, '_execute_provider', side_effect=execute), \
                    patch.object(self.f.runner, 'result_admission',
                                 side_effect=AssertionError('admission must not begin')):
                current = await self.f.advance_run(started.procedure_checkpoint.id)
        finally:
            resolver.stop()
        self.assertEqual(observed, ['succeeded'])
        self.assert_unknown(current, 'result_admission_check')

    async def test_diagnostic_clock_failure_does_not_replace_original_timeout(self):
        started = await self.start()
        original = self.f.runner.clock
        unavailable = [False]
        def clock():
            if unavailable[0]:
                unavailable[0] = False
                raise ValueError('SYNTHETIC_EXCEPTION_SECRET')
            return original()
        async def timeout(*args, **kwargs):
            unavailable[0] = True
            raise TimeoutError('SYNTHETIC_EXCEPTION_SECRET')
        with patch.object(self.f.runner, 'clock', side_effect=clock), \
                patch.object(self.f.intents, '_execute_provider', side_effect=timeout):
            current = await self.f.advance_run(started.procedure_checkpoint.id)
        receipt = self.assert_unknown(current, 'provider_execution')
        self.assertIsNone(receipt.details['shared_remaining_at_timeout'])


class LegacyTimeoutPhaseCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_timeout_receipt_and_failure_details_are_unchanged(self):
        f = legacy.ActionIntentTests('test_timeout_records_unknown_receipt_and_uncertain_state')
        await f.asyncSetUp()
        try:
            await f.test_timeout_records_unknown_receipt_and_uncertain_state()
            state = f.service.store.load()
            self.assertEqual(len(state.receipts), 1)
            self.assertEqual(state.receipts[0].details, {'reason': 'timeout'})
            self.assertEqual(state.intents[0].failure.details, {'action_id': 'slow.write'})
        finally:
            await f.asyncTearDown()
            f.doCleanups()
