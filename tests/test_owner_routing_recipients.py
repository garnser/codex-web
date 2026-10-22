"""Cross-repository handoffs use existing canonical custody, never binding retargets."""
import time
import asyncio
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from codex_web.execution_workers import ExecutionAssignment, AssignmentLease, AssignmentStatus
from codex_web.models import WorkItemHandoff, WorkItemHandoffDelivery, WorkItemAckCreate
from codex_web.owner_routing import OwnerRoutingError, OwnerRoutingPin
from codex_web.resources import RepositoryExecutionScope
from tests import test_owner_routing_dispatch as fixtures


class CompatibleRecipientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = fixtures.OwnerRoutingContinuityTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture.fixture
        self.f, self.service, self.continuity = f, f.service, self.fixture.continuity
        self.state = f.state.model_copy(deep=True)
        self.state.current_owner = 'carl'
        self.state.next_owner = 'orchestrator'
        self.state.resource_ids = ['platform']
        self.state.handoff = WorkItemHandoff(from_agent='carl', to_agent='orchestrator', requested_at=1,
            status='pending', delivery=WorkItemHandoffDelivery())
        f.env.states.put(self.state.ref, self.state)
        self.continuity.binding_for_agent = Mock(return_value=f.binding)
        self.continuity.replace_nonperforming_thread = AsyncMock(side_effect=AssertionError('must preserve source'))
        self.assignments = {}
        self.bootstraps = {}
        for name, scope in [('existing-thread', 'core'), ('platform-thread', 'platform')]:
            assignment = ExecutionAssignment.model_construct(id='assignment-'+name,
                organization_id='org-a', workspace_id='workspace-a', project_id='project-a',
                created_by=f.actor.identity_id, execution_id='execution-'+name,
                execution_workspace_id='workspace-'+name, status=AssignmentStatus.RUNNING, fence=1,
                assigned_worker_id='worker', deadline_at=time.time()+1000,
                lease=AssignmentLease(worker_id='worker', fence=1, lease_token='not-a-real-secret-token',
                    acquired_at=time.time(), expires_at=time.time()+1000),
                agent_profile=f.assignment.agent_profile, execution_profile_id='repository-write',
                sandbox=f.binding.sandbox, approval_policy=f.binding.approval_policy,
                resource_ids=(scope,), repository_scope=RepositoryExecutionScope(
                    organization_id='org-a', workspace_id='workspace-a', project_id='project-a',
                    writable_repository_ids=(scope,), source='explicit'))
            self.assignments[assignment.id] = assignment
            self.bootstraps[name] = SimpleNamespace(id='bootstrap-'+name, thread_id=name,
                assignment_id=assignment.id, execution_id=assignment.execution_id,
                execution_workspace_id=assignment.execution_workspace_id, created_by=f.actor.identity_id,
                organization_id='org-a', workspace_id='workspace-a')
        self.service.bootstraps = SimpleNamespace(
            get_by_thread=lambda thread, actor: self.bootstraps[thread],
            list=lambda actor: tuple(self.bootstraps.values()))
        self.service.assignments = self.assignments.get
        self.worker = SimpleNamespace(lifecycle='active', revoked_at=None,
            organization_id='org-a', workspace_id='workspace-a')
        self.service.workers = lambda worker: self.worker
        self.source_before = self.assignments['assignment-existing-thread'].model_dump(mode='json')
        self.binding_before = f.binding.model_dump(mode='json')

    def select(self):
        return self.service.compatible_pin(self.state, self.f.binding, 'orchestrator',
            writable_repository_ids=('platform',))

    def validate(self, pin, *, attempt_id=None):
        self.service.validate_native(pin, thread_id='platform-thread',
            project=self.f.env.env.projects.get('project-a', scope=self.f.env.env.scope),
            writable_repository_resource_ids=('platform',),
            read_only_repository_resource_ids=self.assignments['assignment-platform-thread'].repository_scope.read_only_repository_ids,
            work_item_ref=self.state.ref, handoff_requested_at=1, queued_admission_attempt_id=attempt_id,
            agent_profile_id=self.f.profile.profile_id, agent_profile_revision=self.f.profile.revision,
            agent_profile_actor_id=self.f.actor.identity_id)

    async def test_real_dispatch_selects_unbound_compatible_lane_preserving_core_and_reply_binding(self):
        await self.continuity.dispatch_structured_handoff(self.state, source='work-item-handoff')
        self.fixture.dispatch.assert_awaited_once()
        args, kwargs = self.fixture.dispatch.call_args
        pin = kwargs['owner_routing_pin']
        self.assertEqual(pin.recipient.thread_id, 'platform-thread')
        self.assertEqual(pin.original_thread_id, 'existing-thread')
        self.assertEqual(args[0], self.f.binding)
        self.continuity.replace_nonperforming_thread.assert_not_called()
        self.assertEqual(self.assignments['assignment-existing-thread'].model_dump(mode='json'), self.source_before)
        self.assertEqual(self.f.binding.model_dump(mode='json'), self.binding_before)
        self.validate(pin)
        self.assertEqual(self.service.context_for_pin(pin, self.f.binding), (self.f.profile, self.f.actor))

    async def test_canonical_continuity_uses_compatible_readonly_recipient_without_replacement(self):
        assignment = self.assignments['assignment-platform-thread']
        assignment.repository_scope = assignment.repository_scope.model_copy(
            update={'read_only_repository_ids': ('core',)})
        self.assertIsNone(self.continuity._alias_intent(self.state, 'orchestrator'))
        before = assignment.model_dump(mode='json')
        with patch.object(self.continuity.policy, 'handoff_continuity_delay', return_value=0):
            await self.continuity.run_handoff_continuity_check(self.state.ref,
                expected_recipient='orchestrator', expected_requested_at=1, source='handoff-continuity')
        self.fixture.dispatch.assert_awaited_once()
        pin = self.fixture.dispatch.call_args.kwargs['owner_routing_pin']
        self.assertEqual(pin.recipient.thread_id, 'platform-thread')
        self.assertEqual(pin.handoff_requested_at, 1)
        self.assertEqual(self.service.repository_scope_for_pin(pin, self.f.binding).read_only_repository_ids, ('core',))
        self.continuity.replace_nonperforming_thread.assert_not_called()
        self.assertEqual(assignment.model_dump(mode='json'), before)
        self.assertEqual(self.f.binding.model_dump(mode='json'), self.binding_before)
        self.assertEqual(self.assignments['assignment-existing-thread'].model_dump(mode='json'), self.source_before)
        self.validate(pin)

    async def test_canonical_continuity_refuses_unavailable_compatible_recipient_without_replacement(self):
        self.assignments['assignment-platform-thread'].status = AssignmentStatus.CANCELLED
        with patch.object(self.continuity.policy, 'handoff_continuity_delay', return_value=0):
            await self.continuity.run_handoff_continuity_check(self.state.ref,
                expected_recipient='orchestrator', expected_requested_at=1, source='handoff-continuity')
        self.fixture.dispatch.assert_not_awaited()
        self.continuity.replace_nonperforming_thread.assert_not_called()
        state = self.f.env.states.get(self.state.ref)
        self.assertEqual(state.owner_routing_blocker.code, 'owner_recipient_compatible_lane_unavailable')
        self.assertEqual(state.handoff.requested_at, 1)
        self.assertEqual(state.handoff.delivery.status, 'awaiting_dispatch')
        self.assertIsNone(state.handoff.acknowledged_at)

    async def test_data_write_core_readonly_pin_survives_real_rpc_capture_without_grant_or_ack(self):
        assignment = self.assignments['assignment-platform-thread']
        assignment.repository_scope = assignment.repository_scope.model_copy(
            update={'read_only_repository_ids': ('core',)})
        before = assignment.model_dump(mode='json')
        await self.test_real_dispatch_selects_unbound_compatible_lane_preserving_core_and_reply_binding()
        pin = self.select()
        self.assertEqual(self.service.repository_scope_for_pin(pin, self.f.binding).read_only_repository_ids, ('core',))
        await self.test_retained_native_capture_observer_projects_selected_lane_then_allows_ack()
        self.assertEqual(assignment.model_dump(mode='json'), before)
        self.assertEqual(self.f.env.states.get(self.state.ref).handoff.delivery.read_only_repository_resource_ids, ('core',))

    async def test_same_source_readonly_custody_requires_typed_witness_not_legacy_shortcut(self):
        source = self.assignments['assignment-existing-thread']
        source.repository_scope = source.repository_scope.model_copy(update={
            'writable_repository_ids': ('platform',), 'read_only_repository_ids': ('core',)})
        self.bootstraps.pop('platform-thread')
        pin = self.select()
        self.assertEqual(pin.recipient.thread_id, 'existing-thread')
        self.assertEqual(self.service.repository_scope_for_pin(pin, self.f.binding), source.repository_scope)

    async def test_readonly_changes_and_sender_scope_union_refuse_before_native(self):
        assignment = self.assignments['assignment-platform-thread']
        assignment.repository_scope = assignment.repository_scope.model_copy(update={'read_only_repository_ids': ('core',)})
        pin = self.select()
        common = dict(thread_id='platform-thread',
            project=self.f.env.env.projects.get('project-a', scope=self.f.env.env.scope),
            work_item_ref=self.state.ref, handoff_requested_at=1,
            agent_profile_id=self.f.profile.profile_id, agent_profile_revision=self.f.profile.revision,
            agent_profile_actor_id=self.f.actor.identity_id, writable_repository_resource_ids=('platform',))
        for supplied in ((), ('core', 'foreign'), ('foreign',)):
            with self.subTest(supplied=supplied), self.assertRaisesRegex(OwnerRoutingError, 'request_scope_changed'):
                self.service.validate_native(pin, **common, read_only_repository_resource_ids=supplied)
        assignment.repository_scope = assignment.repository_scope.model_copy(update={'read_only_repository_ids': ('foreign',)})
        with self.assertRaisesRegex(OwnerRoutingError, 'custody_changed'):
            self.service.repository_scope_for_pin(pin, self.f.binding)
        with self.assertRaisesRegex(OwnerRoutingError, 'custody_changed'):
            self.validate(pin)

    async def test_recipient_lookup_does_not_block_native_heartbeat_loop(self):
        entered, release = threading.Event(), threading.Event()
        original = self.service.compatible_pin
        def slow(*args, **kwargs):
            entered.set()
            release.wait(3)
            return original(*args, **kwargs)
        self.service.compatible_pin = slow
        task = asyncio.create_task(self.continuity.dispatch_structured_handoff(self.state, source='work-item-handoff'))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            # This callback must run before the blocked repository lookup returns.
            tick = asyncio.Event()
            asyncio.get_running_loop().call_soon(tick.set)
            await asyncio.wait_for(tick.wait(), 1)
            self.assertFalse(task.done())
            self.fixture.dispatch.assert_not_called()
        finally:
            release.set()
            await task

    async def test_delivery_projection_requires_matching_selected_scope_and_preserves_provenance(self):
        pin = self.select()
        self.assertFalse(self.continuity.record_handoff_delivery(self.state.ref, expected_requested_at=1,
            recipient='orchestrator', organization_id='org-a', workspace_id='workspace-a', project_id='project-a',
            thread_id='wrong-thread', turn_id='turn', execution_id='execution-platform-thread', started_at=2,
            execution_profile_id='repository-write', repository_resource_id='platform',
            writable_repository_resource_ids=('platform',), owner_routing_pin=pin))
        self.assertTrue(self.continuity.record_handoff_delivery(self.state.ref, expected_requested_at=1,
            recipient='orchestrator', organization_id='org-a', workspace_id='workspace-a', project_id='project-a',
            thread_id='platform-thread', turn_id='turn', execution_id='execution-platform-thread', started_at=2,
            execution_profile_id='repository-write', repository_resource_id='platform',
            writable_repository_resource_ids=('platform',), owner_routing_pin=pin))
        result = self.f.env.machine._structured_ack(self.state.ref,
            WorkItemAckCreate(actor='orchestrator', expected_handoff_requested_at=1))
        self.assertEqual(result.current_owner, 'orchestrator')
        self.assertEqual(result.handoff.delivery.thread_id, 'platform-thread')
        self.assertEqual(self.f.binding.model_dump(mode='json'), self.binding_before)

    async def test_retained_native_capture_observer_projects_selected_lane_then_allows_ack(self):
        from codex_web.models import QueuedTurn
        from codex_web.services.execution_preflight import ExecutionPreflightService
        from codex_web.services.queued_native_start import QueuedNativeStartCapture
        pin = self.select()
        original = QueuedTurn(id='recipient-original', execution_id='requested-recipient-original',
            thread_id='platform-thread', project_id='project-a', message='Review selected artifact',
            work_item_ref=self.state.ref, handoff_requested_at=1, created_at=1, owner_routing_pin=pin,
            agent_profile_id=self.f.profile.profile_id, agent_profile_revision=self.f.profile.revision,
            agent_profile_actor_id=self.f.actor.identity_id, execution_profile_id='repository-write',
            repository_resource_id='platform', writable_repository_resource_ids=('platform',),
            read_only_repository_resource_ids=self.assignments['assignment-platform-thread'].repository_scope.read_only_repository_ids)
        preflight = ExecutionPreflightService(self.f.preflight, clock=lambda: 3)
        preflight.queued_start_observer = self.service.project_native_start
        self.service.delivery_recorder = self.continuity.record_handoff_delivery
        attempt = preflight.retain_queued(original, organization_id='org-a', workspace_id='workspace-a')
        _, claim = preflight.claim_queued_admission(attempt.id)
        preflight.begin_queued_start(attempt.id, claim_id=claim)
        assignment = self.assignments[pin.recipient.assignment_id]
        capture = QueuedNativeStartCapture(preflight, attempt.id, claim, original.execution_id,
            original.thread_id, self.f.actor.identity_id,
            admission_validator=lambda: self.validate(pin, attempt_id=attempt.id), require_send_guard=True).bind_assignment(assignment,
                worker_id='worker', fence=1, validator=lambda: assignment)
        correlation = capture.prepare(generation='test-generation', rpc_request_id='test-rpc',
            native_session_id='test-native-session')
        capture.validate_before_send(correlation)
        self.assertFalse(self.service.project_native_start(self.f.preflight.get(attempt.id)))
        self.assertEqual(self.f.env.states.get(self.state.ref).handoff.delivery.status, 'awaiting_dispatch')
        # Exact trusted RPC-reader fixture, not an operator-provided delivery assertion.
        self.assertTrue(capture.observe(correlation, generation='test-generation', message={
            'id': 'test-rpc', 'result': {'thread': {'id': 'test-native-session'},
                'turn': {'id': 'test-native-turn', 'threadId': 'test-native-session', 'status': 'inProgress'}}}))
        delivered = self.f.env.states.get(self.state.ref)
        self.assertEqual(delivered.handoff.delivery.thread_id, 'platform-thread')
        self.assertEqual(delivered.handoff.delivery.execution_id, 'execution-platform-thread')
        self.assertEqual(delivered.handoff.delivery.turn_id, 'test-native-turn')
        self.assertIsNone(delivered.handoff.acknowledged_at)
        saved = self.f.preflight.get(attempt.id)
        for changes in ({'thread_id': 'core-thread'}, {'execution_id': 'foreign-execution'}):
            corrupted = saved.model_copy(update={'queued_start_receipt':
                saved.queued_start_receipt.model_copy(update=changes)})
            with self.assertRaisesRegex(OwnerRoutingError, 'native_fact_mismatch'):
                self.service.project_native_start(corrupted)
        result = self.f.env.machine._structured_ack(self.state.ref,
            WorkItemAckCreate(actor='orchestrator', expected_handoff_requested_at=1))
        self.assertEqual(result.current_owner, 'orchestrator')
        self.assertEqual(self.f.binding.model_dump(mode='json'), self.binding_before)

    async def test_missing_or_ambiguous_current_executor_is_bounded_and_never_replaces_core(self):
        assignment = self.assignments['assignment-platform-thread']
        assignment.lease = assignment.lease.model_copy(update={'expires_at': 0})
        result = await self.continuity.dispatch_structured_handoff(self.state, source='work-item-handoff')
        self.assertEqual(result['code'], 'owner_recipient_compatible_lane_unavailable')
        self.fixture.dispatch.assert_not_called()
        self.continuity.replace_nonperforming_thread.assert_not_called()
        assignment.lease = assignment.lease.model_copy(update={'expires_at': time.time()+1000})
        self.bootstraps['another-thread'] = SimpleNamespace(**{**vars(self.bootstraps['platform-thread']),
            'id': 'another-bootstrap', 'thread_id': 'another-thread'})
        with self.assertRaisesRegex(OwnerRoutingError, 'ambiguous'):
            self.select()

    async def test_fresh_custody_worker_scope_and_actor_changes_refuse_before_native_input(self):
        pin = self.select()
        assignment = self.assignments['assignment-platform-thread']
        for field, value in [('fence', 2), ('status', 'cancelled'), ('created_by', 'another-actor'),
                             ('project_id', 'foreign-project'), ('execution_workspace_id', 'other-workspace')]:
            with self.subTest(field=field):
                old = getattr(assignment, field)
                setattr(assignment, field, value)
                with self.assertRaises(OwnerRoutingError):
                    self.validate(pin)
                setattr(assignment, field, old)
        self.worker.lifecycle = 'quarantined'
        with self.assertRaisesRegex(OwnerRoutingError, 'worker_unavailable'):
            self.validate(pin)
        self.worker.lifecycle = 'active'
        assignment.repository_scope = assignment.repository_scope.model_copy(update={'writable_repository_ids': ('core',)})
        with self.assertRaisesRegex(OwnerRoutingError, 'custody_changed'):
            self.validate(pin)

    async def test_new_owner_claim_and_profile_revocation_refuse_before_input(self):
        from codex_web.agent_profiles import AgentProfileLifecycleChange, AgentProfileLifecycle
        pin = self.select()
        changed = self.state.model_copy(deep=True)
        changed.implementation_owner = 'another-owner'
        self.f.env.states.put(changed.ref, changed)
        with self.assertRaisesRegex(OwnerRoutingError, 'work_item_changed'):
            self.validate(pin)
        self.f.env.states.put(self.state.ref, self.state)
        self.f.profiles.lifecycle(self.f.profile.profile_id, AgentProfileLifecycle.DISABLED,
            AgentProfileLifecycleChange(reason='Fixture revocation'), actor=self.f.actor)
        with self.assertRaises(OwnerRoutingError):
            self.validate(pin)

    async def test_source_and_target_admission_policy_must_match_without_mutating_either_lane(self):
        pin = self.select()
        target = self.assignments['assignment-platform-thread']
        target.sandbox = 'read-only' if self.f.binding.sandbox != 'read-only' else 'workspace-write'
        with self.assertRaisesRegex(OwnerRoutingError, 'policy_incompatible'):
            self.validate(pin)
        with self.assertRaisesRegex(OwnerRoutingError, 'compatible_lane_unavailable'):
            self.select()
        self.assertEqual(self.assignments['assignment-existing-thread'].model_dump(mode='json'), self.source_before)
        self.assertEqual(self.f.binding.model_dump(mode='json'), self.binding_before)

    async def test_current_work_item_repository_change_or_source_master_refuses_before_input(self):
        pin = self.select()
        changed = self.state.model_copy(deep=True)
        changed.resource_ids = ['core']
        self.f.env.states.put(changed.ref, changed)
        with self.assertRaisesRegex(OwnerRoutingError, 'work_item_scope_changed'):
            self.validate(pin)
        self.f.env.states.put(self.state.ref, self.state)
        self.f.binding.is_master = True
        with self.assertRaisesRegex(OwnerRoutingError, 'binding_changed'):
            self.validate(pin)
        with self.assertRaisesRegex(OwnerRoutingError, 'binding_changed'):
            self.select()

    async def test_renewal_timestamps_and_append_only_procedure_discovery_are_not_custody(self):
        pin = self.select()
        assignment = self.assignments['assignment-platform-thread']
        assignment.lease = assignment.lease.model_copy(update={'expires_at': time.time()+2000, 'renewed_at': time.time()})
        assignment.updated_at = time.time()
        assignment.procedure_discoveries = ('ignored-append-only-evidence',)
        self.validate(pin)

    async def test_scratch_coordination_definition_keeps_normal_no_repository_routing(self):
        self.service.execution_profiles = SimpleNamespace(resolve=Mock(return_value=(
            SimpleNamespace(repository_access='none'), None)))
        self.assertIsNone(self.select())
        self.service.execution_profiles.resolve.assert_called_once()

    async def test_unknown_original_and_profile_disable_block_new_recipient_without_transfer(self):
        pin = self.select()
        unknown = self.f.uncertain()
        self.f.preflight.put(unknown)
        with self.assertRaisesRegex(OwnerRoutingError, 'native_outcome_unresolved'):
            self.select()
        with self.assertRaisesRegex(OwnerRoutingError, 'native_outcome_unresolved'):
            self.validate(pin)
        self.assertEqual(self.f.preflight.get(unknown.id), unknown)

    async def test_retained_legacy_pin_serializes_exact_old_shape(self):
        old = self.f.pin()
        raw = old.model_dump(mode='json')
        self.assertNotIn('recipient', raw)
        reopened = OwnerRoutingPin.model_validate(raw)
        self.assertEqual(reopened.model_dump(mode='json'), raw)
        self.assertEqual(reopened.model_dump_json(), old.model_dump_json())
