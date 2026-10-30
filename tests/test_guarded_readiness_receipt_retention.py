"""A completed provider read remains historical truth after parent expiry.

Actual canonical publisher/procedure/intent/receipt owners are used. Only provider
HTTP is synthetic; no native or model process is launched. The injected clock
moves solely the procedure's wall deadline after its real receipt has committed.
"""
import json
import unittest
import warnings
from unittest.mock import patch

from codex_web.action_intents import ActionIntentStatus
from codex_web.execution_workers import AssignmentRenewRequest
from codex_web.services.code_host_readiness_observation import observation_outputs
from tests import test_mr_guarded_readonly_budget as guarded


class GuardedReadinessReceiptRetentionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = guarded.GuardedReadinessCompositionTests.asyncSetUp
    asyncTearDown = guarded.GuardedReadinessCompositionTests.asyncTearDown

    async def test_expired_parent_before_fresh_verification_retains_original_completed_receipt(self):
        f = self.f
        f.configure(120)
        await guarded.publish_guarded_skill(f)
        current = f.workers.store.assignment(f.assignment.id)
        # A legitimate same-fence renewal isolates parent expiry from lease
        # expiry; it does not replace or extend the procedure/step deadline.
        f.workers.renew(f.worker.id, current.id, AssignmentRenewRequest(
            lease_token=current.lease.lease_token, fence=current.fence, lease_seconds=300), actor=f.worker_actor)
        published = await f.publish_v2()
        started = await f.start_snapshot(published['procedure_input']['id'])
        captured = {}
        original = f.intents._verify_completion
        real_clock = f.runner.clock

        async def expire_after_durable_receipt(intent, result, *, actor, **kwargs):
            history = f.intents.store.load(intent_id=intent.id)
            self.assertEqual(len(history.receipts), 1)
            receipt = history.receipts[0]
            self.assertEqual(receipt.outcome, 'completed')
            self.assertEqual(receipt.result.model_dump(mode='json'), result.model_dump(mode='json'))
            self.assertNotIn(f.expected_fixture_credential, receipt.model_dump_json())
            outputs = observation_outputs(result.output, binding_id=intent.binding_id,
                resource_id=f.target.id, provider_instance=intent.provider_instance,
                parameters=intent.request.parameters, observed_at=receipt.received_at)
            self.assertEqual(outputs['readiness'], 'blocked')
            self.assertEqual(outputs['independent_review'], 'fail')
            captured.update(receipt=receipt.model_dump(mode='json'), intent_id=intent.id,
                            calls=len(f.observed_calls), outputs=outputs)
            deadline = f.runner.readonly_operation_deadline(intent, f.admin)
            f.runner.clock = lambda: deadline + 0.001
            return await original(intent, result, actor=actor, **kwargs)

        try:
            with warnings.catch_warnings(record=True) as emitted:
                warnings.simplefilter('always', RuntimeWarning)
                with patch.object(f.intents, '_verify_completion', side_effect=expire_after_durable_receipt):
                    await f.advance_run(started.procedure_checkpoint.id)
        finally:
            f.runner.clock = real_clock

        self.assertFalse([str(row.message) for row in emitted
                          if issubclass(row.category, RuntimeWarning) and 'never awaited' in str(row.message)])
        self.assertTrue(captured, 'The real provider receipt must commit before expiry injection')
        intent = f.intents.get(captured['intent_id'], f.admin)
        self.assertEqual(intent.status, ActionIntentStatus.REQUIRES_RECONCILIATION)
        history = f.intents.store.load(intent_id=intent.id)
        self.assertEqual([r.model_dump(mode='json') for r in history.receipts], [captured['receipt']])
        self.assertEqual(intent.last_receipt_id, captured['receipt']['id'])
        self.assertTrue(history.verifications)
        self.assertFalse(any(row.verified for row in history.verifications))
        self.assertIn('procedure_guarded_observation_budget_exhausted',
                      json.dumps([row.model_dump(mode='json') for row in history.verifications]))
        current = f.workers.store.assignment(f.assignment.id)
        self.assertNotEqual(current.procedure_checkpoint.status, 'completed')
        step = current.procedure_checkpoint.steps['observe']
        self.assertNotEqual(step.status, 'succeeded')
        self.assertIsNone(step.output_snapshot_id)
        self.assertEqual(len(f.observed_calls), captured['calls'])  # No fresh verification GET.

        # An ordinary repeat cannot replay the completed observation to resolve
        # its missing verification, even after restoring the test clock.
        await f.advance_run(started.procedure_checkpoint.id)
        self.assertEqual(len(f.observed_calls), captured['calls'])
        retained = f.intents.store.load(intent_id=intent.id)
        self.assertEqual([r.model_dump(mode='json') for r in retained.receipts], [captured['receipt']])
        self.assertNotEqual(f.workers.store.assignment(f.assignment.id).procedure_checkpoint.status, 'completed')
        f.runner.models.invoke.assert_not_called()
