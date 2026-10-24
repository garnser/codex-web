"""A new handoff can reserve new context without resuming cancelled custody."""
import unittest
import time
from types import SimpleNamespace

from codex_web.agent_runtime import AgentSessionStatus
from codex_web.execution_workers import AssignmentStatus
from codex_web.execution_subjects import ExecutionSubject
from tests import test_owner_missing_native_predecessor as fixtures


class CancelledNewHandoffRecipientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = fixtures.MissingNativePredecessorTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.n, self.f = self.fixture.n, self.fixture.f
        current = self.f.assignments['assignment-platform-thread']
        bootstrap = self.f.bootstraps['platform-thread']
        self.n.prepare_fixture()
        self.n.template = self.n.template.model_copy(deep=True)
        current.status = AssignmentStatus.CANCELLED
        current.fence = 2
        current.lease = None
        self.f.assignments[current.id] = current
        self.f.bootstraps['platform-thread'] = bootstrap
        self.fixture.current_session.status = AgentSessionStatus.READY
        self.n.store.upsert(self.fixture.current_session)
        self.n.threads.create.side_effect = self.create
        # A genuinely new canonical handoff, not recovery/replay of nonce1.
        self.nonce = time.time()
        self.n.state.handoff.requested_at = self.nonce
        self.f.f.env.states.put(self.n.state.ref, self.n.state)

    async def create(self, **kwargs):
        # Only the external provider boundary is substituted. The existing
        # canonical preparer, replicated fence and every admission guard run.
        kwargs['admission_guard']()
        token = kwargs['bootstrap_token']
        assignment = self.n.template.model_copy(deep=True, update={
            'id': 'fresh-recipient-assignment', 'status': AssignmentStatus.RUNNING,
            'subject': ExecutionSubject(kind='thread_bootstrap', ref='bootstrap-' + token),
            'execution_workspace_id': 'fresh-recipient-workspace',
            'execution_id': 'thread-bootstrap-' + token})
        self.f.assignments[assignment.id] = assignment
        self.n.reserved = assignment
        self.n.bound = SimpleNamespace(id='fresh-recipient-binding',
            bootstrap_id='bootstrap-' + token, thread_id='fresh-recipient-thread',
            assignment_id=assignment.id, execution_id=assignment.execution_id,
            execution_workspace_id=assignment.execution_workspace_id,
            created_by=assignment.created_by, organization_id='org-a', workspace_id='workspace-a')
        self.f.bootstraps['fresh-recipient-thread'] = self.n.bound
        return {'thread': {'id': 'fresh-recipient-thread'}}

    async def test_new_handoff_prepares_fresh_context_without_resuming_cancelled_replacement(self):
        original = self.f.assignments['assignment-platform-thread'].model_dump(mode='json')
        sessions = self.n.store.load().model_dump(mode='json')
        await self.f.continuity.dispatch_structured_handoff(self.n.state, source='work-item-handoff')
        current = self.f.f.env.states.get(self.n.state.ref)
        # Include the actual canonical denial in the failure without private
        # provider prose; this exercises the real continuity owner end to end.
        self.assertEqual(self.n.threads.create.await_count, 1,
            str(current.owner_routing_blocker.code if current.owner_routing_blocker else None))
        self.f.fixture.dispatch.assert_awaited_once()
        pin = self.f.fixture.dispatch.call_args.kwargs['owner_routing_pin']
        self.assertEqual(pin.recipient.thread_id, 'fresh-recipient-thread')
        self.assertEqual(current.handoff.requested_at, self.nonce)
        self.assertIsNone(current.handoff.acknowledged_at)
        self.assertEqual(self.f.assignments['assignment-platform-thread'].model_dump(mode='json'), original)
        self.assertEqual(self.n.store.load().model_dump(mode='json'), sessions)
        self.assertNotIn(self.n.original.id, self.f.assignments)
        self.assertEqual(self.f.f.binding.model_dump(mode='json'), self.f.binding_before)


    def validate(self, pin, attempt_id=None):
        self.n.routing.validate_native(pin, thread_id=pin.recipient.thread_id,
            project=self.f.f.env.env.projects.get('project-a', scope=self.f.f.env.env.scope),
            writable_repository_resource_ids=('platform',), read_only_repository_resource_ids=(),
            work_item_ref=self.n.state.ref, handoff_requested_at=self.nonce,
            queued_admission_attempt_id=attempt_id,
            agent_profile_id=self.f.f.profile.profile_id,
            agent_profile_revision=self.f.f.profile.revision,
            agent_profile_actor_id=self.f.f.actor.identity_id)

    async def prepared(self):
        return await self.n.routing.prepare_compatible_pin(self.n.state, self.n.binding,
            'orchestrator', writable_repository_ids=('platform',))

    async def test_exact_reservation_reused_and_final_native_guard_rechecks_source(self):
        from codex_web.owner_routing import OwnerRoutingError
        pin = await self.prepared()
        self.validate(pin)
        self.assertEqual((await self.prepared()).recipient, pin.recipient)
        self.n.threads.create.assert_awaited_once()
        # Final admission freshly rechecks the cancelled source's own native
        # association as well as the new recipient witness.
        self.fixture.current_session.assignment_id = 'foreign'
        self.n.store.upsert(self.fixture.current_session)
        with self.assertRaises(OwnerRoutingError): self.validate(pin)

    async def test_unknown_or_running_source_never_reserves_fresh_context(self):
        for update in ({'status': AssignmentStatus.RUNNING}, {'assigned_worker_id': None},
                       {'repository_scope': None}, {'created_by': 'foreign'}):
            original = self.f.assignments['assignment-platform-thread']
            self.f.assignments[original.id] = original.model_copy(deep=True, update=update)
            result = await self.f.continuity.dispatch_structured_handoff(self.n.state, source='work-item-handoff')
            self.assertEqual(result['status'], 'owner_routing_blocked')
            self.n.threads.create.assert_not_awaited()
            self.f.assignments[original.id] = original

    async def test_expired_or_acked_or_delivered_handoff_does_not_create(self):
        from codex_web.owner_routing import OwnerRoutingError
        original = self.n.state.model_copy(deep=True)
        for change in ('expired', 'acked', 'delivered'):
            state = original.model_copy(deep=True)
            if change == 'expired': state.handoff.requested_at = 1
            elif change == 'acked': state.handoff.acknowledged_at = time.time()
            else: state.handoff.delivery.status = 'delivered'
            self.f.f.env.states.put(state.ref, state)
            with self.assertRaises(OwnerRoutingError):
                await self.n.routing.prepare_compatible_pin(state, self.n.binding,
                    'orchestrator', writable_repository_ids=('platform',))
            self.n.threads.create.assert_not_awaited()

    async def test_unresolved_original_holds_without_new_provider_attempt(self):
        attempt = self.f.f.uncertain()
        self.f.f.preflight.put(attempt)
        result = await self.f.continuity.dispatch_structured_handoff(self.n.state, source='work-item-handoff')
        self.assertEqual(result['code'], 'owner_routing_native_outcome_unresolved')
        self.n.threads.create.assert_not_awaited()

    async def test_changed_nonce_during_provider_admission_refuses(self):
        from codex_web.owner_routing import OwnerRoutingError
        async def changed(**kwargs):
            state = self.f.f.env.states.get(self.n.state.ref)
            state.handoff.requested_at += 1
            self.f.f.env.states.put(state.ref, state)
            kwargs['admission_guard']()
        self.n.threads.create.side_effect = changed
        with self.assertRaises(OwnerRoutingError): await self.prepared()
        self.assertIsNone(self.n.bound)

    async def test_actual_native_start_own_exclusion_delivery_and_ack(self):
        from codex_web.models import QueuedTurn, WorkItemAckCreate
        from codex_web.services.execution_preflight import ExecutionPreflightService
        from codex_web.services.queued_native_start import QueuedNativeStartCapture
        pin = await self.prepared()
        queued = QueuedTurn(id='fresh-owner-original', execution_id='fresh-owner-original-execution',
            thread_id=pin.recipient.thread_id, project_id='project-a', message='Review current Work',
            work_item_ref=self.n.state.ref, handoff_requested_at=self.nonce,
            created_at=time.time(), owner_routing_pin=pin,
            agent_profile_id=self.f.f.profile.profile_id, agent_profile_revision=self.f.f.profile.revision,
            agent_profile_actor_id=self.f.f.actor.identity_id, execution_profile_id='repository-write',
            repository_resource_id='platform', writable_repository_resource_ids=('platform',),
            read_only_repository_resource_ids=())
        preflight = ExecutionPreflightService(self.f.f.preflight, clock=time.time)
        preflight.queued_start_observer = self.n.routing.project_native_start
        self.n.routing.delivery_recorder = self.f.continuity.record_handoff_delivery
        attempt = preflight.retain_queued(queued, organization_id='org-a', workspace_id='workspace-a')
        _, claim = preflight.claim_queued_admission(attempt.id)
        preflight.begin_queued_start(attempt.id, claim_id=claim)
        assignment = self.f.assignments[pin.recipient.assignment_id]
        capture = QueuedNativeStartCapture(preflight, attempt.id, claim, queued.execution_id,
            queued.thread_id, self.f.f.actor.identity_id,
            admission_validator=lambda: self.validate(pin, attempt.id), require_send_guard=True).bind_assignment(
                assignment, worker_id=assignment.assigned_worker_id, fence=assignment.fence,
                validator=lambda: assignment)
        correlation = capture.prepare(generation='generation', rpc_request_id='rpc', native_session_id='native')
        capture.validate_before_send(correlation)
        self.assertTrue(capture.observe(correlation, generation='generation', message={
            'id': 'rpc', 'result': {'thread': {'id': 'native'},
            'turn': {'id': 'turn', 'threadId': 'native', 'status': 'inProgress'}}}))
        delivered = self.f.f.env.states.get(self.n.state.ref)
        self.assertEqual(delivered.handoff.delivery.thread_id, pin.recipient.thread_id)
        result = self.f.f.env.machine._structured_ack(self.n.state.ref,
            WorkItemAckCreate(actor='orchestrator', expected_handoff_requested_at=self.nonce))
        self.assertEqual(result.handoff.status, 'accepted')

    async def test_existing_sla_cycle_reevaluates_pending_refusal_via_admitted_owner(self):
        from codex_web.services.autonomy import AutonomyService
        from codex_web.services.autonomy_controller import AutonomyController
        from codex_web.services.canonical_events import CanonicalEventBus, CanonicalEventIngestionService
        from codex_web.storage.autonomy import AutonomyStateStore
        from codex_web.storage.canonical_events import CanonicalEventStore
        store = self.f.f.env.env.state
        host = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(owner_routing_service=self.n.routing)),
            _load_work_item_states=lambda: {self.n.state.ref: self.f.f.env.states.get(self.n.state.ref)},
            _binding_for_agent=lambda *a, **k: self.n.binding,
            _dispatch_event_to_binding=self.f.fixture.dispatch,
            _project_scope=lambda p: ('org-a', 'workspace-a'))
        # Real controller + canonical event window; only provider thread create
        # and eventual external input dispatch are substitutes.
        service = AutonomyService(host,
            controller=AutonomyController(AutonomyStateStore(store)),
            canonical_events=CanonicalEventIngestionService(CanonicalEventBus(CanonicalEventStore(store))))
        await service.run_work_item_sla_cycle()
        self.n.threads.create.assert_awaited_once()
        self.f.fixture.dispatch.assert_awaited_once()
        pin = self.f.fixture.dispatch.call_args.kwargs['owner_routing_pin']
        self.validate(pin)
        await service.run_work_item_sla_cycle()
        self.n.threads.create.assert_awaited_once()
        self.f.fixture.dispatch.assert_awaited_once()

    async def test_same_scope_positive_original_also_uses_exact_new_nonce_reservation(self):
        original = self.n.original.model_copy(deep=True)
        original.resource_ids = ('platform',)
        original.repository_scope = self.n.template.repository_scope.model_copy(deep=True)
        self.f.assignments[original.id] = original
        self.n.session.resource_ids = ('platform',)
        self.n.store.upsert(self.n.session)
        pin = await self.prepared()
        self.validate(pin)
        self.n.threads.create.assert_awaited_once()
        self.assertEqual(pin.recipient.thread_id, 'fresh-recipient-thread')

    async def test_unknown_reservation_without_binding_is_held(self):
        self.n.reserved = self.n.template.model_copy(deep=True)
        result = await self.f.continuity.dispatch_structured_handoff(self.n.state, source='work-item-handoff')
        self.assertEqual(result['code'], 'owner_recipient_bootstrap_pending')
        self.n.threads.create.assert_not_awaited()

    async def test_source_lineage_drift_at_final_edge_refuses(self):
        from codex_web.owner_routing import OwnerRoutingError
        pin = await self.prepared()
        self.n.routing.replacement_resolver = lambda *a, **k: 'missing-successor'
        with self.assertRaises(OwnerRoutingError): self.validate(pin)
