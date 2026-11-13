"""#2096: real refusal producer and separately controlled witness consumer.

The producer uses actual Thread/worker/workspace cleanup with controlled process
transport. Consumer tests reuse the established typed-witness fixture; they do
not claim physical cleanup or provider execution from that fixture.
"""
import asyncio
from dataclasses import replace
import unittest
from unittest.mock import Mock

from codex_web.services.threads import ThreadBootstrapPrestartRefusal
from codex_web.services.thread_bootstrap_bindings import ThreadBootstrapBindingNotFoundError
from codex_web.owner_routing import OwnerRoutingError
from codex_web.services.owner_recipient_preparation import OwnerRecipientPreparationService
from tests import test_owner_postworker_preprovider_reservation as existing
from tests import test_owner_native_session_recipient as native


class CurrentWorkPreproviderProducerTests(unittest.IsolatedAsyncioTestCase):
    setUp = existing.PostworkerPreproviderBoundaryTests.setUp
    refuse = existing.PostworkerPreproviderBoundaryTests.refuse

    def current_work_origin(self):
        mode = ({'intent': 'current_work'} if 'intent' in
                self.origin.__dataclass_fields__ else {})
        self.origin = replace(self.origin, handoff_requested_at=None, **mode)

    async def test_real_current_work_refusal_retains_joined_preprovider_proof(self):
        # Compatibility permits the identical regression to reach the original
        # writer on the pre-fix source, whose internal origin had no mode field.
        self.current_work_origin()
        current = await self.refuse()
        self.assertEqual((current.status, current.fence, current.lease),
                         ('cancelled', 2, None))
        self.assertEqual(self.stopped, [current.id])
        self.assertEqual(self.f.process_session.requests, [])
        self.assertEqual(self.f.bootstrap_bindings.store.load().bindings, [])
        self.assertIsNotNone(current.failure,
            'joined current-Work pre-provider refusal lost its canonical proof')
        self.assertEqual(current.failure.source_native_code, 'before_provider_start')
        details = current.failure.details
        self.assertEqual(details['intent'], 'current_work')
        self.assertIsNone(details['handoff_requested_at'])
        self.assertEqual(details['attribution_sha256'], self.origin.attribution_sha256)
        for field in ('admission_guard_joined', 'worker_teardown_settled'):
            self.assertIs(details[field], True)
        self.assertIs(details['provider_start_called'], False)
        self.assertEqual(self.f.workspaces.get(current.execution_workspace_id,
            self.f.actor).status, 'released')

    async def test_current_work_provider_unknown_never_gets_refusal_proof(self):
        self.current_work_origin()
        current = await self.refuse(provider=True)
        self.assertIsNone(current.failure)

    async def test_current_work_unsettled_cleanup_never_gets_refusal_proof(self):
        self.current_work_origin()
        self.pending = 1
        current = await self.refuse()
        self.assertIsNone(current.failure)
        self.assertIn(current.id, self.manager.sessions)

    async def test_null_nonce_without_current_work_mode_is_not_authority(self):
        self.origin = replace(self.origin, handoff_requested_at=None)
        current = await self.refuse()
        self.assertIsNone(current.failure)

    async def test_current_work_mode_cannot_claim_a_pending_nonce(self):
        self.origin = replace(self.origin, intent='current_work')
        current = await self.refuse()
        self.assertIsNone(current.failure)

    async def test_real_current_work_preworker_refusal_keeps_explicit_mode(self):
        self.current_work_origin()
        calls = 0
        def guard():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ValueError('controlled current-Work admission refusal')
        with self.assertRaises(ThreadBootstrapPrestartRefusal):
            await self.f.thread_service.create(project_id=self.f.project.id,
                bootstrap_token=self.origin.logical_id, admission_guard=guard,
                recipient_origin=self.origin)
        current = self.f.workers.list_assignments(self.f.actor)[0]
        self.assertEqual((current.status, current.fence, current.assigned_worker_id,
                          current.started_at, current.lease),
                         ('cancelled', 1, None, None, None))
        self.assertEqual(current.failure.details['intent'], 'current_work')
        self.assertEqual(current.failure.source_native_code, 'before_worker_start')
        self.assertEqual(self.started, [])
        self.assertEqual(self.f.process_session.requests, [])


class CurrentWorkPreproviderRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        existing.PostworkerRecipientRecoveryTests.setUp(self)
        self.state = self.f.n.state
        self.state.handoff = None
        self.state.current_owner = self.state.next_owner = 'orchestrator'
        self.state.current_stage = 'implementation_active'
        self.f.n.fixture.f.env.states.put(self.state.ref, self.state)
        self.recovery_mutation = None
        def get_bootstrap(key, actor):
            # Match the canonical point lookup: a bound generation1 does not
            # fabricate an original native binding under the generation0 key.
            bound = self.f.n.bound
            if bound is None or bound.bootstrap_id != key:
                raise ThreadBootstrapBindingNotFoundError('not bound')
            return bound
        self.f.n.routing.bootstraps.get_by_bootstrap = get_bootstrap

    async def create(self, **kwargs):
        # Consumer-only witness: all original worker/fence/intent fields remain
        # those produced by the established canonical assignment fixture.
        if kwargs['recipient_origin'].generation == 1 and self.recovery_mutation:
            self.recovery_mutation()
        try:
            return await existing.PostworkerRecipientRecoveryTests.create(self, **kwargs)
        except ThreadBootstrapPrestartRefusal:
            if self.typed:
                execution = 'thread-bootstrap-' + kwargs['bootstrap_token']
                aid = next(row.id for row in self.f.workers.list_assignments(self.f.actor)
                           if row.execution_id == execution)
                def update(state):
                    rows = []
                    for row in state.assignments:
                        if row.id == aid and row.failure is not None:
                            failure = row.failure.model_copy(update={'details': {
                                **row.failure.details, 'intent': kwargs['recipient_origin'].intent}})
                            row = row.model_copy(update={'failure': failure})
                        rows.append(row)
                    return state.model_copy(update={'assignments': rows})
                self.f.workers.store.update(update)
                if self.original.id == aid:
                    self.original = self.f.workers.store.assignment(aid)
            raise

    refuse_once = existing.PostworkerRecipientRecoveryTests.refuse_once

    async def test_ordinary_compatible_recipient_without_reservation_keeps_legacy_pin(self):
        fixture = native.NativeRecipientScopeTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        state = fixture.state
        state.handoff = None
        state.current_owner = state.next_owner = 'orchestrator'
        state.current_stage = 'implementation_active'
        fixture.fixture.f.env.states.put(state.ref, state)
        fixture.routing.bootstraps.get_by_bootstrap = Mock(
            side_effect=ThreadBootstrapBindingNotFoundError('not bound'))
        preparer = OwnerRecipientPreparationService(routing=fixture.routing,
            threads=None, existing_assignment=lambda **kwargs: None,
            ownership=None, queue_lock=None)
        preparer.context = Mock(side_effect=AssertionError('ordinary path entered recovery context'))
        fixture.routing.recipient_preparer = preparer
        pin = fixture.select()
        self.assertEqual(pin.schema_version, '1.1')
        self.assertEqual(pin.recipient.thread_id, 'platform-thread')
        self.assertIsNone(pin.current_work_intent_sha256)
        preparer.context.assert_not_called()

    async def test_same_current_work_recovers_one_generation_with_native_pin(self):
        before = await self.refuse_once()
        pin = await self.f.n.prepare()
        self.assertEqual(pin.schema_version, '1.2')
        self.assertIsNotNone(pin.current_work_intent_sha256)
        self.assertIsNone(pin.handoff_requested_at)
        self.assertIsNone(self.state.handoff)
        self.assertEqual([origin.generation for origin in self.origins], [0, 1])
        self.assertEqual(self.origins[1].predecessor_assignment_id, self.original.id)
        self.assertEqual(self.f.workers.store.assignment(self.original.id).model_dump(mode='json'), before)
        self.assertEqual(await self.f.n.prepare(), pin)
        self.assertEqual(len(self.origins), 2)
        self.assertEqual([origin.intent for origin in self.origins],
                         ['current_work', 'current_work'])
        self.f.n.routing.validate_native(pin, thread_id=pin.recipient.thread_id,
            project=self.f.n.fixture.f.env.env.projects.get('project-a',
                scope=self.f.n.fixture.f.env.env.scope), work_item_ref=self.state.ref,
            handoff_requested_at=None, writable_repository_resource_ids=('platform',),
            read_only_repository_resource_ids=(),
            agent_profile_id=self.f.n.fixture.f.profile.profile_id,
            agent_profile_revision=self.f.n.fixture.f.profile.revision,
            agent_profile_actor_id=self.f.n.fixture.f.actor.identity_id)

    async def test_concurrent_recovery_reuses_one_generation(self):
        before = await self.refuse_once()
        pins = await asyncio.gather(self.f.n.prepare(), self.f.n.prepare())
        self.assertEqual(pins[0], pins[1])
        self.assertEqual([origin.generation for origin in self.origins], [0, 1])
        self.assertEqual(self.f.workers.store.assignment(self.original.id).model_dump(mode='json'), before)

    async def test_second_refusal_never_mints_generation_two(self):
        self.remaining_refusals = 2
        before = await self.refuse_once()
        with self.assertRaises(ThreadBootstrapPrestartRefusal):
            await self.f.n.prepare()
        with self.assertRaisesRegex(OwnerRoutingError, 'bootstrap_recovery_pending'):
            await self.f.n.prepare()
        self.assertEqual([origin.generation for origin in self.origins], [0, 1])
        self.assertEqual(self.f.workers.store.assignment(self.original.id).model_dump(mode='json'), before)

    async def test_historical_missing_proof_stays_unchanged(self):
        self.typed = False
        before = await self.refuse_once()
        self.assertIsNone(self.original.failure)
        with self.assertRaisesRegex(OwnerRoutingError, 'bootstrap_pending'):
            await self.f.n.prepare()
        self.assertEqual(len(self.origins), 1)
        self.assertEqual(self.f.workers.store.assignment(self.original.id).model_dump(mode='json'), before)

    async def test_current_work_proof_mode_and_join_fields_are_required(self):
        before = await self.refuse_once()
        original = self.original
        for field, value in (('intent', 'pending_handoff'),
                             ('handoff_requested_at', 1.),
                             ('admission_guard_joined', False),
                             ('worker_teardown_settled', False),
                             ('attribution_sha256', 'f' * 64),
                             ('generation', 1)):
            with self.subTest(field=field):
                failure = original.failure.model_copy(update={'details': {
                    **original.failure.details, field: value}})
                changed = original.model_copy(update={'failure': failure})
                self.f.workers.store.update(lambda state: state.model_copy(update={
                    'assignments': [changed if row.id == original.id else row
                                    for row in state.assignments]}))
                with self.assertRaisesRegex(OwnerRoutingError, 'bootstrap_pending'):
                    await self.f.n.prepare()
                self.assertEqual(len(self.origins), 1)
        self.f.workers.store.update(lambda state: state.model_copy(update={
            'assignments': [original if row.id == original.id else row for row in state.assignments]}))
        self.assertEqual(self.f.workers.store.assignment(original.id).model_dump(mode='json'), before)

    async def test_changed_owner_at_recovery_create_guard_cannot_create(self):
        before = await self.refuse_once()
        assignments = len(self.f.workers.list_assignments(self.f.actor))
        def change_owner():
            self.state.current_owner = self.state.next_owner = 'james'
            self.f.n.fixture.f.env.states.put(self.state.ref, self.state)
        self.recovery_mutation = change_owner
        with self.assertRaisesRegex(OwnerRoutingError, 'preparation_changed'):
            await self.f.n.prepare()
        self.assertEqual(len(self.f.workers.list_assignments(self.f.actor)), assignments)
        self.assertEqual(self.f.workers.store.assignment(self.original.id).model_dump(mode='json'), before)
