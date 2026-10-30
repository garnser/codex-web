"""Canonical Upgrade/Home/worker/Session/Artifact proof; fake process observations."""
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch
from codex_web.execution_workers import ExecutionRuntimeBinding
from codex_web.models import ThreadRunSettings
from codex_web.services.home_operator import HomeOperatorService
from codex_web.services.home_operator_goals import HomeOperatorGoalInspection
from codex_web.services.goals import GoalService
from codex_web.services.goal_execution_bindings import GoalExecutionBindingService
from codex_web.storage.goals import GoalStore
from codex_web.storage.goal_execution_bindings import GoalExecutionBindingStore
from codex_web.runtime.execution import ExecutionPrestartRefusal
from codex_web.services.control_plane_broker import ControlPlaneBrokerDeniedError
from tests.test_home_upgrade_checkpoint import HomeUpgradeCheckpointTests as Fixture
from tests.test_goals import _Graph


class DormantHomeUpgradeTests(Fixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        # Declare the same canonical provider/runtime custody as the existing
        # Session. No Session repair, lease override or alternate worker owner.
        def bind_runtime(state):
            state.assignments = [a.model_copy(update={'runtime_binding':ExecutionRuntimeBinding(
                provider_id='openai',runtime_id='codex',capability_revision=1)})
                if a.id==self.assignment.id else a for a in state.assignments]
            return state
        self.f.workers.store.transition_assignment(self.f.worker.id,self.assignment.id,bind_runtime)
        self.assignment=self.current()
        self.goals=GoalService(GoalStore(self.f.sqlite),self.projects,_Graph())
        self.goal_bindings=GoalExecutionBindingService(GoalExecutionBindingStore(self.f.sqlite),self.goals)
        navigation=HomeOperatorService(projects=self.projects,thread_scope=self.scope,thread_index=self.index,
            settings=NS(get=lambda _:ThreadRunSettings(execution_profile_id='orchestration-only')),
            agent_profiles=self.profiles)
        self.observation={'processRunning':False,'pendingExecutorWork':0}
        self.planned.sessions=self.sessions.store
        self.planned.goal_inspection=HomeOperatorGoalInspection(navigation,self.goal_bindings,self.sessions)
        self.planned.runner_observer=self.observe
        self.upgrades.home_checkpoint_before_drain=lambda plan_id,actor:self.planned.checkpoint_for_service_drain(
            plan_id,worker_runtime=self.runtime,operator_actor=actor)
        self.upgrades.home_resume_after_upgrade=lambda plan_id,actor:self.planned.resume_completed(plan_id,operator_actor=actor)

    def strict_checkpoint(self):
        # Home readiness remains strict, independently of global Upgrade drain.
        return self.planned.checkpoint_current(self.plan.id, worker_runtime=self.runtime,
            operator_actor=self.operator)

    def observe(self,thread_id):
        self.assertEqual(thread_id,'operator-thread')
        a=self.current()
        return {'assignmentId':a.id,'fence':a.fence,'assignmentStatus':str(a.status),
            'live':False,'leaseValid':False,**self.observation}

    def lose(self):
        self.f.workers.recover_expired(actor=self.actor,now=self.current().lease.expires_at+1)
        self.assignment=self.current();self.assertEqual(self.assignment.status,'lost');self.assertIsNone(self.assignment.lease)

    def preserved(self):
        return {'selected':self.projects.get('home',self.actor.tenant).home_operator.model_dump(mode='json'),
            'session':self.sessions.store.find_unique_by_native_id('operator-thread',provider_id='openai',runtime_id='codex').model_dump(mode='json'),
            'queue':[q.model_dump(mode='json')for q in self.queue.get('operator-thread')],
            'assignment':self.current().model_dump(mode='json')}

    async def test_actual_dormant_upgrade_preserves_selection_without_host_grant_or_resume(self):
        self.lose();before=self.preserved()
        with self.assertRaises(ControlPlaneBrokerDeniedError):
            self.service.home_operator_host.authorize_operation(self.assignment,operation_id='operator.host.inspect',
                worker_actor=self.worker_actor,requester_id=self.actor.identity_id,mutating=False)
        drained=self.drain();self.assertTrue(drained.maintenance_mode)
        artifact=self.planned._dormant_artifact(self.plan.id,self.actor)
        self.assertIsNotNone(artifact);self.assertIn('"historical_native_outcome":"UNKNOWN"',artifact.metadata['dormant_preservation'])
        self.assertIsNone(self.artifacts.find_idempotent_artifact(actor=self.actor,source='home-upgrade-checkpoint',idempotency_key=self.plan.id))
        self.completed()
        # After real service replacement, old process-local status may be gone.
        # Its omission grants no input and cannot rewrite the historical proof.
        self.observation={}
        self.assertIsNone(self.planned.resume_completed(self.plan.id,operator_actor=self.operator))
        self.assertEqual(self.preserved(),before);self.assertEqual(self.effects,0)
        self.assertEqual(self.queue.get('operator-thread'),[])

    async def test_actual_active_pending_and_unknown_process_observations_block(self):
        self.lose()
        for observation in [{'processRunning':True,'pendingExecutorWork':0},
                {'processRunning':False,'pendingExecutorWork':1},
                {'processRunning':False,'pendingExecutorWork':None},
                {'processRunning':False,'pendingExecutorWork':False},
                {'processRunning':False,'pendingExecutorWork':0,'assignmentId':'foreign-assignment'}]:
            self.observation=observation
            with self.subTest(observation=observation),self.assertRaises(ExecutionPrestartRefusal):self.strict_checkpoint()
            self.assertFalse(self.upgrades.get(self.plan.id,actor=self.operator).maintenance_mode)

    async def test_retained_active_native_or_queued_human_blocks(self):
        self.lose();self.active_store.put('operator-thread',self.interrupted)
        with self.assertRaises(ExecutionPrestartRefusal):self.strict_checkpoint()
        self.active_store.delete('operator-thread')
        import asyncio
        pending=asyncio.get_running_loop().create_future()
        self.execution.thread_completion_tasks['operator-thread']=pending
        try:
            with self.assertRaises(ExecutionPrestartRefusal):self.strict_checkpoint()
        finally:
            self.execution.thread_completion_tasks.pop('operator-thread');pending.cancel()
        self.execution.enqueue_turn(thread_id='operator-thread',project_id='home',message='Retained real human context',source='web')
        before=self.preserved()
        with self.assertRaises(ExecutionPrestartRefusal):self.strict_checkpoint()
        self.assertEqual(self.preserved(),before)

    async def test_actual_goal_binding_is_not_dormant(self):
        from codex_web.goals import GoalCreate,GoalBudget,GoalSuccessCriterion,GoalWorkGraphBinding,GoalTransitionRequest
        from codex_web.goal_execution_bindings import GoalExecutionBindingCreate
        g=self.goals.create(GoalCreate(title='Active retained mission',description='Private retained Home mission',owner_identity_id=self.actor.identity_id,
            budget=GoalBudget(max_model_calls=1),success_criteria=(GoalSuccessCriterion(description='Real acceptance'),),
            work_graph_bindings=(GoalWorkGraphBinding(project_id='home'),)),scope=self.actor.tenant,actor_id=self.actor.identity_id)
        g=self.goals.transition(g.id,GoalTransitionRequest(status='active',reason='Private real mission'),scope=self.actor.tenant,actor_id=self.actor.identity_id)
        session=self.sessions.store.find_unique_by_native_id('operator-thread',provider_id='openai',runtime_id='codex')
        self.goal_bindings.create(g.id,GoalExecutionBindingCreate(project_id='home',goal_revision=g.revision,
            provider_id='openai',runtime_id='codex',agent_session_id=session.id,thread_id='operator-thread',
            execution_owner_id=self.actor.identity_id,reason='Explicit retained mission'),scope=self.actor.tenant,actor_id=self.actor.identity_id)
        self.lose()
        with self.assertRaises(ExecutionPrestartRefusal):self.strict_checkpoint()
        self.assertEqual(self.goals.get(g.id,scope=self.actor.tenant).status,'active');self.assertEqual(self.effects,0)

    async def test_actual_uncertain_original_is_held_without_replay(self):
        self.after_spawn=lambda:(_ for _ in ()).throw(RuntimeError('Private lost provider observation'))
        intent=self.create();await self.execute(intent)
        current=self.intents.get(intent.id,self.actor);self.assertEqual(current.status,'uncertain');self.assertEqual(current.attempt,1)
        self.lose()
        with self.assertRaises(ExecutionPrestartRefusal):self.strict_checkpoint()
        self.assertEqual(self.intents.get(intent.id,self.actor).model_dump(mode='json'),current.model_dump(mode='json'))
        self.assertEqual(self.effects,1)

    async def test_session_custody_and_selection_drift_refuse(self):
        self.lose();original=self.planned._dormant_snapshot
        def drift(*args,**kwargs):
            value=original(*args,**kwargs)
            current=self.projects.get('home',self.actor.tenant).home_operator
            self.projects.repository.update_home_operator(organization_id=self.actor.organization_id,
                workspace_id=self.actor.workspace_id,expected_revision=current.revision,
                binding=current.model_copy(update={'revision':current.revision+1}))
            return value
        with patch.object(self.planned,'_dormant_snapshot',side_effect=drift):
            with self.assertRaises((ExecutionPrestartRefusal,ControlPlaneBrokerDeniedError)):self.strict_checkpoint()
        self.assertFalse(self.upgrades.get(self.plan.id,actor=self.operator).maintenance_mode)

    async def test_current_profile_revision_drift_refuses(self):
        from codex_web.agent_profiles import AgentProfileUpdate
        self.lose()
        self.profiles.update('home-operator',AgentProfileUpdate(name='Changed current Profile',reason='Private current revision drift negative'),actor=self.actor)
        with self.assertRaises(ExecutionPrestartRefusal):self.strict_checkpoint()
        self.assertFalse(self.upgrades.get(self.plan.id,actor=self.operator).maintenance_mode)
        self.assertIsNone(self.planned._dormant_artifact(self.plan.id,self.actor))

    async def test_actual_session_custody_drift_refuses(self):
        self.lose()
        session=self.sessions.store.find_unique_by_native_id('operator-thread',provider_id='openai',runtime_id='codex')
        self.sessions.store.upsert(session.model_copy(update={'assignment_id':'foreign-assignment'}))
        with self.assertRaises(ExecutionPrestartRefusal):self.strict_checkpoint()
        self.assertFalse(self.upgrades.get(self.plan.id,actor=self.operator).maintenance_mode)

    async def test_real_checkpoint_and_dormant_disposition_cannot_coexist(self):
        checkpoint=self.checkpoint();self.lose()
        with self.assertRaises(ExecutionPrestartRefusal):self.strict_checkpoint()
        self.assertIsNone(self.planned._dormant_artifact(self.plan.id,self.actor))
        self.assertEqual(self.artifacts.get_artifact(checkpoint.id,actor=self.actor).lifecycle,'active')

    async def test_reread_requires_fresh_dormancy(self):
        self.lose();first=self.planned.checkpoint_current(self.plan.id,worker_runtime=self.runtime,operator_actor=self.operator)
        self.assertEqual(self.planned.checkpoint_current(self.plan.id,worker_runtime=self.runtime,operator_actor=self.operator).id,first.id)
        self.observation['pendingExecutorWork']=1
        with self.assertRaises(ExecutionPrestartRefusal):self.planned.checkpoint_current(self.plan.id,worker_runtime=self.runtime,operator_actor=self.operator)


def load_tests(loader,tests,pattern):
    return unittest.TestSuite(loader.loadTestsFromName(name,DormantHomeUpgradeTests)
        for name in sorted(DormantHomeUpgradeTests.__dict__)if name.startswith('test_'))
