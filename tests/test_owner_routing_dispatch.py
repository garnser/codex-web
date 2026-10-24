from pathlib import Path
from types import SimpleNamespace
import unittest

from codex_web.agent_profiles import AgentProfileCreate, AgentProfileLifecycleChange, AgentProfileLifecycle, AgentProfileUpdate, AgentProfileExecutionBinding
from codex_web.identity import AuthenticationActor, AuthenticationAssurance, MembershipRole, PrincipalKind
from codex_web.models import BotBinding, QueuedTurn, ExecutionPreflightAttempt
from codex_web.owner_routing import OwnerRoutingError
from codex_web.services.agent_profiles import AgentProfileService
from codex_web.services.owner_routing import OwnerRoutingService
from codex_web.storage.agent_profiles import AgentProfileStore
from codex_web.storage.turn_queue import TurnQueueRepository
from codex_web.storage.execution_preflight import ExecutionPreflightStore
from codex_web.services.work_item_continuity import WorkItemContinuityService
from codex_web.services.runtime_policy import RuntimePolicy
from codex_web.models import WorkItemHandoffDelivery, WorkItemAckCreate
from fastapi import HTTPException
from unittest.mock import AsyncMock, Mock, patch
from tests import test_owner_routing_state as state_fixtures


class OwnerRoutingDispatchTests(unittest.TestCase):
    def setUp(self):
        self.env = state_fixtures.OwnerRoutingStateTests()
        self.env.setUp()
        self.addCleanup(self.env.doCleanups)
        self.state = self.env.pending().model_copy(update={'handoff': None,
            'current_owner': 'operator', 'next_owner': 'operator'})
        self.env.states.put(self.state.ref, self.state)
        self.actor = AuthenticationActor(identity_id='fixture-actor', principal_kind=PrincipalKind.HUMAN,
            organization_id='org-a', workspace_id='workspace-a', roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.MFA)
        self.profiles = AgentProfileService(AgentProfileStore(self.env.env.state),
            definitions=self.env.env.registry, role_resolver=lambda role, actor: self.env.env.roles.reference(),
            usage_loader=lambda profile, actor: {'available': True, 'blocking_count': 0})
        self.profile = self.profiles.create(AgentProfileCreate(profile_id='actual-existing-agent',
            name='Existing coordination executor', role_id='orchestrator'), actor=self.actor)
        self.assignment = SimpleNamespace(organization_id='org-a', workspace_id='workspace-a',
            project_id='project-a', created_by=self.actor.identity_id,
            agent_profile=AgentProfileExecutionBinding(profile_id=self.profile.profile_id,
                profile_revision=self.profile.revision, profile_record_id=self.profile.record_id,
                role_id='orchestrator'))
        self.binding = BotBinding(id='existing-binding', provider='slack', external_conversation_id='coordination',
            thread_id='existing-thread', project_id='project-a', route_prefix='Orchestrator', created_at=1, updated_at=1)
        self.bindings = [self.binding]
        self.replacement = None
        self.service = OwnerRoutingService(routing=self.env.machine._owner_routing,
            get_state=self.env.machine._work_item_state, projects=self.env.env.projects,
            load_bindings=lambda: self.bindings, binding_prefix=lambda binding: binding.route_prefix,
            bootstraps=SimpleNamespace(get_by_thread=lambda thread, actor: SimpleNamespace(assignment_id='actual-bootstrap')),
            assignments=lambda assignment: self.assignment, profiles=self.profiles,
            control_actor=lambda: self.actor, actor_resolver=lambda identity, project: self.actor,
            replacement_resolver=lambda thread, **scope: self.replacement)
        self.service.update_state = self.env.states.update
        self.service.append_event = self.env.machine._append_work_item_event
        self.preflight = ExecutionPreflightStore(self.env.env.state)
        self.preflight.prepare_work_item_index()
        self.service.preflight = self.preflight

    def pin(self):
        return self.service.pin(self.state, self.binding, 'operator')

    def validate(self, pin, **overrides):
        return self.service.validate_native(pin, **{**dict(thread_id=self.binding.thread_id,
            project=self.env.env.projects.get('project-a', scope=self.env.env.scope),
            work_item_ref=self.state.ref, handoff_requested_at=None,
            agent_profile_id=self.profile.profile_id, agent_profile_revision=self.profile.revision,
            agent_profile_actor_id=self.actor.identity_id), **overrides})

    def uncertain(self, *, pin=None, item_id='old-original'):
        original = QueuedTurn(id=item_id, thread_id=pin.original_thread_id if pin else 'original-uncertain-thread', project_id='project-a',
            message='Retained original input', work_item_ref=self.state.ref, created_at=1,
            owner_routing_pin=pin)
        return ExecutionPreflightAttempt(id='preflight-queued-'+item_id, correlation_id='fixture-'+item_id,
            execution_id='reserved-queue-'+item_id, organization_id='org-a', workspace_id='workspace-a',
            thread_id=original.thread_id, project_id='project-a', message=original.message,
            queued_turn_snapshot=original.model_dump(mode='json'), status='reconciliation_required',
            created_at=1, updated_at=2)

    def test_actual_bootstrap_profile_is_selected_and_pin_survives_reopen(self):
        pin = self.pin()
        self.assertEqual(self.service.context_for_pin(pin, self.binding), (self.profile, self.actor))
        self.validate(pin)
        root = Path(self.env.env.temp.name)
        queue = TurnQueueRepository(self.env.env.state, legacy_path=root / 'queue.json')
        original = QueuedTurn(id='original', thread_id=self.binding.thread_id, project_id='project-a',
            message='Original scoped recovery', created_at=2, owner_routing_pin=pin,
            agent_profile_id=self.profile.profile_id, agent_profile_revision=self.profile.revision,
            agent_profile_actor_id=self.actor.identity_id)
        queue.save({original.thread_id: [original]})
        reopened = TurnQueueRepository(self.env.env.state, legacy_path=root / 'queue.json')
        self.assertEqual(reopened.load()[original.thread_id], [original])
        self.assertEqual(reopened.load()[original.thread_id][0].owner_routing_pin, pin)

    def test_current_definition_change_blocks_same_target_and_never_redirects_old_request(self):
        pin = self.pin()
        # Even a same-target publication requires a new reviewed intent; exact
        # definition provenance cannot be silently replaced during queue wait.
        self.env.env.publish()
        with self.assertRaisesRegex(OwnerRoutingError, 'definition_changed'):
            self.validate(pin)

    def test_fresh_mapping_cannot_replay_an_older_unknown_original_on_another_thread(self):
        old = self.uncertain()
        self.preflight.put(old)
        self.env.env.publish()
        with self.assertRaisesRegex(OwnerRoutingError, 'native_outcome_unresolved'):
            self.pin()
        self.assertEqual(self.preflight.get(old.id), old)

    def test_native_guard_excludes_only_its_exact_original_reservation(self):
        pin = self.pin()
        own = self.uncertain(pin=pin, item_id='own')
        self.preflight.put(own)
        self.validate(pin, queued_admission_attempt_id=own.id)
        with self.assertRaisesRegex(OwnerRoutingError, 'native_outcome_unresolved'):
            self.validate(pin)
        other = self.uncertain(item_id='other')
        self.preflight.put(other)
        with self.assertRaisesRegex(OwnerRoutingError, 'native_outcome_unresolved'):
            self.validate(pin, queued_admission_attempt_id=own.id)
        with self.assertRaisesRegex(OwnerRoutingError, 'retained_intent_mismatch'):
            self.validate(pin, queued_admission_attempt_id=other.id)

    def test_derived_backfill_finds_old_unpinned_unknown_without_rewriting_original(self):
        old = self.uncertain()
        raw = old.model_dump(mode='json')
        self.env.env.state.record_replace(self.preflight.NAMESPACE, {'a/'+old.id: raw})
        with self.assertRaisesRegex(OwnerRoutingError, 'index_unavailable'):
            self.pin()
        reopened = ExecutionPreflightStore(self.env.env.state)
        reopened.prepare_work_item_index()
        self.service.preflight = reopened
        with self.assertRaisesRegex(OwnerRoutingError, 'native_outcome_unresolved'):
            self.pin()
        self.assertEqual(self.env.env.state.record_get(self.preflight.NAMESPACE, 'a/'+old.id), raw)

    def test_interrupted_index_backfill_resumes_and_truncated_work_item_lookup_refuses(self):
        originals = [self.uncertain(item_id=f'original-{number:04}') for number in range(205)]
        raw = {'a/'+item.id: item.model_dump(mode='json') for item in originals}
        self.env.env.state.record_replace(self.preflight.NAMESPACE, raw)
        apply = self.env.env.state.record_apply
        def interrupted(*args, **kwargs):
            apply(*args, **kwargs)
            raise RuntimeError('simulated process interruption after committed page')
        with patch.object(self.env.env.state, 'record_apply', side_effect=interrupted):
            with self.assertRaises(RuntimeError):
                self.preflight.prepare_work_item_index()
        marker = self.env.env.state.record_get(self.preflight.NAMESPACE, 'metadata/work-item-index-1.0')
        self.assertFalse(marker['complete'])
        with self.assertRaisesRegex(OwnerRoutingError, 'index_unavailable'):
            self.pin()
        self.preflight.prepare_work_item_index()
        with self.assertRaisesRegex(OwnerRoutingError, 'lookup_truncated'):
            self.pin()
        for key, value in raw.items():
            self.assertEqual(self.env.env.state.record_get(self.preflight.NAMESPACE, key), value)

    def test_distinct_pinned_originals_with_identical_text_survive_concurrent_storage_merge(self):
        root = Path(self.env.env.temp.name)
        queue = TurnQueueRepository(self.env.env.state, root / 'queue.json')
        pin = self.pin()
        first = QueuedTurn(id='first', thread_id=self.binding.thread_id, project_id='project-a',
            message='Identical work text', created_at=2, owner_routing_pin=pin)
        queue.save({first.thread_id: [first]})
        left, right = queue.load(), queue.load()
        left[first.thread_id].append(first.model_copy(update={'id': 'second'}))
        queue.save(left)
        right[first.thread_id].append(first.model_copy(update={'id': 'third'}))
        queue.save(right)
        self.assertEqual({item.id for item in queue.load()[first.thread_id]}, {'first', 'second', 'third'})

    def test_disabled_and_updated_current_profile_block_retained_original(self):
        pin = self.pin()
        self.profiles.update(self.profile.profile_id, AgentProfileUpdate(description='Changed configuration',
            reason='Private regression'), actor=self.actor)
        with self.assertRaisesRegex(OwnerRoutingError, 'profile_changed'):
            self.validate(pin)
        self.profiles.lifecycle(self.profile.profile_id, AgentProfileLifecycle.DISABLED,
            AgentProfileLifecycleChange(reason='Private regression'), actor=self.actor)
        with self.assertRaisesRegex(OwnerRoutingError, 'profile_unavailable'):
            self.validate(pin)

    def test_missing_fuzzy_cross_project_and_ambiguous_binding_do_not_select_executor(self):
        for bindings in ([], [self.binding.model_copy(update={'route_prefix': 'Orchestrator unrelated'})],
                         [self.binding.model_copy(update={'project_id': 'project-b'})]):
            self.bindings = bindings
            with self.assertRaisesRegex(OwnerRoutingError, 'binding_unavailable'):
                self.service.existing_binding(self.state, 'orchestrator')
        self.bindings = [self.binding, self.binding.model_copy(update={'id': 'other', 'thread_id': 'unrelated'})]
        with self.assertRaisesRegex(OwnerRoutingError, 'binding_ambiguous'):
            self.service.existing_binding(self.state, 'orchestrator')

    def test_canonical_replacement_lineage_required_and_original_pin_is_unchanged(self):
        pin = self.pin()
        self.binding = self.binding.model_copy(update={'id': 'replacement-binding', 'thread_id': 'new-thread'})
        self.bindings = [self.binding]
        with self.assertRaisesRegex(OwnerRoutingError, 'replacement_unverified'):
            self.validate(pin)
        self.replacement = 'new-thread'
        self.validate(pin)
        self.assertEqual(pin.original_thread_id, 'existing-thread')

    def test_state_scope_retry_and_actual_profile_actor_are_revalidated(self):
        pin = self.pin()
        for overrides in ({'agent_profile_actor_id': 'other-actor'}, {'agent_profile_revision': 99},
                          {'work_item_ref': 'other#1'}):
            with self.assertRaises(OwnerRoutingError):
                self.validate(pin, **overrides)
        changed = self.state.model_copy(deep=True)
        changed.execution.retry.attempt += 1
        self.env.states.put(changed.ref, changed)
        with self.assertRaisesRegex(OwnerRoutingError, 'work_item_changed'):
            self.validate(pin)

    def test_retained_legacy_pending_intent_cannot_be_rebound_from_current_alias(self):
        pending = self.env.pending()
        with self.assertRaisesRegex(OwnerRoutingError, 'intent_unbound_or_changed'):
            self.service.pin(pending, self.binding, 'operator')

    def test_unsupported_pin_schema_and_extra_authority_fields_fail_visibly(self):
        pin = self.pin()
        for changes in ({'schema_version': '2.0'}, {'grant_admin': True}):
            with self.assertRaises(ValueError):
                type(pin).model_validate({**pin.model_dump(), **changes})

    def test_configuration_blocker_is_durable_once_and_clears_only_for_current_qualified_intent(self):
        pin = self.pin()
        error = OwnerRoutingError('work_item_owner_binding_unavailable')
        for _ in range(3):
            self.service.record_failure(self.env.states.get(self.state.ref), error)
        self.assertEqual(len(self.env.machine.event_store.load()), 1)
        blocked = self.env.states.get(self.state.ref)
        self.assertEqual(blocked.owner_routing_blocker.code, str(error))
        self.assertEqual(blocked.implementation_owner, 'carl')
        self.assertEqual(blocked.current_owner, 'operator')
        self.env.env.publish()
        self.service.clear_failure(pin)
        self.assertIsNotNone(self.env.states.get(self.state.ref).owner_routing_blocker)
        current_pin = self.pin()
        self.service.clear_failure(current_pin)
        self.assertIsNone(self.env.states.get(self.state.ref).owner_routing_blocker)


class OwnerRoutingContinuityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = OwnerRoutingDispatchTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        env = self.fixture.env
        async def same_thread(binding, source):
            return binding
        self.dispatch = AsyncMock(return_value={'ok': True, 'queued': True})
        self.continuity = WorkItemContinuityService(
            policy=RuntimePolicy(Path(env.env.temp.name)), get_state=env.machine._work_item_state,
            update_state=env.states.update, coerce_owner=env.machine._coerce_owner,
            binding_for_agent=Mock(side_effect=AssertionError('alias must use exact existing binding')),
            replace_nonperforming_thread=same_thread, dispatch_event=self.dispatch,
            dispatch_text=lambda state: 'Original scoped recovery', append_event=lambda event: None,
            truncate_text=lambda value, limit: value[:limit], thread_is_active=lambda thread: False,
            thread_queue_depth=lambda thread: 0, thread_recently_active=lambda thread: False,
            watchdog_dispatch_allowed=lambda key: True, record_watchdog_dispatch=lambda key: None,
            coordination_channel='coordination')
        self.continuity.owner_routing = self.fixture.service

    async def test_scheduled_handoff_refusal_survives_blocker_CAS_drift(self):
        env = self.fixture.env
        pending = env.pending()
        pending.handoff.owner_routing = env.machine._owner_routing(pending).binding('operator')
        env.states.put(pending.ref, pending)
        events = []
        self.continuity.append_event = events.append

        async def refused(*args, **kwargs):
            current = env.states.get(pending.ref)
            current.updated_at += 1
            env.states.put(current.ref, current)
            raise OwnerRoutingError('owner_recipient_preparation_changed')

        self.fixture.service.prepare_compatible_pin = AsyncMock(side_effect=refused)
        self.continuity.schedule_structured_handoff_dispatch(pending, source='work-item-handoff')
        task = self.continuity.dispatch_coordinator._tasks['handoff-dispatch:'+pending.ref]
        await task
        self.assertEqual(self.continuity.dispatch_coordinator.status()['completed'], 1)
        self.assertEqual(self.continuity.dispatch_coordinator.status()['failed'], 0)
        self.assertIsNone(env.states.get(pending.ref).owner_routing_blocker)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0], {
            'type': 'work_item_handoff_dispatch_failed', 'ref': pending.ref,
            'source': 'work-item-handoff', 'handoff_requested_at': 1,
            'recipient': 'operator', 'reason_code': 'owner_recipient_preparation_changed',
            'dispatch_outcome': 'owner_routing_blocked', 'native_outcome': 'not_inferred',
        })
        self.assertEqual(env.states.get(pending.ref).handoff.requested_at, 1)
        self.assertEqual(env.states.get(pending.ref).handoff.status, 'pending')
        self.fixture.service.prepare_compatible_pin.assert_awaited_once()
        self.dispatch.assert_not_called()
        await self.continuity.stop()

    async def test_direct_continuity_refusal_preserves_result_without_error_prose(self):
        env = self.fixture.env
        pending = env.pending()
        pending.handoff.owner_routing = env.machine._owner_routing(pending).binding('operator')
        env.states.put(pending.ref, pending)
        events = []
        self.continuity.append_event = events.append
        self.fixture.service.prepare_compatible_pin = AsyncMock(side_effect=OwnerRoutingError('opaque_private_refusal'))
        result = await self.continuity.dispatch_structured_handoff(pending, source='work-item-handoff-continuity')
        self.assertEqual(result, {'status': 'owner_routing_blocked', 'code': 'opaque_private_refusal', 'dispatched': False})
        self.assertEqual(events[0]['reason_code'], 'owner_routing_refused')
        self.assertNotIn('private', str(events))
        self.dispatch.assert_not_called()

    async def test_refusal_event_write_failure_preserves_blocked_result(self):
        env = self.fixture.env
        pending = env.pending()
        pending.handoff.owner_routing = env.machine._owner_routing(pending).binding('operator')
        env.states.put(pending.ref, pending)
        self.continuity.append_event = Mock(side_effect=OSError('private journal failure'))
        self.fixture.service.prepare_compatible_pin = AsyncMock(side_effect=OwnerRoutingError('owner_recipient_preparation_changed'))
        result = await self.continuity.dispatch_structured_handoff(pending, source='work-item-handoff')
        self.assertEqual(result['code'], 'owner_recipient_preparation_changed')
        self.assertFalse(result['dispatched'])
        self.dispatch.assert_not_called()

    async def test_superseded_pending_handoff_does_not_create_refusal_event(self):
        env = self.fixture.env
        pending = env.pending()
        events = []
        self.continuity.append_event = events.append
        self.continuity.schedule_structured_handoff_dispatch(pending, source='work-item-handoff')
        changed = env.states.get(pending.ref)
        changed.handoff.requested_at = 2
        env.states.put(changed.ref, changed)
        await self.continuity.dispatch_coordinator._tasks['handoff-dispatch:'+pending.ref]
        self.assertEqual(events, [])
        self.dispatch.assert_not_called()
        await self.continuity.stop()

    async def test_alias_dispatch_keeps_claims_and_actual_canonical_executor(self):
        state = self.fixture.state
        result = await self.continuity.dispatch_actionable_owner(state, source='owner-work-watchdog')
        self.assertTrue(result['dispatched'])
        self.assertEqual(result['owner'], 'orchestrator')
        args, kwargs = self.dispatch.call_args
        self.assertEqual(args[0].id, self.fixture.binding.id)
        self.assertEqual(kwargs['owner_routing_pin'].binding.definition.record_id, self.fixture.env.reference.record_id)
        retained = self.fixture.env.states.get(state.ref)
        self.assertEqual(retained.current_owner, 'operator')
        self.assertEqual(retained.implementation_owner, 'carl')

    async def test_definition_change_while_thread_recovery_yields_blocks_without_dispatch(self):
        async def changed(binding, source):
            self.fixture.env.env.publish()
            return binding
        self.continuity.replace_nonperforming_thread = changed
        result = await self.continuity.dispatch_actionable_owner(self.fixture.state, source='owner-work-watchdog')
        self.assertEqual(result['code'], 'owner_routing_definition_changed')
        self.dispatch.assert_not_called()
        self.assertEqual(self.fixture.env.states.get(self.fixture.state.ref).owner_routing_blocker.code,
            'owner_routing_definition_changed')

    async def test_repeated_missing_binding_is_retained_once_without_dispatch_or_new_threads(self):
        self.fixture.bindings = []
        for _ in range(3):
            result = await self.continuity.dispatch_actionable_owner(
                self.fixture.env.states.get(self.fixture.state.ref), source='owner-work-watchdog')
            self.assertFalse(result['dispatched'])
        self.dispatch.assert_not_called()
        self.assertEqual(len(self.fixture.env.machine.event_store.load()), 1)
        self.assertEqual(self.continuity.owner_dispatch_projection(self.fixture.state)['blocked_reason'],
            'work_item_owner_binding_unavailable')

    async def test_alias_steering_requires_exact_current_definition(self):
        observed = self.continuity.owner_dispatch_projection(self.fixture.state)
        self.assertTrue(observed['eligible'])
        kwargs = dict(expected_owner='orchestrator', expected_thread_id=self.fixture.binding.thread_id,
            idempotency_key='original-scope-1', actor='real-actor', source='assignment-control-plane')
        with self.assertRaises(HTTPException) as caught:
            await self.continuity.steer_actionable_owner(self.fixture.state, **kwargs)
        self.assertEqual(caught.exception.detail['code'], 'owner_routing_definition_changed')
        self.dispatch.assert_not_called()
        result = await self.continuity.steer_actionable_owner(self.fixture.state, **kwargs,
            expected_definition=self.fixture.env.machine._owner_routing(self.fixture.state).definition)
        self.assertTrue(result['dispatched'])

    async def test_bound_handoff_needs_actual_start_and_mapping_change_does_not_rename_native_fact(self):
        env = self.fixture.env
        pending = env.pending()
        pending.handoff.owner_routing = env.machine._owner_routing(pending).binding('operator')
        pending.handoff.delivery = WorkItemHandoffDelivery()
        env.states.put(pending.ref, pending)
        await self.continuity.dispatch_structured_handoff(pending, source='work-item-handoff')
        pin = self.dispatch.call_args.kwargs['owner_routing_pin']
        self.assertEqual(env.states.get(pending.ref).handoff.delivery.status, 'awaiting_dispatch')
        with self.assertRaises(HTTPException):
            env.machine._structured_ack(pending.ref, WorkItemAckCreate(actor='orchestrator',
                expected_handoff_requested_at=1))
        env.env.publish({'operator': 'james'})
        # Synthetic native callback fixture proves source attribution only.
        # This test is not installed original-work acceptance or a live ACK.
        self.assertTrue(self.continuity.record_handoff_delivery(pending.ref,
            expected_requested_at=1, recipient='orchestrator', organization_id='org-a',
            workspace_id='workspace-a', project_id='project-a', thread_id=self.fixture.binding.thread_id,
            turn_id='fixture-native-turn', execution_id='fixture-execution', started_at=2,
            execution_profile_id='orchestration-admin', owner_routing_pin=pin))
        accepted = env.machine._structured_ack(pending.ref, WorkItemAckCreate(actor='orchestrator',
            expected_handoff_requested_at=1))
        self.assertEqual(accepted.current_owner, 'orchestrator')
        self.assertEqual(accepted.handoff.to_agent, 'operator')
        self.assertEqual(accepted.handoff.owner_routing.definition.record_id, env.reference.record_id)
        self.assertEqual(accepted.implementation_owner, 'carl')

    async def test_captured_late_native_fact_projects_delivery_without_ack_after_mapping_and_profile_change(self):
        from codex_web.execution_workers import ExecutionAssignment, AssignmentLease, NetworkPolicy, WorkerResourceLimits
        from codex_web.services.execution_preflight import ExecutionPreflightService
        from codex_web.services.queued_native_start import QueuedNativeStartCapture, reconcile_captured_start
        env = self.fixture.env
        pending = env.pending()
        pending.handoff.owner_routing = env.machine._owner_routing(pending).binding('operator')
        pending.handoff.delivery = WorkItemHandoffDelivery()
        env.states.put(pending.ref, pending)
        pin = self.fixture.service.pin(pending, self.fixture.binding, 'operator')
        original = QueuedTurn(id='native-original', execution_id='requested-native-original',
            thread_id=self.fixture.binding.thread_id, project_id='project-a', message='Original recovery',
            work_item_ref=pending.ref, handoff_requested_at=1, created_at=1, owner_routing_pin=pin,
            agent_profile_id=self.fixture.profile.profile_id, agent_profile_revision=1,
            agent_profile_actor_id=self.fixture.actor.identity_id, execution_profile_id='orchestration-only')
        service = ExecutionPreflightService(self.fixture.preflight, clock=lambda: 3)
        service.queued_start_observer = self.fixture.service.project_native_start
        self.fixture.service.delivery_recorder = self.continuity.record_handoff_delivery
        attempt = service.retain_queued(original, organization_id='org-a', workspace_id='workspace-a')
        _, claim = service.claim_queued_admission(attempt.id)
        service.begin_queued_start(attempt.id, claim_id=claim)
        assignment = ExecutionAssignment(id='native-assignment', organization_id='org-a', workspace_id='workspace-a',
            subject={'kind': 'thread_bootstrap', 'ref': 'fixture-bootstrap'}, project_id='project-a',
            execution_id='native-bootstrap-execution', resource_ids=(), execution_contract_version='1.0',
            required_capabilities=(), sandbox='read-only', approval_policy='on-request', network=NetworkPolicy(),
            limits=WorkerResourceLimits(), created_by=self.fixture.actor.identity_id, assigned_worker_id='fixture-worker',
            fence=7, execution_profile_id='orchestration-only', agent_profile=self.fixture.assignment.agent_profile,
            lease=AssignmentLease(worker_id='fixture-worker', fence=7, lease_token='private-test-lease-never-persisted',
                acquired_at=1, expires_at=10))
        capture = QueuedNativeStartCapture(service, attempt.id, claim, original.execution_id,
            original.thread_id, self.fixture.actor.identity_id).bind_assignment(assignment,
                worker_id='fixture-worker', fence=7, validator=lambda: assignment)
        correlation = capture.prepare(generation='fixture-generation', rpc_request_id='fixture-rpc',
            native_session_id='fixture-native-session')
        self.assertFalse(self.fixture.service.project_native_start(self.fixture.preflight.get(attempt.id)))
        self.assertEqual(env.states.get(pending.ref).handoff.delivery.status, 'awaiting_dispatch')
        env.env.publish({'operator': 'james'})
        self.fixture.profiles.lifecycle(self.fixture.profile.profile_id, AgentProfileLifecycle.DISABLED,
            AgentProfileLifecycleChange(reason='Private post-start revocation fixture'), actor=self.fixture.actor)
        # Real capture/reconciliation over a synthetic exact RPC response. This
        # establishes source attribution, never installed work or a live ACK.
        self.assertTrue(capture.observe(correlation, generation='fixture-generation', message={
            'id': 'fixture-rpc', 'result': {'thread': {'id': 'fixture-native-session'},
                'turn': {'id': 'fixture-native-turn', 'threadId': 'fixture-native-session', 'status': 'inProgress'}}}))
        delivered = env.states.get(pending.ref)
        self.assertEqual(delivered.handoff.delivery.status, 'delivered')
        self.assertTrue(delivered.handoff.delivery.scope_verified)
        self.assertEqual(delivered.handoff.delivery.turn_id, 'fixture-native-turn')
        self.assertEqual(delivered.handoff.status, 'pending')
        self.assertIsNone(delivered.handoff.acknowledged_at)
        saved = self.fixture.preflight.get(attempt.id)
        reconcile_captured_start(service, attempt.id)
        self.assertEqual(env.states.get(pending.ref), delivered)
        self.assertEqual(self.fixture.preflight.get(attempt.id), saved)
        bad = saved.model_copy(update={'queued_start_receipt': saved.queued_start_receipt.model_copy(
            update={'provider_native_turn_id': 'foreign-turn'})})
        with self.assertRaisesRegex(OwnerRoutingError, 'native_fact_mismatch'):
            self.fixture.service.project_native_start(bad)
        self.assertEqual(env.states.get(pending.ref), delivered)
