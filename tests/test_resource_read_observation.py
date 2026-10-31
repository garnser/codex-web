"""Explicit Resource observations retain the complete canonical reader contract."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
import tempfile
from types import MethodType
import unittest
from unittest.mock import patch

from codex_web.identity import (
    AuthenticationActor, AuthenticationAssurance, MembershipRole, PrincipalKind,
)
from codex_web.resources import Resource, ResourceAlias, ResourceCatalogState, ResourceUpdate
from codex_web.services.resources import ResourceCatalogError, ResourceCatalogService, ResourceNotFoundError
from codex_web.storage.resource_catalog import ResourceCatalogStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class ResourceReadObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.backend = SQLiteStateStore(Path(self.temp.name) / 'state.sqlite3')
        self.store = ResourceCatalogStore(self.backend)
        self.service = ResourceCatalogService(self.store)
        self.actor = AuthenticationActor(identity_id='private-owner', principal_kind=PrincipalKind.HUMAN,
            organization_id='private-org', workspace_id='private-workspace',
            roles=(MembershipRole.OWNER,), assurance=AuthenticationAssurance.MFA)
        self.resource = Resource(id='resource-private', organization_id=self.actor.organization_id,
            workspace_id=self.actor.workspace_id, resource_type='repository', name='Original',
            aliases=[ResourceAlias(namespace='provider', provider='gitlab', value='group/private')])
        self.store.update(lambda state: state.model_copy(update={'resources': [self.resource]}))

    @contextmanager
    def reads(self):
        original = self.backend.get
        calls = []
        def get(namespace):
            calls.append(namespace)
            return original(namespace)
        with patch.object(self.backend, 'get', get):
            yield calls

    def test_unused_view_is_lazy_and_existing_gets_share_one_load(self):
        with self.reads() as calls:
            self.assertEqual(self.service.observe_reads(lambda _view: 'unused'), 'unused')
            self.assertEqual(calls, [])
            def lookup(view):
                first = view.get(self.resource.id, self.actor)
                second = view.get(self.resource.id, self.actor)
                self.assertEqual(first, second)
                self.assertIsNot(first, second)
                return first
            observed = self.service.observe_reads(lookup)
            self.assertEqual(calls, [self.store.namespace])
        with self.reads() as ordinary:
            self.assertEqual(self.service.get(self.resource.id, self.actor), observed)
            self.service.get(self.resource.id, self.actor)
            self.assertEqual(ordinary, [self.store.namespace] * 2)

    def test_each_lookup_checks_actor_scope_and_first_scoped_match(self):
        foreign = self.resource.model_copy(update={'organization_id': 'other-org', 'name': 'Foreign'})
        later = self.resource.model_copy(update={'name': 'Later duplicate'})
        self.store.update(lambda state: state.model_copy(update={'resources': [foreign, self.resource, later]}))
        stranger = self.actor.model_copy(update={'workspace_id': 'other-workspace'})
        def lookup(view):
            self.assertEqual(view.get(self.resource.id, self.actor).name, 'Original')
            with self.assertRaises(ResourceNotFoundError):
                view.get(self.resource.id, stranger)
            with self.assertRaises(ResourceNotFoundError):
                view.get('missing-resource', self.actor)
        self.service.observe_reads(lookup)
        self.assertEqual(self.service.get(self.resource.id, self.actor).name, 'Original')

    def test_returned_nested_models_do_not_mutate_observation_or_storage(self):
        def lookup(view):
            first = view.get(self.resource.id, self.actor)
            first.name = 'Caller mutation'
            first.aliases[0].value = 'private-mutated'
            first.aliases.clear()
            current = view.get(self.resource.id, self.actor)
            self.assertEqual(current.name, 'Original')
            self.assertEqual(current.aliases[0].value, 'group/private')
        self.service.observe_reads(lookup)
        self.assertEqual(self.service.get(self.resource.id, self.actor), self.resource)

    def test_next_callback_observes_resource_changes_and_removal(self):
        read = lambda view: view.get(self.resource.id, self.actor)
        self.assertEqual(self.service.observe_reads(read).lifecycle, 'active')
        self.service.update(self.resource.id, ResourceUpdate(lifecycle='disabled'), actor=self.actor)
        # Metadata lookup has never itself granted lifecycle/action authority.
        self.assertEqual(self.service.observe_reads(read).lifecycle, 'disabled')
        self.store.update(lambda state: state.model_copy(update={'resources': []}))
        with self.assertRaises(ResourceNotFoundError):
            self.service.observe_reads(read)

    def test_full_decode_rejects_unrelated_malformed_rows_and_envelope(self):
        original = self.store.load().model_dump(mode='json')
        invalid = dict(original, resources=[*original['resources'], {'id': 'unrelated-broken'}])
        for raw in (invalid, {'schema_version': '99.0', 'resources': []}, []):
            with self.subTest(kind=type(raw).__name__):
                self.backend.put(self.store.namespace, raw)
                with self.assertRaises(ValueError):
                    self.service.get(self.resource.id, self.actor)
                with self.assertRaises(ValueError):
                    self.service.observe_reads(lambda view: view.get(self.resource.id, self.actor))

    def test_legacy_migration_and_missing_namespace_use_full_decoder(self):
        raw = self.store.load().model_dump(mode='json')
        raw['schema_version'] = '1.0'
        self.backend.put(self.store.namespace, raw)
        with self.reads() as calls:
            self.assertEqual(self.service.observe_reads(lambda view: view.get(self.resource.id, self.actor)),
                             self.resource)
            self.assertEqual(calls, [self.store.namespace])
        empty = ResourceCatalogService(ResourceCatalogStore(SQLiteStateStore(
            Path(self.temp.name) / 'empty.sqlite3')))
        with self.assertRaises(ResourceNotFoundError):
            empty.observe_reads(lambda view: view.get(self.resource.id, self.actor))

    def test_foreign_closed_thread_and_replaced_store_views_refuse(self):
        other = ResourceCatalogService(self.store)
        captured = []
        def lookup(view):
            captured.append(view)
            self.service.require_read_observation(view)
            with self.assertRaises(ResourceCatalogError):
                other.get(self.resource.id, self.actor, _read_observation=view)
            with ThreadPoolExecutor(max_workers=1) as executor:
                task = executor.submit(view.get, self.resource.id, self.actor)
                with self.assertRaises(ResourceCatalogError):
                    task.result(timeout=5)
            old = self.service.store
            self.service.store = ResourceCatalogStore(self.backend)
            try:
                with self.assertRaises(ResourceCatalogError):
                    view.get(self.resource.id, self.actor)
            finally:
                self.service.store = old
            return view.get(self.resource.id, self.actor)
        self.service.observe_reads(lookup)
        with self.assertRaises(ResourceCatalogError):
            captured[0].get(self.resource.id, self.actor)
        with self.assertRaises(ResourceCatalogError):
            self.service.require_read_observation(object())

    def test_callback_exception_is_not_replayed_and_view_is_closed(self):
        failure = RuntimeError('private original failure')
        entered = []
        def fail(view):
            entered.append(view)
            view.get(self.resource.id, self.actor)
            raise failure
        with self.assertRaises(RuntimeError) as caught, self.reads() as calls:
            self.service.observe_reads(fail)
        self.assertIs(caught.exception, failure)
        self.assertEqual(len(entered), 1)
        self.assertEqual(calls, [self.store.namespace])
        with self.assertRaises(ResourceCatalogError):
            entered[0].get(self.resource.id, self.actor)

    def test_async_and_generator_results_are_rejected_without_execution(self):
        entered = []
        async def coroutine():
            entered.append('coroutine')
        def generator():
            entered.append('generator')
            yield None
        async def async_generator():
            entered.append('async-generator')
            yield None
        for make in (coroutine, generator, async_generator):
            result = make()
            with self.subTest(kind=make.__name__), self.reads() as calls:
                with self.assertRaisesRegex(ValueError, 'must be synchronous'):
                    self.service.observe_reads(lambda _view: result)
                self.assertEqual(calls, [])
            if make is coroutine:
                self.assertIsNone(result.cr_frame)
            elif make is generator:
                self.assertIsNone(result.gi_frame)
            else:
                asyncio.run(result.aclose())
        self.assertEqual(entered, [])

    def test_custom_service_and_store_callbacks_receive_no_view(self):
        owners = ((self.service, 'get'), (self.store, 'load'), (self.store, '_decode'))
        for owner, name in owners:
            original = getattr(owner, name)
            with self.subTest(method=name):
                calls = []
                def custom(*args, **kwargs):
                    calls.append(name)
                    return original(*args, **kwargs)
                def fallback(view):
                    self.assertIsNone(view)
                    return self.service.get(self.resource.id, self.actor)
                with patch.object(owner, name, custom):
                    self.assertEqual(self.service.observe_reads(fallback), self.resource)
                self.assertEqual(calls, [name])

    def test_class_overrides_subclasses_and_foreign_bound_methods_fall_back(self):
        failure = RuntimeError('private custom refusal')
        for owner, name in ((self.service, 'get'), (self.store, 'load'), (self.store, '_decode')):
            with self.subTest(class_override=name):
                def fallback(view):
                    self.assertIsNone(view)
                    return self.service.get(self.resource.id, self.actor)
                with patch.object(type(owner), name, side_effect=failure):
                    with self.assertRaises(RuntimeError) as caught:
                        self.service.observe_reads(fallback)
                self.assertIs(caught.exception, failure)
        for owner, name in ((self.service, 'get'), (self.store, 'load'), (self.store, '_decode')):
            with self.subTest(rebound=name):
                foreign = type(owner)(self.store if owner is self.service else self.backend)
                method = MethodType(getattr(type(owner), name), foreign)
                with patch.object(owner, name, method):
                    self.assertIsNone(self.service.observe_reads(lambda view: view))
        for owner in (self.service, self.store):
            original = type(owner)
            owner.__class__ = type('CustomResourceOwner', (original,), {})
            try:
                self.assertIsNone(self.service.observe_reads(lambda view: view))
            finally:
                owner.__class__ = original

    def test_custom_scope_and_legacy_store_fall_back_before_read(self):
        with patch.object(self.service, '_scope_matches', return_value=False), self.reads() as calls:
            def fallback(view):
                self.assertIsNone(view)
                return self.service.get(self.resource.id, self.actor)
            with self.assertRaises(ResourceNotFoundError):
                self.service.observe_reads(fallback)
            self.assertEqual(calls, [self.store.namespace])
        class LegacyStore:
            def load(_self):
                return ResourceCatalogState(resources=[self.resource])
        legacy = ResourceCatalogService(LegacyStore())
        def legacy_lookup(view):
            self.assertIsNone(view)
            return legacy.get(self.resource.id, self.actor)
        self.assertEqual(legacy.observe_reads(legacy_lookup), self.resource)
