"""Accepted ownership reuses proven native custody, without preparing new input."""
import time
import unittest

from codex_web.owner_routing import OwnerRoutingError
from tests import test_owner_cancelled_new_handoff as fixtures


class AcceptedPreparedOwnerRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = fixtures.CancelledNewHandoffRecipientTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    async def accepted(self):
        f = self.fixture
        # Real retained original, claim, correlated START, delivery and ACK.
        await f.test_actual_native_start_own_exclusion_delivery_and_ack()
        state = f.f.f.env.states.get(f.n.state.ref)
        self.assertEqual(state.handoff.status, 'accepted')
        self.assertTrue(state.handoff.delivery.scope_verified)
        self.assertIsNotNone(state.handoff.acknowledged_at)
        return state

    def select(self, state):
        return self.fixture.n.routing.compatible_pin(state, self.fixture.n.binding,
            'orchestrator', writable_repository_ids=('platform',))

    def attempt(self):
        f = self.fixture
        rows = f.f.f.preflight.for_work_item(organization_id='org-a',
            workspace_id='workspace-a', project_id='project-a', ref=f.n.state.ref)
        self.assertEqual(len(rows), 1)
        return rows[0]

    def snapshot(self):
        f = self.fixture
        return ({k: a.model_dump(mode='json') for k, a in f.f.assignments.items()},
            f.n.store.load().model_dump(mode='json'),
            f.n.binding.model_dump(mode='json'), f.n.threads.create.await_count)

    async def test_accepted_owner_resolves_same_prepared_recipient_without_new_context(self):
        state = await self.accepted()
        before = self.snapshot()
        pin = self.select(state)
        self.assertEqual(pin.recipient.thread_id, state.handoff.delivery.thread_id)
        self.assertEqual(pin.recipient.assignment_id, 'fresh-recipient-assignment')
        self.assertIsNone(pin.handoff_requested_at)
        self.assertEqual(self.fixture.n.routing.context_for_pin(pin, self.fixture.n.binding),
            (self.fixture.f.f.profile, self.fixture.f.f.actor))
        self.assertEqual(self.snapshot(), before)

    def configure_alias(self):
        from codex_web.services.thread_bootstrap_bindings import ThreadBootstrapBindingNotFoundError
        f = self.fixture
        f.n.state.handoff.to_agent = 'operator'
        f.n.state.next_owner = 'operator'
        f.n.state.handoff.owner_routing = f.n.routing.snapshot(f.n.state).binding('operator')
        f.f.f.env.states.put(f.n.state.ref, f.n.state)
        async def alias_prepared():
            return await f.n.routing.prepare_compatible_pin(f.n.state, f.n.binding,
                'operator', writable_repository_ids=('platform',))
        f.prepared = alias_prepared
        # Model the canonical point lookup's typed absence for another logical
        # key, rather than the older fixture's assert-on-any-wrong-key shortcut.
        def exact_bootstrap(key, actor):
            if f.n.bound is None or key != f.n.bound.bootstrap_id:
                raise ThreadBootstrapBindingNotFoundError('Private exact reservation absent')
            return f.n.bound
        f.n.routing.bootstraps.get_by_bootstrap = exact_bootstrap

    async def test_pending_alias_final_guard_uses_original_configured_reservation(self):
        f = self.fixture
        self.configure_alias()
        pin = await f.prepared()
        self.assertEqual(pin.binding.configured_owner, 'operator')
        self.assertEqual(pin.binding.canonical_owner, 'orchestrator')
        token = f.n.routing.recipient_preparer.logical_id(f.n.state, f.n.binding,
            'operator', ('platform',))
        self.assertEqual(f.n.bound.bootstrap_id, 'bootstrap-' + token)
        before = self.snapshot()
        f.validate(pin)
        self.assertEqual(self.snapshot(), before)
        self.assertIsNone(f.f.f.env.states.get(f.n.state.ref).handoff.acknowledged_at)

    async def test_alias_handoff_retains_configured_reservation_and_canonical_owner(self):
        f = self.fixture
        self.configure_alias()
        state = await self.accepted()
        self.assertEqual(state.handoff.to_agent, 'operator')
        self.assertEqual(state.current_owner, 'orchestrator')
        pin = self.select(state)
        self.assertEqual(pin.recipient.thread_id, state.handoff.delivery.thread_id)
        self.assertEqual(f.n.routing.context_for_pin(pin, f.n.binding),
            (f.f.f.profile, f.f.f.actor))
        f.n.threads.create.assert_awaited_once()

    async def test_missing_or_inconsistent_positive_start_cannot_select_owner(self):
        state = await self.accepted()
        f = self.fixture
        original = self.attempt()
        before = self.snapshot()
        changes = [
            {'queued_start_receipt': None},
            {'queued_start_evidence': None},
            {'queued_start_correlation': None},
            {'queued_start_receipt': original.queued_start_receipt.model_copy(
                update={'provider_native_turn_id': 'foreign-turn'})},
            {'queued_start_evidence': original.queued_start_evidence.model_copy(
                update={'rpc_request_id': 'foreign-rpc'})},
            {'queued_start_correlation': original.queued_start_correlation.model_copy(
                update={'actor_identity_id': 'foreign-actor'})},
        ]
        for change in changes:
            with self.subTest(change=tuple(change)):
                f.f.f.preflight.put(original.model_copy(deep=True, update=change))
                with self.assertRaises(OwnerRoutingError):
                    self.select(state)
                self.assertEqual(self.snapshot(), before)
        f.f.f.preflight.put(original)
        self.assertEqual(self.select(state).recipient.thread_id, state.handoff.delivery.thread_id)

    async def test_ambiguous_retained_start_and_unresolved_other_original_refuse(self):
        state = await self.accepted()
        f = self.fixture
        original = self.attempt()
        duplicate = original.model_copy(deep=True, update={'id': 'second-start-witness'})
        f.f.f.preflight.put(duplicate)
        with self.assertRaisesRegex(OwnerRoutingError, 'accepted_lineage_unverified'):
            self.select(state)
        # Remove only the private corrupt fixture's extra authoritative record;
        # its stale derived locator must never authorize a missing original.
        f.f.f.preflight.store.record_apply(f.f.f.preflight.NAMESPACE, upserts={},
            deletes=(f.f.f.preflight._attempt_key(duplicate.id),))
        self.assertEqual(self.select(state).recipient.thread_id, state.handoff.delivery.thread_id)
        uncertain = f.f.f.uncertain()
        f.f.f.preflight.put(uncertain)
        with self.assertRaisesRegex(OwnerRoutingError, 'native_outcome_unresolved'):
            self.select(state)
        self.assertEqual(f.f.f.preflight.get(uncertain.id), uncertain)
        f.n.threads.create.assert_awaited_once()

    async def test_incomplete_retained_index_and_foreign_source_native_custody_refuse(self):
        state = await self.accepted()
        f = self.fixture
        store = f.f.f.preflight.store
        key = 'metadata/work-item-index-1.0'
        marker = store.record_get(f.f.f.preflight.NAMESPACE, key)
        store.record_apply(f.f.f.preflight.NAMESPACE, upserts={key: {**marker, 'complete': False}})
        with self.assertRaises(OwnerRoutingError):
            self.select(state)
        store.record_apply(f.f.f.preflight.NAMESPACE, upserts={key: marker})
        source = f.fixture.current_session
        for update in ({'assignment_id': 'foreign'}, {'project_id': 'foreign'}):
            f.n.store.upsert(source.model_copy(update=update))
            with self.assertRaises(OwnerRoutingError):
                self.select(state)
        f.n.store.upsert(source)
        f.n.store.upsert(source.model_copy(update={'id': 'ambiguous-source', 'runtime_id': 'other'}))
        with self.assertRaises(OwnerRoutingError):
            self.select(state)
        f.n.threads.create.assert_awaited_once()

    async def test_stale_routing_definition_refuses_existing_accepted_start(self):
        state = await self.accepted()
        self.fixture.f.f.env.env.publish()
        with self.assertRaises(OwnerRoutingError):
            self.select(state)
        self.fixture.n.threads.create.assert_awaited_once()

    async def test_current_owner_nonce_delivery_and_scope_cannot_be_reinterpreted(self):
        state = await self.accepted()
        before = self.snapshot()
        changes = []
        for name, value in [('requested_at', state.handoff.requested_at + 1),
                            ('to_agent', 'james'), ('acknowledged_at', None)]:
            changed = state.model_copy(deep=True)
            setattr(changed.handoff, name, value)
            changes.append((name, changed))
        for name, value in [('thread_id', 'foreign-thread'),
                            ('execution_id', 'thread-bootstrap-' + 'f' * 32),
                            ('scope_verified', False),
                            ('writable_repository_resource_ids', ('foreign',))]:
            changed = state.model_copy(deep=True)
            setattr(changed.handoff.delivery, name, value)
            changes.append(('delivery_' + name, changed))
        for name, value in [('current_owner', 'james'),
                            ('organization_id', 'foreign'), ('workspace_id', 'foreign'),
                            ('resource_ids', ['foreign'])]:
            changes.append((name, state.model_copy(deep=True, update={name: value})))
        for name, changed in changes:
            with self.subTest(change=name):
                with self.assertRaises(OwnerRoutingError):
                    self.select(changed)
                self.assertEqual(self.snapshot(), before)
        pin = self.select(state)
        changed = state.model_copy(deep=True, update={'next_owner': 'james'})
        # A current owner may legitimately retain an executor after changing
        # its next-owner plan, but the pre-change native witness is stale.
        self.assertEqual(self.select(changed).recipient.thread_id, pin.recipient.thread_id)
        with self.assertRaisesRegex(OwnerRoutingError, 'work_item_changed'):
            self.fixture.n.routing.recipient_context(pin, changed, self.fixture.n.binding)
        self.assertEqual(self.snapshot(), before)

    async def test_live_profile_actor_scope_and_fence_changes_refuse(self):
        state = await self.accepted()
        f = self.fixture
        original = f.f.assignments['fresh-recipient-assignment']
        changes = [
            {'created_by': 'foreign'}, {'project_id': 'foreign'},
            {'agent_profile': original.agent_profile.model_copy(update={'profile_revision': 999})},
            {'fence': original.fence + 1},
            {'lease': None},
            {'repository_scope': original.repository_scope.model_copy(
                update={'writable_repository_ids': ('foreign',)})},
            {'resource_ids': ('foreign',)},
        ]
        for change in changes:
            with self.subTest(change=tuple(change)):
                f.f.assignments[original.id] = original.model_copy(deep=True, update=change)
                with self.assertRaises(OwnerRoutingError):
                    self.select(state)
                f.n.threads.create.assert_awaited_once()
        f.f.assignments[original.id] = original
        self.assertEqual(self.select(state).recipient.assignment_id, original.id)

    async def test_current_owner_start_excludes_only_its_own_retained_original(self):
        from codex_web.models import QueuedTurn
        from codex_web.services.execution_preflight import ExecutionPreflightService
        from codex_web.services.queued_native_start import QueuedNativeStartCapture, NativeStartAdmissionRefused
        state = await self.accepted()
        f = self.fixture
        pin = self.select(state)
        queued = QueuedTurn(id='accepted-owner-input', execution_id='accepted-owner-input-execution',
            thread_id=pin.recipient.thread_id, project_id='project-a', message='Current owned Work',
            work_item_ref=state.ref, created_at=time.time(), owner_routing_pin=pin,
            agent_profile_id=f.f.f.profile.profile_id,
            agent_profile_revision=f.f.f.profile.revision,
            agent_profile_actor_id=f.f.f.actor.identity_id, execution_profile_id='repository-write',
            repository_resource_id='platform', writable_repository_resource_ids=('platform',))
        service = ExecutionPreflightService(f.f.f.preflight, clock=time.time)
        attempt = service.retain_queued(queued, organization_id='org-a', workspace_id='workspace-a')
        _, claim = service.claim_queued_admission(attempt.id)
        service.begin_queued_start(attempt.id, claim_id=claim)
        assignment = f.f.assignments[pin.recipient.assignment_id]
        def validate():
            f.n.routing.validate_native(pin, thread_id=pin.recipient.thread_id,
                project=f.f.f.env.env.projects.get('project-a', scope=f.f.f.env.env.scope),
                work_item_ref=state.ref, handoff_requested_at=None,
                queued_admission_attempt_id=attempt.id,
                writable_repository_resource_ids=('platform',),
                agent_profile_id=queued.agent_profile_id,
                agent_profile_revision=queued.agent_profile_revision,
                agent_profile_actor_id=queued.agent_profile_actor_id)
        capture = QueuedNativeStartCapture(service, attempt.id, claim, queued.execution_id,
            queued.thread_id, queued.agent_profile_actor_id,
            admission_validator=validate, require_send_guard=True).bind_assignment(
                assignment, worker_id=assignment.assigned_worker_id, fence=assignment.fence,
                validator=lambda: assignment)
        correlation = capture.prepare(generation='accepted-owner-generation',
            rpc_request_id='accepted-owner-rpc', native_session_id='accepted-owner-native')
        capture.validate_before_send(correlation)
        self.assertIsNone(service.get(attempt.id, actor=f.f.f.actor).queued_start_receipt)
        other = f.f.f.uncertain()
        f.f.f.preflight.put(other)
        with self.assertRaises(NativeStartAdmissionRefused) as caught:
            capture.validate_before_send(correlation)
        self.assertIsInstance(caught.exception.__cause__, OwnerRoutingError)
        self.assertEqual(str(caught.exception.__cause__), 'owner_routing_native_outcome_unresolved')
        self.assertEqual(f.f.f.preflight.get(other.id), other)
        self.assertIsNone(service.get(attempt.id, actor=f.f.f.actor).queued_start_receipt)
        f.n.threads.create.assert_awaited_once()

    def configure_generation_one(self):
        from codex_web.execution_workers import (WorkerCapability, ExecutionAssignmentCreate,
            WorkerResourceLimits, AssignmentCancelRequest)
        from codex_web.execution_subjects import ExecutionSubject
        from codex_web.services.execution_workers import ExecutionWorkerService
        from codex_web.storage.execution_workers import ExecutionWorkerStore
        from codex_web.services.thread_bootstrap_bindings import ThreadBootstrapBindingNotFoundError
        from codex_web.services.threads import ThreadBootstrapPrestartRefusal
        from codex_web.thread_bootstrap import BootstrapPreworkerRefusal
        f = self.fixture
        self.workers = ExecutionWorkerService(ExecutionWorkerStore(f.f.f.env.env.state))
        f.n.routing.assignments = lambda key: self.workers.store.assignment(key) or f.f.assignments.get(key)
        f.n.routing.recipient_preparer.existing_assignment = lambda **kw: next(
            (a for a in self.workers.list_assignments(f.f.f.actor)
             if a.execution_id == kw['execution_id']), None)
        bindings = {}
        def get_bootstrap(key, actor):
            if key not in bindings:
                raise ThreadBootstrapBindingNotFoundError('Private missing exact bootstrap')
            return bindings[key]
        f.n.routing.bootstraps.get_by_bootstrap = get_bootstrap
        self.predecessor = None
        async def create(**kwargs):
            token = kwargs['bootstrap_token']
            if self.predecessor is not None:
                # Keep the original fixture's sole external native-create seam.
                result = await f.create(**kwargs)
                bindings['bootstrap-' + token] = f.n.bound
                return result
            kwargs['admission_guard']()
            a = self.workers.create_assignment(ExecutionAssignmentCreate(
                subject=ExecutionSubject(kind='thread_bootstrap', ref='bootstrap-' + token),
                execution_id='thread-bootstrap-' + token, project_id='project-a',
                resource_ids=('platform',), execution_contract_version='1.0',
                required_capabilities=(WorkerCapability.GIT,), sandbox=f.n.binding.sandbox,
                approval_policy=f.n.binding.approval_policy,
                limits=WorkerResourceLimits(cpu_seconds=10, memory_bytes=128 * 1024 * 1024,
                    disk_bytes=64 * 1024 * 1024, process_count=4, wall_seconds=60),
                repository_scope=f.n.template.repository_scope,
                execution_profile_id=f.f.f.profile.execution_profile_id,
                agent_profile=f.n.template.agent_profile), actor=f.f.f.actor)
            self.predecessor = self.workers.cancel_bootstrap(a.id,
                AssignmentCancelRequest(expected_fence=0, reason='Private canonical preworker refusal'),
                actor=f.f.f.actor, preworker_refusal=BootstrapPreworkerRefusal(
                    'bootstrap-' + token, a.execution_id, 'TimeoutError', True,
                    kwargs['recipient_origin']))
            raise ThreadBootstrapPrestartRefusal(status_code=503, detail={'code': 'bootstrap_prestart_failed'})
        f.n.threads.create.side_effect = create

    async def generation_one_accepted(self):
        from codex_web.services.threads import ThreadBootstrapPrestartRefusal
        self.configure_generation_one()
        with self.assertRaises(ThreadBootstrapPrestartRefusal):
            await self.fixture.prepared()
        self.assertEqual(self.predecessor.failure.details['generation'], 0)
        state = await self.accepted()
        self.assertEqual(self.fixture.n.threads.create.await_count, 2)
        return state

    async def test_generation_one_accepted_owner_keeps_canonical_preworker_refusal(self):
        state = await self.generation_one_accepted()
        before = self.predecessor.model_dump(mode='json')
        pin = self.select(state)
        self.assertEqual(pin.recipient.thread_id, state.handoff.delivery.thread_id)
        self.assertEqual(self.workers.store.assignment(self.predecessor.id).model_dump(mode='json'), before)
        self.assertEqual(self.fixture.n.threads.create.await_count, 2)

    async def test_generation_one_changed_typed_predecessor_does_not_authorize_owner(self):
        state = await self.generation_one_accepted()
        predecessor = self.predecessor
        for details_change in ({'provider_start_called': True}, {'generation': 1},
                               {'attribution_sha256': '0' * 64}):
            with self.subTest(change=details_change):
                changed = predecessor.model_copy(deep=True)
                changed.failure.details.update(details_change)
                self.workers.store.update(lambda state: state.model_copy(update={
                    'assignments': [changed if a.id == changed.id else a for a in state.assignments]}))
                with self.assertRaises(OwnerRoutingError):
                    self.select(state)
                self.assertEqual(self.fixture.n.threads.create.await_count, 2)
        self.workers.store.update(lambda state: state.model_copy(update={
            'assignments': [predecessor if a.id == predecessor.id else a for a in state.assignments]}))
