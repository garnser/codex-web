"""Canonical same-thread custody adoption; no app import or native process."""
import unittest
from tests.test_home_goal_restart import HomeGoalRestartTests
from tests.test_home_restart_recovery import HomeRestartRecoveryTests
from codex_web.agent_runtime import AgentRuntimeSessionRequest

class HomeGoalSessionCustodyTests(HomeGoalRestartTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        from codex_web.services.agent_session_custody import AgentSessionCustodyService
        from codex_web.services.codex_agent_runtime import CodexAgentRuntimeAdapter
        session=self.sessions.get(self.goal_binding.agent_session_id,self.actor)
        # Initial fixture contract only, BEFORE original START/fencing. Production
        # Codex adoption records the actual adapter runtime type, not the generic host fixture's native.
        self.sessions.store.upsert(session.model_copy(update={'runtime_type':CodexAgentRuntimeAdapter.runtime_type}))
        self.execution.session_custody=AgentSessionCustodyService(self.sessions,self.f.workers,
            self.bindings,self.f.workspaces,self.f.execution_profiles)

    async def attach_native(self):
        await super().attach_native()
        self.native_generation=getattr(self,'native_generation',0)+1
        generation=self.native_generation
        from codex_web.runtime.codex import CodexRuntime
        dispatch=CodexRuntime._dispatch_message.__get__(self.native)
        async def unique(message):
            if isinstance(message.get('result'),dict) and 'turn' in message['result']:
                message={**message,'result':{'turn':{**message['result']['turn'],
                    'id':f'custody-{generation}-'+message['result']['turn']['id']}}}
            await dispatch(message)
        self.native._dispatch_message=unique

    async def rollover(self,*,observe_start=False):
        active,queued,restart=await self.retain_active_goal()
        self.actor=self.f.identity.actor_for_identity(self.actor.identity_id,scope=self.actor.tenant)
        original=self.sessions.get(self.goal_binding.agent_session_id,self.actor)
        if observe_start:
            from codex_web.agent_runtime import AgentRuntimeEvent
            from codex_web.services.agent_runtime_telemetry import AgentRuntimeTelemetryService
            self.telemetry=AgentRuntimeTelemetryService(self.home_goal.usage,self.sessions.store,self.sessions.registry)
            self.telemetry.observe_event('openai','codex',AgentRuntimeEvent(event_type='turn/started',
                provider_native_session_id=active.thread_id,provider_native_turn_id=active.turn_id))
        current=HomeRestartRecoveryTests.heal_same_thread(self)
        self.execution.clear_thread_active(self.thread)
        self.target=current
        await self.attach_native()
        return active,queued,restart,original,current

    async def test_late_original_and_new_telemetry_keep_distinct_custody_and_actual_usage(self):
        from codex_web.agent_runtime import AgentRuntimeEvent
        from codex_web.services.agent_runtime_telemetry import AgentRuntimeTelemetryService
        active,queued,restart,original,current=await self.rollover(observe_start=True)
        await self.execution.drain_thread_queue(self.thread)
        recovered=self.execution._active_turn(self.thread)
        self.assertIsNotNone(recovered,self.host.events)
        self.assertNotEqual(active.turn_id,recovered.turn_id)
        old_id=self.canonical_usage(active).id
        retained=self.home_goal.usage.get(old_id)
        self.assertEqual(retained.assignment_id,active.assignment_id)
        self.assertEqual(retained.terminal_outcome,'unknown')
        self.assertIsNone(retained.input_tokens)
        self.assertIsNone(retained.model_call_count)
        self.assertFalse(self.home_goal._settle_usage(self.goal_bindings.get(self.goal_binding.id,scope=self.actor.tenant),self.actor.tenant))
        telemetry=self.telemetry
        old=telemetry.observe_event('openai','codex',AgentRuntimeEvent(event_type='turn/interrupted',
            provider_native_session_id=self.thread,provider_native_turn_id=active.turn_id,
            payload={'usage':{'input_tokens':17,'output_tokens':9}}))
        new=telemetry.observe_event('openai','codex',AgentRuntimeEvent(event_type='turn/completed',
            provider_native_session_id=self.thread,provider_native_turn_id=recovered.turn_id,
            payload={'usage':{'input_tokens':19,'output_tokens':11}}))
        self.assertEqual(old.id,old_id)
        self.assertEqual((old.assignment_id,old.execution_id,old.execution_workspace_id),
            (active.assignment_id,active.execution_id,active.execution_workspace_id))
        self.assertEqual((new.assignment_id,new.execution_id,new.execution_workspace_id),
            (current.id,current.execution_id,current.execution_workspace_id))
        self.assertEqual((old.input_tokens,new.input_tokens),(17,19))
        self.assertEqual((old.model_call_count,new.model_call_count),(1,1))

    async def test_actual_existing_custody_owner_refreshes_session_after_guarded_resume(self):
        active,queued,restart=await self.retain_active_goal()
        self.actor=self.f.identity.actor_for_identity(self.actor.identity_id,scope=self.actor.tenant)
        original=self.sessions.get(self.goal_binding.agent_session_id,self.actor)
        old=self.assignment
        current=HomeRestartRecoveryTests.heal_same_thread(self)
        self.assertEqual(self.bindings.get_by_thread(active.thread_id,self.actor).assignment_id,current.id)
        self.assertEqual(self.f.workers.store.assignment(old.id).status,'cancelled')
        self.execution.clear_thread_active(self.thread)
        self.target=current
        await self.attach_native()
        await self.execution.drain_thread_queue(self.thread)
        self.assertEqual(len(self.starts()),1,self.host.events)
        adopted=self.sessions.adopt(provider_id=original.provider_id,runtime_id=original.runtime_id,
            runtime_type=original.runtime_type,provider_native_session_id=active.thread_id,
            request=AgentRuntimeSessionRequest(project_id='home',execution_id=current.execution_id,
                assignment_id=current.id,execution_workspace_id=current.execution_workspace_id,worker_id=current.assigned_worker_id),
            actor=self.actor,capability_snapshot=original.capability_snapshot)
        self.assertEqual(adopted.id,original.id)
        self.assertEqual(adopted.assignment_id,current.id,
            'Actual canonical adopt returned fenced old custody after same-thread bootstrap CAS')

def load_tests(loader,tests,pattern):
    return unittest.TestSuite(HomeGoalSessionCustodyTests(name) for name in HomeGoalSessionCustodyTests.__dict__ if name.startswith('test_'))
