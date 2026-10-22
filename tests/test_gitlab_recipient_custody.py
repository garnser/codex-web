"""Terminal provider events retain canonical owner custody and ingress proof."""
from dataclasses import replace
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock

from tests import test_gitlab_pipeline_progress_admission as event_fixtures
from tests import test_owner_routing_recipients as recipient_fixtures
from tests import test_bot_event_dispatch_scope as dispatch_fixtures


class GitLabRecipientCustodyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.events = event_fixtures.PipelineProgressAdmissionTests()
        await self.events.asyncSetUp()
        self.addCleanup(self.events.doCleanups)
        self.recipient = recipient_fixtures.CompatibleRecipientTests()
        self.recipient.setUp()
        self.addCleanup(self.recipient.doCleanups)
        f = self.recipient
        f.state.handoff = None
        f.state.current_owner = 'orchestrator'
        f.state.current_stage = 'validation_running'
        f.f.env.states.put(f.state.ref, f.state)
        assignment = f.assignments['assignment-platform-thread']
        assignment.repository_scope = assignment.repository_scope.model_copy(
            update={'read_only_repository_ids': ('core',)})
        service = self.events.service
        service.work_items = NS(project_event=Mock(return_value=f.state),
            owner_routing=f.service, coerce_owner=lambda owner: owner,
            project_lookup=lambda _: f.f.env.env.projects.get('project-a', scope=f.f.env.env.scope))
        service.project_settings_for_payload = lambda *_: (
            'project-a', event_fixtures.GitLabProjectRoutingSettings())
        service.event_target_agents = lambda *_: ['orchestrator']
        service.routing_bindings_for_agent = lambda *_: [f.f.binding]
        self.dispatch = AsyncMock(return_value={'ok': True, 'queued': True,
            'threadId': 'platform-thread'})
        service.operations = replace(service.operations, dispatch_scoped_event=self.dispatch)

    async def test_terminal_event_keeps_original_binding_and_compatible_readonly_custody(self):
        await self.events.handle('success')
        f = self.recipient
        self.dispatch.assert_awaited_once()
        args, kwargs = self.dispatch.call_args
        self.assertEqual(args[0], f.f.binding)
        pin = kwargs['owner_routing_pin']
        self.assertEqual(pin.recipient.thread_id, 'platform-thread')
        self.assertIsNone(pin.handoff_requested_at)
        self.assertEqual(f.service.repository_scope_for_pin(pin, f.f.binding).read_only_repository_ids,
            ('core',))
        self.assertEqual(kwargs['writable_repository_resource_ids'], ('platform',))
        self.assertIn('provider_notification_pin', kwargs)
        self.assertEqual(kwargs['provider_notification_pin'].work_item_ref, f.state.ref)
        self.assertEqual(f.f.binding.model_dump(mode='json'), f.binding_before)
        self.assertEqual(f.assignments['assignment-existing-thread'].model_dump(mode='json'),
            f.source_before)

    async def test_pending_event_records_progress_without_selecting_or_starting_recipient(self):
        self.recipient.service.compatible_pin = Mock(side_effect=AssertionError('no reasoning for progress'))
        result = await self.events.handle('running')
        self.assertTrue(result['deterministic'])
        self.dispatch.assert_not_awaited()

    async def test_cancelled_recipient_refuses_and_records_existing_canonical_blocker(self):
        f = self.recipient
        f.assignments['assignment-platform-thread'].status = 'cancelled'
        result = await self.events.handle('failed')
        self.dispatch.assert_not_awaited()
        self.assertFalse(result['targets'][0]['ok'])
        state = f.f.env.states.get(f.state.ref)
        self.assertEqual(state.owner_routing_blocker.code,
            'owner_recipient_compatible_lane_unavailable')
        self.assertEqual(state.current_owner, 'orchestrator')
        self.assertEqual(state.current_stage, 'validation_running')

    async def test_ambiguous_recipient_refuses_without_fallback_to_source(self):
        f = self.recipient
        original = f.assignments['assignment-platform-thread']
        duplicate = original.model_copy(update={'id': 'assignment-second',
            'execution_id': 'execution-second', 'execution_workspace_id': 'workspace-second'})
        f.assignments[duplicate.id] = duplicate
        f.bootstraps['second-thread'] = NS(id='bootstrap-second', thread_id='second-thread',
            assignment_id=duplicate.id, execution_id=duplicate.execution_id,
            execution_workspace_id=duplicate.execution_workspace_id, created_by=f.f.actor.identity_id,
            organization_id='org-a', workspace_id='workspace-a')
        await self.events.handle('success')
        self.dispatch.assert_not_awaited()
        self.assertEqual(f.f.env.states.get(f.state.ref).owner_routing_blocker.code,
            'owner_recipient_ambiguous')

    async def test_custody_change_at_guarded_dispatch_is_not_reselected_or_retargeted(self):
        f = self.recipient
        async def guarded_dispatch(binding, _prompt, _source, **kwargs):
            f.assignments['assignment-platform-thread'].fence += 1
            f.service.context_for_pin(kwargs['owner_routing_pin'], binding)
            self.fail('stale custody must never reach native transport')
        self.dispatch.side_effect = guarded_dispatch
        await self.events.handle('success')
        self.dispatch.assert_awaited_once()
        self.assertEqual(f.f.env.states.get(f.state.ref).owner_routing_blocker.code,
            'owner_recipient_custody_unavailable')

    async def test_duplicate_terminal_event_does_not_readmit_the_recipient(self):
        await self.events.handle('success')
        result = await self.events.service.handle_event(NS(json=AsyncMock(return_value=self.events.payload)))
        self.assertTrue(result['ignored'])
        self.dispatch.assert_awaited_once()

    async def test_retained_unknown_original_refuses_new_terminal_native_input(self):
        f = self.recipient
        original = f.f.uncertain()
        f.f.preflight.put(original)
        await self.events.handle('success')
        self.dispatch.assert_not_awaited()
        self.assertEqual(f.f.preflight.get(original.id), original)
        self.assertEqual(f.f.env.states.get(f.state.ref).owner_routing_blocker.code,
            'owner_routing_native_outcome_unresolved')

    async def test_real_dispatch_retains_both_pins_and_exact_recipient_scope_in_single_queue(self):
        f = self.recipient
        dispatch = dispatch_fixtures.BotEventDispatchScopeTests()
        dispatch.setUp()
        self.addCleanup(dispatch.doCleanups)
        dispatch.service.projects.get.return_value = f.f.env.env.projects.get(
            'project-a', scope=f.f.env.env.scope)
        dispatch.service.owner_routing_context = f.service.context_for_pin
        dispatch.service.owner_routing_repository_scope = f.service.repository_scope_for_pin
        self.events.service.operations = replace(self.events.service.operations,
            dispatch_scoped_event=dispatch.service.dispatch)
        await self.events.handle('success')
        dispatch.execution.enqueue_turn.assert_called_once()
        queued = dispatch.execution.enqueue_turn.call_args.kwargs
        self.assertEqual(queued['thread_id'], 'platform-thread')
        self.assertEqual(queued['writable_repository_resource_ids'], ('platform',))
        self.assertEqual(queued['read_only_repository_resource_ids'], ('core',))
        self.assertEqual(queued['owner_routing_pin'].original_thread_id, 'existing-thread')
        self.assertEqual(queued['provider_notification_pin'].work_item_ref, f.state.ref)
        self.assertIsNone(queued.get('handoff_requested_at'))
        dispatch.execution.schedule_queue_drain.assert_called_once_with('platform-thread')
        dispatch.service.bindings.upsert.assert_not_called()

