"""Actual Goal/queued/procedure owners; policy changes before native input."""
import unittest

from codex_web.autonomy import AutonomyMode
from codex_web.runtime.codex import CodexRuntime
from tests.test_project_goal_worker_admission import ProjectGoalWorkerAdmissionTests


class ProjectGoalPreinputPolicyRefusalTests(ProjectGoalWorkerAdmissionTests):
    def original_attempt(self):
        rows = self.execution.queued_preflight.store.for_thread(self.thread)
        self.assertEqual(len(rows), 1)
        return rows[0]

    async def test_global_pause_at_procedure_authorizer_retains_typed_nonstart(self):
        await self.queued_goal()
        queued = self.queue_store.get(self.thread)
        self.assertEqual(len(queued), 1)
        attempt_id = 'preflight-queued-' + queued[0].id
        snapshot = queued[0].model_dump(mode='json')
        authorize = self.admission.input_authorizer
        seen = []

        def pause_then_authorize(**kwargs):
            # The actual queued claim already passed its initial admission.
            # Change real policy at the existing pre-input collaborator seam,
            # then invoke the exact application-composed authority owner.
            current = self.execution.queued_preflight.store.get(attempt_id)
            seen.append((current.status, current.queued_admission_claim_id))
            self.control = self.control.model_copy(update={'mode': AutonomyMode.PAUSED})
            return authorize(**kwargs)

        self.admission.input_authorizer = pause_then_authorize
        await self.execution.drain_thread_queue(self.thread)
        current = self.execution.queued_preflight.store.get(attempt_id)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], 'reconciliation_required')
        self.assertTrue(seen[0][1])
        self.assertEqual(self.starts(), [], self.native_diagnostics())
        self.transport.request.assert_not_awaited()
        self.assertEqual(current.queued_turn_snapshot, snapshot)
        self.assertEqual(current.queued_admission_claim_id, seen[0][1])
        self.assertIsNone(current.queued_start_correlation)
        self.assertIsNone(current.queued_start_evidence)
        self.assertIsNone(current.queued_start_receipt)
        self.assertIsNone(current.started_at)
        self.assertEqual(current.last_error, 'global_autonomy_paused', self.native_diagnostics())
        self.assertEqual(current.status, 'blocked', self.native_diagnostics())
        self.assertTrue(self.execution.queued_preflight.recorded_prestart_refusal(current),
            'A real local pre-input refusal must retain authenticated claim/snapshot evidence')
        # Recovery evidence is not permission to start while the hold remains.
        self.assertEqual(self.control.mode, AutonomyMode.PAUSED)
        self.assertEqual(self.starts(), [])

    async def test_pause_at_guard_construction_preserves_exact_nonstart(self):
        await self.queued_goal()
        factory = self.execution.native_input_guard_factory
        def pause_then_construct(**kwargs):
            self.control = self.control.model_copy(update={'mode': AutonomyMode.PAUSED})
            return factory(**kwargs)
        self.execution.native_input_guard_factory = pause_then_construct
        await self.execution.drain_thread_queue(self.thread)
        current = self.original_attempt()
        self.assertEqual(self.starts(), [], self.native_diagnostics())
        self.assertEqual(current.status, 'blocked', self.native_diagnostics())
        self.assertEqual(current.last_error, 'global_autonomy_paused')
        self.assertTrue(self.execution.queued_preflight.recorded_prestart_refusal(current))

    async def test_pause_at_native_writer_keeps_capture_owned_refusal(self):
        await self.late_native_refusal(lambda: setattr(self, 'control',
            self.control.model_copy(update={'mode': AutonomyMode.PAUSED})))
        current = self.original_attempt()
        self.assertEqual(current.status, 'queued', self.native_diagnostics())
        self.assertIsNone(current.queued_start_correlation)
        self.assertIsNone(current.queued_start_receipt)
        witnesses = [row for item in current.blocker_history for row in item.blockers
            if row.code == 'native_preinput_admission_refused']
        self.assertTrue(witnesses, self.native_diagnostics())
        correlation = witnesses[-1].model_dump(mode='json')['correlation']
        self.assertEqual(correlation['admission_claim_id'], current.queued_admission_claim_id)
        self.assertEqual(correlation['requested_execution_id'], current.execution_id)

    async def test_arbitrary_authorizer_value_error_is_not_nonstart_proof(self):
        await self.queued_goal()
        def unexpected(**kwargs):
            raise ValueError('global_autonomy_paused')
        self.admission.input_authorizer = unexpected
        await self.execution.drain_thread_queue(self.thread)
        current = self.original_attempt()
        self.assertEqual(current.status, 'reconciliation_required')
        self.assertFalse(self.execution.queued_preflight.recorded_prestart_refusal(current))
        self.assertEqual(self.starts(), [])

    async def test_identical_error_after_native_write_remains_unknown(self):
        await self.queued_goal()
        reply = self.native._send
        async def write_then_fail(message, **options):
            # Actual writer/capture/serialization, controlled external response
            # loss. Identical exception prose is not local nonstart authority.
            if message['method'] == 'turn/start':
                await CodexRuntime._send(self.native, message, **options)
                raise ValueError('global_autonomy_paused')
            return await reply(message, **options)
        self.native._send = write_then_fail
        await self.execution.drain_thread_queue(self.thread)
        current = self.original_attempt()
        self.assertEqual(len(self.starts()), 1, self.native_diagnostics())
        self.assertEqual(current.status, 'reconciliation_required')
        self.assertIsNotNone(current.queued_start_correlation)
        self.assertIsNone(current.queued_start_receipt)
        self.assertFalse(self.execution.queued_preflight.recorded_prestart_refusal(current))


def load_tests(loader, standard_tests, pattern):
    return unittest.TestSuite(ProjectGoalPreinputPolicyRefusalTests(name)
        for name, method in ProjectGoalPreinputPolicyRefusalTests.__dict__.items()
        if name.startswith('test_') and callable(method))
