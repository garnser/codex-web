"""Real broker/ActionIntent, Goal stores, queue/preflight and admission gates.

Only the final native execution body is a seam; this is not installed native
qualification. The public runtime admission method and its validator are real.
"""
import unittest
from unittest.mock import AsyncMock

from codex_web.agent_runtime import AgentSession
from codex_web.goal_execution_bindings import GoalExecutionBindingCreate
from codex_web.goals import GoalRecord, GoalWorkGraphBinding
from codex_web.services.goal_execution_bindings import GoalExecutionBindingService
from codex_web.services.goals import GoalService
from codex_web.services.execution_preflight import ExecutionPreflightService
from codex_web.storage.goal_execution_bindings import GoalExecutionBindingStore
from codex_web.storage.goals import GoalStore
from codex_web.storage.execution_preflight import ExecutionPreflightStore
from codex_web.storage.turn_queue import TurnQueueRepository
from tests import test_project_coordination as coordination_tests


class ProjectDelegatedDispatchTests(unittest.IsolatedAsyncioTestCase):
    # Reuse composition, without inheriting and duplicating its test methods.
    _request = coordination_tests.ProjectCoordinationBrokerTests._request
    call = coordination_tests.ProjectCoordinationBrokerTests.call
    publish = coordination_tests.ProjectCoordinationBrokerTests.publish
    create = coordination_tests.ProjectCoordinationBrokerTests.create

    async def asyncSetUp(self):
        await coordination_tests.ProjectCoordinationBrokerTests.asyncSetUp(self)
        self.execution = self.turn_service.execution
        self.execution.automated_admission_allowed = lambda: False
        self.execution.project_coordination_admission = self.coordination.validate_queued_delegation
        self.host._project = lambda project_id: self.projects.get(project_id, self.actor.tenant)
        self.execution.actor_resolver = lambda identity, project: self.f.identity.actor_for_identity(
            identity, scope=self.actor.tenant)
        self.execution.queued_preflight = ExecutionPreflightService(ExecutionPreflightStore(self.f.sqlite))
        self.execution._start_thread_turn_now_admitted = AsyncMock(return_value={'turn': {'id': 'admitted-native-seam'}})
        self.goals = GoalService(GoalStore(self.f.sqlite), self.projects, None)
        self.goal = GoalRecord(organization_id=self.actor.organization_id, workspace_id=self.actor.workspace_id,
            title='Explicit own-project delivery', description='Metadata only; prose is never admission authority.',
            owner_identity_id=self.actor.identity_id, status='active',
            work_graph_bindings=(GoalWorkGraphBinding(project_id='home'),),
            created_by=self.actor.identity_id, updated_by=self.actor.identity_id)
        self.goals.store.update(lambda state: state.model_copy(update={'goals': [self.goal]}))
        self.goal_bindings = GoalExecutionBindingService(GoalExecutionBindingStore(self.f.sqlite), self.goals)
        self.coordination.goal_bindings = self.goal_bindings
        origin = self.assignment
        self.bindings.bind(bootstrap_id='bootstrap-governor', thread_id='native-governor',
            execution_id=origin.execution_id, assignment_id=origin.id,
            execution_workspace_id=origin.execution_workspace_id, actor=self.actor)
        session = self.sessions.store.upsert(AgentSession(organization_id=self.actor.organization_id,
            workspace_id=self.actor.workspace_id, project_id='home', provider_id='openai', runtime_id='codex',
            runtime_type='app-server', provider_native_session_id='native-governor', status='ready',
            execution_id=origin.execution_id, assignment_id=origin.id,
            execution_workspace_id=origin.execution_workspace_id))
        self.goal_binding = self.goal_bindings.create(self.goal.id, GoalExecutionBindingCreate(
            project_id='home', provider_id=session.provider_id, runtime_id=session.runtime_id,
            agent_session_id=session.id, thread_id='native-governor', execution_owner_id=self.actor.identity_id,
            reason='Explicit user project delegation'), scope=self.actor.tenant, actor_id=self.actor.identity_id)
        self.goal_binding = self.goal_bindings.authorize_project_delegation(self.goal_binding.id,
            actor=self.actor, expected_updated_at=self.goal_binding.updated_at,
            expected_delegation_revision=self.goal_binding.delegation_revision,
            reason='Explicit own-Project human delegation, no app continuation')

    async def message(self, thread, *, bound=True, key='input'):
        body = {'message': 'Implement the next bounded repository issue', 'idempotency_key': key}
        if bound:
            body['goal_binding_id'] = self.goal_binding.id
        status, _, result = await self.call('POST', f'/threads/{thread}/messages', body)
        self.assertEqual(status, 200, result)
        self.assertEqual(result['intent']['status'], 'succeeded', result)
        return self.host._thread_queue(thread)[-1], result

    async def test_unbound_agent_input_never_acquires_human_pause_bypass(self):
        child, _ = await self.create()
        queued, _ = await self.message(child, bound=False)
        self.assertEqual(queued.source, 'project-coordination')
        self.assertIsNone(queued.project_coordination_dispatch)
        self.assertIsNotNone(self.execution.automation_wait_reason(queued.source))
        self.assertEqual(self.turn_service.queue(child)['automationWaiting'], 1)
        await self.execution.drain_thread_queue(child)
        self.assertEqual(self.host._thread_queue(child), [queued])
        self.execution._start_thread_turn_now_admitted.assert_not_awaited()

    async def test_bound_two_children_admit_under_pause_and_proof_survives_reopen(self):
        children = [await self.create(), await self.create('scanner', 'scanner')]
        for index, (child, _) in enumerate(children):
            queued, receipt = await self.message(child, key=f'input-{index}')
            self.assertFalse(receipt['receipts'][0]['result']['output']['native_start_verified'])
            self.assertEqual(queued.source, 'project-coordination')
            repo = TurnQueueRepository(self.f.sqlite, self.f.sqlite.path.parent/'queues.json')
            repo.put(child, [queued])
            reopened = repo.get(child)[0]
            self.assertEqual(reopened.project_coordination_dispatch, queued.project_coordination_dispatch)
            self.assertIsNone(self.execution._queued_automation_wait_reason(reopened))
            self.assertEqual(self.turn_service.queue(child)['automationWaiting'], 0)
            self.assertEqual(self.turn_service.queue(child)['queued'][0]['projectCoordinationDispatch']['goal_binding_id'], self.goal_binding.id)
            await self.execution.drain_thread_queue(child)
            self.assertEqual(self.host._thread_queue(child), [])
        self.assertEqual(self.execution._start_thread_turn_now_admitted.await_count, 2)
        for call in self.execution._start_thread_turn_now_admitted.await_args_list:
            self.assertEqual(call.kwargs['source'], 'queued:project-coordination')
            self.assertIsNotNone(call.kwargs['operator_admission_validator'])
        self.assertIsNotNone(self.execution.automation_wait_reason('business-scheduler'))
        self.assertIsNotNone(self.execution.automation_wait_reason('goal-continuation'))
        # An unrelated original remains paused, even in the same Project.
        self.execution.enqueue_turn(thread_id='business', project_id='home', message='held original', source='business')
        await self.execution.drain_thread_queue('business')
        self.assertEqual(self.host._thread_queue_depth('business'), 1)

    async def test_goal_pause_revision_and_owner_revocation_preserve_original(self):
        child, _ = await self.create()
        queued, _ = await self.message(child)
        for changes in ({'status': 'paused'}, {'revision': self.goal.revision+1}, {'owner_identity_id': 'other'},
                        {'work_graph_bindings': (GoalWorkGraphBinding(project_id='foreign'),)},
                        {'work_graph_bindings': (GoalWorkGraphBinding(project_id='home', root_work_item_refs=('root',)),)}):
            self.goals.store.update(lambda state: state.model_copy(update={'goals': [self.goal.model_copy(update=changes)]}))
            await self.execution.drain_thread_queue(child)
            self.assertEqual(self.host._thread_queue(child), [queued])
        self.execution._start_thread_turn_now_admitted.assert_not_awaited()

    async def test_binding_stop_foreign_session_and_exclusive_lease_deny(self):
        child, _ = await self.create()
        queued, _ = await self.message(child)
        for changes in ({'delegation_authorization': 'revoked'}, {'project_id': 'foreign'}, {'execution_owner_id': 'other'},
                        {'thread_id': child}, {'agent_session_id': 'missing'},
                        {'lease_owner_id': 'other-continuation', 'lease_expires_at': self.service._clock()+100}):
            self.goal_bindings.store.update(lambda state: state.model_copy(update={'bindings': [self.goal_binding.model_copy(update=changes)]}))
            await self.execution.drain_thread_queue(child)
            self.assertEqual(self.host._thread_queue(child), [queued])
        self.execution._start_thread_turn_now_admitted.assert_not_awaited()

    async def test_origin_loss_and_current_authority_revocation_block_delegation(self):
        child, _ = await self.create()
        queued, _ = await self.message(child)
        original = self.assignment
        for changes in ({'status': 'lost'}, {'fence': original.fence+1}):
            self.f.workers.store.update(lambda state: state.model_copy(update={'assignments': [
                v.model_copy(update=changes) if v.id == original.id else v for v in state.assignments]}))
            await self.execution.drain_thread_queue(child)
            self.assertEqual(self.host._thread_queue(child), [queued])
        self.f.workers.store.update(lambda state: state.model_copy(update={'assignments': [
            original if v.id == original.id else v for v in state.assignments]}))
        record = self.authority.catalog_record(actor=self.actor, project_id='home')
        self.registry.quarantine(record.record_id, actor='fixture', reason='Revoke current Role authority')
        await self.execution.drain_thread_queue(child)
        self.execution._start_thread_turn_now_admitted.assert_not_awaited()

    async def test_mid_admission_revocation_is_a_pause_and_retains_original(self):
        child, _ = await self.create()
        queued, _ = await self.message(child)
        real = self.execution.start_thread_turn_now
        async def revoke_then_enter(*args, **kwargs):
            self.goal_bindings.operator_stop(self.goal_binding.id, scope=self.actor.tenant,
                actor_id=self.actor.identity_id, cancelled=False, reason='Revoke before native gate')
            return await real(*args, **kwargs)
        self.execution.start_thread_turn_now = revoke_then_enter
        await self.execution.drain_thread_queue(child)
        self.execution._start_thread_turn_now_admitted.assert_not_awaited()
        self.assertEqual(self.host._thread_queue(child)[0].id, queued.id)
        self.assertEqual(self.host._thread_queue(child)[0].project_coordination_dispatch,
            queued.project_coordination_dispatch)

    async def test_model_cannot_supply_source_or_server_proof(self):
        child, _ = await self.create()
        for field in ('source', 'project_coordination_dispatch', 'delegation_revision', 'actor', 'operator_admission_validator'):
            status, _, result = await self.call('POST', f'/threads/{child}/messages', {
                'message': 'do not acquire human privileges', 'idempotency_key': field, field: 'web'})
            self.assertEqual(status, 422, result)
        self.assertEqual(self.host._thread_queue(child), [])

    async def test_agent_and_human_wakeups_cannot_coalesce_in_either_order(self):
        self.host._work_item_wakeup_entries = lambda _: [{'ref': 'home/item'}]
        for sources in (('web', 'project-coordination'), ('project-coordination', 'web')):
            self.host.queues.clear()
            for index, source in enumerate(sources):
                self.execution.enqueue_turn(thread_id='wakeups', project_id='home',
                    message=f'immutable-{index}', source=source)
            rows = self.host._thread_queue('wakeups')
            self.assertEqual([v.source for v in rows], list(sources))
            self.assertEqual([v.message for v in rows], ['immutable-0', 'immutable-1'])

    async def test_invalid_binding_is_denied_before_action_or_input(self):
        child, _ = await self.create()
        before = len(self.intents.store.load().intents)
        status, _, result = await self.call('POST', f'/threads/{child}/messages', {
            'message': 'Unbound reference is not authority', 'idempotency_key': 'bad-binding',
            'goal_binding_id': 'goal-binding-not-present'})
        self.assertEqual(status, 403, result)
        self.assertEqual(len(self.intents.store.load().intents), before)
        self.assertEqual(self.host._thread_queue(child), [])

    async def test_changed_queued_scope_and_human_relabel_fail_closed(self):
        child, _ = await self.create()
        original, _ = await self.message(child)
        for changes in ({'source': 'web'}, {'project_id': 'foreign'},
                        {'repository_resource_id': 'resource-foreign'},
                        {'writable_repository_resource_ids': ('resource-foreign',)},
                        {'agent_profile_id': 'scanner'}, {'agent_profile_revision': 999},
                        {'agent_profile_actor_id': 'other'},
                        {'project_coordination_dispatch': original.project_coordination_dispatch.model_copy(
                            update={'fence': original.project_coordination_dispatch.fence+1})}):
            changed = original.model_copy(update=changes)
            self.host._put_thread_queue_record(child, [changed])
            await self.execution.drain_thread_queue(child)
            self.assertEqual(self.host._thread_queue(child), [changed])
        self.execution._start_thread_turn_now_admitted.assert_not_awaited()
