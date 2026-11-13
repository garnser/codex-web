"""Cancellation proof is original-fence cleanup, never input replay authority."""
import time
import unittest
from pathlib import Path

from fastapi import HTTPException

from codex_web.models import QueuedTurn
from codex_web.services.bootstrap_native_create import joined_cleanup_matches
from codex_web.services.execution_preflight import ExecutionPreflightService
from tests import test_current_work_recipient_recovery as recovery


class CancelledCurrentWorkBoundaries(recovery.CurrentWorkRecipientRecoveryTests):
    async def cancelled(self):
        physical = await self.owned_replacement_service()
        started = await self.started()
        physical.session._bootstrap_create_capture_enabled = True
        await physical.manager.cancel_bootstrap(self.pin.recipient.assignment_id,
            reason='Private owned idle retirement; preserve current Work')
        original = physical.store.assignment(self.pin.recipient.assignment_id)
        self.n.reserved = original
        self.assertTrue(joined_cleanup_matches(original))
        return physical, started, original

    async def replace(self):
        return await self.service.replace(self.pin.recipient.thread_id,
            work_item_ref=self.state.ref, actor=self.f.f.actor, project=self.project)

    def save_assignment(self, physical, assignment):
        physical.store.transition_assignment(assignment.assigned_worker_id, assignment.id,
            lambda state: state.model_copy(update={'assignments': [assignment]}))
        self.n.reserved = assignment

    async def test_wrong_cancel_marker_fence_or_missing_join_cannot_authorize_replacement(self):
        physical, started, original = await self.cancelled()
        observation = original.bootstrap_create_observation
        changes = (
            {'failure_code': 'operator_work_cancelled'},
            {'fence': original.fence + 1},
            {'fence': observation.fence},
            {'bootstrap_create_observation': observation.model_copy(update={
                'cleanup_joined_at': None, 'process_exit_code': None})},
            {'bootstrap_create_observation': observation.model_copy(update={
                'assignment_id': 'foreign-assignment'})},
            {'bootstrap_create_observation': observation.model_copy(update={
                'provider_native_session_id': 'foreign-native-thread'})},
        )
        for change in changes:
            with self.subTest(fields=tuple(change)):
                changed = original.model_copy(deep=True, update=change)
                self.save_assignment(physical, changed)
                before = physical.store.assignment(original.id).model_dump(mode='json')
                with self.assertRaises(HTTPException) as refused:
                    await self.replace()
                self.assertEqual(refused.exception.status_code, 409)
                self.assertEqual(self.new_calls, [])
                self.assertEqual(physical.store.assignment(original.id).model_dump(mode='json'), before)
                self.assertEqual(self.f.f.preflight.get(started.id), started)
                self.assertIsNone(physical.recovery.replacement_thread_id(self.pin.recipient.thread_id))
        self.save_assignment(physical, original)

    async def test_closed_work_and_changed_owner_do_not_revive_after_assignment_cancel(self):
        physical, started, original = await self.cancelled()
        before = original.model_dump(mode='json')
        saved = self.state.model_copy(deep=True)
        for change in ({'current_stage': 'closed', 'closed_at': time.time()},
                       {'current_owner': 'foreign-owner', 'next_owner': 'foreign-owner'}):
            with self.subTest(fields=tuple(change)):
                changed = saved.model_copy(deep=True, update=change)
                self.f.f.env.states.put(changed.ref, changed)
                with self.assertRaises(HTTPException) as refused:
                    await self.replace()
                self.assertEqual(refused.exception.status_code, 409)
                self.assertEqual(self.new_calls, [])
                self.assertEqual(physical.store.assignment(original.id).model_dump(mode='json'), before)
                self.assertEqual(self.f.f.preflight.get(started.id), started)
        self.f.f.env.states.put(saved.ref, saved)

    async def test_unrelated_unknown_without_transcript_stays_held_after_cancel(self):
        physical, started, original = await self.cancelled()
        preflight = ExecutionPreflightService(self.f.f.preflight, clock=time.time)
        queued = QueuedTurn(id='cancelled-recipient-other-input', execution_id='other-execution',
            thread_id=self.pin.recipient.thread_id, project_id=self.project.id,
            work_item_ref='team/other!389', message='Retained unrelated original', created_at=time.time())
        attempt = preflight.retain_queued(queued, organization_id='org-a', workspace_id='workspace-a')
        _, claim = preflight.claim_queued_admission(attempt.id)
        preflight.begin_queued_start(attempt.id, claim_id=claim)
        retained = self.f.f.preflight.get(attempt.id).model_dump(mode='json')
        with self.assertRaises(HTTPException) as refused:
            await self.replace()
        self.assertEqual(refused.exception.detail['code'], 'owner_current_work_outcome_unresolved')
        self.assertEqual(self.f.f.preflight.get(attempt.id).model_dump(mode='json'), retained)
        self.assertEqual(self.f.f.preflight.get(started.id), started)
        self.assertEqual(physical.store.assignment(original.id), original)
        self.assertEqual(self.new_calls, [])

    async def test_cancelled_bound_successor_lost_reply_reuses_original_fence_token(self):
        physical, started, original = await self.cancelled()
        create = physical.recovery.thread_creator
        async def lost_reply(**kwargs):
            await create(**kwargs)
            raise ConnectionError('Controlled external reply loss after actual creation')
        physical.recovery.thread_creator = lost_reply
        with self.assertRaises(ConnectionError):
            await self.replace()
        token = self.new_calls[0]['bootstrap_token']
        expected = self.n.routing.recipient_preparer.digest({
            'purpose': 'current-work-recipient-operator-replacement-v1',
            'organization_id': self.state.organization_id, 'workspace_id': self.state.workspace_id,
            'project_id': self.state.project_id, 'work_item_ref': self.state.ref,
            'source_assignment_id': original.id,
            'source_fence': original.bootstrap_create_observation.fence,
            'binding_id': self.n.binding.id})[:32]
        self.assertEqual(token, expected)
        result = await self.replace()
        self.assertEqual(result['newThreadId'], 'successor-thread-1')
        self.assertEqual(len(self.new_calls), 1)
        self.assertEqual(physical.store.assignment(original.id), original)
        self.assertEqual(self.f.f.preflight.get(started.id), started)
        # Re-reading eligibility follows the canonical graph without adopting
        # the cancellation fence as a different historical START generation.
        self.pin = self.n.routing.compatible_pin(self.state, self.n.binding,
            'orchestrator', writable_repository_ids=('platform',))
        self.assertEqual(self.pin.recipient.thread_id, result['newThreadId'])
        self.n.reserved = physical.store.assignment(self.pin.recipient.assignment_id)
        self.active_native = self.successors[self.pin.recipient.thread_id].runtime
        continuation = await self.started(native_turn='native-after-idle-cancel')
        self.assertEqual(continuation.queued_start_receipt.thread_id, result['newThreadId'])
        self.current.validate(self.pin)
        self.assertEqual(self.f.f.preflight.get(started.id), started)
        self.assertEqual(physical.store.assignment(original.id), original)


class SealedCancelledCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_initial_seal_then_manager_cancel_retains_original_joined_attribution(self):
        import weakref
        from codex_web.services.agent_process_session import AssignmentBoundAgentProcessSessionManager
        from tests import test_native_procedure_credential_custody as custody
        env = custody.NativeProcedureCredentialCustodyTests()
        await env.asyncSetUp()
        self.addAsyncCleanup(env.asyncTearDown)
        env.bootstrap()
        physical = await recovery.CurrentWorkRecipientRecoveryTests.adopt_native(self,
            env.f.workers.store, env.f.workers, env.current(), 'custody-native-thread',
            actor=env.f.actor, workspace_path=Path(env.f.temp.name))
        observed = env.current().bootstrap_create_observation
        discovery, _ = await env.admit()
        self.assertTrue(discovery.admitted)
        sealed = env.current()
        self.assertIsNotNone(sealed.native_credential_custody.admitted_at)
        physical.session._bootstrap_create_capture_enabled = True
        manager = AssignmentBoundAgentProcessSessionManager.__new__(AssignmentBoundAgentProcessSessionManager)
        manager._assignment_locks = weakref.WeakValueDictionary()
        manager.sessions = {sealed.id: physical.session}
        manager.local_worker = physical.local
        await manager.cancel_bootstrap(sealed.id, reason='Private owned idle retirement; preserve Work')
        cancelled = env.current()
        self.assertEqual(str(cancelled.status), 'cancelled')
        self.assertEqual(cancelled.fence, sealed.fence + 1)
        self.assertEqual(cancelled.native_credential_custody, sealed.native_credential_custody)
        self.assertEqual(cancelled.secret_refs, sealed.secret_refs)
        self.assertEqual(cancelled.bootstrap_create_observation.model_dump(
            exclude={'cleanup_joined_at', 'process_exit_code'}), observed.model_dump(
                exclude={'cleanup_joined_at', 'process_exit_code'}))
        self.assertTrue(joined_cleanup_matches(cancelled, worker=env.worker))
        self.assertIsNone(manager.joined_cleanup_status(cancelled))
        self.assertNotIn(sealed.id, manager.sessions)
        for seal_change in ({'fence': cancelled.fence}, {'worker_identity_id': 'foreign-worker'},
                            {'requester_identity_id': 'foreign-requester'}):
            with self.subTest(seal_change=seal_change):
                changed = cancelled.model_copy(update={'native_credential_custody':
                    sealed.native_credential_custody.model_copy(update=seal_change)})
                self.assertFalse(joined_cleanup_matches(changed, worker=env.worker))


def load_tests(loader, standard_tests, pattern):
    cases = [CancelledCurrentWorkBoundaries, SealedCancelledCleanupTests]
    return unittest.TestSuite(case(name) for case in cases
        for name in sorted(case.__dict__) if name.startswith('test_'))
