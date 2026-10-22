"""Owner steering keeps its source CAS while admitting compatible current custody."""
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

from fastapi import HTTPException

from codex_web.execution_workers import AssignmentStatus
from tests import test_owner_routing_recipients as recipients


class OwnerSteerRecipientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = recipients.CompatibleRecipientTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.continuity = self.fixture.continuity
        self.state = self.fixture.state
        self.state.handoff = None
        self.state.current_owner = 'orchestrator'
        self.state.next_owner = 'orchestrator'
        self.state.current_stage = 'validation_running'
        self.fixture.f.env.states.put(self.state.ref, self.state)
        assignment = self.fixture.assignments['assignment-platform-thread']
        assignment.repository_scope = assignment.repository_scope.model_copy(
            update={'read_only_repository_ids': ('core',)})
        self.continuity.record_watchdog_dispatch = Mock()

    async def steer(self, *, thread='existing-thread', source='assignment-control-plane'):
        return await self.continuity.steer_actionable_owner(self.state,
            expected_owner='orchestrator', expected_thread_id=thread,
            idempotency_key='current-owner-steer', actor='carl', source=source)

    async def test_original_source_cas_selects_full_scope_recipient_without_replacement(self):
        result = await self.steer()
        self.assertTrue(result['dispatched'])
        self.assertEqual(result['thread_id'], 'platform-thread')
        args, kwargs = self.fixture.fixture.dispatch.call_args
        self.assertIs(args[0], self.fixture.f.binding)
        pin = kwargs['owner_routing_pin']
        self.assertEqual(pin.original_thread_id, 'existing-thread')
        self.assertEqual(pin.recipient.thread_id, 'platform-thread')
        scope = self.fixture.service.repository_scope_for_pin(pin, args[0])
        self.assertEqual(scope.read_only_repository_ids, ('core',))
        self.assertTrue(kwargs['require_idle'])
        self.continuity.replace_nonperforming_thread.assert_not_called()
        self.continuity.record_watchdog_dispatch.assert_called_once()
        self.assertEqual(self.fixture.f.binding.model_dump(mode='json'), self.fixture.binding_before)
        self.assertEqual(self.fixture.assignments['assignment-existing-thread'].model_dump(mode='json'),
            self.fixture.source_before)

    async def test_selected_thread_cannot_replace_original_source_cas(self):
        with self.assertRaises(HTTPException) as caught:
            await self.steer(thread='platform-thread')
        self.assertEqual(caught.exception.detail['code'], 'work_item_owner_thread_changed')
        self.fixture.fixture.dispatch.assert_not_awaited()

    async def test_busy_selected_recipient_is_preserved_even_for_explicit_retry(self):
        self.continuity.thread_is_active = lambda thread: thread == 'platform-thread'
        for source in ('assignment-control-plane', 'operator-retry'):
            result = await self.steer(source=source)
            self.assertEqual(result['status'], 'already_active')
            self.assertEqual(result['thread_id'], 'platform-thread')
        self.fixture.fixture.dispatch.assert_not_awaited()
        self.continuity.replace_nonperforming_thread.assert_not_called()
        self.continuity.record_watchdog_dispatch.assert_not_called()

    async def test_source_activity_does_not_block_idle_selected_recipient(self):
        self.continuity.thread_is_active = lambda thread: thread == 'existing-thread'
        result = await self.steer()
        self.assertTrue(result['dispatched'])

    async def test_cancelled_recipient_refuses_without_replacement_or_dispatch(self):
        self.fixture.assignments['assignment-platform-thread'].status = AssignmentStatus.CANCELLED
        result = await self.steer()
        self.assertEqual(result['code'], 'owner_recipient_compatible_lane_unavailable')
        self.fixture.fixture.dispatch.assert_not_awaited()
        self.continuity.replace_nonperforming_thread.assert_not_called()

    async def test_queued_selected_input_compares_readonly_scope(self):
        self.continuity.thread_queue_depth = lambda thread: int(thread == 'platform-thread')
        item = dict(work_item_ref=self.state.ref, project_id=self.state.project_id,
            message=self.continuity.dispatch_text(self.state), repository_resource_id='platform',
            writable_repository_resource_ids=['platform'], read_only_repository_resource_ids=['core'])
        self.continuity.thread_queue = lambda thread: [SimpleNamespace(**item)]
        result = await self.steer()
        self.assertEqual(result['status'], 'already_queued')
        item['read_only_repository_resource_ids'] = []
        result = await self.steer()
        self.assertEqual(result['status'], 'owner_queue_context_mismatch')
        self.fixture.fixture.dispatch.assert_not_awaited()
        self.continuity.record_watchdog_dispatch.assert_not_called()

    async def test_changed_fence_during_lookup_refuses_before_dispatch(self):
        original = self.fixture.service.compatible_pin
        def changed(*args, **kwargs):
            pin = original(*args, **kwargs)
            self.fixture.assignments['assignment-platform-thread'].fence += 1
            return pin
        with patch.object(self.fixture.service, 'compatible_pin', side_effect=changed):
            result = await self.steer()
        self.assertFalse(result['dispatched'])
        self.fixture.fixture.dispatch.assert_not_awaited()
        self.continuity.replace_nonperforming_thread.assert_not_called()

    async def test_unresolved_original_outcome_is_retained_without_dispatch(self):
        original = self.fixture.f.uncertain()
        self.fixture.f.preflight.put(original)
        before = original.model_dump(mode='json')
        result = await self.steer()
        self.assertEqual(result['code'], 'owner_routing_native_outcome_unresolved')
        self.assertEqual(self.fixture.f.preflight.get(original.id).model_dump(mode='json'), before)
        self.fixture.fixture.dispatch.assert_not_awaited()
        self.continuity.replace_nonperforming_thread.assert_not_called()

    async def test_projection_checks_recipient_queue_but_exposes_original_source_cas(self):
        self.continuity.thread_queue_depth = lambda thread: int(thread == 'existing-thread')
        self.continuity.thread_queue = lambda thread: [SimpleNamespace(message='unrelated')]
        projection = self.continuity.owner_dispatch_projection(self.state)
        self.assertTrue(projection['eligible'])
        self.assertEqual(projection['expected_thread_id'], 'existing-thread')
        self.continuity.thread_queue_depth = lambda thread: int(thread == 'platform-thread')
        projection = self.continuity.owner_dispatch_projection(self.state)
        self.assertFalse(projection['eligible'])
        self.assertEqual(projection['blocked_reason'], 'owner_queue_context_mismatch')

    async def test_owner_change_while_lookup_yields_refuses_dispatch(self):
        original = self.fixture.service.compatible_pin
        def changed(*args, **kwargs):
            pin = original(*args, **kwargs)
            latest = self.state.model_copy(deep=True)
            latest.current_owner = 'carl'
            latest.next_owner = 'carl'
            self.fixture.f.env.states.put(latest.ref, latest)
            return pin
        with patch.object(self.fixture.service, 'compatible_pin', side_effect=changed):
            result = await self.steer()
        self.assertFalse(result['dispatched'])
        self.assertIn(result['status'], ('stale_state', 'owner_routing_blocked'))
        self.fixture.fixture.dispatch.assert_not_awaited()

    async def test_duplicate_selected_dispatch_keeps_cooldown_without_replacement(self):
        self.continuity.watchdog_dispatch_allowed = lambda key: False
        result = await self.steer()
        self.assertEqual(result['status'], 'duplicate')
        self.assertIn(':platform-thread:', result['dispatch_key'])
        self.continuity.record_watchdog_dispatch.assert_not_called()
        self.fixture.fixture.dispatch.assert_not_awaited()
        self.continuity.replace_nonperforming_thread.assert_not_called()

    async def test_unavailable_selected_queue_context_refuses_without_guessing(self):
        self.continuity.thread_queue_depth = lambda thread: int(thread == 'platform-thread')
        self.continuity.thread_queue = lambda thread: None
        result = await self.steer()
        self.assertEqual(result['status'], 'owner_queue_context_unavailable')
        self.fixture.fixture.dispatch.assert_not_awaited()
        self.continuity.record_watchdog_dispatch.assert_not_called()

    async def test_legacy_no_pin_projection_keeps_original_cas(self):
        with patch.object(self.fixture.service, 'compatible_pin', return_value=None):
            projection = self.continuity.owner_dispatch_projection(self.state)
        self.assertTrue(projection['eligible'])
        self.assertEqual(projection['expected_thread_id'], 'existing-thread')


class OperatorProjectionThreadTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_operator_detail_runs_off_event_loop(self):
        import threading
        from starlette.requests import Request
        from codex_web.api.work_items import build_work_items_router
        loop_thread = threading.get_ident()
        observed = []
        state = SimpleNamespace(organization_id='org', workspace_id='ws', project_id='project')
        service = SimpleNamespace(state_machine=SimpleNamespace(_work_item_state=lambda ref: state),
            work_items=SimpleNamespace())
        def detail(ref):
            observed.append(threading.get_ident())
            return {'item': {'ref': ref}}
        with patch('codex_web.api.work_items.WorkItemOperatorService',
                return_value=SimpleNamespace(detail=detail)):
            router = build_work_items_router(service)
        endpoint = next(route.endpoint for route in router.routes
            if route.path == '/api/work-items/{ref:path}/operator')
        request = Request({'type': 'http', 'query_string': b'',
            'state': {'tenant_scope': SimpleNamespace(organization_id='org', workspace_id='ws')}})
        self.assertEqual(await endpoint('item', request), {'item': {'ref': 'item'}})
        self.assertEqual(len(observed), 1)
        self.assertNotEqual(observed[0], loop_thread)
