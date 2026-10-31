"""Explicit identity data observations never authenticate or retain authority."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
from types import MethodType
import unittest
from unittest.mock import patch

from codex_web.identity import (
    DEFAULT_HUMAN_IDENTITY_ID, Membership, MembershipRole,
    PrincipalKind, ServiceIdentity, TenantScope,
)
from codex_web.services.identity import (
    AuthenticationError, IdentityError, IdentityService, TenantIsolationError,
)
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class IdentityReadObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.backend = SQLiteStateStore(Path(self.temp.name) / 'identity.sqlite3')
        self.store = IdentityStateStore(self.backend)
        self.service = IdentityService(self.store)
        self.service.bootstrap_local()
        self.scope = TenantScope()
        self.identity_id = DEFAULT_HUMAN_IDENTITY_ID

    def tearDown(self):
        self.temp.cleanup()

    def test_unused_view_is_lazy_and_repeated_lookups_have_one_physical_read(self):
        original = self.backend.get
        reads = []
        def get(namespace):
            reads.append(namespace)
            return original(namespace)
        with patch.object(self.backend, 'get', get):
            self.assertTrue(self.service.can_observe_reads())
            self.service.observe_reads(lambda view: self.assertIsNotNone(view))
            self.assertEqual(reads, [])
            def inspect(view):
                first = view.actor_for_identity(self.identity_id, scope=self.scope)
                second = view.actor_for_identity(self.identity_id, scope=self.scope)
                self.assertEqual(first.model_dump(exclude={'authenticated_at'}),
                                 second.model_dump(exclude={'authenticated_at'}))
                self.assertGreaterEqual(second.authenticated_at, first.authenticated_at)
                self.assertIsNot(first, second)
                # Each resolution gets a fresh owned state, not mutable shared DTOs.
                copy = view._raw.load(self.store)
                copy.memberships.clear()
                self.assertEqual(view.actor_for_identity(self.identity_id, scope=self.scope)
                                 .model_dump(exclude={'authenticated_at'}),
                                 first.model_dump(exclude={'authenticated_at'}))
            self.service.observe_reads(inspect)
        self.assertEqual(reads, [self.store.namespace])

    def test_state_reader_is_lazy_owned_and_shared_with_actor_resolution(self):
        original = self.backend.get
        reads = []
        def get(namespace):
            reads.append(namespace)
            return original(namespace)
        def inspect(view):
            first = view.state()
            first.humans.clear()
            self.assertTrue(view.state().humans)
            self.assertEqual(view.actor_for_identity(self.identity_id, scope=self.scope).identity_id, self.identity_id)
            with self.assertRaises(IdentityError):
                IdentityService(self.store).state(_read_observation=view)
        with patch.object(self.backend, 'get', get):
            self.service.observe_reads(inspect)
        self.assertEqual(reads, [self.store.namespace])
        with patch.object(self.service, 'state', lambda: None):
            self.service.observe_reads(lambda view: self.assertIsNone(view))

    def test_active_view_refuses_changed_state_or_decoder_hooks_before_custom_effects(self):
        def inspect(view):
            view.state()
            with patch.object(self.service, 'state', side_effect=AssertionError('custom state entered')):
                with self.assertRaises(IdentityError):
                    view.state()
            with patch.object(self.store, '_decode', side_effect=AssertionError('custom decoder entered')):
                with self.assertRaises(IdentityError):
                    view.actor_for_identity(self.identity_id, scope=self.scope)
        self.service.observe_reads(inspect)

    def test_lazy_first_use_observes_change_before_first_lookup(self):
        def inspect(view):
            def revoke(state):
                state.humans[0].disabled_at = 1
                return state
            self.store.update(revoke)
            with self.assertRaises(AuthenticationError):
                view.actor_for_identity(self.identity_id, scope=self.scope)
        self.service.observe_reads(inspect)

    def test_next_observation_sees_membership_revocation(self):
        self.service.observe_reads(lambda view: view.actor_for_identity(self.identity_id, scope=self.scope))
        def revoke(state):
            for membership in state.memberships:
                membership.revoked_at = 1
            return state
        self.store.update(revoke)
        with self.assertRaises(TenantIsolationError):
            self.service.observe_reads(lambda view: view.actor_for_identity(self.identity_id, scope=self.scope))

    def test_tenant_and_principal_judgments_rerun_and_no_service_scopes_added(self):
        def add(state):
            state.services.append(ServiceIdentity(id='test-service', name='Test'))
            state.memberships.append(Membership(identity_id='test-service',
                principal_kind=PrincipalKind.SERVICE, organization_id='local',
                workspace_id='default', roles=[MembershipRole.MEMBER]))
            return state
        self.store.update(add)
        def inspect(view):
            actor = view.actor_for_identity('test-service', scope=self.scope)
            self.assertEqual(actor.principal_kind, PrincipalKind.SERVICE)
            self.assertEqual(actor.service_scopes, ())
            with self.assertRaises(TenantIsolationError):
                view.actor_for_identity('test-service', scope=TenantScope(organization_id='foreign'))
            with self.assertRaises(AuthenticationError):
                view.actor_for_identity('missing', scope=self.scope)
        self.service.observe_reads(inspect)

    def test_ambiguous_identity_and_unrelated_malformed_row_still_fail(self):
        def ambiguous(state):
            state.services.append(ServiceIdentity(id=self.identity_id, name='Ambiguous'))
            return state
        self.store.update(ambiguous)
        with self.assertRaisesRegex(AuthenticationError, 'ambiguous'):
            self.service.observe_reads(lambda view: view.actor_for_identity(self.identity_id, scope=self.scope))
        raw = self.backend.get(self.store.namespace)
        raw['humans'].append({'id': 'unrelated', 'display_name': None})
        self.backend.put(self.store.namespace, raw)
        with self.assertRaises(ValueError):
            self.service.observe_reads(lambda view: view.actor_for_identity(self.identity_id, scope=self.scope))

    def test_full_legacy_envelope_uses_canonical_migration(self):
        raw = self.backend.get(self.store.namespace)
        raw['schema_version'] = '1.3'
        raw.pop('authentication_policies', None)
        raw.pop('authentication_policy_events', None)
        self.backend.put(self.store.namespace, raw)
        expected = self.service.actor_for_identity(self.identity_id, scope=self.scope)
        actual = self.service.observe_reads(
            lambda view: view.actor_for_identity(self.identity_id, scope=self.scope))
        self.assertEqual(actual.model_dump(exclude={'authenticated_at'}),
                         expected.model_dump(exclude={'authenticated_at'}))
        self.assertGreaterEqual(actual.authenticated_at, expected.authenticated_at)

    def test_closed_wrong_owner_and_wrong_thread_views_refuse(self):
        escaped = []
        other = IdentityService(self.store)
        def inspect(view):
            escaped.append(view)
            with self.assertRaises(IdentityError):
                other.actor_for_identity(self.identity_id, scope=self.scope, _read_observation=view)
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(view.actor_for_identity, self.identity_id, scope=self.scope)
                with self.assertRaises(IdentityError):
                    future.result()
        self.service.observe_reads(inspect)
        with self.assertRaises(IdentityError):
            escaped[0].actor_for_identity(self.identity_id, scope=self.scope)
        with self.assertRaises(IdentityError):
            self.service.require_read_observation(object())

    def test_backend_replacement_refuses_before_and_after_first_read(self):
        other_backend = SQLiteStateStore(Path(self.temp.name) / 'other.sqlite3')
        for read_first in (False, True):
            with self.subTest(read_first=read_first):
                def inspect(view):
                    if read_first:
                        view.actor_for_identity(self.identity_id, scope=self.scope)
                    self.store.store = other_backend
                    try:
                        with patch.object(other_backend, 'get', side_effect=AssertionError('foreign backend read')):
                            with self.assertRaisesRegex(ValueError, 'foreign'):
                                view.actor_for_identity(self.identity_id, scope=self.scope)
                    finally:
                        self.store.store = self.backend
                self.service.observe_reads(inspect)

    def test_callback_exception_is_not_replayed_and_view_is_closed(self):
        calls = []
        failure = RuntimeError('sentinel')
        def inspect(view):
            calls.append(view)
            raise failure
        with self.assertRaises(RuntimeError) as caught:
            self.service.observe_reads(inspect)
        self.assertIs(caught.exception, failure)
        self.assertEqual(len(calls), 1)
        with self.assertRaises(IdentityError):
            calls[0].actor_for_identity(self.identity_id, scope=self.scope)

    def test_coroutine_generator_and_async_generator_cannot_escape(self):
        async def coroutine():
            return 1
        def generator():
            yield 1
        async def async_generator():
            yield 1
        for factory in (coroutine, generator, async_generator):
            with self.subTest(factory=factory.__name__):
                with self.assertRaisesRegex(ValueError, 'synchronous'):
                    self.service.observe_reads(lambda view: factory())

    def test_custom_actor_class_instance_and_rebound_owner_take_legacy_path(self):
        original = self.service.actor_for_identity
        calls = []
        def custom(identity_id, *, scope):
            calls.append(identity_id)
            return original(identity_id, scope=scope)
        def inspect(view):
            self.assertIsNone(view)
            return self.service.actor_for_identity(self.identity_id, scope=self.scope)
        with patch.object(self.service, 'actor_for_identity', custom):
            self.assertFalse(self.service.can_observe_reads())
            self.service.observe_reads(inspect)
        self.assertEqual(calls, [self.identity_id])
        other = IdentityService(self.store)
        for replacement in (other.actor_for_identity,
                            MethodType(lambda owner, identity_id, *, scope: original(identity_id, scope=scope), self.service)):
            with patch.object(self.service, 'actor_for_identity', replacement):
                self.service.observe_reads(lambda view: self.assertIsNone(view))
        with patch.object(IdentityService, 'actor_for_identity', lambda owner, identity_id, *, scope: original(identity_id, scope=scope)):
            self.service.observe_reads(lambda view: self.assertIsNone(view))

    def test_custom_store_decoder_loader_and_actor_helpers_decline_before_reads(self):
        other = IdentityStateStore(self.backend)
        with patch.object(self.backend, 'get', side_effect=AssertionError('unexpected read')):
            for name in ('load', '_decode'):
                with self.subTest(name=name), patch.object(self.store, name, getattr(other, name)):
                    self.service.observe_reads(lambda view: self.assertIsNone(view))
            for name in ('_actor', '_active_memberships'):
                with self.subTest(name=name), patch.object(self.service, name, lambda *args, **kwargs: None):
                    self.service.observe_reads(lambda view: self.assertIsNone(view))
            with patch.object(IdentityStateStore, '_decode', lambda *args: None):
                self.service.observe_reads(lambda view: self.assertIsNone(view))


if __name__ == '__main__':
    unittest.main()
