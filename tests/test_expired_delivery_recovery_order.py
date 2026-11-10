"""Real SLA/archive and pending-recipient owners; no model or provider execution.

Reuse the existing private SQLite routing fixture. Only its external thread
creation/dispatch edges are controlled; this does not qualify live START or QA.
"""
import time
import unittest

from codex_web.models import WorkItemHandoffCreate
from codex_web.owner_routing import OwnerRoutingBlocker, OwnerRoutingError
from codex_web.services.autonomy import AutonomyService
from tests import test_autonomy_service as sla_fixtures
from tests import test_owner_cancelled_new_handoff as recipient_fixtures


class ExpiredDeliveryRecoveryOrderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.recipient = recipient_fixtures.CancelledNewHandoffRecipientTests()
        self.recipient.setUp()
        self.addCleanup(self.recipient.doCleanups)
        self.n, self.f = self.recipient.n, self.recipient.f
        self.repository = self.f.f.env.states
        self.machine = self.f.f.env.machine
        self.ref = self.n.state.ref
        state = self.repository.get(self.ref)
        state.handoff.requested_at = time.time() - 3700
        state.owner_routing_blocker = OwnerRoutingBlocker(
            code='owner_recipient_preparation_changed', configured_owner='orchestrator',
            recorded_at=state.handoff.requested_at + 1)
        self.repository.put(self.ref, state)
        self.original = state.model_copy(deep=True)
        self.assignment_before = self.f.assignments['assignment-platform-thread'].model_dump(mode='json')
        self.sessions_before = self.n.store.load().model_dump(mode='json')
        # Use the actual canonical archive/event owner, rather than _Host's
        # historical simplified archive, which assigns an artificial ACK.
        host = sla_fixtures._Host()
        host._load_work_item_states = self.repository.load
        host._update_work_item_state = self.repository.update
        host._archive_active_handoff = self.machine._archive_active_handoff
        host._work_item_event = self.machine._work_item_event
        host._append_work_item_event = self.machine._append_work_item_event
        self.host = host
        self.sla = AutonomyService(host)

    async def expire(self):
        await self.sla.run_work_item_sla_cycle()
        current = self.repository.get(self.ref)
        self.assertIsNone(current.handoff)
        self.assertEqual((current.current_owner, current.next_owner, current.current_stage),
                         ('carl', 'carl', 'implementation_active'))
        archived = current.handoff_history[-1]
        self.assertEqual(archived.requested_at, self.original.handoff.requested_at)
        self.assertEqual(archived.delivery, self.original.handoff.delivery)
        self.assertIsNone(archived.acknowledged_at)
        self.assertEqual(archived.reason_code, 'handoff_delivery_expired')
        self.assertEqual(current.owner_routing_blocker, self.original.owner_routing_blocker)
        self.n.threads.create.assert_not_awaited()
        self.f.fixture.dispatch.assert_not_awaited()
        self.assertEqual(self.f.assignments['assignment-platform-thread'].model_dump(mode='json'),
                         self.assignment_before)
        return current

    def request_genuine_next_stage(self):
        # A controlled current business decision, not automatic SLA renewal or
        # a fabricated native outcome. The canonical transition creates nonce.
        current = self.machine._structured_handoff(self.ref, WorkItemHandoffCreate(
            from_agent='carl', to_agent='orchestrator', artifact_state='merge_request',
            current_stage='ready_for_validation',
            reason='Current implementation and applicable evidence are ready for coordination of validation.',
            expected_action='Coordinate the current independent validation intake.'))
        self.n.state = current
        self.recipient.nonce = current.handoff.requested_at
        return current

    async def test_expiry_directs_current_business_review_before_nonce_bound_preparation(self):
        current = await self.expire()
        # Reproduces the real impossible prerequisite: pending-only preparation
        # cannot run against the now archived handoff, without any provider I/O.
        with self.assertRaisesRegex(OwnerRoutingError, 'owner_recipient_preparation_changed'):
            await self.n.routing.prepare_compatible_pin(current, self.n.binding,
                'orchestrator', writable_repository_ids=('platform',))
        self.n.threads.create.assert_not_awaited()
        # The behavioral regression is the actual persisted SLA recovery
        # disposition after that refusal, not a separately formatted prompt.
        self.assertIn('request a new normal handoff', current.next_action)
        self.assertIn('required evidence', current.next_action)
        self.assertIn('current custody or authority denials', current.next_action)
        self.assertNotIn('renew only after its exact blocker changes', current.next_action)
        self.assertEqual(self.n.store.load().model_dump(mode='json'), self.sessions_before)

    async def test_genuine_new_handoff_then_dispatch_prepares_only_the_current_nonce(self):
        expired = await self.expire()
        history = expired.handoff_history[-1].model_dump(mode='json')
        current = self.request_genuine_next_stage()
        self.assertGreater(current.handoff.requested_at, self.original.handoff.requested_at)
        self.assertEqual(current.handoff.status, 'pending')
        self.assertIsNone(current.handoff.acknowledged_at)
        self.n.threads.create.assert_not_awaited()
        await self.f.continuity.dispatch_structured_handoff(current, source='work-item-handoff')
        observed = self.repository.get(self.ref)
        self.n.threads.create.assert_awaited_once()
        self.f.fixture.dispatch.assert_awaited_once()
        pin = self.f.fixture.dispatch.call_args.kwargs['owner_routing_pin']
        self.assertEqual(pin.handoff_requested_at, current.handoff.requested_at)
        self.assertEqual(pin.recipient.thread_id, 'fresh-recipient-thread')
        self.assertEqual(observed.handoff_history[-2].model_dump(mode='json'), history)
        self.assertIsNone(observed.handoff.acknowledged_at)
        self.assertEqual(self.f.assignments['assignment-platform-thread'].model_dump(mode='json'),
                         self.assignment_before)
        self.assertEqual(self.n.store.load().model_dump(mode='json'), self.sessions_before)

    async def test_unresolved_original_still_blocks_genuine_new_handoff_dispatch(self):
        await self.expire()
        original = self.f.f.uncertain()
        self.f.f.preflight.put(original)
        before = self.f.f.preflight.get(original.id).model_dump(mode='json')
        current = self.request_genuine_next_stage()
        result = await self.f.continuity.dispatch_structured_handoff(current, source='work-item-handoff')
        self.assertEqual(result['code'], 'owner_routing_native_outcome_unresolved')
        self.n.threads.create.assert_not_awaited()
        self.f.fixture.dispatch.assert_not_awaited()
        self.assertEqual(self.f.f.preflight.get(original.id).model_dump(mode='json'), before)
        self.assertIsNone(self.repository.get(self.ref).handoff.acknowledged_at)

    async def test_concurrent_genuine_handoff_preserves_new_intent(self):
        # The actual handoff owner commits a different genuine intent between
        # the collection observation and expiry's canonical per-record update.
        update = self.repository.update
        renewed = []
        def handoff_before_update(ref, updater):
            current = self.request_genuine_next_stage()
            renewed.append(current.handoff.model_copy(deep=True))
            return update(ref, updater)
        self.host._update_work_item_state = handoff_before_update
        await AutonomyService(self.host).run_work_item_sla_cycle()
        current = self.repository.get(self.ref)
        self.assertEqual(current.handoff, renewed[0])
        self.assertEqual(current.handoff.status, 'pending')
        self.assertIsNone(current.handoff.acknowledged_at)
        self.assertEqual(current.current_stage, 'ready_for_validation')
        self.assertFalse(any(event.event_type == 'handoff_delivery_expired'
                             for event in self.machine.event_store.load()))
        self.n.threads.create.assert_not_awaited()
