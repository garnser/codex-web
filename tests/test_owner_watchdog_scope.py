from __future__ import annotations

import contextvars
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from codex_web.models import WorkItemState
from codex_web.services.autonomy import AutonomyService
from codex_web.autonomy import AutonomyCycleOutcome
from tests import test_autonomy_service as autonomy_fixture
from tests import test_bot_event_dispatch_scope as dispatch_fixture


class OwnerWatchdogScopeTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def states():
        return {state.ref: state for state in (
            WorkItemState(ref='group/saas!1', project_id='project-a',
                resource_ids=['repo-saas'], current_owner='james',
                current_stage='implementation_active', created_at=1, updated_at=2,
                last_meaningful_update_at=2),
            WorkItemState(ref='group/platform!2', project_id='project-a',
                resource_ids=['repo-platform'], current_owner='james',
                current_stage='implementation_active', created_at=1, updated_at=1,
                last_meaningful_update_at=1),
        )}

    async def test_two_repo_owner_delivers_selected_scope_to_actual_dispatch(self):
        runtime = autonomy_fixture.AutonomyOwnerWorkTests._runtime(self.states())
        fixture = dispatch_fixture.BotEventDispatchScopeTests()
        fixture.setUp()
        fixture.execution.thread_is_active.return_value = False
        fixture.queue_policy.depth.return_value = 0
        fixture.execution.start_thread_turn_now = AsyncMock(return_value={'turn': {'id': 'turn'}})
        runtime.binding_for_agent = lambda *_a, **_kw: fixture.binding
        runtime.dispatch_event = fixture.service.dispatch
        await AutonomyService(runtime=runtime).run_owner_work_cycle()
        kwargs = fixture.execution.start_thread_turn_now.await_args.kwargs
        self.assertEqual(kwargs['work_item_ref'], 'group/platform!2')
        self.assertEqual(kwargs['repository_resource_id'], 'repo-platform')
        self.assertEqual(kwargs['writable_repository_resource_ids'], ('repo-platform',))
        self.assertIn('group/platform!2', kwargs['message'])
        fixture.execution.enqueue_turn.assert_not_called()

    async def test_controller_reasoner_retains_selected_scope_and_context(self):
        runtime = autonomy_fixture.AutonomyOwnerWorkTests._runtime(self.states())
        runtime.project_scope = lambda _p: ('tenant', 'workspace')
        context = contextvars.ContextVar('watchdog_scope_test')
        context.set('tenant-context')
        seen = []
        async def dispatch(*args, **kwargs):
            seen.append((context.get(), kwargs))
            return {'ok': True}
        runtime.dispatch_event = dispatch
        async def process(_event, _observation, *, reasoner, **_kwargs):
            await reasoner()
            return SimpleNamespace(outcome=AutonomyCycleOutcome.COMPLETED, id='cycle')
        events = SimpleNamespace(ingest=AsyncMock(return_value=SimpleNamespace(
            inserted=True, event=SimpleNamespace(event_id='event'))))
        await AutonomyService(runtime=runtime, controller=SimpleNamespace(process=process),
            canonical_events=events).run_owner_work_cycle()
        self.assertEqual(seen[0][0], 'tenant-context')
        self.assertEqual(seen[0][1]['work_item_ref'], 'group/platform!2')
        self.assertEqual(seen[0][1]['writable_repository_resource_ids'], ('repo-platform',))
        self.assertTrue(seen[0][1]['require_idle'])
        self.assertEqual(events.ingest.await_args.kwargs['payload']['work_item_ref'], 'group/platform!2')

    async def test_execution_writable_scope_takes_precedence_over_resource_fallback(self):
        states = self.states()
        selected = states['group/platform!2']
        selected.execution.writable_repository_resource_ids = ('repo-authoritative',)
        runtime = autonomy_fixture.AutonomyOwnerWorkTests._runtime(states)
        await AutonomyService(runtime=runtime).run_owner_work_cycle()
        kwargs = runtime.dispatch_event.await_args.kwargs
        self.assertEqual(kwargs['repository_resource_id'], 'repo-authoritative')
        self.assertEqual(kwargs['writable_repository_resource_ids'], ('repo-authoritative',))
        self.assertEqual(selected.resource_ids, ['repo-platform'])

    async def test_legacy_no_selected_work_keeps_three_argument_callback(self):
        runtime = autonomy_fixture.AutonomyOwnerWorkTests._runtime({})
        runtime.gitlab_group_issues.return_value = [{
            'references': {'full': 'group/new#3'}, 'state': 'opened'}]
        calls = []
        async def old_dispatch(binding, text, source):
            calls.append((binding, text, source))
            return {'ok': True}
        runtime.dispatch_event = old_dispatch
        await AutonomyService(runtime=runtime).run_owner_work_cycle()
        self.assertEqual(len(calls), 1)
        self.assertIn('group/new#3', calls[0][1])

    async def test_fifo_race_keeps_existing_queue_and_does_not_start_or_enqueue(self):
        runtime = autonomy_fixture.AutonomyOwnerWorkTests._runtime(self.states())
        fixture = dispatch_fixture.BotEventDispatchScopeTests()
        fixture.setUp()
        fixture.execution.thread_is_active.return_value = False
        fixture.execution.start_thread_turn_now = AsyncMock()
        runtime.binding_for_agent = lambda *_a, **_kw: fixture.binding
        runtime.dispatch_event = fixture.service.dispatch
        events = []
        runtime.append_bot_event = events.append
        await AutonomyService(runtime=runtime).run_owner_work_cycle()
        fixture.execution.start_thread_turn_now.assert_not_awaited()
        fixture.execution.enqueue_turn.assert_not_called()
        self.assertEqual(events[-1]['result']['skipped'], 'already_queued')

    async def test_scope_preflight_error_surfaces_without_wrong_lane_or_fallback(self):
        runtime = autonomy_fixture.AutonomyOwnerWorkTests._runtime(self.states())
        fixture = dispatch_fixture.BotEventDispatchScopeTests()
        fixture.setUp()
        fixture.execution.thread_is_active.return_value = False
        fixture.queue_policy.depth.return_value = 0
        error = RuntimeError('repository_target_unauthorized')
        fixture.execution.start_thread_turn_now = AsyncMock(side_effect=error)
        fixture.service.resume = SimpleNamespace(is_timeout_error=lambda _e: False,
            is_stale_thread_error=lambda _e: False)
        runtime.binding_for_agent = lambda *_a, **_kw: fixture.binding
        runtime.dispatch_event = fixture.service.dispatch
        with self.assertRaisesRegex(RuntimeError, 'repository_target_unauthorized'):
            await AutonomyService(runtime=runtime).run_owner_work_cycle()
        fixture.execution.start_thread_turn_now.assert_awaited_once()
        self.assertEqual(fixture.execution.start_thread_turn_now.await_args.kwargs[
            'writable_repository_resource_ids'], ('repo-platform',))
        fixture.execution.enqueue_turn.assert_not_called()
        runtime.gitlab_group_issues.assert_not_awaited()

    async def test_scratch_profile_keeps_task_context_without_repository_grant(self):
        runtime = autonomy_fixture.AutonomyOwnerWorkTests._runtime(self.states())
        fixture = dispatch_fixture.BotEventDispatchScopeTests()
        fixture.setUp()
        fixture.execution.thread_is_active.return_value = False
        fixture.queue_policy.depth.return_value = 0
        fixture.execution.start_thread_turn_now = AsyncMock(return_value={'turn': {'id': 'turn'}})
        fixture.service.projects.get.return_value.organization_id = 'tenant'
        fixture.service.projects.get.return_value.workspace_id = 'workspace'
        fixture.service.execution_profiles = SimpleNamespace(resolve=lambda *_a, **_kw: (
            SimpleNamespace(repository_access='none', id='scratch'), None))
        runtime.binding_for_agent = lambda *_a, **_kw: fixture.binding
        runtime.dispatch_event = fixture.service.dispatch
        await AutonomyService(runtime=runtime).run_owner_work_cycle()
        kwargs = fixture.execution.start_thread_turn_now.await_args.kwargs
        self.assertEqual(kwargs['work_item_ref'], 'group/platform!2')
        self.assertIsNone(kwargs['repository_resource_id'])
        self.assertEqual(kwargs['writable_repository_resource_ids'], ())
        self.assertEqual(kwargs['execution_profile_id'], 'scratch')

    async def test_multiple_historical_resources_never_become_writable_scope(self):
        states = self.states()
        selected = states['group/platform!2']
        selected.resource_ids = ['repo-platform', 'historical-infra']
        runtime = autonomy_fixture.AutonomyOwnerWorkTests._runtime(states)
        await AutonomyService(runtime=runtime).run_owner_work_cycle()
        kwargs = runtime.dispatch_event.await_args.kwargs
        self.assertEqual(kwargs['work_item_ref'], selected.ref)
        self.assertIsNone(kwargs['repository_resource_id'])
        self.assertEqual(kwargs['writable_repository_resource_ids'], ())
        self.assertEqual(selected.execution.writable_repository_resource_ids, ())
