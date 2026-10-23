"""Pending handoff scope is distinct from the receiver's current native scope."""
import unittest
from codex_web.execution_workers import AssignmentStatus
from codex_web.owner_routing import OwnerRoutingError
from tests import test_owner_native_session_recipient as native_fixtures


class RequestedNativeScopeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.n = native_fixtures.NativeRecipientScopeTests()
        self.n.setUp()
        self.addCleanup(self.n.doCleanups)
        self.n.prepare_fixture()
        self.source = self.n.fixture.assignments['assignment-existing-thread']
        self.source.resource_ids = self.n.original.resource_ids
        self.source.repository_scope = self.n.original.repository_scope.model_copy(deep=True)
        self.source.status = AssignmentStatus.CANCELLED
        self.source.lease = None

    async def test_cancelled_original_core_receiver_prepares_requested_platform_context(self):
        before = self.source.model_dump(mode='json')
        native = self.n.session.model_dump(mode='json')
        self.assertTrue(self.n.routing.native_scope_compatible(
            self.n.state, self.n.fixture.bootstraps['existing-thread'], self.source))
        await self.n.fixture.continuity.dispatch_structured_handoff(
            self.n.state, source='work-item-handoff')
        self.n.threads.create.assert_awaited_once()
        pin = self.n.fixture.fixture.dispatch.call_args.kwargs['owner_routing_pin']
        self.n.fixture.validate(pin)
        self.assertEqual(pin.recipient.thread_id, 'platform-thread')
        self.assertEqual(self.source.model_dump(mode='json'), before)
        self.assertEqual(self.n.store.get(self.n.session.id,
            organization_id='org-a', workspace_id='workspace-a').model_dump(mode='json'), native)
        self.n.fixture.continuity.replace_nonperforming_thread.assert_not_called()
        await self.n.prepare()
        self.n.threads.create.assert_awaited_once()

    async def test_missing_native_custody_does_not_mint_fresh_context(self):
        self.n.routing.sessions = None
        with self.assertRaisesRegex(OwnerRoutingError, 'compatible_lane_unavailable'):
            await self.n.prepare()
        self.n.threads.create.assert_not_awaited()

    async def test_same_requested_native_scope_keeps_ordinary_cancelled_recovery(self):
        scope = self.n.template.repository_scope.model_copy(deep=True)
        for assignment in (self.n.original, self.source):
            assignment.repository_scope = scope.model_copy(deep=True)
            assignment.resource_ids = ('platform',)
        self.assertIsNone(await self.n.prepare())
        self.n.threads.create.assert_not_awaited()

    async def test_unknown_create_for_same_nonce_is_not_sent_twice(self):
        async def unknown(**kwargs):
            kwargs['admission_guard']()
            self.n.reserved = self.n.template.model_copy(deep=True,
                update={'execution_id': 'thread-bootstrap-' + kwargs['bootstrap_token']})
            raise TimeoutError('provider outcome unavailable')
        self.n.threads.create.side_effect = unknown
        with self.assertRaises(TimeoutError):
            await self.n.prepare()
        with self.assertRaisesRegex(OwnerRoutingError, 'bootstrap_pending'):
            await self.n.prepare()
        self.n.threads.create.assert_awaited_once()

    async def test_without_genuine_pending_handoff_no_fresh_context(self):
        self.n.state.handoff = None
        self.n.fixture.f.env.states.put(self.n.state.ref, self.n.state)
        with self.assertRaisesRegex(OwnerRoutingError, 'preparation_changed'):
            await self.n.prepare()
        self.n.threads.create.assert_not_awaited()

    async def test_original_custody_not_terminal_still_refuses(self):
        self.n.original.status = AssignmentStatus.RUNNING
        with self.assertRaisesRegex(OwnerRoutingError, 'native_custody_unverified'):
            await self.n.prepare()
        self.n.threads.create.assert_not_awaited()

    async def test_extra_original_readonly_scope_is_not_requested_scope(self):
        scope = self.n.template.repository_scope.model_copy(deep=True,
            update={'read_only_repository_ids': ('core',)})
        for assignment in (self.n.original, self.source):
            assignment.repository_scope = scope.model_copy(deep=True)
            assignment.resource_ids = ('platform', 'core')
        pin = await self.n.prepare()
        self.n.fixture.validate(pin)
        self.n.threads.create.assert_awaited_once()
        self.assertEqual(pin.recipient.thread_id, 'platform-thread')
        self.assertEqual(self.source.repository_scope.read_only_repository_ids, ('core',))
