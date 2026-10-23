"""Canonical Goal/binding/Home admission integration; private SQLite, no app import."""
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from codex_web.autonomy import AutonomyControl, AutonomyExclusiveGoalScope
from codex_web.goals import GoalCreate, GoalBudget, GoalSuccessCriterion, GoalTransitionRequest, GoalWorkGraphBinding
from codex_web.goal_execution_bindings import GoalExecutionBindingCreate
from codex_web.services.goals import GoalService
from codex_web.services.goal_execution_bindings import GoalExecutionBindingService
from codex_web.services.goal_continuation import GoalContinuationService
from codex_web.services.procedure_discovery import NativeProcedureAdmissionService
from codex_web.services.codex_agent_runtime import CodexAgentRuntimeAdapter
from codex_web.storage.goals import GoalStore
from codex_web.storage.goal_execution_bindings import GoalExecutionBindingStore
from tests.test_goals import _Graph
from tests.test_home_coordination_canonical_send import HomeCanonicalSendTests


class HomeGoalContinuationTests(HomeCanonicalSendTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.goals = GoalService(GoalStore(self.f.sqlite), self.projects, _Graph())
        draft = self.goals.create(GoalCreate(title='Real bounded Home mission', description='Resolve current accepted bugs and prioritize existing VDO MRs.',
            owner_identity_id=self.actor.identity_id, budget=GoalBudget(max_input_tokens=4000,max_output_tokens=1000,max_model_calls=2 if self._testMethodName=='test_fast_terminal_then_late_queue_response_preserves_next_delivery' else 1,max_retries=0,max_handoffs=1),
            success_criteria=(GoalSuccessCriterion(description='Independent qualified acceptance'),),
            work_graph_bindings=(GoalWorkGraphBinding(project_id='home'),)),scope=self.actor.tenant,actor_id=self.actor.identity_id)
        self.goal = self.goals.transition(draft.id,GoalTransitionRequest(status='active',reason='Explicit purposeful user mission'),scope=self.actor.tenant,actor_id=self.actor.identity_id)
        self.goal_bindings = GoalExecutionBindingService(GoalExecutionBindingStore(self.f.sqlite),self.goals)
        session = self.sessions.store.find_unique_by_native_id('operator-thread',provider_id='openai',runtime_id='codex')
        self.goal_binding = self.goal_bindings.create(self.goal.id,GoalExecutionBindingCreate(project_id='home',goal_revision=self.goal.revision,
            provider_id=session.provider_id,runtime_id=session.runtime_id,agent_session_id=session.id,thread_id=session.provider_native_session_id,
            execution_owner_id=self.actor.identity_id,reason='Explicit SAME selected Home mission'),scope=self.actor.tenant,actor_id=self.actor.identity_id)
        self.transport = NS(request=AsyncMock(side_effect=AssertionError('Generic transport must not receive Home Goal input')))
        self.sessions.registry.register(CodexAgentRuntimeAdapter(self.transport))
        admission = NativeProcedureAdmissionService(None,self.f.workers,None,self.actor,self.f.execution_profiles)
        admission.input_authorizer = self.service.home_operator_host.authorize_native_input
        self.sessions.procedure_admission = admission.agent_session_admission
        control = AutonomyControl(mode='active',exclusive_goal_scope=AutonomyExclusiveGoalScope(goal_id=self.goal.id,project_id='home',reason='Explicit bounded Home mission'))
        self.control=control
        self.goal_continuation = GoalContinuationService(self.goal_bindings,self.goals,self.sessions,owner_id='goal-controller',autonomy=NS(store=NS(load=lambda:NS(control=control))))
        from codex_web.services.home_goal_continuation import HomeGoalContinuation
        from codex_web.storage.agent_runtime_usage import AgentRuntimeUsageStore
        self.home_goal = HomeGoalContinuation(continuation=self.goal_continuation, home_host=self.service.home_operator_host,
            turns=self.turns,preflight=self.execution.queued_preflight,usage=AgentRuntimeUsageStore(self.f.sqlite))
        self.goal_continuation.home_continuation=self.home_goal
        self.service.home_operator_host.goal_input_authorizer=self.home_goal.authorize_input
        self.execution.native_input_guard_factory=self.service.home_operator_host.prepare_native_input_guard
        self.execution.queued_preflight.queued_start_observer=self.home_goal.project_native_start
        self.execution.retained_turn_outcome_recorder=self.home_goal.record_terminal


    async def test_active_goal_uses_same_home_canonical_queue(self):
        result = await self.goal_continuation.dispatch_once(self.goal_binding.id,scope=self.actor.tenant)
        self.assertEqual(result.outcome,'queued',result.reason)
        self.transport.request.assert_not_awaited()
        self.assertEqual(len(self.queue_store.get('operator-thread')),1)

    async def start_home_goal_native(self):
        result=await self.goal_continuation.dispatch_once(self.goal_binding.id,scope=self.actor.tenant)
        self.assertEqual(result.outcome,'queued',result.reason)
        self.target,self.thread=self.assignment,'operator-thread'
        self.bindings.mark_initial_turn_started(self.thread,self.actor)
        await self.attach_native()
        await self.execution.drain_thread_queue(self.thread)
        self.assertEqual(len(self.starts()),1,self.host.events)
        return self.execution._active_turn(self.thread)

    async def test_actual_start_and_owned_terminal_settle_same_binding_with_true_usage(self):
        await self.account_native_usage(delayed=False)

    async def test_terminal_before_usage_then_canonical_event_accounts_exact_original_once(self):
        await self.account_native_usage(delayed=True)

    async def account_native_usage(self, *, delayed):
        active=await self.start_home_goal_native()
        usage=self.canonical_usage(active)
        if not delayed:self.home_goal.usage.upsert(usage)
        self.execution.record_thread_activity({'method':'turn/completed','params':{'threadId':self.thread,'turn':{'id':active.turn_id}}})
        await self.execution.assignment_completion_tasks[self.assignment.id]
        if delayed:
            pending=self.goal_bindings.get(self.goal_binding.id,scope=self.actor.tenant)
            self.assertEqual(pending.home_continuation.delivery.phase,'terminal_pending_usage')
            self.assertEqual(pending.home_continuation.model_calls,0)
            self.assertEqual(len(self.starts()),1)
            self.home_goal.usage.upsert(usage)
            from codex_web.agent_runtime import AgentRuntimeEvent
            from codex_web.services.goal_continuation_events import GoalContinuationEventService
            events=GoalContinuationEventService(self.goal_bindings,self.sessions,self.goal_continuation)
            event=AgentRuntimeEvent(event_type='turn.completed',
                provider_native_session_id=active.thread_id,provider_native_turn_id=active.turn_id)
            await events.handle_event(event,scope=self.actor.tenant)
            await events.handle_event(event,scope=self.actor.tenant)
        current=self.goal_bindings.get(self.goal_binding.id,scope=self.actor.tenant)
        self.assertEqual(current.home_continuation.delivery.phase,'settled')
        self.assertEqual(current.home_continuation.delivery.usage_id,usage.id)
        self.assertEqual((current.home_continuation.input_tokens,current.home_continuation.output_tokens),(17,9))
        self.assertEqual(current.status,'blocked')  # One-call budget exhausted, not a completed Goal.
        self.assertEqual(current.home_continuation.reserved_calls,1)
        self.assertEqual(len(self.starts()),1)
        self.assertEqual(self.queue_store.get(self.thread),[])
        self.transport.request.assert_not_awaited()

    def canonical_usage(self,active):
        import hashlib
        from codex_web.agent_runtime_usage import AgentRuntimeUsage
        session=self.sessions.store.find_unique_by_native_id(active.thread_id,provider_id='openai',runtime_id='codex')
        return AgentRuntimeUsage(id='runtime-usage-'+hashlib.sha256(f'{session.id}\n{active.turn_id}'.encode()).hexdigest()[:32],
            organization_id=self.actor.organization_id,workspace_id=self.actor.workspace_id,project_id='home',
            execution_id=active.execution_id,assignment_id=active.assignment_id,execution_workspace_id=active.execution_workspace_id,
            agent_session_id=session.id,provider_id='openai',runtime_id='codex',runtime_type='app-server',
            provider_native_session_id=active.thread_id,provider_native_turn_id=active.turn_id,
            input_tokens=17,output_tokens=9,model_call_count=1,runtime_duration_seconds=2.5,terminal_outcome='succeeded',telemetry_completeness='exact',completed_at=123.)

    async def test_two_concurrent_usage_settlements_have_one_actual_transition_winner(self):
        import asyncio,threading
        active=await self.start_home_goal_native()
        self.execution.record_thread_activity({'method':'turn/completed','params':{'threadId':self.thread,'turn':{'id':active.turn_id}}})
        await self.execution.assignment_completion_tasks[self.assignment.id]
        pending=self.goal_bindings.get(self.goal_binding.id,scope=self.actor.tenant)
        self.home_goal.usage.upsert(self.canonical_usage(active))
        barrier=threading.Barrier(2,timeout=5.)
        original=self.home_goal._observed_usage
        def both(*args):
            result=original(*args)
            barrier.wait()
            return result
        self.home_goal._observed_usage=both
        results=await asyncio.gather(*(asyncio.to_thread(self.home_goal._settle_usage,pending,self.actor.tenant) for _ in range(2)))
        self.assertEqual(sorted(results),[False,True])
        current=self.goal_bindings.get(self.goal_binding.id,scope=self.actor.tenant)
        self.assertEqual((current.home_continuation.model_calls,current.home_continuation.input_tokens),(1,17))
        self.assertEqual(current.home_continuation.delivery.phase,'settled')

    async def test_concurrent_claim_reservation_loser_does_not_block_winner(self):
        import asyncio,threading
        barrier=threading.Barrier(2,timeout=5.)
        lock=threading.Lock()
        calls=[]
        original=self.home_goal.context
        def both(*args,**kwargs):
            result=original(*args,**kwargs)
            with lock:
                calls.append(True)
                wait=len(calls)<=2
            if wait:barrier.wait()
            return result
        self.home_goal.context=both
        results=await asyncio.gather(*(self.goal_continuation.dispatch_once(self.goal_binding.id,scope=self.actor.tenant) for _ in range(2)))
        self.assertEqual(sorted(r.outcome for r in results),['not_claimed','queued'])
        current=self.goal_bindings.get(self.goal_binding.id,scope=self.actor.tenant)
        self.assertEqual(current.status,'active')
        self.assertEqual(current.home_continuation.reserved_calls,1)
        self.assertEqual(current.home_continuation.delivery.phase,'queued')
        self.assertEqual(len(self.queue_store.get('operator-thread')),1)

    async def test_cancelled_stale_claim_preserves_operator_pause_and_cancellation(self):
        import asyncio
        claimed=self.goal_bindings.claim_continuation(self.goal_binding.id,scope=self.actor.tenant,
            owner_id=self.goal_continuation.owner_id,lease_seconds=60.)
        def cancelled(binding):
            self.goal_bindings.operator_stop(binding.id,scope=self.actor.tenant,actor_id=self.actor.identity_id,
                cancelled=False,reason='Pause during cancelled preparation')
            raise asyncio.CancelledError()
        self.home_goal.context=cancelled
        with self.assertRaises(asyncio.CancelledError):
            await self.home_goal._dispatch(claimed,scope=self.actor.tenant)
        current=self.goal_bindings.get(self.goal_binding.id,scope=self.actor.tenant)
        self.assertEqual(current.status,'blocked')
        self.assertIn('Pause during cancelled preparation',current.stop_reason)
        self.assertIsNone(current.home_continuation)
        self.assertEqual(self.queue_store.get('operator-thread'),[])

    async def test_fast_terminal_then_late_queue_response_preserves_next_delivery(self):
        import asyncio
        from codex_web.agent_runtime import AgentRuntimeEvent
        from codex_web.services.goal_continuation_events import GoalContinuationEventService
        entered,release=asyncio.Event(),asyncio.Event()
        original=self.turns.start
        calls=[]
        async def held(*args,**kwargs):
            result=await original(*args,**kwargs)
            calls.append(result)
            if len(calls)==1:
                entered.set()
                await asyncio.wait_for(release.wait(),5.)
            return result
        self.turns.start=held
        request=asyncio.create_task(self.goal_continuation.dispatch_once(self.goal_binding.id,scope=self.actor.tenant))
        try:
            await asyncio.wait_for(entered.wait(),5.)
            self.target,self.thread=self.assignment,'operator-thread'
            self.bindings.mark_initial_turn_started(self.thread,self.actor)
            await self.attach_native()
            await self.execution.drain_thread_queue(self.thread)
            active=self.execution._active_turn(self.thread)
            self.home_goal.usage.upsert(self.canonical_usage(active))
            self.execution.record_thread_activity({'method':'turn/completed','params':{'threadId':self.thread,'turn':{'id':active.turn_id}}})
            await self.execution.assignment_completion_tasks[self.assignment.id]
            next_binding=self.goal_bindings.get(self.goal_binding.id,scope=self.actor.tenant)
            self.assertEqual(next_binding.status,'active')
            self.assertEqual(next_binding.home_continuation.reserved_calls,2)
            self.assertEqual(next_binding.home_continuation.model_calls,1)
            self.assertNotEqual(next_binding.home_continuation.delivery.queued_id,calls[0]['queuedId'])
            next_delivery=next_binding.home_continuation.delivery.model_copy(deep=True)
            self.assertEqual(len(self.queue_store.get(self.thread)),1)
        finally:
            release.set()
            result=await request
        self.assertEqual(result.outcome,'queued',result.reason)
        current=self.goal_bindings.get(self.goal_binding.id,scope=self.actor.tenant)
        self.assertEqual(current.home_continuation.delivery,next_delivery)
        events=GoalContinuationEventService(self.goal_bindings,self.sessions,self.goal_continuation)
        event=AgentRuntimeEvent(event_type='turn.completed',provider_native_session_id=active.thread_id,provider_native_turn_id=active.turn_id)
        await events.handle_event(event,scope=self.actor.tenant)
        self.assertEqual(len(self.queue_store.get(self.thread)),1)
        self.goal_bindings.operator_stop(self.goal_binding.id,scope=self.actor.tenant,actor_id=self.actor.identity_id,cancelled=False,reason='Pause before next native input')
        await self.execution.drain_thread_queue(self.thread)
        self.assertEqual(len(self.starts()),1)
        current=self.goal_bindings.get(self.goal_binding.id,scope=self.actor.tenant)
        self.assertEqual((current.home_continuation.reserved_calls,current.home_continuation.model_calls),(2,1))

    async def test_generic_service_session_still_refuses_home(self):
        from codex_web.agent_runtime import AgentRuntimeTurnRequest
        from codex_web.runtime.execution import ExecutionPrestartRefusal
        with self.assertRaises(ExecutionPrestartRefusal) as refused:
            await self.sessions.start_turn(self.goal_binding.agent_session_id,
                AgentRuntimeTurnRequest(message='Generic service must not borrow Home'),
                actor=self.goal_continuation._actor(self.actor.tenant))
        self.assertEqual(refused.exception.detail['code'],'native_input_authority_denied')
        self.transport.request.assert_not_awaited()

    async def test_stale_goal_revision_blocks_before_queue(self):
        from codex_web.goals import GoalUpdate
        self.goals.revise(self.goal.id,GoalUpdate(title='Changed authorized objective',reason='Current revision changed'),scope=self.actor.tenant,actor_id=self.actor.identity_id)
        result=await self.goal_continuation.dispatch_once(self.goal_binding.id,scope=self.actor.tenant)
        self.assertEqual(result.outcome,'blocked')
        self.assertEqual(self.queue_store.get('operator-thread'),[])
        self.transport.request.assert_not_awaited()

    async def test_accept_then_lost_queue_response_retains_unknown_one_original(self):
        real=self.turns.start
        async def lost(*args,**kwargs):
            await real(*args,**kwargs)
            raise TimeoutError('Synthetic lost response after actual retained queue')
        self.turns.start=lost
        result=await self.goal_continuation.dispatch_once(self.goal_binding.id,scope=self.actor.tenant)
        self.assertEqual(result.outcome,'unknown')
        original=self.queue_store.get('operator-thread')[0].model_copy(deep=True)
        current=self.goal_bindings.get(self.goal_binding.id,scope=self.actor.tenant)
        self.assertEqual(current.status,'unknown')
        self.assertEqual(current.home_continuation.delivery.phase,'unknown')
        await self.goal_continuation.dispatch_once(self.goal_binding.id,scope=self.actor.tenant)
        self.assertEqual([r.model_dump() for r in self.queue_store.get('operator-thread')],[original.model_dump()])
        self.assertEqual(current.home_continuation.reserved_calls,1)
        self.transport.request.assert_not_awaited()

    async def test_expired_lease_never_resubmits_original_queued_input(self):
        await self.goal_continuation.dispatch_once(self.goal_binding.id,scope=self.actor.tenant)
        original=self.queue_store.get('operator-thread')[0].model_copy(deep=True)
        from codex_web.goal_execution_bindings import GoalExecutionBindingUpdate
        self.goal_bindings.update(self.goal_binding.id,GoalExecutionBindingUpdate(lease_expires_at=1.,reason='Lease expired after queue'),scope=self.actor.tenant,actor_id=self.actor.identity_id)
        result=await self.goal_continuation.dispatch_once(self.goal_binding.id,scope=self.actor.tenant)
        self.assertEqual(result.outcome,'not_dispatched')
        self.assertEqual([r.model_dump() for r in self.queue_store.get('operator-thread')],[original.model_dump()])
        self.transport.request.assert_not_awaited()

    async def test_missing_usage_keeps_original_pending_without_model_poll(self):
        active=await self.start_home_goal_native()
        self.execution.record_thread_activity({'method':'turn/completed','params':{'threadId':self.thread,'turn':{'id':active.turn_id}}})
        await self.execution.assignment_completion_tasks[self.assignment.id]
        current=self.goal_bindings.get(self.goal_binding.id,scope=self.actor.tenant)
        self.assertEqual(current.home_continuation.delivery.phase,'terminal_pending_usage')
        self.assertIsNone(current.home_continuation.delivery.usage_id)
        self.assertEqual(current.status,'active')
        await self.goal_continuation.dispatch_once(current.id,scope=self.actor.tenant)
        self.assertEqual(len(self.starts()),1)
        self.assertEqual(self.queue_store.get(self.thread),[])

    async def revoke_at_home_writer(self, kind):
        import asyncio
        result=await self.goal_continuation.dispatch_once(self.goal_binding.id,scope=self.actor.tenant)
        self.assertEqual(result.outcome,'queued',result.reason)
        original=self.queue_store.get('operator-thread')[0].model_copy(deep=True)
        self.target,self.thread=self.assignment,'operator-thread'
        self.bindings.mark_initial_turn_started(self.thread,self.actor)
        await self.attach_native()
        self.hold_writer=True
        task=asyncio.create_task(self.execution.drain_thread_queue(self.thread))
        try:
            await asyncio.wait_for(self.writes_entered.wait(),5.)
            if kind=='selection':
                self.select(None,1)
            elif kind=='profile':
                from codex_web.agent_profiles import AgentProfileUpdate
                self.profiles.update('home-operator',AgentProfileUpdate(name='New current Profile revision',reason='Final send revocation'),actor=self.actor)
            elif kind=='definition':
                self.publish(self.payload)
            elif kind=='role':
                self.registry.quarantine(self.authority.catalog_record(actor=self.actor,project_id='home').record_id,actor=self.actor.identity_id,reason='Final Goal Role revocation')
            elif kind=='fence':
                self.f.workers.store.update(lambda state:state.model_copy(update={'assignments':[
                    r.model_copy(update={'fence':r.fence+1}) if r.id==self.assignment.id else r for r in state.assignments]}))
            elif kind=='pause':
                self.goal_bindings.operator_stop(self.goal_binding.id,scope=self.actor.tenant,actor_id=self.actor.identity_id,cancelled=False,reason='Explicit operator pause before send')
            elif kind=='goal':
                self.goals.transition(self.goal.id,GoalTransitionRequest(status='paused',reason='Current Goal paused'),scope=self.actor.tenant,actor_id=self.actor.identity_id)
            else:
                raise AssertionError(kind)
        finally:
            if self.native.write_lock.locked():self.native.write_lock.release()
            await task
        self.assertEqual(self.starts(),[])
        # Fence retirement restores the SAME local entry; other typed
        # pre-write refusals retain only the canonical admission snapshot.
        expected=[original.model_dump()] if kind=='fence' else []
        self.assertEqual([r.model_dump() for r in self.queue_store.get(self.thread)],expected)
        saved=self.execution.queued_preflight.store.get('preflight-queued-'+original.id)
        self.assertIsNone(saved.queued_start_receipt)
        self.assertEqual(saved.queued_turn_snapshot,original.model_dump(mode='json'))
        self.transport.request.assert_not_awaited()

    async def test_selection_revocation_at_actual_writer_retains_original(self):
        await self.revoke_at_home_writer('selection')

    async def test_profile_revocation_at_actual_writer_retains_original(self):
        await self.revoke_at_home_writer('profile')

    async def test_definition_revocation_at_actual_writer_retains_original(self):
        await self.revoke_at_home_writer('definition')

    async def test_role_revocation_at_actual_writer_retains_original(self):
        await self.revoke_at_home_writer('role')

    async def test_fence_revocation_at_actual_writer_retains_original(self):
        await self.revoke_at_home_writer('fence')

    async def test_operator_pause_at_actual_writer_retains_original(self):
        await self.revoke_at_home_writer('pause')

    async def test_goal_pause_at_actual_writer_retains_original(self):
        await self.revoke_at_home_writer('goal')

    async def test_global_pause_blocks_before_original_reservation(self):
        from codex_web.autonomy import AutonomyMode
        self.goal_continuation.autonomy.store.load=lambda:NS(control=self.control.model_copy(update={'mode':AutonomyMode.PAUSED}))
        result=await self.goal_continuation.dispatch_once(self.goal_binding.id,scope=self.actor.tenant)
        self.assertEqual(result.outcome,'blocked')
        current=self.goal_bindings.get(self.goal_binding.id,scope=self.actor.tenant)
        self.assertIsNone(current.home_continuation)
        self.assertEqual(self.queue_store.get('operator-thread'),[])

    async def test_legacy_binding_omission_and_global_version_boundary(self):
        from codex_web.compatibility import ContractSpec
        raw=self.goal_bindings.store.load().model_dump(mode='json')
        self.assertNotIn('home_continuation',raw['bindings'][0])
        raw['schema_version']='1.1'
        self.f.sqlite.put(self.goal_bindings.store.namespace,raw)
        migrated=self.goal_bindings.store.load()
        self.assertEqual(migrated.schema_version,'1.2')
        # Reading is inert: no new contract may be written by cold composition.
        self.assertEqual(self.f.sqlite.get(self.goal_bindings.store.namespace)['schema_version'],'1.1')
        self.goal_bindings.store.update(lambda state:state)
        written=self.f.sqlite.get(self.goal_bindings.store.namespace)
        self.assertEqual(written['schema_version'],'1.2')
        old=ContractSpec('goal-execution-binding-state','1.1',('1.0','1.1'))
        with self.assertRaises(Exception):old.require(written['schema_version'])
        selected=self.goal_bindings.store.binding_state(self.goal_binding.id,
            organization_id=self.actor.organization_id,workspace_id=self.actor.workspace_id)
        self.assertEqual(selected.bindings,[self.goal_binding])

    async def test_actual_application_terminal_composition_preserves_all_owners_on_failure(self):
        import ast
        from pathlib import Path
        tree=ast.parse((Path(__file__).parents[1]/'codex_web/application.py').read_text())
        fn=next(row for row in tree.body if isinstance(row,ast.AsyncFunctionDef) and row.name=='_record_home_retained_terminal')
        calls=[]
        async def failed(active,**kwargs):
            calls.append('child')
            raise ValueError('Preserved child observer failure')
        async def goal(active,**kwargs):calls.append('goal')
        async def restart(active,**kwargs):calls.append('restart')
        async def followup(active,**kwargs):calls.append('followup')
        env={'home_child_terminal_service':NS(record_terminal=failed),
            'home_goal_continuation':NS(record_terminal=goal),
            'home_restart_recovery_service':NS(record_terminal=restart),
            'home_operator_followup':NS(record_terminal=followup)}
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'actual-application-terminal-composition','exec'),env)
        with self.assertRaisesRegex(ValueError,'Preserved child'):
            await env[fn.name](object(),succeeded=True)
        self.assertEqual(calls,['child','goal','restart','followup'])


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(HomeGoalContinuationTests(name) for name in HomeGoalContinuationTests.__dict__ if name.startswith('test_'))
