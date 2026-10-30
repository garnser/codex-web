"""Request-local stage diagnostics preserve real verification owners and failures."""
import asyncio
import math
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from codex_web import guarded_verification_telemetry as telemetry
from codex_web.code_host_read_budget import ReadExecutionBudget
from codex_web.observability import JsonFormatter, correlated, current_correlation
from codex_web.services.code_host_action_contract import (
    CODE_HOST_PULL_REQUEST_OBSERVE_READINESS_GUARDED_ACTION_ID as GUARDED,
)
from tests import test_action_intents as ordinary
from tests import test_mr_guarded_readonly_budget as guarded


class GuardedVerificationStageTimingTests(unittest.IsolatedAsyncioTestCase):
    def intent(self, **updates):
        fields = dict(id='intent-private-stage', action_id=GUARDED,
                      correlation_id='correlation-private-stage',
                      execution_id='execution-private-stage',
                      organization_id='organization-private-stage',
                      workspace_id='workspace-private-stage', work_item_ref=None)
        fields.update(updates)
        return SimpleNamespace(**fields)

    def events(self, captured, intent):
        rows = [record.structured for record in captured.records]
        self.assertTrue(rows)
        for record, row in zip(captured.records, rows):
            fields = {'event', 'correlation_id', 'tenant_id', 'workspace_id',
                      'execution_id', 'action_intent_id', 'stage', 'outcome', 'duration_seconds'}
            if intent.work_item_ref is not None:
                fields.add('work_item_ref')
                self.assertEqual(row['work_item_ref'], intent.work_item_ref)
            self.assertEqual(set(row), fields)
            self.assertEqual(row['event'], 'guarded_readonly.verify.stage')
            self.assertEqual(row['correlation_id'], intent.correlation_id)
            self.assertEqual(row['action_intent_id'], intent.id)
            self.assertEqual(row['execution_id'], intent.execution_id)
            self.assertEqual(row['tenant_id'], intent.organization_id)
            self.assertEqual(row['workspace_id'], intent.workspace_id)
            self.assertIn(row['stage'], {'verification', 'provider_verification',
                'admission', 'inputs', 'custody', 'http'})
            self.assertIn(row['outcome'], {'completed', 'refused'})
            self.assertTrue(math.isfinite(row['duration_seconds']))
            self.assertGreaterEqual(row['duration_seconds'], 0)
            self.assertIsNone(record.exc_info)
            serialized = JsonFormatter().format(record)
            for private in ('PRIVATE-CONTENT', 'private.invalid', 'Traceback'):
                self.assertNotIn(private, serialized)
        return rows

    def assert_closed(self):
        self.assertIsNone(telemetry._scope.get())
        self.assertIsNone(current_correlation())

    async def test_ordinary_stages_and_legacy_scope_emit_nothing(self):
        with patch('codex_web.observability.log_event') as emit:
            with telemetry.verification_stage('inputs'):
                pass
            with telemetry.guarded_verification_timings(self.intent(action_id='reference.set')):
                with telemetry.verification_stage('custody'):
                    pass
                result = await ReadExecutionBudget(200, clock=lambda: 100).request(
                    asyncio.sleep(0, result='ordinary-result'))
            self.assertEqual(result, 'ordinary-result')
            emit.assert_not_called()
        self.assert_closed()

    async def test_exact_scope_crosses_thread_guard_and_real_read_budget(self):
        intent = self.intent()
        self.assertIsNone(current_correlation())  # No ambient worker correlation.

        def guard():
            with telemetry.verification_stage('admission'):
                with telemetry.verification_stage('inputs'):
                    with telemetry.verification_stage('custody'):
                        return 'PRIVATE-CONTENT'

        with self.assertLogs(telemetry._logger, level='INFO') as captured:
            with telemetry.guarded_verification_timings(intent):
                self.assertEqual(await asyncio.to_thread(guard), 'PRIVATE-CONTENT')
                value = await ReadExecutionBudget(200, clock=lambda: 100).request(
                    asyncio.sleep(0, result='PRIVATE-CONTENT'))
                self.assertEqual(value, 'PRIVATE-CONTENT')
            # A later ordinary thread read must not inherit the closed scope.
            await asyncio.to_thread(guard)
        rows = self.events(captured, intent)
        self.assertEqual(sorted(row['stage'] for row in rows),
                         ['admission', 'custody', 'http', 'inputs'])
        self.assertTrue(all(row['outcome'] == 'completed' for row in rows))
        self.assert_closed()

    async def test_timeout_and_cancellation_keep_original_failure_and_close_scope(self):
        intent = self.intent()
        for failure in (TimeoutError('PRIVATE-CONTENT https://private.invalid'),
                        asyncio.CancelledError('PRIVATE-CONTENT')):
            with self.subTest(kind=type(failure).__name__):
                async def fail():
                    raise failure
                budget = ReadExecutionBudget(200, clock=lambda: 100)
                with self.assertLogs(telemetry._logger, level='INFO') as captured:
                    with self.assertRaises(type(failure)) as caught:
                        with telemetry.guarded_verification_timings(intent):
                            await budget.request(fail())
                self.assertIs(caught.exception, failure)
                rows = self.events(captured, intent)
                self.assertEqual([(row['stage'], row['outcome']) for row in rows],
                                 [('http', 'refused')])
                self.assertGreaterEqual(budget.network_seconds_used, 0)
                self.assert_closed()

    async def test_logging_failure_changes_neither_result_nor_original_refusal(self):
        calls = []
        failure = TimeoutError('PRIVATE-CONTENT original timeout')
        with patch('codex_web.observability.log_event', side_effect=asyncio.CancelledError):
            with telemetry.guarded_verification_timings(self.intent()):
                with telemetry.verification_stage('inputs'):
                    calls.append('successful-body')
                with self.assertRaises(TimeoutError) as caught:
                    with telemetry.verification_stage('custody'):
                        calls.append('refused-body')
                        raise failure
        self.assertIs(caught.exception, failure)
        self.assertEqual(calls, ['successful-body', 'refused-body'])
        self.assert_closed()

    async def test_stage_instrumentation_enter_and_exit_failure_never_changes_body(self):
        class BrokenStage:
            def __init__(self, boundary):
                self.boundary = boundary

            def __enter__(self):
                if self.boundary == 'enter':
                    raise RuntimeError('PRIVATE-CONTENT instrumentation entry')

            def __exit__(self, *_args):
                raise RuntimeError('PRIVATE-CONTENT instrumentation exit')

        for boundary in ('enter', 'exit'):
            for failure in (None, TimeoutError('PRIVATE-CONTENT original timeout'),
                            asyncio.CancelledError('PRIVATE-CONTENT original cancellation')):
                with self.subTest(boundary=boundary, failure=type(failure).__name__):
                    calls = []

                    def body():
                        with telemetry.guarded_verification_timings(self.intent()):
                            with telemetry.verification_stage('inputs'):
                                calls.append('body-once')
                                if failure is not None:
                                    raise failure
                                return 'original-result'

                    with patch.object(telemetry, 'operation_stage',
                                      return_value=BrokenStage(boundary)):
                        if failure is None:
                            self.assertEqual(body(), 'original-result')
                        else:
                            with self.assertRaises(type(failure)) as caught:
                                body()
                            self.assertIs(caught.exception, failure)
                    self.assertEqual(calls, ['body-once'])
                    self.assert_closed()

    async def test_nested_scope_restores_exact_outer_correlation_and_rejects_open_stage(self):
        outer = self.intent()
        inner = self.intent(id='intent-inner', correlation_id='correlation-inner',
                            execution_id='execution-inner', work_item_ref='canonical/work#2')
        with correlated(correlation_id='ambient-parent', action_intent_id='ambient-intent',
                        work_item_ref='foreign/work#1', causation_id='foreign-causation'):
            parent = current_correlation()
            with telemetry.guarded_verification_timings(outer):
                outer_context = current_correlation()
                self.assertIsNone(outer_context.work_item_ref)
                self.assertIsNone(outer_context.causation_id)
                with self.assertLogs(telemetry._logger, level='INFO') as captured:
                    with telemetry.verification_stage('inputs'):
                        pass
                self.events(captured, outer)
                with telemetry.guarded_verification_timings(inner):
                    self.assertEqual(current_correlation().action_intent_id, inner.id)
                    self.assertEqual(current_correlation().work_item_ref, inner.work_item_ref)
                self.assertEqual(current_correlation(), outer_context)
                self.assertIsNone(current_correlation().work_item_ref)
                with patch('codex_web.observability.log_event') as emit:
                    with telemetry.verification_stage('PRIVATE-CONTENT https://private.invalid'):
                        pass
                    emit.assert_not_called()
            self.assertEqual(current_correlation(), parent)
            self.assertEqual(current_correlation().work_item_ref, 'foreign/work#1')
            self.assertEqual(current_correlation().causation_id, 'foreign-causation')
        self.assert_closed()

    async def test_real_guarded_completion_attributes_verification_without_ambient_context(self):
        f = guarded.policy_fixture.MRObservationPolicyTests(
            'test_v2_real_publish_start_readonly_receipt_evidence_after_47_seconds')
        await f.asyncSetUp()
        try:
            f.broker_response_timeout = 150
            f.configure(180)
            await guarded.publish_guarded_skill(f)
            publication = await f.publish_v2()
            started = await f.start_snapshot(publication['procedure_input']['id'])
            self.assertIsNone(current_correlation())
            with self.assertLogs(telemetry._logger, level='INFO') as captured:
                advanced = await f.advance_run(started.procedure_checkpoint.id)
            step = advanced.procedure_checkpoint.steps['observe']
            self.assertEqual(step.status, 'succeeded', advanced.procedure_checkpoint.failure_code)
            intent = f.intents.get(step.action_intent_id, f.admin)
            rows = self.events(captured, intent)
            self.assertLessEqual(len(rows), 100)
            stages = [row['stage'] for row in rows]
            # This canonical fixture is generic, not native sealed custody.
            for stage in ('verification', 'provider_verification', 'admission', 'inputs', 'http'):
                self.assertIn(stage, stages)
            self.assertEqual(stages.count('verification'), 1)
            history = f.intents.store.load(intent_id=intent.id)
            self.assertEqual(len(history.receipts), 1)
            self.assertEqual(history.receipts[0].outcome, 'completed')
            self.assertTrue(history.verifications[-1].verified)
            calls = len(f.observed_calls)
            completed = await f.advance_run(started.procedure_checkpoint.id)
            self.assertEqual(completed.procedure_checkpoint.status, 'completed')
            self.assertEqual(len(f.observed_calls), calls)
            f.runner.models.invoke.assert_not_called()
            self.assert_closed()
        finally:
            await f.asyncTearDown()
            f.doCleanups()

    async def test_real_ordinary_action_remains_silent(self):
        f = ordinary.ActionIntentTests('test_concurrent_claims_have_one_owner_and_recheck_stale_candidates')
        await f.asyncSetUp()
        try:
            with patch('codex_web.observability.log_event') as emit:
                intent = f._create()
                completed = await f._execute(intent)
                self.assertEqual(completed.status, 'succeeded')
            stage_calls = [call for call in emit.call_args_list
                           if 'guarded_readonly.verify.stage' in call.args]
            self.assertEqual(stage_calls, [])
            self.assert_closed()
        finally:
            await f.asyncTearDown()
            f.doCleanups()
