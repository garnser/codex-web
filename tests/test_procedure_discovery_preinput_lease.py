"""Canonical lease guard, native admission and retained queue; no provider I/O."""
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import HTTPException

from codex_web.models import ActiveThreadTurn
from codex_web.runtime.execution import ExecutionPrestartRefusal
from codex_web.services.execution_preflight import ExecutionPreflightService
from codex_web.services.execution_workers import WorkerLeaseError
from codex_web.services.procedure_discovery import install_native_procedure_admission
from codex_web.storage.execution_preflight import ExecutionPreflightStore
from tests import test_procedure_native_admission as native
from tests import test_turn_execution as turns


class ProcedureDiscoveryPreinputLeaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = native.ProductionNativeProcedureAdmissionTests()
        self.f.addCleanup = self.addCleanup
        self.f.addAsyncCleanup = self.addAsyncCleanup
        await self.f.asyncSetUp()
        self.addAsyncCleanup(self.f.asyncTearDown)
        self.host, binding, self.sessions, self.runtime = turns.TurnExecutionStartTests._service(self)
        self.host._project = lambda _: self.f.project
        self.runtime.control_actor = self.f.actor
        self.runtime.schedule_queue_drain = Mock()
        # Only assignment/session provisioning and provider transport are doubles.
        # The discovery registry, writer, assignment store and queue are real.
        binding.prepare = lambda **_: SimpleNamespace(
            assignment_id=self.f.native_assignment.id, workspace_id=self.f.workspace.id,
            execution_id=self.f.native_assignment.execution_id)
        self.sessions.session.workspace_path = Path(self.f.workspace.path)
        self.admission = install_native_procedure_admission(self.runtime, SimpleNamespace(),
            self.f.discovery, self.f.workers, self.f.history, self.f.actor, self.f.profiles)
        self.preflight = ExecutionPreflightService(ExecutionPreflightStore(self.f.sqlite))
        self.runtime.queued_preflight = self.preflight
        self.queued = self.runtime.enqueue_turn(thread_id='t1', project_id=self.f.project.id,
            execution_id=self.f.native_assignment.execution_id,
            message='Preserve this actual queued request.\nDo not replay it.', source='web',
            sandbox='workspace-write', approval_policy='on-request')
        self.original = self.queued.model_dump(mode='json')
        self.runtime.schedule_queue_drain.reset_mock()
        self.record = self.f.workers.record_procedure_discovery
        self.caught = []

    def expire_at_writer(self, assignment_id, discovery, **kwargs):
        def expire(state):
            state.assignments = [item.model_copy(update={'lease': item.lease.model_copy(
                update={'expires_at': 0})}) if item.id == assignment_id else item
                for item in state.assignments]
            return state
        self.f.workers.store.update_assignment(self.f.procedure_worker.id, assignment_id, expire)
        try:
            return self.record(assignment_id, discovery, **kwargs)
        except WorkerLeaseError as exc:
            self.caught.append(exc)
            raise

    def assert_no_native_input(self):
        self.assertFalse(any(method in {'thread/resume', 'turn/start'}
            for method, _ in self.sessions.session.requests))
        self.runtime.schedule_queue_drain.assert_not_called()

    async def test_actual_expired_discovery_guard_blocks_original_before_native_input(self):
        # OLD reaches the real live-lease guard and generic queued disposition;
        # its failure is status reconciliation_required, not a new-type import.
        with patch.object(self.f.workers, 'record_procedure_discovery', side_effect=self.expire_at_writer):
            await self.runtime.drain_thread_queue('t1')
        current = self.preflight.store.get('preflight-queued-' + self.queued.id)
        self.assertEqual(current.status, 'blocked')
        self.assertTrue(self.caught)
        self.assertIsInstance(self.caught[0], WorkerLeaseError)
        self.assertEqual(current.queued_turn_snapshot, self.original)
        self.assertTrue(current.queued_admission_claim_id)
        self.assertEqual(current.attempt_number, 1)
        self.assertEqual(current.blockers[0].code, 'procedure_assignment_lease_unavailable')
        self.assertEqual(current.blockers[0].target_id, self.f.native_assignment.id)
        self.assertEqual(current.blockers[0].remediation_route, '/api/execution-workers')
        self.assertIsNone(current.queued_start_correlation)
        self.assertIsNone(current.queued_resume_correlation)
        self.assertIsNone(current.queued_start_receipt)
        self.assertIsNone(current.started_at)
        self.assertIsNone(current.scheduling_retirement)
        self.assertIsNone(current.bootstrap_recovery)
        self.assert_no_native_input()
        before = current.model_dump(mode='json')
        await self.runtime.drain_thread_queue('t1')
        self.assertEqual(self.preflight.store.get(current.id).model_dump(mode='json'), before)
        self.assert_no_native_input()

    async def test_generic_writer_errors_and_provider_lookalikes_stay_unknown(self):
        errors = [WorkerLeaseError('procedure discovery requires a live assignment lease'),
            RuntimeError('procedure discovery requires a live assignment lease'),
            TimeoutError('procedure discovery requires a live assignment lease'),
            HTTPException(status_code=409, detail={'code': 'procedure_assignment_lease_unavailable'})]
        for index, error in enumerate(errors):
            with self.subTest(kind=type(error).__name__):
                queued = self.runtime.enqueue_turn(thread_id='t1', project_id=self.f.project.id,
                    execution_id=self.f.native_assignment.execution_id,
                    message=f'Independent original {index}', source='web', sandbox='workspace-write',
                    approval_policy='on-request') if index else self.queued
                self.runtime.schedule_queue_drain.reset_mock()
                with patch.object(self.f.workers, 'record_procedure_discovery', side_effect=error):
                    await self.runtime.drain_thread_queue('t1')
                current = self.preflight.store.get('preflight-queued-' + queued.id)
                self.assertEqual(current.status, 'reconciliation_required')
                self.assertFalse(self.preflight.recorded_prestart_refusal(current))
                self.assertIsNone(current.bootstrap_recovery)
                self.assertIsNone(current.scheduling_retirement)
                self.assert_no_native_input()

    async def test_historical_unknown_text_is_not_reclassified_or_replayed(self):
        attempt = self.preflight.retain_queued(self.queued,
            organization_id=self.f.actor.organization_id, workspace_id=self.f.actor.workspace_id)
        _, claim = self.preflight.claim_queued_admission(attempt.id)
        self.assertTrue(self.preflight.begin_queued_start(attempt.id, claim_id=claim))
        original = self.preflight.queued_disposition(attempt.id, status='reconciliation_required',
            claim_id=claim, detail={'code': 'native_start_outcome_unknown',
                'message': 'procedure discovery requires a live assignment lease'})
        with patch.object(self.f.workers, 'record_procedure_discovery', side_effect=AssertionError('replay')):
            await self.runtime.drain_thread_queue('t1')
        self.assertEqual(self.preflight.store.get(attempt.id), original)
        self.assertFalse(self.preflight.recorded_prestart_refusal(original))
        self.assert_no_native_input()

    async def test_provider_neutral_preinput_gate_uses_same_canonical_guard(self):
        session = SimpleNamespace(project_id=self.f.project.id,
            organization_id=self.f.actor.organization_id, workspace_id=self.f.actor.workspace_id,
            id='provider-neutral-session', execution_id=self.f.native_assignment.execution_id,
            assignment_id=self.f.native_assignment.id)
        request = SimpleNamespace(model_dump=lambda **_: {'message': 'One actual requested continuation'})
        with patch.object(self.f.workers, 'record_procedure_discovery', side_effect=self.expire_at_writer):
            with self.assertRaises(ExecutionPrestartRefusal) as caught:
                await self.admission.agent_session_admission(session, request, self.f.actor, 'resume')
        self.assertEqual(caught.exception.detail['blockers'][0]['code'],
            'procedure_assignment_lease_unavailable')
        self.assertIsInstance(caught.exception.__cause__, WorkerLeaseError)
        self.assert_no_native_input()

    async def test_actual_active_turn_prevents_known_noninput_disposition(self):
        active = ActiveThreadTurn(thread_id='t1', turn_id='existing-active-turn',
            project_id=self.f.project.id, execution_id='existing-active-execution',
            started_at=1, updated_at=1)
        def active_then_expire(*args, **kwargs):
            self.host.active['t1'] = active
            return self.expire_at_writer(*args, **kwargs)
        with patch.object(self.f.workers, 'record_procedure_discovery', side_effect=active_then_expire):
            await self.runtime.drain_thread_queue('t1')
        current = self.preflight.store.get('preflight-queued-' + self.queued.id)
        self.assertEqual(current.status, 'reconciliation_required')
        self.assertFalse(self.preflight.recorded_prestart_refusal(current))
        self.assertEqual(self.host.active['t1'], active)
        self.assert_no_native_input()
