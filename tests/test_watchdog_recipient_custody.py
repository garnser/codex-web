"""Watchdog admission retains canonical recipient custody and original outcomes."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from codex_web.services.autonomy import AutonomyService
from tests import test_owner_routing_recipients as fixtures


class WatchdogRecipientCustodyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = fixtures.CompatibleRecipientTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        assignment = f.assignments['assignment-platform-thread']
        assignment.repository_scope = assignment.repository_scope.model_copy(
            update={'read_only_repository_ids': ('core',)})
        self.dispatch = f.fixture.dispatch
        self.runtime = SimpleNamespace(owner_routing=f.service, owner_lane_recovery=None,
            dispatch_event=self.dispatch, thread_is_active=lambda thread: False,
            thread_queue_depth=lambda thread: 0, record_watchdog_dispatch=Mock(),
            replace_nonperforming_thread=AsyncMock())
        self.autonomy = AutonomyService(runtime=self.runtime)

    async def test_pending_handoff_watchdog_pins_the_compatible_readonly_recipient(self):
        f = self.fixture
        result = await self.autonomy._bounded_reasoning_dispatch(f.f.binding,
            'Current owning result', 'work-item-sla', cycle_key='current-handoff',
            payload={}, work_item=f.state, recovery_reason='work-item-handoff')
        self.assertTrue(result['ok'])
        pin = self.dispatch.call_args.kwargs['owner_routing_pin']
        self.assertEqual(pin.recipient.thread_id, 'platform-thread')
        self.assertEqual(pin.handoff_requested_at, 1)
        self.assertEqual(f.service.repository_scope_for_pin(pin, f.f.binding).read_only_repository_ids, ('core',))
        self.assertEqual(self.dispatch.call_args.args[0], f.f.binding)
        self.assertEqual(f.f.binding.model_dump(mode='json'), f.binding_before)
        f.validate(pin)
        self.runtime.record_watchdog_dispatch.assert_called_once_with('current-handoff')
        self.runtime.replace_nonperforming_thread.assert_not_awaited()

    async def test_cancelled_recipient_is_refused_without_native_or_recovery(self):
        f = self.fixture
        f.assignments['assignment-platform-thread'].status = 'cancelled'
        result = await self.autonomy._bounded_reasoning_dispatch(f.f.binding,
            'Current owning result', 'work-item-sla', cycle_key='current-handoff',
            payload={}, work_item=f.state, recovery_reason='work-item-handoff')
        self.assertEqual(result['code'], 'owner_recipient_compatible_lane_unavailable')
        self.assertFalse(result['ok'])
        self.dispatch.assert_not_awaited()
        self.runtime.record_watchdog_dispatch.assert_not_called()
        self.runtime.replace_nonperforming_thread.assert_not_awaited()

    async def test_selected_busy_lane_is_not_replaced_or_dispatched(self):
        f = self.fixture
        self.runtime.thread_is_active = lambda thread: thread == 'platform-thread'
        result = await self.autonomy._bounded_reasoning_dispatch(f.f.binding,
            'Current owning result', 'work-item-sla', cycle_key='current-handoff',
            payload={}, work_item=f.state, recovery_reason='work-item-handoff')
        self.assertEqual(result['reason'], 'owner_lane_busy')
        self.assertEqual(result['threadId'], 'platform-thread')
        self.dispatch.assert_not_awaited()
        self.runtime.record_watchdog_dispatch.assert_not_called()
        self.runtime.replace_nonperforming_thread.assert_not_awaited()

    async def test_retained_uncertain_original_blocks_new_watchdog_dispatch(self):
        f = self.fixture
        original = f.f.uncertain()
        f.f.preflight.put(original)
        result = await self.autonomy._bounded_reasoning_dispatch(f.f.binding,
            'Current owning result', 'work-item-sla', cycle_key='current-handoff',
            payload={}, work_item=f.state, recovery_reason='work-item-handoff')
        self.assertEqual(result['code'], 'owner_routing_native_outcome_unresolved')
        self.assertEqual(f.f.preflight.get(original.id), original)
        self.dispatch.assert_not_awaited()
        self.runtime.record_watchdog_dispatch.assert_not_called()
        self.runtime.replace_nonperforming_thread.assert_not_awaited()

    async def test_runtime_without_owner_routing_retains_legacy_dispatch(self):
        f = self.fixture
        self.runtime.owner_routing = None
        result = await self.autonomy._bounded_reasoning_dispatch(f.f.binding,
            'Current owning result', 'work-item-sla', cycle_key='current-handoff',
            payload={}, work_item=f.state)
        self.assertTrue(result['ok'])
        self.assertNotIn('owner_routing_pin', self.dispatch.call_args.kwargs)

    async def test_ambiguous_recipients_are_refused_without_dispatch(self):
        f = self.fixture
        original = f.assignments['assignment-platform-thread']
        duplicate = original.model_copy(update={'id': 'assignment-second',
            'execution_id': 'execution-second', 'execution_workspace_id': 'workspace-second'})
        f.assignments[duplicate.id] = duplicate
        f.bootstraps['second-thread'] = SimpleNamespace(id='bootstrap-second', thread_id='second-thread',
            assignment_id=duplicate.id, execution_id=duplicate.execution_id,
            execution_workspace_id=duplicate.execution_workspace_id, created_by=f.f.actor.identity_id,
            organization_id='org-a', workspace_id='workspace-a')
        result = await self.autonomy._bounded_reasoning_dispatch(f.f.binding,
            'Current owning result', 'work-item-sla', cycle_key='current-handoff',
            payload={}, work_item=f.state, recovery_reason='work-item-handoff')
        self.assertEqual(result['code'], 'owner_recipient_ambiguous')
        self.dispatch.assert_not_awaited()
        self.runtime.record_watchdog_dispatch.assert_not_called()
        self.runtime.replace_nonperforming_thread.assert_not_awaited()

    async def test_current_owner_watchdog_also_retains_compatible_recipient_scope(self):
        f = self.fixture
        f.state.handoff = None
        f.state.current_owner = 'orchestrator'
        f.f.env.states.put(f.state.ref, f.state)
        result = await self.autonomy._bounded_reasoning_dispatch(f.f.binding,
            'Current owning work', 'owner-work-watchdog', cycle_key='current-owner',
            payload={}, work_item=f.state)
        self.assertTrue(result['ok'])
        pin = self.dispatch.call_args.kwargs['owner_routing_pin']
        self.assertIsNone(pin.handoff_requested_at)
        self.assertEqual(pin.recipient.thread_id, 'platform-thread')
        self.assertEqual(f.service.repository_scope_for_pin(pin, f.f.binding).read_only_repository_ids, ('core',))
        self.assertEqual(result['threadId'], 'platform-thread')
