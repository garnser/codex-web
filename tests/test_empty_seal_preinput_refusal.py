"""Real empty credential CAS failures; no process, provider or historical repair."""
import unittest

from dataclasses import replace
from codex_web.runtime.codex import CodexRuntime
from codex_web.runtime.execution import ExecutionPrestartRefusal
from codex_web.services.procedure_execution import ProcedureExecutionService
from tests.test_empty_native_preparation_sealing import EmptyNativePreparationSealingTests
from tests.test_project_goal_worker_admission import ProjectGoalWorkerAdmissionTests


CODE = 'procedure_empty_preparation_unavailable'


def failing_guarded_context(discovery, lock, observations, *, blockers=('private_current_context_unavailable',)):
    """Cause the real discovery owner to return failed only inside credential CAS."""
    original = discovery.assignment_context
    def resolve(*args, **options):
        context = original(*args, **options)
        guarded = lock._is_owned()
        observations.append(guarded)
        if guarded:
            return replace(context, resolution_blockers=blockers)
        return context
    discovery.assignment_context = resolve


def failing_guarded_capacity(discovery, state, lock, observations):
    original = discovery.context_capacity_resolver
    if original is None:
        from codex_web.services.configuration import ConfigurationService
        from codex_web.storage.configuration_registry import ConfigurationRegistryStore
        from codex_web.services.procedure_configuration import install_procedure_configuration
        configuration = ConfigurationService(ConfigurationRegistryStore(state))
        spec = install_procedure_configuration(configuration)
        original = lambda: configuration.resolve(spec.key)
    def capacity():
        if lock._is_owned():
            observations.append(True)
            raise OSError('private capacity reader unavailable')
        return original()
    discovery.context_capacity_resolver = capacity


class EmptySealOwnerRefusalTests(EmptyNativePreparationSealingTests):
    async def assert_closed_failure(self, *, zero_candidates):
        if zero_candidates:
            self.f.definition_registry.store.update(lambda rows: [r for r in rows if r.kind != 'agent.skill'])
        observations = []
        failing_guarded_context(self.f.discovery, self.f.workers.store._lock, observations)
        error = None
        try:
            await self.env.admit()
        except Exception as caught:
            error = caught
        self.assertIn(True, observations, 'Real credential writer validation was not reached')
        self.assertIn(False, observations, 'Initial discovery must succeed outside the writer')
        self.assert_unsealed()
        self.assertIsInstance(error, ExecutionPrestartRefusal,
            'Closed canonical empty-seal observation failure must be typed at its pre-input owner')
        self.assertEqual(error.detail['code'], CODE)
        self.assertIs(error.detail['retryable'], False)
        self.assertNotIn('private metadata', str(error.detail))

    async def test_attested_empty_seal_failed_discovery_is_typed_local_refusal(self):
        await self.assert_closed_failure(zero_candidates=False)

    async def test_zero_candidate_seal_failed_discovery_is_typed_local_refusal(self):
        await self.assert_closed_failure(zero_candidates=True)

    async def assert_opaque_capacity_failure(self, *, zero_candidates):
        if zero_candidates:
            self.f.definition_registry.store.update(lambda rows: [r for r in rows if r.kind != 'agent.skill'])
        observations, reached = [], []
        # Deliberately match the catcher's safe blocker text: equality of a
        # failed DTO is not evidence that its early resolution branch ran.
        failing_guarded_context(self.f.discovery, self.f.workers.store._lock, observations,
            blockers=('procedure_discovery_failed:OSError',))
        failing_guarded_capacity(self.f.discovery, self.f.sqlite, self.f.workers.store._lock, reached)
        with self.assertRaises(ValueError) as caught:
            await self.env.admit()
        self.assertNotIsInstance(caught.exception, ExecutionPrestartRefusal)
        self.assertIn(True, observations)
        self.assertEqual(reached, [True])
        self.assert_unsealed()

    async def test_attested_context_blockers_do_not_hide_opaque_capacity_failure(self):
        await self.assert_opaque_capacity_failure(zero_candidates=False)

    async def test_zero_candidate_context_blockers_do_not_hide_opaque_capacity_failure(self):
        await self.assert_opaque_capacity_failure(zero_candidates=True)

    async def test_arbitrary_matching_exception_inside_seal_remains_unclassified(self):
        original = self.f.discovery.assignment_context
        marker = ValueError('Current empty native preparation observation is unavailable')
        seen = []
        def unavailable(*args, **options):
            context = original(*args, **options)
            if self.f.workers.store._lock._is_owned():
                seen.append(True)
                raise marker
            return context
        self.f.discovery.assignment_context = unavailable
        with self.assertRaises(ValueError) as caught:
            await self.env.admit()
        self.assertIs(caught.exception, marker)
        self.assertEqual(seen, [True])
        self.assert_unsealed()

    async def test_arbitrary_attestor_failure_is_not_a_closed_seal_refusal(self):
        await super().test_attestor_failure_is_not_reclassified_or_ignored()


class QueuedGoalEmptySealRefusalTests(ProjectGoalWorkerAdmissionTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.procedures = ProcedureExecutionService(workers=self.f.workers,
            skills=self.discovery.skills, discovery=self.discovery, intents=self.intents,
            evidence=None, identity=self.f.identity, authority=self.authority,
            approvals=None, attention=None, control_actor=self.actor)
        self.admission.procedure_execution = self.procedures
        # This fixture has a controlled external native backend, not physical
        # Bubblewrap attestation. Exercise the supported plain zero-candidate
        # seal, without presenting its fake process as an attested session.
        self.admission.native_session_attestor = None
        self.admission.native_session_preparer = None
        self.assertIsNotNone(self.target.native_credential_custody)
        self.assertIsNone(self.target.native_credential_custody.admitted_at)
        self.assertEqual(self.target.resource_ids, ())

    async def test_queued_goal_closed_seal_failure_preserves_same_blocked_original(self):
        await self.queued_goal()
        queued = self.queue_store.get(self.thread)
        self.assertEqual(len(queued), 1)
        snapshot = queued[0].model_dump(mode='json')
        attempt_id = 'preflight-queued-'+queued[0].id
        observations = []
        failing_guarded_context(self.discovery, self.f.workers.store._lock, observations)
        await self.execution.drain_thread_queue(self.thread)
        current = self.execution.queued_preflight.store.get(attempt_id)
        self.assertIn(True, observations, self.native_diagnostics())
        self.assertEqual(self.starts(), [], self.native_diagnostics())
        self.transport.request.assert_not_awaited()
        self.assertEqual(current.queued_turn_snapshot, snapshot)
        self.assertTrue(current.queued_admission_claim_id)
        self.assertIsNone(current.queued_start_correlation)
        self.assertIsNone(current.queued_start_evidence)
        self.assertIsNone(current.queued_start_receipt)
        self.assertIsNone(current.started_at)
        assignment = self.f.workers.store.assignment(self.target.id)
        self.assertIsNone(assignment.native_credential_custody.admitted_at)
        self.assertEqual(assignment.secret_refs, self.target.secret_refs)
        self.assertEqual(current.status, 'blocked', self.native_diagnostics())
        self.assertEqual([row.code for row in current.blockers], [CODE])
        self.assertFalse(current.blockers[0].retryable)
        self.assertEqual(len(self.execution.queued_preflight.store.for_thread(self.thread)), 1)

    async def test_queued_arbitrary_matching_exception_preserves_unknown_original(self):
        await self.queued_goal()
        queued = self.queue_store.get(self.thread)[0]
        original = self.discovery.assignment_context
        seen = []
        def unavailable(*args, **options):
            context = original(*args, **options)
            if self.f.workers.store._lock._is_owned():
                seen.append(True)
                raise ValueError('Current empty native preparation observation is unavailable')
            return context
        self.discovery.assignment_context = unavailable
        await self.execution.drain_thread_queue(self.thread)
        current = self.execution.queued_preflight.store.get('preflight-queued-'+queued.id)
        self.assertEqual(seen, [True], self.native_diagnostics())
        self.assertEqual(self.starts(), [], self.native_diagnostics())
        self.assertEqual(current.status, 'reconciliation_required')
        self.assertEqual([row.code for row in current.blockers], ['native_start_outcome_unknown'])
        self.assertEqual(current.queued_turn_snapshot, queued.model_dump(mode='json'))
        self.assertFalse(self.execution.queued_preflight.recorded_prestart_refusal(current))

    async def test_queued_context_blockers_plus_opaque_capacity_failure_preserves_unknown(self):
        await self.queued_goal()
        queued = self.queue_store.get(self.thread)[0]
        observations, reached = [], []
        failing_guarded_context(self.discovery, self.f.workers.store._lock, observations,
            blockers=('procedure_discovery_failed:OSError',))
        failing_guarded_capacity(self.discovery, self.f.sqlite, self.f.workers.store._lock, reached)
        await self.execution.drain_thread_queue(self.thread)
        current = self.execution.queued_preflight.store.get('preflight-queued-'+queued.id)
        self.assertIn(True, observations, self.native_diagnostics())
        self.assertEqual(reached, [True])
        self.assertEqual(self.starts(), [], self.native_diagnostics())
        self.transport.request.assert_not_awaited()
        self.assertEqual(current.status, 'reconciliation_required')
        self.assertEqual([row.code for row in current.blockers], ['native_start_outcome_unknown'])
        self.assertEqual(current.queued_turn_snapshot, queued.model_dump(mode='json'))
        self.assertIsNone(current.queued_start_correlation)
        self.assertIsNone(current.queued_start_receipt)
        self.assertFalse(self.execution.queued_preflight.recorded_prestart_refusal(current))
        self.assertEqual(len(self.execution.queued_preflight.store.for_thread(self.thread)), 1)

    async def test_actual_native_write_wins_over_matching_typed_refusal(self):
        await self.queued_goal()
        queued = self.queue_store.get(self.thread)[0]
        reply = self.native._send
        async def write_then_refuse(message, **options):
            if message['method'] == 'turn/start':
                # Real serialization and capture commit first. A transported
                # matching typed error cannot turn that possible input into a
                # local empty-seal refusal or authorize another input.
                await CodexRuntime._send(self.native, message, **options)
                raise ExecutionPrestartRefusal(status_code=409, detail={
                    'code': CODE, 'message': 'Current empty native preparation could not be validated before input.',
                    'retryable': False})
            return await reply(message, **options)
        self.native._send = write_then_refuse
        await self.execution.drain_thread_queue(self.thread)
        current = self.execution.queued_preflight.store.get('preflight-queued-'+queued.id)
        self.assertEqual(len(self.starts()), 1, self.native_diagnostics())
        self.assertEqual(current.status, 'reconciliation_required')
        self.assertIsNotNone(current.queued_start_correlation)
        self.assertIsNone(current.queued_start_receipt)
        self.assertEqual(current.queued_turn_snapshot, queued.model_dump(mode='json'))
        self.assertFalse(self.execution.queued_preflight.recorded_prestart_refusal(current))
        self.assertEqual(len(self.execution.queued_preflight.store.for_thread(self.thread)), 1)


def load_tests(loader, standard_tests, pattern):
    return unittest.TestSuite(cls(name) for cls in
        (EmptySealOwnerRefusalTests, QueuedGoalEmptySealRefusalTests)
        for name, method in cls.__dict__.items() if name.startswith('test_') and callable(method))
