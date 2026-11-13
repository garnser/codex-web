"""Terminal cleanup appends to real captured creation, never fabricates it."""
import asyncio
import unittest
import weakref
from pathlib import Path
from types import SimpleNamespace

from codex_web.execution_workers import AssignmentStatus
from codex_web.services.bootstrap_native_create import (
    BootstrapNativeCreateCapture, creation_attribution_matches,
    joined_cleanup_matches, record_joined_cleanup)
from tests import test_bootstrap_native_create_observation as fixture


class TerminalBootstrapCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = fixture.BootstrapCreateObservationTests()
        await self.f.asyncSetUp()
        self.addCleanup(self.f.doCleanups)

    def terminal(self):
        def lost(state):
            state.assignments = [state.assignments[0].model_copy(update={
                'status': AssignmentStatus.LOST, 'lease': None})]
            return state
        self.f.f.service.store.transition_assignment(self.f.f.worker.id,
            self.f.original.id, lost)
        return self.f.saved()

    async def test_terminal_cleanup_preserves_original_created_response_and_all_other_fields(self):
        task, rpc = await self.f.request()
        await self.f.response(rpc)
        await task
        original = self.terminal()
        self.f.process.poll = lambda: 0
        status = SimpleNamespace(running=False, pending_executor_work=0,
            worker_id=original.assigned_worker_id, fence=original.fence)
        before = original.model_dump(mode='json', exclude={'bootstrap_create_observation'})
        observed = original.bootstrap_create_observation
        joined = record_joined_cleanup(self.f.f.service, original, self.f.f.admin,
            runtime_generation=observed.runtime_generation, process=self.f.process,
            status=status, terminal_original=True)
        saved = self.f.saved()
        self.assertTrue(joined_cleanup_matches(saved))
        self.assertEqual(saved.model_dump(mode='json', exclude={'bootstrap_create_observation'}), before)
        self.assertEqual(joined.model_dump(exclude={'cleanup_joined_at', 'process_exit_code'}),
            observed.model_dump(exclude={'cleanup_joined_at', 'process_exit_code'}))
        self.assertEqual(record_joined_cleanup(self.f.f.service, saved, self.f.f.admin,
            runtime_generation=observed.runtime_generation, process=self.f.process,
            status=status, terminal_original=True), joined)
        self.assertEqual(len(self.f.pipe.getvalue().splitlines()), 1)

    async def test_terminal_unknown_create_cannot_gain_late_creation_or_cleanup(self):
        task, rpc = await self.f.request()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        original = self.terminal()
        before = original.model_dump(mode='json')
        correlation = original.bootstrap_create_observation
        with self.assertRaises(ValueError):
            self.f.capture.observe(correlation, generation=correlation.runtime_generation,
                message={'id': rpc, 'result': {'thread': {'id': 'native-original'}}})
        self.f.process.poll = lambda: 0
        with self.assertRaises(ValueError):
            record_joined_cleanup(self.f.f.service, original, self.f.f.admin,
                runtime_generation=correlation.runtime_generation, process=self.f.process,
                status=SimpleNamespace(running=False, pending_executor_work=0,
                    worker_id=original.assigned_worker_id, fence=original.fence), terminal_original=True)
        self.assertEqual(self.f.saved().model_dump(mode='json'), before)
        self.assertFalse(joined_cleanup_matches(self.f.saved()))

    async def test_real_initial_credential_seal_keeps_original_creation_and_allows_cleanup_append(self):
        from tests import test_native_procedure_credential_custody as custody
        from tests import test_current_work_recipient_recovery as recovery
        from codex_web.services.agent_process_session import AssignmentBoundAgentProcessSessionManager
        env = custody.NativeProcedureCredentialCustodyTests()
        await env.asyncSetUp()
        self.addAsyncCleanup(env.asyncTearDown)
        env.bootstrap()
        # Real Session.start captures the external transport owner, followed by
        # the actual RPC writer/reader and real credential/Definition owners.
        self.f.f.service, self.f.f.admin, self.f.f.worker = env.f.workers, env.f.actor, env.worker
        self.f.original = env.current()
        physical = await recovery.CurrentWorkRecipientRecoveryTests.adopt_native(self,
            env.f.workers.store, env.f.workers, self.f.original, 'custody-native-thread',
            actor=env.f.actor, workspace_path=Path(env.f.temp.name))
        observed = env.current().bootstrap_create_observation
        self.assertIsNone(env.current().native_credential_custody.admitted_at)
        discovery, _ = await env.admit()
        self.assertTrue(discovery.admitted)
        sealed = env.current()
        self.assertIsNotNone(sealed.native_credential_custody.admitted_at)
        self.assertEqual(sealed.bootstrap_create_observation, observed)
        self.assertTrue(creation_attribution_matches(sealed, worker=env.worker))
        self.assertFalse(creation_attribution_matches(sealed))
        # Arbitrary reference, requester, fence, scope and worker-identity
        # changes cannot masquerade as the one canonical initial seal.
        for changed in (
                sealed.model_copy(update={'secret_refs': (*sealed.secret_refs, 'foreign-reference')}),
                sealed.model_copy(update={'resource_ids': ('foreign-resource',)}),
                sealed.model_copy(update={'native_credential_custody': sealed.native_credential_custody.model_copy(
                    update={'requester_identity_id': 'foreign-requester'})}),
                sealed.model_copy(update={'native_credential_custody': sealed.native_credential_custody.model_copy(
                    update={'worker_identity_id': 'foreign-worker-identity'})}),
                sealed.model_copy(update={'fence': sealed.fence + 1})):
            with self.subTest(changed=changed.id):
                self.assertFalse(creation_attribution_matches(changed, worker=env.worker))
        original = self.terminal()
        generation = physical.runtime.generation
        await physical.session._stop_runtime_from_watchdog()
        self.assertNotEqual(physical.runtime.generation, generation)
        manager = AssignmentBoundAgentProcessSessionManager.__new__(AssignmentBoundAgentProcessSessionManager)
        manager._assignment_locks = weakref.WeakValueDictionary()
        manager.sessions = {original.id: physical.session}
        manager.local_worker = physical.local
        await manager.stop(original.id, expected_terminal=original, require_joined=True)
        self.assertNotIn(original.id, manager.sessions)
        saved = env.current()
        self.assertTrue(joined_cleanup_matches(saved, worker=env.worker))
        self.assertEqual(saved.bootstrap_create_observation.model_dump(
            exclude={'cleanup_joined_at', 'process_exit_code'}), observed.model_dump(
                exclude={'cleanup_joined_at', 'process_exit_code'}))
        self.assertEqual(saved.model_dump(exclude={'bootstrap_create_observation'}),
            original.model_dump(exclude={'bootstrap_create_observation'}))
