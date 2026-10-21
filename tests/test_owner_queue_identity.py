from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from codex_web.models import QueuedTurn, WorkItemState
from codex_web.services.turn_queue_policy import TurnQueuePolicy
from tests import test_background_task_coalescing as continuity_fixture
from tests import test_bot_event_dispatch_scope as dispatch_fixture
from tests import test_owner_routing_dispatch as routing_fixture
from codex_web.owner_routing import OwnerRoutingPin, OwnerRoutingRecipient


class OwnerQueueIdentityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.state = WorkItemState(
            ref='veridataops/data-packs!52', project_id='project-a',
            current_owner='dana', current_stage='implementation_active',
            resource_ids=['repo-data'], next_action='Consume Core544 wheel79e225',
            created_at=1, updated_at=2, last_meaningful_update_at=2,
        )
        self.dispatches = []
        self.service = continuity_fixture.ContinuityCoalescingTests()._service(
            {self.state.ref: self.state}, dispatches=self.dispatches,
        )
        self.service.dispatch_text = lambda state: f'{state.ref}: {state.next_action}'
        self.items = [self.queued()]
        self.service.thread_queue = lambda _: self.items
        self.service.thread_queue_depth = lambda _: len(self.items)

    def queued(self, **changes):
        values = dict(
            id='retained-input', thread_id='thread-project-a-dana',
            project_id='project-a', work_item_ref=self.state.ref,
            message=self.service.dispatch_text(self.state),
            repository_resource_id='repo-data',
            writable_repository_resource_ids=('repo-data',),
            source='work-item-owner-progress', created_at=1,
        )
        values.update(changes)
        return QueuedTurn(**values)

    async def steer(self):
        return await self.service.steer_actionable_owner(
            self.state, expected_owner='dana',
            expected_thread_id='thread-project-a-dana',
            idempotency_key='current-data-result', actor='orchestrator-worker',
            source='assignment-control-plane',
        )

    async def test_actual_owner_steer_does_not_equate_retained_core_input_with_data_result(self):
        self.items[:] = [self.queued(
            work_item_ref='veridataops/core#25', message='Old failed Core5c result',
            repository_resource_id='repo-core',
            writable_repository_resource_ids=('repo-core',),
            handoff_requested_at=10, attempts=3,
        )]
        original = self.items[0].model_dump_json()
        result = await self.steer()
        self.assertEqual(result['status'], 'owner_queue_context_mismatch')
        self.assertFalse(result['dispatched'])
        self.assertEqual(self.dispatches, [])
        self.assertEqual(self.items[0].model_dump_json(), original)
        projection = self.service.owner_dispatch_projection(self.state)
        self.assertFalse(projection['eligible'])
        self.assertEqual(projection['queue_status'], 'owner_queue_context_mismatch')
        self.assertEqual(projection['blocked_reason'], 'owner_queue_context_mismatch')
        self.assertIsNone(projection['expected_thread_id'])
        self.assertIn('original outcomes', projection['next_action'])

    async def test_exact_duplicate_is_still_queued_without_claiming_delivery(self):
        original = self.items[0].model_dump_json()
        result = await self.steer()
        self.assertEqual(result['status'], 'already_queued')
        self.assertFalse(result['dispatched'])
        self.assertEqual(self.items[0].model_dump_json(), original)
        self.assertIsNone(self.state.handoff)
        self.assertEqual(self.dispatches, [])

        projection = self.service.owner_dispatch_projection(self.state)
        self.assertEqual(projection['queue_status'], 'already_queued')
        self.assertTrue(projection['eligible'])

    async def test_same_work_item_stale_result_or_scope_is_not_a_duplicate(self):
        cases = (
            {'message': 'Consume old Core5c wheel2160'},
            {'project_id': 'other-project'},
            {'repository_resource_id': 'repo-core'},
            {'writable_repository_resource_ids': ('repo-core',)},
            {'read_only_repository_resource_ids': ('repo-core',)},
            {'handoff_requested_at': 42},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                self.items[:] = [self.queued(**changes)]
                original = self.items[0].model_dump_json()
                result = await self.steer()
                self.assertEqual(result['status'], 'owner_queue_context_mismatch')
                self.assertFalse(result['dispatched'])
                self.assertEqual(self.items[0].model_dump_json(), original)
        self.assertEqual(self.dispatches, [])

    async def test_unreadable_legacy_queue_is_an_explicit_blocker(self):
        self.service.thread_queue = None
        result = await self.steer()
        self.assertEqual(result['status'], 'owner_queue_context_unavailable')
        self.assertFalse(result['dispatched'])
        self.assertEqual(self.dispatches, [])

    async def test_queue_drain_race_does_not_infer_a_duplicate_or_start_work(self):
        self.service.thread_queue_depth = lambda _: 1
        self.service.thread_queue = lambda _: []
        result = await self.steer()
        self.assertEqual(result['status'], 'owner_queue_context_changed')
        self.assertEqual(self.dispatches, [])

    async def test_real_bot_dispatch_race_preserves_unrelated_or_matching_queue(self):
        fixture = dispatch_fixture.BotEventDispatchScopeTests()
        fixture.setUp()
        fixture.execution.thread_is_active.return_value = False
        fixture.execution.start_thread_turn_now = AsyncMock()
        fixture.queue_policy.depth.return_value = 1
        text = 'NEW Core544 wheel79e225 result'
        for matching in (False, True):
            with self.subTest(matching=matching):
                item = self.queued(
                    thread_id=fixture.binding.thread_id,
                    project_id=fixture.binding.project_id,
                    message=text if matching else 'OLD Core5c wheel2160 result',
                    handoff_requested_at=77,
                )
                original = item.model_dump_json()
                fixture.queue_policy.queue = lambda _: [item]
                result = await fixture.service.dispatch(
                    fixture.binding, text, 'owner-work-watchdog',
                    work_item_ref=self.state.ref, repository_resource_id='repo-data',
                    writable_repository_resource_ids=('repo-data',),
                    handoff_requested_at=77, require_idle=True,
                )
                self.assertEqual(result['skipped'], 'already_queued' if matching else 'owner_queue_context_mismatch')
                self.assertFalse(result['queued'])
                self.assertEqual(item.model_dump_json(), original)
        fixture.execution.start_thread_turn_now.assert_not_awaited()
        fixture.execution.enqueue_turn.assert_not_called()

    async def test_validated_pinned_recipient_queue_race_never_enqueues_unrelated_work(self):
        routing = routing_fixture.OwnerRoutingDispatchTests()
        routing.setUp()
        self.addCleanup(routing.doCleanups)
        base = routing.pin()
        recipient = OwnerRoutingRecipient(thread_id='actual-data-recipient',
            bootstrap_id='current-bootstrap', assignment_id='current-assignment',
            execution_id='current-execution', worker_id='current-worker', fence=1,
            authority_checksum='a' * 64, work_item_owner_checksum='b' * 64)
        pin = OwnerRoutingPin.model_validate({**base.model_dump(),
            'schema_version': '1.1', 'recipient': recipient.model_dump()})
        fixture = dispatch_fixture.BotEventDispatchScopeTests()
        fixture.setUp()
        fixture.execution.thread_is_active.return_value = False
        fixture.execution.start_thread_turn_now = AsyncMock()
        fixture.queue_policy.depth.return_value = 0
        text = 'Current artifact544/wheel79e225'
        queued = self.queued(thread_id=recipient.thread_id,
            project_id=fixture.binding.project_id, work_item_ref=pin.work_item_ref,
            message='Old unrelated Core5c result')
        original = queued.model_dump_json()

        def context_after_racing_enqueue(_pin, _binding):
            # This happens after the continuity precheck and while the real
            # dispatch awaits its validated immutable recipient/profile.
            fixture.queue_policy.depth.return_value = 1
            fixture.queue_policy.queue = Mock(return_value=[queued])
            return (routing.profile, routing.actor)

        fixture.service.owner_routing_context = context_after_racing_enqueue
        fixture.service.owner_routing_repository_scope = Mock(return_value=SimpleNamespace(
            writable_repository_ids=('repo-data',), read_only_repository_ids=()))
        result = await fixture.service.dispatch(fixture.binding, text, 'owner-work-watchdog',
            owner_routing_pin=pin, work_item_ref=pin.work_item_ref,
            repository_resource_id='repo-data', writable_repository_resource_ids=('repo-data',),
            require_idle=True)
        self.assertEqual(result['skipped'], 'owner_queue_context_mismatch')
        self.assertEqual(result['threadId'], recipient.thread_id)
        fixture.queue_policy.queue.assert_called_once_with(recipient.thread_id)
        fixture.execution.enqueue_turn.assert_not_called()
        fixture.execution.start_thread_turn_now.assert_not_awaited()
        fixture.execution.schedule_queue_drain.assert_not_called()
        self.assertEqual(queued.model_dump_json(), original)

    def test_handoff_identity_and_unknown_entries_are_preserved(self):
        expected = dict(
            work_item_ref=self.state.ref, project_id='project-a',
            message=self.service.dispatch_text(self.state),
            repository_resource_id='repo-data',
            writable_repository_resource_ids=('repo-data',), handoff_requested_at=100,
        )
        item = self.queued(handoff_requested_at=99, attempts=3)
        original = item.model_dump_json()
        self.assertEqual(TurnQueuePolicy.work_item_queue_outcome([item], **expected), 'owner_queue_context_mismatch')
        self.assertEqual(TurnQueuePolicy.work_item_queue_outcome([object()], **expected), 'owner_queue_context_mismatch')
        expected['handoff_requested_at'] = 99
        self.assertEqual(TurnQueuePolicy.work_item_queue_outcome([item], **expected), 'already_queued')
        self.assertEqual(item.model_dump_json(), original)
