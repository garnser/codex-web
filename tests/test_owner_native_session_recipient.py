"""Original native custody remains authoritative after a bootstrap rebind."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from codex_web.agent_runtime import AgentSession
from codex_web.execution_workers import AssignmentStatus
from codex_web.owner_routing import OwnerRoutingError
from codex_web.storage.agent_sessions import AgentSessionStore
from codex_web.services.owner_recipient_preparation import OwnerRecipientPreparationService
from codex_web.services.thread_bootstrap_bindings import ThreadBootstrapBindingNotFoundError
from codex_web.services.coordination import StateStoreCoordinationBackend
from codex_web.services.replicated_ownership import ReplicatedOwnershipService
from tests import test_owner_routing_recipients as fixtures


class NativeRecipientScopeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = fixtures.CompatibleRecipientTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        self.routing, self.state, self.binding = f.service, f.state, f.f.binding
        self.store = AgentSessionStore(f.f.env.env.state)
        self.routing.sessions = self.store
        self.original = f.assignments['assignment-existing-thread'].model_copy(deep=True,
            update={'id': 'original-native-assignment'})
        self.original.status = AssignmentStatus.CANCELLED
        self.original.lease = None
        f.assignments[self.original.id] = self.original
        current = f.assignments['assignment-existing-thread']
        current.resource_ids = ('platform',)
        current.repository_scope = f.assignments['assignment-platform-thread'].repository_scope
        self.session = AgentSession(organization_id='org-a', workspace_id='workspace-a',
            provider_id='fixture-provider', runtime_id='fixture-runtime', runtime_type='codex',
            provider_native_session_id='existing-thread', project_id='project-a',
            assignment_id=self.original.id, resource_ids=('core',))
        self.store.upsert(self.session)

    def select(self):
        return self.routing.compatible_pin(self.state, self.binding, 'orchestrator',
            writable_repository_ids=('platform',))

    async def test_real_continuity_selects_actual_native_compatible_lane_after_bootstrap_drift(self):
        before = self.session.model_dump(mode='json')
        selected = self.select()
        self.assertIsNotNone(selected, 'current bootstrap scope must not substitute original native custody')
        self.assertIsNotNone(selected.recipient)
        await self.fixture.continuity.dispatch_structured_handoff(self.state, source='work-item-handoff')
        pin = self.fixture.fixture.dispatch.call_args.kwargs['owner_routing_pin']
        self.assertEqual(pin.recipient.thread_id, 'platform-thread')
        self.fixture.validate(pin)
        self.assertEqual(self.store.get(self.session.id, organization_id='org-a', workspace_id='workspace-a').model_dump(mode='json'), before)
        self.fixture.continuity.replace_nonperforming_thread.assert_not_called()

    async def test_final_native_guard_rejects_candidate_original_scope_drift(self):
        pin = self.select()
        self.store.upsert(self.session.model_copy(update={'id': 'recipient-session',
            'provider_native_session_id': 'platform-thread'}))
        with self.assertRaisesRegex(OwnerRoutingError, 'native_scope_incompatible'):
            self.fixture.validate(pin)

    async def test_missing_original_assignment_and_foreign_session_fail_closed(self):
        for changed in ({'assignment_id': 'missing'}, {'project_id': 'foreign-project'}):
            with self.subTest(changed=changed):
                self.store.upsert(self.session.model_copy(update=changed))
                with self.assertRaisesRegex(OwnerRoutingError, 'native_custody_unverified'):
                    self.select()
        self.store.upsert(self.session)

    async def test_ambiguous_native_aliases_never_mean_first_admission(self):
        self.store.upsert(self.session.model_copy(update={'id': 'other-session', 'runtime_id': 'other'}))
        with self.assertRaisesRegex(OwnerRoutingError, 'native_custody_unverified'):
            self.select()

    def prepare_fixture(self):
        f = self.fixture
        self.template = f.assignments.pop('assignment-platform-thread')
        f.bootstraps.pop('platform-thread')
        self.reserved = None
        self.bound = None
        def get_bootstrap(key, actor):
            if self.bound is None:
                raise ThreadBootstrapBindingNotFoundError('not bound')
            self.assertEqual(key, self.bound.bootstrap_id)
            return self.bound
        self.routing.bootstraps.get_by_bootstrap = get_bootstrap
        self.threads = SimpleNamespace(create=AsyncMock(side_effect=self.create))
        self.locks = {}
        ownership = ReplicatedOwnershipService(StateStoreCoordinationBackend(f.f.env.env.state),
            instance_id='native-recipient-fixture')
        self.routing.recipient_preparer = OwnerRecipientPreparationService(routing=self.routing,
            threads=self.threads, existing_assignment=lambda **kwargs: self.reserved,
            ownership=ownership, queue_lock=lambda name: self.locks.setdefault(name, asyncio.Lock()))

    async def create(self, **kwargs):
        # The external thread-creation boundary stands in for the real provider;
        # all routing/session/ownership and final pin checks are canonical here.
        kwargs['admission_guard']()
        token = kwargs['bootstrap_token']
        self.reserved = self.template.model_copy(deep=True)
        self.reserved.execution_id = 'thread-bootstrap-' + token
        self.fixture.assignments[self.reserved.id] = self.reserved
        self.bound = SimpleNamespace(id='fresh-binding', bootstrap_id='bootstrap-' + token,
            thread_id='platform-thread', assignment_id=self.reserved.id,
            execution_id=self.reserved.execution_id,
            execution_workspace_id=self.reserved.execution_workspace_id,
            created_by=self.reserved.created_by, organization_id='org-a', workspace_id='workspace-a')
        self.fixture.bootstraps['platform-thread'] = self.bound
        return {'thread': {'id': 'platform-thread'}}

    async def prepare(self):
        return await self.routing.prepare_compatible_pin(self.state, self.binding, 'orchestrator',
            writable_repository_ids=('platform',))

    async def test_no_live_compatible_lane_creates_one_fresh_context_then_actual_dispatch(self):
        self.prepare_fixture()
        await self.fixture.continuity.dispatch_structured_handoff(self.state, source='work-item-handoff')
        pin = self.fixture.fixture.dispatch.call_args.kwargs['owner_routing_pin']
        self.fixture.validate(pin)
        self.assertEqual(pin.recipient.thread_id, 'platform-thread')
        self.threads.create.assert_awaited_once()
        await self.prepare()
        self.threads.create.assert_awaited_once()
        self.assertEqual(self.store.get(self.session.id, organization_id='org-a', workspace_id='workspace-a'), self.session)
        self.assertEqual(self.fixture.f.binding, self.binding)

    async def test_unknown_create_reserves_original_and_never_creates_again(self):
        self.prepare_fixture()
        async def unknown(**kwargs):
            kwargs['admission_guard']()
            self.reserved = self.template.model_copy(deep=True,
                update={'execution_id': 'thread-bootstrap-' + kwargs['bootstrap_token']})
            raise TimeoutError('provider outcome unavailable')
        self.threads.create.side_effect = unknown
        with self.assertRaises(TimeoutError):
            await self.prepare()
        # Delivery bookkeeping changes do not mint a new logical provider call.
        self.state.handoff.reason = 'Updated current observations, same genuine nonce'
        self.fixture.f.env.states.put(self.state.ref, self.state)
        with self.assertRaisesRegex(OwnerRoutingError, 'bootstrap_pending'):
            await self.prepare()
        self.threads.create.assert_awaited_once()
        self.assertEqual(self.store.get(self.session.id, organization_id='org-a', workspace_id='workspace-a'), self.session)

    async def test_existing_compatible_lane_does_not_prepare_any_context(self):
        self.routing.recipient_preparer = SimpleNamespace(prepare=AsyncMock())
        pin = await self.prepare()
        self.assertEqual(pin.recipient.thread_id, 'platform-thread')
        self.routing.recipient_preparer.prepare.assert_not_awaited()

    async def test_concurrent_same_original_preparation_creates_only_once(self):
        self.prepare_fixture()
        pins = await asyncio.gather(self.prepare(), self.prepare())
        self.assertEqual([pin.recipient.thread_id for pin in pins],
            ['platform-thread', 'platform-thread'])
        self.threads.create.assert_awaited_once()

    async def test_original_custody_still_live_never_authorizes_new_scope(self):
        self.original.status = AssignmentStatus.RUNNING
        with self.assertRaisesRegex(OwnerRoutingError, 'native_custody_unverified'):
            self.select()

    async def test_source_changes_at_actual_create_guard_prevent_provider_creation(self):
        self.prepare_fixture()
        async def changed(**kwargs):
            self.fixture.assignments['assignment-existing-thread'].created_by = 'foreign-actor'
            kwargs['admission_guard']()
            self.fail('changed source reached provider creation')
        self.threads.create.side_effect = changed
        with self.assertRaises(OwnerRoutingError):
            await self.prepare()
        self.assertIsNone(self.reserved)
        self.assertEqual(self.store.get(self.session.id, organization_id='org-a',
            workspace_id='workspace-a'), self.session)

    async def test_source_fence_changes_at_actual_create_guard_refuse(self):
        self.prepare_fixture()
        async def changed(**kwargs):
            self.fixture.assignments['assignment-existing-thread'].fence += 1
            kwargs['admission_guard']()
            self.fail('changed source fence reached provider creation')
        self.threads.create.side_effect = changed
        with self.assertRaisesRegex(OwnerRoutingError, 'preparation_changed'):
            await self.prepare()
        self.assertIsNone(self.reserved)
