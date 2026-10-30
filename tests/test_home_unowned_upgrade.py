"""Canonical Home/Upgrade and real manager/projection; no process or app import."""
import json
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

from codex_web.execution_workers import ExecutionRuntimeBinding
from codex_web.runtime.execution import ExecutionPrestartRefusal
from codex_web.services.agent_process_session import AssignmentBoundAgentProcessSessionManager
from codex_web.services.threads import ThreadService
from tests.test_home_dormant_upgrade import DormantHomeUpgradeTests


class UnownedHomeUpgradeTests(DormantHomeUpgradeTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.manager = None

    def lose(self):
        # Expire only this private fixture's real canonical lease, then use the
        # actual recovery owner/current clock. No elapsed-time clock substitution.
        def expire(state):
            state.assignments = [a.model_copy(update={'lease':a.lease.model_copy(
                update={'expires_at':time.time()-1})}) if a.id == self.assignment.id else a
                for a in state.assignments]
            return state
        self.f.workers.store.transition_assignment(self.f.worker.id,self.assignment.id,expire)
        self.f.workers.recover_expired(actor=self.actor,now=time.time())
        self.assignment=self.current()
        self.assertEqual(self.assignment.status,'lost');self.assertIsNone(self.assignment.lease)
        self.manager=AssignmentBoundAgentProcessSessionManager(self.runtime,self.host,
            runtime_factory=lambda **_:self.fail('no runtime may start'),
            credential_provider=NS(),runtime_binding=self.assignment.runtime_binding)

    def observe(self,thread_id):
        manager=self.manager
        projection=NS(bootstrap_bindings=self.bindings,control_actor=self.actor,
            binding_service=NS(workers=self.f.workers),_index=lambda:self.index,
            _manager_for_binding=lambda _:manager)
        return ThreadService._runner_projection(projection,thread_id)

    async def test_actual_restart_projection_preserves_unknown_without_navigation_clear(self):
        self.lose();before=self.preserved();observed=self.observe('operator-thread')
        self.assertNotIn('processRunning',observed);self.assertNotIn('pendingExecutorWork',observed)
        self.assertTrue(self.drain().maintenance_mode)
        self.assertEqual(observed['processOwnershipObservation']['state'],'unowned_lost')
        artifact=self.planned._dormant_artifact(self.plan.id,self.actor)
        raw=json.loads(artifact.metadata['dormant_preservation'])
        self.assertEqual(raw['schema_version'],'1.1')
        self.assertIsNone(raw['stopped_process_observed']);self.assertIsNone(raw['pending_executor_work_observed'])
        self.assertIsNone(raw['process_ownership']['process_running'])
        self.assertIsNone(raw['process_ownership']['pending_executor_work'])
        self.assertIn('UNKNOWN',artifact.name)
        self.assertIsNone(self.artifacts.find_idempotent_artifact(actor=self.actor,
            source='home-upgrade-checkpoint',idempotency_key=self.plan.id))
        self.completed();self.assertIsNone(self.planned.resume_completed(self.plan.id,operator_actor=self.operator))
        self.assertEqual(self.preserved(),before);self.assertEqual(self.effects,0)

    async def test_routed_enrichment_matches_production_manager_identity(self):
        def enrich(state):
            state.assignments = [a.model_copy(update={'runtime_binding':
                a.runtime_binding.model_copy(update={'sandbox_profiles':('workspace-write',)})})
                if a.id == self.assignment.id else a for a in state.assignments]
            return state
        self.f.workers.store.transition_assignment(self.f.worker.id,self.assignment.id,enrich)
        self.lose()
        # application.py constructs the Codex manager with identity only, while
        # SelectedRuntime.execution_binding retains routing sandbox facts.
        self.manager.runtime_binding=ExecutionRuntimeBinding(
            provider_id='openai',runtime_id='codex',capability_revision=1)
        before=self.preserved()
        self.assertTrue(self.drain().maintenance_mode)
        self.assertEqual(self.preserved(),before)
        self.assertNotIn('processRunning',self.observe('operator-thread'))

    async def test_runtime_identity_drift_and_missing_bindings_refuse(self):
        self.lose();a=self.current()
        for binding in (None,
                a.runtime_binding.model_copy(update={'provider_id':'other'}),
                a.runtime_binding.model_copy(update={'runtime_id':'other'}),
                a.runtime_binding.model_copy(update={'capability_revision':2})):
            with self.subTest(binding=binding):
                self.manager.runtime_binding=binding
                self.assertIsNone(self.manager.unowned_lost_observation(a))
                with self.assertRaises(ExecutionPrestartRefusal):self.strict_checkpoint()
        self.manager.runtime_binding=a.runtime_binding
        self.assertIsNone(self.manager.unowned_lost_observation(
            a.model_copy(update={'runtime_binding':None})))

    async def test_failed_owner_lookup_never_becomes_absence(self):
        self.lose()
        with patch.object(self.manager,'get',side_effect=RuntimeError('private unavailable lookup')):
            self.assertNotIn('processOwnershipObservation',self.observe('operator-thread'))
            with self.assertRaises(ExecutionPrestartRefusal):self.strict_checkpoint()
        self.assertFalse(self.upgrades.get(self.plan.id,actor=self.operator).maintenance_mode)

    async def test_runtime_worker_and_snapshot_mismatch_refuse(self):
        self.lose();a=self.current()
        self.manager.runtime_binding=ExecutionRuntimeBinding(provider_id='other',runtime_id='codex',capability_revision=1)
        self.assertIsNone(self.manager.unowned_lost_observation(a))
        with self.assertRaises(ExecutionPrestartRefusal):self.strict_checkpoint()
        self.manager.runtime_binding=a.runtime_binding
        stale=a.model_copy(update={'fence':a.fence+1})
        self.assertIsNone(self.manager.unowned_lost_observation(stale))
        worker=self.runtime.worker;self.runtime.worker=worker.model_copy(update={'id':'worker-unrelated'})
        try:
            self.assertIsNone(self.manager.unowned_lost_observation(a))
            with self.assertRaises(ExecutionPrestartRefusal):self.strict_checkpoint()
        finally:self.runtime.worker=worker

    async def test_inflight_or_registered_owner_refuses(self):
        self.lose();lock=self.manager._assignment_lock(self.assignment.id)
        await lock.acquire()
        try:
            self.assertIsNone(self.manager.unowned_lost_observation(self.current()))
            with self.assertRaises(ExecutionPrestartRefusal):self.strict_checkpoint()
        finally:lock.release()
        a=self.current()
        self.manager.sessions[a.id]=NS(status=lambda:NS(fence=a.fence,worker_id=a.assigned_worker_id,
            running=True,pending_executor_work=0))
        self.assertIsNone(self.manager.unowned_lost_observation(a))
        with self.assertRaises(ExecutionPrestartRefusal):self.strict_checkpoint()

    async def test_current_lease_or_same_incarnation_is_not_unowned_restart(self):
        live=self.current()
        manager=AssignmentBoundAgentProcessSessionManager(self.runtime,self.host,
            runtime_factory=lambda **_:None,credential_provider=NS(),runtime_binding=live.runtime_binding)
        self.assertIsNone(manager.unowned_lost_observation(live))
        self.lose();self.manager.started_at=self.current().updated_at
        self.assertIsNone(self.manager.unowned_lost_observation(self.current()))
        with self.assertRaises(ExecutionPrestartRefusal):self.strict_checkpoint()

    async def test_unowned_retains_active_and_queued_input_refusals(self):
        self.lose();self.active_store.put('operator-thread',self.interrupted)
        with self.assertRaises(ExecutionPrestartRefusal):self.strict_checkpoint()
        self.active_store.delete('operator-thread')
        self.execution.enqueue_turn(thread_id='operator-thread',project_id='home',
            message='Existing human context',source='web')
        before=self.preserved()
        with self.assertRaises(ExecutionPrestartRefusal):self.strict_checkpoint()
        self.assertEqual(self.preserved(),before);self.assertEqual(self.effects,0)

    async def test_unowned_goal_binding_still_blocks(self):
        await super().test_actual_goal_binding_is_not_dormant()

    async def test_unowned_uncertain_original_still_blocks_without_replay(self):
        await super().test_actual_uncertain_original_is_held_without_replay()


def load_tests(loader,tests,pattern):
    return unittest.TestSuite(loader.loadTestsFromName(name,UnownedHomeUpgradeTests)
        for name in sorted(UnownedHomeUpgradeTests.__dict__) if name.startswith('test_'))
