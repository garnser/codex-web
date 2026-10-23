"""Canonical active Home Goal restart qualification; no app import or live fault."""
import unittest
from codex_web.services.home_restart_recovery import HomeRestartRecoveryService
from codex_web.services.stale_active_turns import ActiveTurnRecoveryStore
from codex_web.runtime.execution import ExecutionPrestartRefusal
from tests.test_home_goal_continuation import HomeGoalContinuationTests

class HomeGoalRestartTests(HomeGoalContinuationTests):
    GOAL_MODEL_CALLS=5
    async def asyncSetUp(self):
        if self._testMethodName in {'test_all_original_and_recovered_usage_accounts_once_then_budget_stops','test_late_original_usage_event_settles_same_recovery_once'}:self.GOAL_MODEL_CALLS=2
        if self._testMethodName=='test_goal_budget_cannot_be_bypassed_by_restart_input':self.GOAL_MODEL_CALLS=1
        await super().asyncSetUp()

    async def attach_native(self):
        await super().attach_native()
        original=self.native._dispatch_message
        sequence=[]
        async def distinct(message):
            if isinstance(message.get('result'),dict) and 'turn' in message['result']:
                sequence.append(True)
                message={**message,'result':{'turn':{**message['result']['turn'],'id':'goal-native-'+str(len(sequence))}}}
            await original(message)
        self.native._dispatch_message=distinct

    async def retain_active_goal(self):
        active=await self.start_home_goal_native()
        restart=HomeRestartRecoveryService(home_host=self.service.home_operator_host,
            recovery_store=ActiveTurnRecoveryStore(self.f.sqlite),execution=self.execution,
            intents=self.intents,preflight=self.execution.queued_preflight)
        restart.goal_continuation=self.home_goal
        self.home_goal.restart_recovery=restart
        self.execution.home_restart_recovery=restart
        self.execution.retained_turn_outcome_recorder=self.record_all_goal_terminals
        actor=self.f.identity.actor_for_identity(self.actor.identity_id,scope=self.actor.tenant)
        queued=restart.retain_startup(active,project=self.projects.get('home',actor.tenant),actor=actor)
        return active,queued,restart

    async def record_all_goal_terminals(self,active,*,succeeded):
        try:await self.home_goal.record_terminal(active,succeeded=succeeded)
        finally:await self.execution.home_restart_recovery.record_terminal(active,succeeded=succeeded)

    async def test_expired_original_goal_lease_renews_same_known_not_sent_input(self):
        from codex_web.goal_execution_bindings import GoalExecutionBindingUpdate
        from codex_web.services.goal_continuation_recovery import GoalContinuationRecoveryService
        await self.goal_continuation.dispatch_once(self.goal_binding.id,scope=self.actor.tenant)
        original=self.queue_store.get('operator-thread')[0].model_copy(deep=True)
        self.execution.queued_preflight.retain_queued(original,organization_id=self.actor.organization_id,workspace_id=self.actor.workspace_id)
        self.goal_bindings.update(self.goal_binding.id,GoalExecutionBindingUpdate(lease_expires_at=1.,reason='Known queued Goal lease expired'),scope=self.actor.tenant,actor_id=self.actor.identity_id)
        recovery=GoalContinuationRecoveryService(self.goal_bindings,self.goal_continuation,self.sessions)
        await recovery.recover_scope(scope=self.actor.tenant)
        self.assertEqual(self.queue_store.get('operator-thread')[0],original)
        self.target,self.thread=self.assignment,'operator-thread'
        self.bindings.mark_initial_turn_started(self.thread,self.actor)
        await self.attach_native()
        await self.execution.drain_thread_queue(self.thread)
        self.assertEqual(len(self.starts()),1,self.host.events)
        self.assertEqual(self.execution.queued_preflight.store.get('preflight-queued-'+original.id).status,'started')
        current=self.goal_bindings.get(self.goal_binding.id,scope=self.actor.tenant)
        self.assertEqual(current.home_continuation.reserved_calls,1)

    async def test_unknown_original_start_never_renews_or_resubmits(self):
        from codex_web.goal_execution_bindings import GoalExecutionBindingUpdate
        await self.goal_continuation.dispatch_once(self.goal_binding.id,scope=self.actor.tenant)
        original=self.queue_store.get('operator-thread')[0].model_copy(deep=True)
        service=self.execution.queued_preflight
        attempt=service.retain_queued(original,organization_id=self.actor.organization_id,workspace_id=self.actor.workspace_id)
        attempt,claim=service.claim_queued_admission(attempt.id)
        self.assertTrue(service.begin_queued_start(attempt.id,claim_id=claim))
        self.goal_bindings.update(self.goal_binding.id,GoalExecutionBindingUpdate(lease_expires_at=1.,reason='Unknown start lease expired'),scope=self.actor.tenant,actor_id=self.actor.identity_id)
        current=self.goal_bindings.get(self.goal_binding.id,scope=self.actor.tenant)
        self.assertFalse(self.home_goal.renew_known_not_sent(current))
        self.assertEqual(self.queue_store.get('operator-thread')[0],original)
        held=service.store.get(attempt.id)
        self.assertEqual(held.status,'reconciliation_required')
        self.assertEqual(held.queued_admission_claim_id,claim)

    async def test_all_original_and_recovered_usage_accounts_once_then_budget_stops(self):
        active,queued,restart=await self.retain_active_goal()
        self.execution.clear_thread_active(self.thread)
        await self.execution.drain_thread_queue(self.thread)
        recovered=self.execution._active_turn(self.thread)
        original_usage=self.canonical_usage(active).model_copy(update={'terminal_outcome':'interrupted'})
        self.home_goal.usage.upsert(original_usage)
        self.home_goal.usage.upsert(self.canonical_usage(recovered))
        self.execution.record_thread_activity({'method':'turn/completed','params':{'threadId':self.thread,'turn':{'id':recovered.turn_id}}})
        await self.execution.assignment_completion_tasks[self.assignment.id]
        await restart.record_terminal(recovered,succeeded=True) # duplicate exact owned observation
        binding=self.goal_bindings.get(self.goal_binding.id,scope=self.actor.tenant)
        self.assertEqual(binding.home_continuation.delivery.phase,'settled')
        self.assertEqual(binding.home_continuation.delivery.usage_id,original_usage.id)
        self.assertEqual((binding.home_continuation.reserved_calls,binding.home_continuation.model_calls),(2,2))
        self.assertEqual((binding.home_continuation.input_tokens,binding.home_continuation.output_tokens),(34,18))
        self.assertEqual(binding.status,'blocked')
        self.assertEqual(len(self.starts()),2)
        self.assertEqual(self.queue_store.get(self.thread),[])

    async def test_late_original_usage_event_settles_same_recovery_once(self):
        from codex_web.agent_runtime import AgentRuntimeEvent
        from codex_web.services.goal_continuation_events import GoalContinuationEventService
        active,queued,restart=await self.retain_active_goal()
        self.execution.clear_thread_active(self.thread)
        await self.execution.drain_thread_queue(self.thread)
        recovered=self.execution._active_turn(self.thread)
        self.home_goal.usage.upsert(self.canonical_usage(recovered))
        self.execution.record_thread_activity({'method':'turn/completed','params':{'threadId':self.thread,'turn':{'id':recovered.turn_id}}})
        await self.execution.assignment_completion_tasks[self.assignment.id]
        held=self.goal_bindings.get(self.goal_binding.id,scope=self.actor.tenant)
        self.assertEqual(held.home_continuation.delivery.phase,'terminal_pending_usage')
        self.assertEqual(held.last_turn_id,recovered.turn_id)
        self.home_goal.usage.upsert(self.canonical_usage(active).model_copy(update={'terminal_outcome':'interrupted'}))
        events=GoalContinuationEventService(self.goal_bindings,self.sessions,self.goal_continuation)
        event=AgentRuntimeEvent(event_type='turn.interrupted',provider_native_session_id=active.thread_id,provider_native_turn_id=active.turn_id)
        first=await events.handle_event(event,scope=self.actor.tenant)
        await events.handle_event(event,scope=self.actor.tenant)
        self.assertEqual(first.binding_id,self.goal_binding.id)
        current=self.goal_bindings.get(self.goal_binding.id,scope=self.actor.tenant)
        self.assertEqual(current.home_continuation.delivery.phase,'settled')
        self.assertEqual(current.home_continuation.model_calls,2)
        self.assertEqual((current.home_continuation.input_tokens,current.home_continuation.output_tokens),(34,18))
        self.assertEqual(current.status,'blocked')
        self.assertEqual(len(self.starts()),2)
        self.assertEqual(self.queue_store.get(self.thread),[])

    async def test_repeated_recovery_keeps_original_goal_and_reserves_each_new_input(self):
        active,queued,restart=await self.retain_active_goal()
        self.execution.clear_thread_active(self.thread)
        await self.execution.drain_thread_queue(self.thread)
        first=self.execution._active_turn(self.thread)
        actor=self.f.identity.actor_for_identity(self.actor.identity_id,scope=self.actor.tenant)
        second=restart.retain_startup(first,project=self.projects.get('home',actor.tenant),actor=actor)
        self.execution.clear_thread_active(self.thread)
        await self.execution.drain_thread_queue(self.thread)
        self.assertEqual(len(self.starts()),3,self.host.events)
        saved=restart.store.get(self.thread)
        self.assertEqual(len(saved.prior),1)
        self.assertEqual(saved.evidence['home_restart']['goal_origin']['active']['turn_id'],active.turn_id)
        binding=self.goal_bindings.get(self.goal_binding.id,scope=self.actor.tenant)
        self.assertEqual(binding.home_continuation.delivery.native_turn_id,active.turn_id)
        self.assertEqual(binding.home_continuation.reserved_calls,3)
        self.assertEqual(binding.home_continuation.model_calls,0)
        self.assertNotEqual(queued.id,second.id)

    async def test_goal_budget_cannot_be_bypassed_by_restart_input(self):
        active=await self.start_home_goal_native()
        restart=HomeRestartRecoveryService(home_host=self.service.home_operator_host,
            recovery_store=ActiveTurnRecoveryStore(self.f.sqlite),execution=self.execution,
            intents=self.intents,preflight=self.execution.queued_preflight)
        restart.goal_continuation=self.home_goal
        actor=self.f.identity.actor_for_identity(self.actor.identity_id,scope=self.actor.tenant)
        with self.assertRaisesRegex(ValueError,'budget'):
            restart.retain_startup(active,project=self.projects.get('home',actor.tenant),actor=actor)
        self.assertEqual(len(self.starts()),1)
        self.assertEqual(self.queue_store.get(self.thread),[])
        self.assertEqual(self.goal_bindings.get(self.goal_binding.id,scope=self.actor.tenant).home_continuation.reserved_calls,1)

    async def test_original_unknown_usage_stays_held_after_recovered_terminal(self):
        active,queued,restart=await self.retain_active_goal()
        self.execution.clear_thread_active(self.thread)
        await self.execution.drain_thread_queue(self.thread)
        recovered=self.execution._active_turn(self.thread)
        self.home_goal.usage.upsert(self.canonical_usage(recovered))
        self.execution.record_thread_activity({'method':'turn/completed','params':{'threadId':self.thread,'turn':{'id':recovered.turn_id}}})
        await self.execution.assignment_completion_tasks[self.assignment.id]
        binding=self.goal_bindings.get(self.goal_binding.id,scope=self.actor.tenant)
        self.assertEqual(binding.home_continuation.delivery.phase,'terminal_pending_usage')
        self.assertEqual(binding.home_continuation.model_calls,1)
        self.assertIsNone(binding.home_continuation.delivery.usage_id)
        self.assertEqual(len(self.starts()),2)
        self.assertEqual(self.queue_store.get(self.thread),[])

    async def test_retained_goal_recovery_starts_once_through_actual_final_transport(self):
        active,queued,restart=await self.retain_active_goal()
        self.execution.clear_thread_active(self.thread)
        await self.execution.drain_thread_queue(self.thread)
        self.assertEqual(len(self.starts()),2,self.host.events)
        current=self.execution._active_turn(self.thread)
        self.assertNotEqual(current.turn_id,active.turn_id)
        attempt=self.execution.queued_preflight.store.get('preflight-queued-'+queued.id)
        self.assertEqual(attempt.status,'started')
        self.assertEqual(attempt.queued_start_receipt.provider_native_turn_id,current.turn_id)
        original=self.goal_bindings.get(self.goal_binding.id,scope=self.actor.tenant)
        self.assertEqual(original.home_continuation.delivery.native_turn_id,active.turn_id)
        self.assertEqual(original.home_continuation.reserved_calls,2)
        self.assertEqual(original.home_continuation.model_calls,0) # No unknown usage is fabricated.
        self.assertEqual(restart.store.get(self.thread).schema_version,'1.1')

    async def revoke_restart_at_writer(self,kind):
        import asyncio
        active,queued,restart=await self.retain_active_goal()
        self.execution.clear_thread_active(self.thread)
        self.hold_writer=True
        task=asyncio.create_task(self.execution.drain_thread_queue(self.thread))
        try:
            await asyncio.wait_for(self.writes_entered.wait(),5.)
            if kind=='goal':
                from codex_web.goals import GoalTransitionRequest
                self.goals.transition(self.goal.id,GoalTransitionRequest(status='paused',reason='Pause interrupted Goal'),scope=self.actor.tenant,actor_id=self.actor.identity_id)
            elif kind=='pause':
                self.goal_bindings.operator_stop(self.goal_binding.id,scope=self.actor.tenant,actor_id=self.actor.identity_id,cancelled=False,reason='Pause before recovered input')
            elif kind=='selection':self.select(None,1)
            elif kind=='role':
                self.registry.quarantine(self.authority.catalog_record(actor=self.actor,project_id='home').record_id,actor=self.actor.identity_id,reason='Revoke recovery Role')
            else:raise AssertionError(kind)
        finally:
            if self.native.write_lock.locked():self.native.write_lock.release()
            await task
        self.assertEqual(len(self.starts()),1)
        saved=self.execution.queued_preflight.store.get('preflight-queued-'+queued.id)
        self.assertIsNone(saved.queued_start_receipt)
        self.assertEqual(saved.queued_turn_snapshot,queued.model_dump(mode='json'))
        binding=self.goal_bindings.get(self.goal_binding.id,scope=self.actor.tenant)
        self.assertEqual(binding.home_continuation.delivery.native_turn_id,active.turn_id)

    async def test_goal_pause_at_recovered_final_writer_denies_new_input(self):await self.revoke_restart_at_writer('goal')
    async def test_binding_pause_at_recovered_final_writer_denies_new_input(self):await self.revoke_restart_at_writer('pause')
    async def test_selection_revocation_at_recovered_final_writer_denies_new_input(self):await self.revoke_restart_at_writer('selection')
    async def test_role_revocation_at_recovered_final_writer_denies_new_input(self):await self.revoke_restart_at_writer('role')

    async def test_expired_recovery_lease_renews_same_reserved_input(self):
        from codex_web.goal_execution_bindings import GoalExecutionBindingUpdate
        from codex_web.services.goal_continuation_recovery import GoalContinuationRecoveryService
        active,queued,restart=await self.retain_active_goal()
        self.execution.clear_thread_active(self.thread)
        self.execution.queued_preflight.retain_queued(queued,organization_id=self.actor.organization_id,workspace_id=self.actor.workspace_id)
        self.goal_bindings.update(self.goal_binding.id,GoalExecutionBindingUpdate(lease_expires_at=1.,reason='Retained recovery Goal lease expired'),scope=self.actor.tenant,actor_id=self.actor.identity_id)
        owner=GoalContinuationRecoveryService(self.goal_bindings,self.goal_continuation,self.sessions)
        await owner.recover_scope(scope=self.actor.tenant)
        self.assertEqual(self.queue_store.get(self.thread),[queued])
        await self.execution.drain_thread_queue(self.thread)
        self.assertEqual(len(self.starts()),2,self.host.events)
        current=self.goal_bindings.get(self.goal_binding.id,scope=self.actor.tenant)
        self.assertEqual(current.home_continuation.reserved_calls,2)
        self.assertEqual(current.home_continuation.delivery.native_turn_id,active.turn_id)

    async def test_unknown_recovery_start_never_renews_or_adds_input(self):
        from codex_web.goal_execution_bindings import GoalExecutionBindingUpdate
        active,queued,restart=await self.retain_active_goal()
        service=self.execution.queued_preflight
        attempt=service.retain_queued(queued,organization_id=self.actor.organization_id,workspace_id=self.actor.workspace_id)
        attempt,claim=service.claim_queued_admission(attempt.id)
        self.assertTrue(service.begin_queued_start(attempt.id,claim_id=claim))
        self.goal_bindings.update(self.goal_binding.id,GoalExecutionBindingUpdate(lease_expires_at=1.,reason='Recovery unknown transport expired'),scope=self.actor.tenant,actor_id=self.actor.identity_id)
        binding=self.goal_bindings.get(self.goal_binding.id,scope=self.actor.tenant)
        self.assertFalse(self.home_goal.renew_known_not_sent(binding))
        self.assertEqual(self.queue_store.get(self.thread),[queued])
        self.assertEqual(service.store.get(attempt.id).queued_admission_claim_id,claim)
        self.assertEqual(binding.home_continuation.reserved_calls,2)
        self.assertEqual(len(self.starts()),1)

    async def test_missing_recovered_usage_stays_held_without_zero_accounting(self):
        active,queued,restart=await self.retain_active_goal()
        self.execution.clear_thread_active(self.thread)
        await self.execution.drain_thread_queue(self.thread)
        recovered=self.execution._active_turn(self.thread)
        self.home_goal.usage.upsert(self.canonical_usage(active).model_copy(update={'terminal_outcome':'interrupted'}))
        self.execution.record_thread_activity({'method':'turn/completed','params':{'threadId':self.thread,'turn':{'id':recovered.turn_id}}})
        await self.execution.assignment_completion_tasks[self.assignment.id]
        binding=self.goal_bindings.get(self.goal_binding.id,scope=self.actor.tenant)
        self.assertEqual(binding.home_continuation.delivery.phase,'terminal_pending_usage')
        self.assertEqual(binding.home_continuation.model_calls,1)
        self.assertEqual(binding.home_continuation.input_tokens,17)
        self.assertEqual(len(self.starts()),2)
        self.assertEqual(self.queue_store.get(self.thread),[])

    async def test_old_recovered_terminal_preserves_next_delivery(self):
        active,queued,restart=await self.retain_active_goal()
        self.execution.clear_thread_active(self.thread)
        await self.execution.drain_thread_queue(self.thread)
        recovered=self.execution._active_turn(self.thread)
        self.home_goal.usage.upsert(self.canonical_usage(active).model_copy(update={'terminal_outcome':'interrupted'}))
        self.home_goal.usage.upsert(self.canonical_usage(recovered))
        self.execution.record_thread_activity({'method':'turn/completed','params':{'threadId':self.thread,'turn':{'id':recovered.turn_id}}})
        await self.execution.assignment_completion_tasks[self.assignment.id]
        before=self.goal_bindings.get(self.goal_binding.id,scope=self.actor.tenant)
        original_queue=list(self.queue_store.get(self.thread))
        self.assertEqual(before.home_continuation.reserved_calls,3)
        self.assertNotEqual(before.home_continuation.delivery.native_turn_id,active.turn_id)
        await restart.record_terminal(recovered,succeeded=True)
        self.assertEqual(self.goal_bindings.get(self.goal_binding.id,scope=self.actor.tenant),before)
        self.assertEqual(self.queue_store.get(self.thread),original_queue)
        self.assertEqual(len(original_queue),1)

    async def test_history_witness_failure_reconciles_without_duplicate_accounting(self):
        from unittest.mock import patch
        from codex_web.agent_runtime import AgentRuntimeEvent
        from codex_web.services.goal_continuation_events import GoalContinuationEventService
        active,queued,restart=await self.retain_active_goal()
        self.execution.clear_thread_active(self.thread)
        await self.execution.drain_thread_queue(self.thread)
        recovered=self.execution._active_turn(self.thread)
        self.home_goal.usage.upsert(self.canonical_usage(active).model_copy(update={'terminal_outcome':'interrupted'}))
        self.home_goal.usage.upsert(self.canonical_usage(recovered))
        with patch.object(restart,'retain_goal_settlement',side_effect=RuntimeError('Point write unavailable')):
            with self.assertRaisesRegex(RuntimeError,'Point write'):
                await restart.record_terminal(recovered,succeeded=True)
        current=self.goal_bindings.get(self.goal_binding.id,scope=self.actor.tenant)
        self.assertEqual(current.home_continuation.delivery.phase,'terminal_pending_usage')
        self.assertEqual(current.home_continuation.model_calls,2)
        self.assertEqual(self.queue_store.get(self.thread),[])
        event=AgentRuntimeEvent(event_type='turn.completed',provider_native_session_id=recovered.thread_id,provider_native_turn_id=recovered.turn_id)
        events=GoalContinuationEventService(self.goal_bindings,self.sessions,self.goal_continuation)
        await events.handle_event(event,scope=self.actor.tenant)
        await events.handle_event(event,scope=self.actor.tenant)
        current=self.goal_bindings.get(self.goal_binding.id,scope=self.actor.tenant)
        self.assertEqual(current.home_continuation.model_calls,2)
        self.assertEqual(current.home_continuation.reserved_calls,3)
        self.assertEqual(len(self.queue_store.get(self.thread)),1)
        self.assertIn('goal_settlement',restart.store.get(self.thread).evidence['home_restart'])

    async def test_actual_call_budget_exhaustion_at_recovery_writer_denies_input(self):
        import asyncio
        active,queued,restart=await self.retain_active_goal()
        self.execution.clear_thread_active(self.thread)
        self.hold_writer=True
        task=asyncio.create_task(self.execution.drain_thread_queue(self.thread))
        try:
            await asyncio.wait_for(self.writes_entered.wait(),5.)
            self.home_goal.mutate(self.goal_binding.id,self.actor.tenant,lambda row,state:row.model_copy(update={
                'home_continuation':row.home_continuation.model_copy(update={'model_calls':self.GOAL_MODEL_CALLS})}),
                'Actual finalized Goal usage exhausted call budget after reservation')
        finally:
            if self.native.write_lock.locked():self.native.write_lock.release()
            await task
        self.assertEqual(len(self.starts()),1)
        saved=self.execution.queued_preflight.store.get('preflight-queued-'+queued.id)
        self.assertIsNone(saved.queued_start_receipt)
        self.assertEqual(saved.queued_turn_snapshot,queued.model_dump(mode='json'))

    async def test_expired_worker_custody_rebinds_same_thread_and_original_goal(self):
        from tests.test_home_restart_recovery import HomeRestartRecoveryTests
        active,queued,restart=await self.retain_active_goal()
        self.actor=self.f.identity.actor_for_identity(self.actor.identity_id,scope=self.actor.tenant)
        old=self.assignment
        current=HomeRestartRecoveryTests.heal_same_thread(self)
        self.assertNotEqual(current.id,old.id)
        self.assertNotEqual(current.execution_workspace_id,old.execution_workspace_id)
        self.assertEqual(self.f.workers.store.assignment(old.id).status,'cancelled')
        self.execution.clear_thread_active(self.thread)
        session=self.sessions.store.find_unique_by_native_id(self.thread,provider_id='openai',runtime_id='codex')
        self.sessions.store.upsert(session.model_copy(update={'assignment_id':current.id,'execution_id':current.execution_id,
            'execution_workspace_id':current.execution_workspace_id}))
        self.target=current
        await self.attach_native()
        await self.execution.drain_thread_queue(self.thread)
        self.assertEqual(len(self.starts()),1,self.host.events) # New worker wire only.
        recovered=self.execution._active_turn(self.thread)
        self.assertEqual((recovered.thread_id,recovered.assignment_id,recovered.execution_id),(active.thread_id,current.id,current.execution_id))
        binding=self.goal_bindings.get(self.goal_binding.id,scope=self.actor.tenant)
        self.assertEqual(binding.home_continuation.delivery.native_turn_id,active.turn_id)
        self.assertEqual(binding.home_continuation.delivery.assignment_id,old.id)
        self.assertEqual(binding.home_continuation.reserved_calls,2)
        self.assertEqual(restart.store.get(self.thread).original,active)
        self.assertEqual(self.queue_store.get(self.thread),[])

    async def prepare_settled_chain_and_new_goal_delivery(self):
        active,queued,restart=await self.retain_active_goal()
        self.execution.clear_thread_active(self.thread)
        await self.execution.drain_thread_queue(self.thread)
        recovered=self.execution._active_turn(self.thread)
        self.home_goal.usage.upsert(self.canonical_usage(active).model_copy(update={'terminal_outcome':'interrupted'}))
        self.home_goal.usage.upsert(self.canonical_usage(recovered))
        self.execution.record_thread_activity({'method':'turn/completed','params':{'threadId':self.thread,'turn':{'id':recovered.turn_id}}})
        await self.execution.assignment_completion_tasks[self.assignment.id]
        previous=restart.store.get(self.thread)
        self.assertIn('goal_settlement',previous.evidence['home_restart'])
        historical=previous.model_dump(mode='json',exclude={'prior'})
        restart.store.store.record_update(restart.store.RECORD_NAMESPACE,self.thread,
            lambda raw:{**raw,'prior':[historical]*20},default=None)
        await self.execution.drain_thread_queue(self.thread)
        fresh=self.execution._active_turn(self.thread)
        self.assertEqual(len(self.starts()),3)
        self.assertTrue(fresh.source.removeprefix('queued:').startswith('home-goal-continuation:'))
        actor=self.f.identity.actor_for_identity(self.actor.identity_id,scope=self.actor.tenant)
        return restart,fresh,actor,active,recovered

    async def test_independent_goal_delivery_archives_twenty_settled_generations(self):
        restart,fresh,actor,active,recovered=await self.prepare_settled_chain_and_new_goal_delivery()
        queued=restart.retain_startup(fresh,project=self.projects.get('home',actor.tenant),actor=actor)
        current=restart.store.get(self.thread)
        self.assertEqual(current.prior,())
        archived=restart.store.archived(current.evidence['home_restart']['previous_history_id'])
        self.assertEqual(len(archived.prior),20)
        self.assertEqual(archived.evidence['home_restart']['goal_origin']['active']['turn_id'],active.turn_id)
        self.assertEqual(current.evidence['home_restart']['goal_origin']['active']['turn_id'],fresh.turn_id)
        self.assertEqual(queued.home_restart_input.delivery_id,self.goal_bindings.get(self.goal_binding.id,scope=self.actor.tenant).home_continuation.delivery.id)

    async def test_unknown_old_usage_prevents_independent_goal_history_reset(self):
        restart,fresh,actor,active,recovered=await self.prepare_settled_chain_and_new_goal_delivery()
        usage=self.canonical_usage(active).model_copy(update={'terminal_outcome':'unknown'})
        self.home_goal.usage.upsert(usage)
        before=restart.store.get(self.thread)
        with self.assertRaises(ExecutionPrestartRefusal):
            restart.retain_startup(fresh,project=self.projects.get('home',actor.tenant),actor=actor)
        self.assertEqual(restart.store.get(self.thread),before)
        self.assertEqual(len(self.starts()),3)
        self.assertEqual(self.queue_store.get(self.thread),[])

    async def test_missing_old_usage_prevents_independent_goal_history_reset(self):
        from unittest.mock import patch
        restart,fresh,actor,active,recovered=await self.prepare_settled_chain_and_new_goal_delivery()
        usage_id=self.canonical_usage(active).id
        read=self.home_goal.usage.get
        before=restart.store.get(self.thread)
        with patch.object(self.home_goal.usage,'get',side_effect=lambda key:None if key==usage_id else read(key)):
            with self.assertRaises(ExecutionPrestartRefusal):
                restart.retain_startup(fresh,project=self.projects.get('home',actor.tenant),actor=actor)
        self.assertEqual(restart.store.get(self.thread),before)
        self.assertEqual(self.queue_store.get(self.thread),[])

    async def test_goal_proof_and_record_versions_refuse_silent_downgrade(self):
        from pydantic import ValidationError
        from codex_web.home_restart import HomeRestartInputProof
        from codex_web.services.stale_active_turns import ActiveTurnRecoveryRecord
        active,queued,restart=await self.retain_active_goal()
        with self.assertRaises(ValidationError):HomeRestartInputProof.model_validate(queued.home_restart_input.model_dump(mode='json'))
        raw=restart.store.get(self.thread).model_dump(mode='json')
        self.assertEqual(raw['schema_version'],'1.1')
        raw.pop('schema_version')
        with self.assertRaises(ValidationError):ActiveTurnRecoveryRecord.model_validate(raw)
        ordinary=ActiveTurnRecoveryRecord(thread_id=active.thread_id,recovery_id='ordinary-legacy',source_updated_at=active.updated_at,
            observed_at=active.updated_at,outcome='live',reason_code='ordinary',actor_id=self.actor.identity_id,original=active,updated_at=active.updated_at)
        self.assertNotIn('schema_version',ordinary.model_dump(mode='json'))

    async def test_started_home_goal_is_a_canonical_restart_origin(self):
        active=await self.start_home_goal_native()
        binding=self.goal_bindings.get(self.goal_binding.id,scope=self.actor.tenant)
        self.assertEqual(binding.home_continuation.delivery.phase,'started')
        self.assertEqual(binding.home_continuation.delivery.native_turn_id,active.turn_id)
        restart=HomeRestartRecoveryService(home_host=self.service.home_operator_host,
            recovery_store=ActiveTurnRecoveryStore(self.f.sqlite),execution=self.execution,
            intents=self.intents,preflight=self.execution.queued_preflight)
        restart.goal_continuation=self.home_goal
        self.home_goal.restart_recovery=restart
        current_actor=self.f.identity.actor_for_identity(self.actor.identity_id,scope=self.actor.tenant)
        try:
            queued=restart.retain_startup(active,project=self.projects.get('home',self.actor.tenant),actor=current_actor)
        except ExecutionPrestartRefusal as exc:
            self.fail('Actual canonical Goal START cannot retain restart continuation: '+str(exc.detail))
        self.assertEqual(queued.thread_id,active.thread_id)
        self.assertEqual(queued.source,'restart-recovery:home')
        self.assertIsNotNone(queued.home_restart_input)
        self.assertEqual(len(self.starts()),1) # retention cannot repeat original native input

def load_tests(loader,tests,pattern):
    return unittest.TestSuite(HomeGoalRestartTests(name) for name in HomeGoalRestartTests.__dict__ if name.startswith('test_'))
