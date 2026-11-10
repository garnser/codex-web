"""Project Goal continuation with canonical services and a private worker fixture."""
import asyncio
import ast
import hashlib
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from codex_web.autonomy import AutonomyControl, AutonomyExclusiveGoalScope
from codex_web.goal_execution_bindings import GoalExecutionBindingCreate
from codex_web.goals import GoalBudget, GoalCreate, GoalSuccessCriterion, GoalTransitionRequest, GoalWorkGraphBinding, GoalUpdate
from codex_web.services.codex_agent_runtime import CodexAgentRuntimeAdapter
from codex_web.services.goal_continuation import GoalContinuationService
from codex_web.services.goal_execution_bindings import (
    GoalExecutionBindingService, GoalExecutionBindingConflictError, GoalExecutionBindingNotFoundError,
)
from codex_web.services.goals import GoalService
from codex_web.services.procedure_discovery import (
    ProcedureDiscoveryService, ProcedureExecutionCapabilityService, install_native_procedure_admission,
)
from codex_web.services.agent_providers import AgentProviderService
from codex_web.agent_providers import AgentProviderUpsert
from codex_web.storage.agent_providers import AgentProviderStore
from codex_web.services.project_goal_continuation import ProjectGoalContinuation
from codex_web.storage.agent_runtime_usage import AgentRuntimeUsageStore
from codex_web.agent_runtime import AgentRuntimeTurnRequest
from codex_web.agent_runtime_usage import AgentRuntimeUsage
from codex_web.agent_profiles import AgentProfileUpdate
from codex_web.autonomy import AutonomyScopedPause
from codex_web.runtime.execution import ExecutionPrestartRefusal
from codex_web.definitions import DefinitionDraftCreate, DefinitionPublishRequest
from codex_web.services.goal_continuation_recovery import GoalContinuationRecoveryService
from codex_web.services.goal_continuation_events import GoalContinuationEventService
from codex_web.agent_runtime import AgentRuntimeEvent
from codex_web.goal_execution_bindings import GoalExecutionBinding
from codex_web.compatibility import ContractSpec, ContractCompatibilityError
from codex_web.home_goal import HomeGoalProgress
from codex_web.services.identity import AuthorizationError
from codex_web.identity import TenantScope
from codex_web.storage.agent_sessions import AgentSessionConflictError
from codex_web.services.skills import SkillService
from codex_web.storage.goal_execution_bindings import GoalExecutionBindingStore
from codex_web.storage.goals import GoalStore
from codex_web.storage.thread_history import ThreadHistoryRepository
from tests.test_goals import _Graph, _node
from tests.test_home_coordination_canonical_send import HomeCanonicalSendTests


class ProjectGoalWorkerAdmissionTests(HomeCanonicalSendTests):
    target_execution_profile = 'orchestration-only'
    target_has_repositories = False
    goal_budget = GoalBudget(max_input_tokens=4000, max_output_tokens=1000, max_model_calls=1, max_retries=0)
    async def asyncSetUp(self):
        await super().asyncSetUp()
        graph = _Graph()
        graph.nodes['data'] = (_node('data!26', 'data'),)
        self.goals = GoalService(GoalStore(self.f.sqlite), self.projects, graph)
        draft = self.goals.create(GoalCreate(title='Finish the existing Data MR',
            description='Coordinate the currently owned MR without creating another MR.',
            owner_identity_id=self.actor.identity_id,
            budget=self.goal_budget,
            success_criteria=(GoalSuccessCriterion(description='Existing MR independently accepted'),),
            work_graph_bindings=(GoalWorkGraphBinding(project_id='data', root_work_item_refs=('data!26',)),)),
            scope=self.actor.tenant, actor_id=self.actor.identity_id)
        self.goal = self.goals.transition(draft.id, GoalTransitionRequest(status='active', reason='Actual bounded fixture mission'),
            scope=self.actor.tenant, actor_id=self.actor.identity_id)
        self.goal_bindings = GoalExecutionBindingService(GoalExecutionBindingStore(self.f.sqlite), self.goals)
        session = self.sessions.store.find_unique_by_native_id(self.thread, provider_id='openai', runtime_id='codex')
        self.sessions.store.upsert(session.model_copy(update={'worker_id': self.target.assigned_worker_id}))
        self.goal_binding = self.goal_bindings.create(self.goal.id, GoalExecutionBindingCreate(
            project_id='data', goal_revision=self.goal.revision, work_item_refs=('data!26',),
            provider_id=session.provider_id, runtime_id=session.runtime_id, agent_session_id=session.id,
            thread_id=self.thread, execution_owner_id=self.actor.identity_id, reason='Same existing assigned Project coordinator'),
            scope=self.actor.tenant, actor_id=self.actor.identity_id)
        self.transport = NS(request=AsyncMock(side_effect=AssertionError('Generic transport must not receive Project Goal input')))
        self.sessions.registry.register(CodexAgentRuntimeAdapter(self.transport))
        provider = AgentProviderService(AgentProviderStore(self.f.sqlite))
        adapter = self.sessions.registry.get('openai', 'codex')
        provider.upsert(AgentProviderUpsert(id='openai', display_name='Isolated fixture native provider',
            declared_capabilities=adapter.capabilities, granted_capabilities=adapter.capabilities, health='healthy'), actor=self.actor)
        self.discovery = ProcedureDiscoveryService(SkillService(self.registry), self.f.resources,
            execution_capabilities=ProcedureExecutionCapabilityService(self.f.workers, self.sessions.registry, provider))
        self.admission = install_native_procedure_admission(self.execution, self.sessions, self.discovery,
            self.f.workers, ThreadHistoryRepository(self.f.sqlite), self.actor, self.f.execution_profiles)
        self.control = AutonomyControl(mode='active', exclusive_goal_scope=AutonomyExclusiveGoalScope(
            goal_id=self.goal.id, project_id='data', root_work_item_refs=('data!26',), reason='Actual exact existing-MR scope'))
        self.goal_continuation = GoalContinuationService(self.goal_bindings, self.goals, self.sessions,
            owner_id='project-goal-controller', autonomy=NS(store=NS(load=lambda: NS(control=self.control))))
        self.usage = AgentRuntimeUsageStore(self.f.sqlite)
        self.project_goal = ProjectGoalContinuation(continuation=self.goal_continuation, turns=self.turns,
            identity=self.f.identity, profiles=self.profiles, execution_profiles=self.f.execution_profiles,
            workers=self.f.workers, bootstrap_bindings=self.bindings, usage=self.usage)
        self.goal_continuation.worker_continuation = self.project_goal
        # Exercise the exact production composition without importing the app
        # (which owns live service startup). Both real admission owners remain.
        names = {'_authorize_current_native_input', '_prepare_current_native_input_guard'}
        source = Path(__file__).resolve().parents[1] / 'codex_web' / 'application.py'
        nodes = [node for node in ast.parse(source.read_text()).body if isinstance(node, ast.FunctionDef) and node.name in names]
        self.assertEqual({node.name for node in nodes}, names)
        self.native_admission_context = {'control_plane_broker_service': self.service,
            'project_goal_continuation': self.project_goal}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), self.native_admission_context)
        self.admission.input_authorizer = self.native_admission_context['_authorize_current_native_input']
        self.execution.native_input_guard_factory = self.native_admission_context['_prepare_current_native_input_guard']
        self.execution.queued_preflight.queued_start_observer = self.project_goal.project_native_start
        self.execution.retained_turn_outcome_recorder = self.project_goal.record_terminal
        self.execution.project_goal_admission = self.project_goal.queued_admission

    async def test_composed_project_goal_uses_assignment_bound_queue_instead_of_generic_session(self):
        result = await self.goal_continuation.dispatch_once(self.goal_binding.id, scope=self.actor.tenant)
        self.transport.request.assert_not_awaited()
        self.assertEqual(result.outcome, 'queued', result.reason)
        queue = self.queue_store.get(self.thread)
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0].project_id, 'data')
        await self.execution.drain_thread_queue(self.thread)
        self.assertEqual(len(self.starts()), 1, self.native_diagnostics())
        current = self.goal_bindings.get(self.goal_binding.id, scope=self.actor.tenant)
        self.assertEqual(current.project_continuation.delivery.phase, 'started')
        self.assertEqual(current.last_turn_id, 'canonical-home-delegated-turn')
        self.assertEqual(current.project_continuation.delivery.work_item_refs, ('data!26',))
        await self.goal_continuation.dispatch_once(current.id, scope=self.actor.tenant)
        self.assertEqual(len(self.starts()), 1)

    async def queued_goal(self):
        result = await self.goal_continuation.dispatch_once(self.goal_binding.id, scope=self.actor.tenant)
        self.assertEqual(result.outcome, 'queued', result.reason)
        return self.goal_bindings.get(self.goal_binding.id, scope=self.actor.tenant)

    async def test_concurrent_dispatch_retains_one_original_without_blocking_winner(self):
        results = await asyncio.gather(*(self.goal_continuation.dispatch_once(self.goal_binding.id,
            scope=self.actor.tenant) for _ in range(2)))
        self.assertEqual(sum(item.outcome == 'queued' for item in results), 1, results)
        current = self.goal_bindings.get(self.goal_binding.id, scope=self.actor.tenant)
        self.assertEqual(current.status, 'active')
        self.assertEqual(current.project_continuation.reserved_calls, 1)
        self.assertEqual(len(self.queue_store.get(self.thread)), 1)
        await self.execution.drain_thread_queue(self.thread)
        self.assertEqual(len(self.starts()), 1, self.native_diagnostics())

    async def test_project_facet_version_and_cross_scope_invariants(self):
        current = await self.queued_goal()
        state = self.goal_bindings.store.load()
        self.assertEqual(state.schema_version, '1.3')
        with self.assertRaises(ContractCompatibilityError):
            ContractSpec('goal-execution-binding-state', '1.2', ('1.0', '1.1', '1.2')).require(state.schema_version)
        raw = current.model_dump(mode='python')
        for change in ({'project_id': 'home'}, {'runtime_id': 'codex-cli'},
                {'agent_session_id': 'another-session'}, {'work_item_refs': ('another!1',)},
                {'home_continuation': HomeGoalProgress()}, {'execution_mode': 'project_delegation'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                GoalExecutionBinding.model_validate({**raw, **change})
        with self.assertRaisesRegex(GoalExecutionBindingConflictError, 'Unresolved Project original'):
            self.goal_bindings.authorize_project_delegation(current.id, actor=self.actor,
                expected_updated_at=current.updated_at, expected_delegation_revision=current.delegation_revision,
                reason='Cannot transfer the unresolved original')

    async def test_composed_project_source_cannot_borrow_home_host_authority(self):
        with self.assertRaises(AuthorizationError):
            self.native_admission_context['_authorize_current_native_input'](
                assignment=self.assignment, actor=self.worker_actor,
                project=self.projects.get('home', self.actor.tenant),
                source='project-goal-continuation:'+self.goal_binding.id)
        self.assertEqual(self.starts(), [])

    def native_diagnostics(self):
        return {'events': self.host.events, 'preflights': [{
            'id': item.id, 'status': item.status, 'last_error': item.last_error,
            'blockers': [blocker.model_dump(mode='json') for blocker in item.blockers],
        } for item in self.execution.queued_preflight.store.for_thread(self.thread)]}

    async def test_generic_session_and_forged_goal_source_still_refuse(self):
        with self.assertRaises(ExecutionPrestartRefusal) as caught:
            await self.sessions.start_turn(self.goal_binding.agent_session_id,
                AgentRuntimeTurnRequest(message='Same ordinary Project objective'), actor=self.actor)
        self.assertEqual(caught.exception.detail['code'], 'procedure_unsupported_enforcement')
        with self.assertRaises(ValueError):
            self.project_goal.prepare_native_input_guard(project=self.data, actor=self.actor,
                execution_id=self.target.execution_id, assignment_id=self.target.id,
                source='project-goal-continuation:'+self.goal_binding.id,
                request_checksum='a'*64, input_execution_id='forged-input')
        self.transport.request.assert_not_awaited()
        self.assertEqual(self.starts(), [])

    async def test_same_valid_project_goal_old_generic_route_still_refuses(self):
        self.goal_continuation.worker_continuation = None
        result = await self.goal_continuation.dispatch_once(self.goal_binding.id, scope=self.actor.tenant)
        self.assertEqual(result.outcome, 'blocked')
        self.assertIn('procedure_unsupported_enforcement', result.reason)
        self.transport.request.assert_not_awaited()
        self.assertEqual(self.starts(), [])

    async def test_canonical_goal_owner_revision_drift_preserves_queued_original(self):
        original = await self.queued_goal()
        self.goals.revise(self.goal.id, GoalUpdate(owner_identity_id=self.worker_actor.identity_id,
            reason='Canonical Goal owner revision changed'), scope=self.actor.tenant, actor_id=self.actor.identity_id)
        await self.execution.drain_thread_queue(self.thread)
        self.assertEqual(self.starts(), [])
        self.assertEqual(self.goal_bindings.get(original.id, scope=self.actor.tenant).last_execution_id, original.last_execution_id)
        self.assertEqual(len(self.queue_store.get(self.thread)), 1)

    async def test_changed_execution_definition_refuses_before_native_send(self):
        await self.queued_goal()
        record = self.registry.get_record(self.target.execution_profile_definition.record_id)
        changed = self.registry.create_draft(DefinitionDraftCreate(definition_id=record.definition_id,
            kind=record.kind, definition_schema_version=record.definition_schema_version,
            scope_type=record.scope_type, scope_id=record.scope_id, payload=record.payload,
            actor=self.actor.identity_id, reason='New exact execution Definition identity'))
        self.registry.publish(changed.record_id, DefinitionPublishRequest(actor=self.actor.identity_id))
        await self.execution.drain_thread_queue(self.thread)
        self.assertEqual(self.starts(), [])

    async def test_cross_project_session_cannot_borrow_goal_authority(self):
        await self.queued_goal()
        session = self.sessions.get(self.goal_binding.agent_session_id, self.actor)
        self.sessions.store.upsert(session.model_copy(update={'project_id': 'home'}))
        await self.execution.drain_thread_queue(self.thread)
        self.assertEqual(self.starts(), [])

    async def test_cross_tenant_session_cannot_borrow_goal_authority(self):
        original = await self.queued_goal()
        session = self.sessions.get(self.goal_binding.agent_session_id, self.actor)
        with self.assertRaises(AgentSessionConflictError):
            self.sessions.store.upsert(session.model_copy(update={'workspace_id': 'foreign-workspace'}))
        self.assertEqual(self.sessions.get(session.id, self.actor), session)
        with self.assertRaises(GoalExecutionBindingNotFoundError):
            await self.goal_continuation.dispatch_once(original.id, scope=TenantScope(
                organization_id=self.actor.organization_id, workspace_id='foreign-workspace'))
        self.assertEqual(self.starts(), [])
        self.assertEqual(len(self.queue_store.get(self.thread)), 1)
        # The rejected foreign operation cannot corrupt or consume the actual
        # owner's retained input; only its legitimate canonical drain starts it.
        await self.execution.drain_thread_queue(self.thread)
        self.assertEqual(len(self.starts()), 1, self.native_diagnostics())

    async def test_project_pause_refuses_existing_queue(self):
        await self.queued_goal()
        self.control = self.control.model_copy(update={'scoped_pauses': (AutonomyScopedPause(
            scope='project', scope_id='data', reason='Actual operator hold', created_by=self.actor.identity_id),)})
        await self.execution.drain_thread_queue(self.thread)
        self.assertEqual(self.starts(), [])
        self.assertEqual(len(self.queue_store.get(self.thread)), 1)
        self.assertEqual(self.goal_bindings.get(self.goal_binding.id, scope=self.actor.tenant).project_continuation.delivery.phase, 'queued')

    async def late_native_refusal(self, pause):
        await self.queued_goal()
        self.hold_writer = True
        drain = asyncio.create_task(self.execution.drain_thread_queue(self.thread))
        entered = asyncio.create_task(self.writes_entered.wait())
        try:
            await asyncio.wait({drain, entered}, return_when=asyncio.FIRST_COMPLETED)
            self.assertTrue(self.writes_entered.is_set(), self.native_diagnostics())
            pause()
            self.native.write_lock.release()
            await drain
            self.assertEqual(self.starts(), [], self.native_diagnostics())
        finally:
            entered.cancel()
            if self.native.write_lock.locked():
                self.native.write_lock.release()
            if not drain.done():
                drain.cancel()
            await asyncio.gather(drain, entered, return_exceptions=True)
            self.hold_writer = False

    async def test_runtime_automation_disable_at_native_writer_blocks_original(self):
        await self.late_native_refusal(lambda: setattr(self.execution, 'automated_admission_allowed', lambda: False))

    async def test_project_pause_at_native_writer_blocks_original(self):
        def pause():
            self.control = self.control.model_copy(update={'scoped_pauses': (AutonomyScopedPause(
                scope='project', scope_id='data', reason='Actual late operator hold', created_by=self.actor.identity_id),)})
        await self.late_native_refusal(pause)

    async def test_changed_profile_refuses_before_native_send(self):
        await self.queued_goal()
        self.profiles.update('quinn', AgentProfileUpdate(name='Revised actual Quinn', reason='Changed published recipient'), actor=self.actor)
        await self.execution.drain_thread_queue(self.thread)
        self.assertEqual(self.starts(), [])

    async def test_changed_worker_fence_refuses_before_native_send(self):
        await self.queued_goal()
        self.f.workers.store.update(lambda state: state.model_copy(update={'assignments': [
            item.model_copy(update={'fence': item.fence+1}) if item.id == self.target.id else item
            for item in state.assignments]}))
        await self.execution.drain_thread_queue(self.thread)
        self.assertEqual(self.starts(), [])

    async def test_queue_response_loss_retains_original_and_never_reserves_replacement(self):
        start = self.turns.start
        async def lost(*args, **kwargs):
            await start(*args, **kwargs)
            raise ConnectionError('Fixture response lost after actual canonical queue write')
        self.turns.start = lost
        result = await self.goal_continuation.dispatch_once(self.goal_binding.id, scope=self.actor.tenant)
        self.assertEqual(result.outcome, 'unknown')
        original = self.goal_bindings.get(self.goal_binding.id, scope=self.actor.tenant)
        self.assertEqual(original.project_continuation.delivery.phase, 'unknown')
        self.turns.start = start
        for _ in range(2):
            await self.goal_continuation.dispatch_once(original.id, scope=self.actor.tenant)
        await self.execution.drain_thread_queue(self.thread)
        retained = self.goal_bindings.get(original.id, scope=self.actor.tenant)
        self.assertEqual(retained.last_execution_id, original.last_execution_id)
        self.assertEqual(retained.project_continuation.reserved_calls, 1)
        self.assertEqual(len(self.queue_store.get(self.thread)), 1)
        self.assertEqual(self.starts(), [])

    async def test_cancel_after_queue_write_preserves_unknown_original(self):
        start = self.turns.start
        async def cancelled(*args, **kwargs):
            await start(*args, **kwargs)
            raise asyncio.CancelledError()
        self.turns.start = cancelled
        with self.assertRaises(asyncio.CancelledError):
            await self.goal_continuation.dispatch_once(self.goal_binding.id, scope=self.actor.tenant)
        current = self.goal_bindings.get(self.goal_binding.id, scope=self.actor.tenant)
        self.assertEqual(current.status, 'unknown')
        self.assertEqual(current.project_continuation.delivery.phase, 'unknown')
        self.assertEqual(len(self.queue_store.get(self.thread)), 1)
        self.assertEqual(self.starts(), [])

    async def preserve_operator_stop_on_response_loss(self, *, cancelled):
        start = self.turns.start
        stopped = []
        async def lost_after_stop(*args, **kwargs):
            await start(*args, **kwargs)
            stopped.append(self.goal_bindings.operator_stop(self.goal_binding.id, scope=self.actor.tenant,
                actor_id=self.actor.identity_id, cancelled=cancelled, reason='Explicit operator action during queue response'))
            raise ConnectionError('Fixture queue response lost after canonical operator stop')
        self.turns.start = lost_after_stop
        result = await self.goal_continuation.dispatch_once(self.goal_binding.id, scope=self.actor.tenant)
        self.assertEqual(result.outcome, 'unknown')
        current = self.goal_bindings.get(self.goal_binding.id, scope=self.actor.tenant)
        self.assertEqual((current.status, current.stop_reason), (stopped[0].status, stopped[0].stop_reason))
        self.assertEqual(current.project_continuation.delivery.phase, 'unknown')
        self.assertEqual(current.project_continuation.reserved_calls, 1)
        await self.execution.drain_thread_queue(self.thread)
        self.assertEqual(self.starts(), [])
        self.assertEqual(len(self.queue_store.get(self.thread)), 1)

    async def test_queue_response_loss_cannot_overwrite_operator_pause(self):
        await self.preserve_operator_stop_on_response_loss(cancelled=False)

    async def test_queue_response_loss_cannot_overwrite_operator_cancel(self):
        await self.preserve_operator_stop_on_response_loss(cancelled=True)

    async def test_reserved_expiry_cannot_overwrite_operator_pause_after_inventory(self):
        entered = asyncio.Event()
        start = self.turns.start
        async def waiting_before_queue(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        self.turns.start = waiting_before_queue
        dispatch = asyncio.create_task(self.goal_continuation.dispatch_once(self.goal_binding.id, scope=self.actor.tenant))
        mutate = self.project_goal.mutate
        stopped = []
        try:
            await entered.wait()
            mutate(self.goal_binding.id, self.actor.tenant, lambda row: row.model_copy(update={
                'lease_expires_at': time.time()-1}), 'Private fixture advances the finite lease past expiry')
            observed = self.goal_bindings.get(self.goal_binding.id, scope=self.actor.tenant)
            self.assertEqual(observed.project_continuation.delivery.phase, 'reserved')
            def pause_before_expiry_commit(*args, **kwargs):
                stopped.append(self.goal_bindings.operator_stop(self.goal_binding.id, scope=self.actor.tenant,
                    actor_id=self.actor.identity_id, cancelled=False, reason='Operator pause wins after inventory'))
                return mutate(*args, **kwargs)
            self.project_goal.mutate = pause_before_expiry_commit
            await self.project_goal.reconcile(observed, scope=self.actor.tenant)
            current = self.goal_bindings.get(observed.id, scope=self.actor.tenant)
            self.assertEqual((current.status, current.stop_reason), (stopped[0].status, stopped[0].stop_reason))
            self.assertEqual(current.project_continuation.delivery.phase, 'reserved')
            self.assertEqual(self.starts(), [])
        finally:
            self.project_goal.mutate = mutate
            dispatch.cancel()
            await asyncio.gather(dispatch, return_exceptions=True)
            self.turns.start = start

    async def test_restart_reclaims_only_proven_queued_original_without_new_input(self):
        original = await self.queued_goal()
        self.project_goal.mutate(original.id, self.actor.tenant,
            lambda row: row.model_copy(update={'lease_expires_at': time.time()-1}), 'Private fixture expires finite owner lease')
        replacement = GoalContinuationService(self.goal_bindings, self.goals, self.sessions,
            owner_id='replacement-project-owner', autonomy=NS(store=NS(load=lambda: NS(control=self.control))))
        worker = ProjectGoalContinuation(continuation=replacement, turns=self.turns, identity=self.f.identity,
            profiles=self.profiles, execution_profiles=self.f.execution_profiles, workers=self.f.workers,
            bootstrap_bindings=self.bindings, usage=self.usage)
        replacement.worker_continuation = worker
        self.native_admission_context['project_goal_continuation'] = worker
        self.execution.queued_preflight.queued_start_observer = worker.project_native_start
        self.execution.project_goal_admission = worker.queued_admission
        recovery = GoalContinuationRecoveryService(self.goal_bindings, replacement, self.sessions)
        results = await recovery.reconcile_retained_worker_scope(scope=self.actor.tenant)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].outcome, 'not_dispatched')
        current = self.goal_bindings.get(original.id, scope=self.actor.tenant)
        self.assertEqual(current.last_execution_id, original.last_execution_id)
        self.assertEqual(current.project_continuation.reserved_calls, 1)
        self.assertEqual(current.project_continuation.delivery.continuation_owner_id, replacement.owner_id)
        await self.execution.drain_thread_queue(self.thread)
        self.assertEqual(len(self.starts()), 1, self.native_diagnostics())

    async def test_owned_terminal_uses_exact_start_and_usage_once(self):
        await self.queued_goal()
        await self.execution.drain_thread_queue(self.thread)
        self.assertEqual(len(self.starts()), 1, self.native_diagnostics())
        active = self.execution._active_turn(self.thread)
        current = self.goal_bindings.get(self.goal_binding.id, scope=self.actor.tenant)
        d = current.project_continuation.delivery
        usage = AgentRuntimeUsage(id='runtime-usage-'+hashlib.sha256(f'{current.agent_session_id}\n{d.native_turn_id}'.encode()).hexdigest()[:32],
            organization_id=self.actor.organization_id, workspace_id=self.actor.workspace_id, project_id='data',
            agent_session_id=current.agent_session_id, assignment_id=self.target.id, execution_id=self.target.execution_id,
            execution_workspace_id=self.target.execution_workspace_id, provider_id='openai', runtime_id='codex', runtime_type='app-server',
            provider_native_session_id=self.thread, provider_native_turn_id=d.native_turn_id,
            completed_at=time.time(), terminal_outcome='succeeded', model_call_count=1,
            input_tokens=40, output_tokens=5, runtime_duration_seconds=1., cost_usd=0.)
        self.usage.upsert(usage)
        await self.project_goal.record_terminal(active, succeeded=True)
        settled = self.goal_bindings.get(current.id, scope=self.actor.tenant)
        self.assertEqual(settled.project_continuation.delivery.phase, 'settled')
        self.assertEqual(settled.project_continuation.model_calls, 1)
        await self.project_goal.record_terminal(active, succeeded=True)
        self.assertEqual(self.goal_bindings.get(current.id, scope=self.actor.tenant), settled)

    async def terminal_order(self, *, event_first, late_usage=False, unknown_metrics=False):
        await self.queued_goal()
        await self.execution.drain_thread_queue(self.thread)
        self.assertEqual(len(self.starts()), 1, self.native_diagnostics())
        active = self.execution._active_turn(self.thread)
        current = self.goal_bindings.get(self.goal_binding.id, scope=self.actor.tenant)
        d = current.project_continuation.delivery
        event = AgentRuntimeEvent(event_type='turn/completed', provider_native_session_id=self.thread,
            provider_native_turn_id=d.native_turn_id)
        events = GoalContinuationEventService(self.goal_bindings, self.sessions, self.goal_continuation)
        usage = AgentRuntimeUsage(id='runtime-usage-'+hashlib.sha256(f'{current.agent_session_id}\n{d.native_turn_id}'.encode()).hexdigest()[:32],
            organization_id=self.actor.organization_id, workspace_id=self.actor.workspace_id, project_id='data',
            agent_session_id=current.agent_session_id, assignment_id=self.target.id, execution_id=self.target.execution_id,
            execution_workspace_id=self.target.execution_workspace_id, provider_id='openai', runtime_id='codex', runtime_type='app-server',
            provider_native_session_id=self.thread, provider_native_turn_id=d.native_turn_id,
            completed_at=time.time(), terminal_outcome='succeeded',
            model_call_count=None if unknown_metrics else 1, input_tokens=None if unknown_metrics else 40,
            output_tokens=None if unknown_metrics else 5, cost_usd=None if unknown_metrics else 0.)
        # Instrument only the subsequent continuation callback; actual reserve,
        # queue, worker transport, START and terminal ownership above are real.
        self.goal_continuation.dispatch_once = AsyncMock(return_value=None)
        if not late_usage:
            self.usage.upsert(usage)
        if event_first:
            await events.handle_event(event, scope=self.actor.tenant)
            self.goal_continuation.dispatch_once.assert_not_awaited()
        await self.project_goal.record_terminal(active, succeeded=True)
        await events.handle_event(event, scope=self.actor.tenant)
        if late_usage:
            pending = self.goal_bindings.get(current.id, scope=self.actor.tenant)
            self.assertEqual(pending.project_continuation.delivery.phase, 'terminal_pending_usage')
            self.goal_continuation.dispatch_once.assert_not_awaited()
            self.usage.upsert(usage)
            recovery = GoalContinuationRecoveryService(self.goal_bindings, self.goal_continuation, self.sessions)
            await recovery.reconcile_retained_worker_scope(scope=self.actor.tenant)
        self.goal_continuation.dispatch_once.assert_awaited_once()
        settled = self.goal_bindings.get(current.id, scope=self.actor.tenant)
        self.assertEqual(settled.project_continuation.delivery.phase, 'settled')
        for _ in range(2):
            await events.handle_event(event, scope=self.actor.tenant)
        self.assertFalse(self.project_goal.settle_usage(current, self.actor.tenant))
        self.goal_continuation.dispatch_once.assert_awaited_once()
        return settled

    async def test_terminal_event_before_owned_callback_never_releases_from_status(self):
        await self.terminal_order(event_first=True)

    async def test_owned_callback_before_terminal_event_settles_once(self):
        await self.terminal_order(event_first=False)

    async def test_late_usage_wake_settles_same_terminal_once(self):
        await self.terminal_order(event_first=True, late_usage=True)


class DisabledGoalBudgetTests(ProjectGoalWorkerAdmissionTests):
    goal_budget = GoalBudget()

    async def test_disabled_ceilings_preserve_unknown_usage_without_holding_terminal(self):
        settled = await self.terminal_order(event_first=True, late_usage=True, unknown_metrics=True)
        self.assertIsNone(settled.project_continuation.model_calls)
        self.assertIsNone(settled.project_continuation.input_tokens)
        self.assertIsNone(settled.project_continuation.output_tokens)
        self.assertIsNone(settled.project_continuation.cost_usd)


class FiniteUnknownBudgetTests(ProjectGoalWorkerAdmissionTests):
    goal_budget = GoalBudget(max_model_calls=2, max_input_tokens=4000, max_output_tokens=1000)

    async def test_finite_ceiling_refuses_next_input_when_observed_metrics_unknown(self):
        dispatch = self.goal_continuation.dispatch_once
        settled = await self.terminal_order(event_first=True, late_usage=True, unknown_metrics=True)
        self.goal_continuation.dispatch_once = dispatch
        result = await dispatch(settled.id, scope=self.actor.tenant)
        self.assertEqual(result.outcome, 'blocked')
        current = self.goal_bindings.get(settled.id, scope=self.actor.tenant)
        self.assertEqual(current.project_continuation.reserved_calls, 1)
        self.assertIsNone(current.project_continuation.model_calls)
        self.assertEqual(len(self.starts()), 1)


def load_tests(loader, standard_tests, pattern):
    # Reuse the canonical worker fixture without rerunning its unrelated tests.
    return unittest.TestSuite(cls(name)
        for cls in (ProjectGoalWorkerAdmissionTests, DisabledGoalBudgetTests, FiniteUnknownBudgetTests)
        for name, method in cls.__dict__.items()
        if name.startswith('test_') and callable(method))
