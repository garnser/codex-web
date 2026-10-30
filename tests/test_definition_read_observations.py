"""Fresh synchronous Definition data, with all canonical judgments rerun."""
import concurrent.futures
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from codex_web.definitions import DefinitionLifecycle, DefinitionPublishRequest, DefinitionReference
from codex_web.execution_role_models import EXECUTION_ROLE_CATALOG_ID, EXECUTION_ROLE_CATALOG_KIND
from codex_web.services.definitions import DefinitionError, DefinitionNotFoundError
from codex_web.services.skills import SkillResolutionFailure
from tests import test_definition_registry as registry_fixture
from tests import test_skill_procedures as skill_fixture
from tests import test_native_provider_preparation as native_fixture


class DefinitionObservationTests(unittest.TestCase):
    setUp = registry_fixture.DefinitionRegistryTests.setUp
    tearDown = registry_fixture.DefinitionRegistryTests.tearDown
    _draft = registry_fixture.DefinitionRegistryTests._draft

    def test_one_physical_read_and_fresh_owned_models_per_lookup(self):
        record = self._draft()
        original = self.state.get
        reads = []
        def get(namespace):
            reads.append(namespace)
            return original(namespace)
        def inspect(view):
            first = view.get_record(record.record_id)
            first.payload['roles'][0]['description'] = 'caller mutation'
            second = view.get_record(record.record_id)
            self.assertEqual(second.payload, record.payload)
            self.assertEqual(view.list_records(kind=record.kind)[0].record_id, record.record_id)
        with patch.object(self.state, 'get', get):
            self.service.observe_reads(inspect)
        self.assertEqual(reads, [self.store.namespace])

    def test_closed_foreign_thread_and_async_escape_refused(self):
        record = self._draft()
        escaped = []
        def inspect(view):
            escaped.append(view)
            other = registry_fixture.DefinitionRegistryService(self.store)
            with self.assertRaises(DefinitionError):
                other.require_read_observation(view)
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(view.get_record, record.record_id)
                with self.assertRaises(ValueError):
                    future.result()
        self.service.observe_reads(inspect)
        with self.assertRaises(ValueError):
            escaped[0].get_record(record.record_id)
        async def deferred():
            return record
        coroutine = deferred()
        with self.assertRaises(ValueError):
            self.service.observe_reads(lambda view: coroutine)
        self.assertIsNone(coroutine.cr_frame)
        def generator():
            yield record
        with self.assertRaises(ValueError):
            self.service.observe_reads(lambda view: generator())

    def test_selected_duplicates_validate_all_and_unrelated_malformed_is_skipped(self):
        record = self._draft()
        raw = self.state.get(self.store.namespace)
        raw['records'].append({'record_id': 'unrelated'})
        self.state.put(self.store.namespace, raw)
        self.assertEqual(self.service.observe_reads(lambda view: view.get_record(record.record_id)).record_id, record.record_id)
        duplicate = dict(raw['records'][0]); duplicate['revision'] = 'invalid'
        raw['records'].append(duplicate)
        self.state.put(self.store.namespace, raw)
        with self.assertRaises(ValueError):
            self.service.observe_reads(lambda view: view.get_record(record.record_id))

    def test_observed_resolve_ignores_old_cache_and_next_call_sees_revocation(self):
        record = self.service.publish(self._draft().record_id, DefinitionPublishRequest(actor='publisher'))
        request = dict(definition_id=EXECUTION_ROLE_CATALOG_ID, kind=EXECUTION_ROLE_CATALOG_KIND)
        self.assertEqual(self.service.resolve(**request).record_id, record.record_id)
        self.store.update(lambda records: [r.model_copy(update={'lifecycle': DefinitionLifecycle.DISABLED}) for r in records])
        with self.assertRaises(DefinitionNotFoundError):
            self.service.observe_reads(lambda view: view.resolve(**request))
        self.assertTrue(self.service._cache)  # No ordinary cache rewrite was needed.

    def test_custom_decoder_and_missing_store_optin_fall_back(self):
        self._draft()
        original = self.store._decode
        calls = []
        def custom(raw, **kwargs):
            calls.append(raw)
            return original(raw, **kwargs)
        with patch.object(self.store, '_decode', custom):
            self.assertIsNone(self.service.observe_reads(lambda view: view))
            self.service.list_records()
        self.assertEqual(len(calls), 1)
        with patch.object(self.service, 'store', SimpleNamespace(load=self.store.load)):
            self.assertFalse(self.service.can_observe_reads())
            self.assertIsNone(self.service.observe_reads(lambda view: view))

    def test_legacy_migration_fallback_and_future_version_refusal(self):
        record = self._draft()
        raw = self.state.get(self.store.namespace)
        raw['schema_version'] = '0.0'
        self.state.put(self.store.namespace, raw)
        self.assertEqual(self.service.observe_reads(lambda view: self.service.get_record(record.record_id) if view is None else None).record_id, record.record_id)
        raw['schema_version'] = '99.0'
        self.state.put(self.store.namespace, raw)
        with self.assertRaises(ValueError):
            self.service.observe_reads(lambda view: self.service.get_record(record.record_id))


class SkillObservationTests(unittest.TestCase):
    setUp = skill_fixture.ProcedureTests.setUp
    tearDown = skill_fixture.ProcedureTests.tearDown
    draft = skill_fixture.ProcedureTests.draft
    publish = skill_fixture.ProcedureTests.publish

    def reference(self):
        return DefinitionReference.model_validate(self.publish(self.draft())['definitionReference'])

    def test_required_procedure_one_read_with_current_security_and_next_call_denials(self):
        ref = self.reference()
        original = self.state.get
        namespaces = []
        def get(namespace):
            namespaces.append(namespace)
            return original(namespace)
        with patch.object(self.state, 'get', get):
            self.assertEqual(self.skills.validate_required_procedure(ref, actor=self.admin), ref)
        self.assertEqual(namespaces.count(self.definitions.store.namespace), 1)
        for lifecycle, code in ((DefinitionLifecycle.DISABLED, 'procedure_disabled'), (DefinitionLifecycle.QUARANTINED, 'procedure_quarantined')):
            self.definitions.store.update(lambda records: [r.model_copy(update={'lifecycle': lifecycle}) if r.record_id == ref.record_id else r for r in records])
            with self.assertRaises(SkillResolutionFailure) as caught:
                self.skills.validate_required_procedure(ref, actor=self.admin)
            self.assertEqual(caught.exception.code, code)
        self.definitions.store.update(lambda records: [r.model_copy(update={'lifecycle': DefinitionLifecycle.PUBLISHED}) if r.record_id == ref.record_id else r for r in records])
        self.now = 200
        with self.assertRaises(SkillResolutionFailure) as caught:
            self.skills.validate_required_procedure(ref, actor=self.admin)
        self.assertEqual(caught.exception.code, 'procedure_expired')

    def test_foreign_checksum_and_current_security_refuse(self):
        ref = self.reference()
        with self.assertRaises(SkillResolutionFailure):
            self.skills.validate_required_procedure(ref, actor=self.foreign)
        raw = self.state.get(self.definitions.store.namespace)
        next(r for r in raw['records'] if r['record_id'] == ref.record_id)['payload']['instructions'] = 'tampered'
        self.state.put(self.definitions.store.namespace, raw)
        with self.assertRaises(SkillResolutionFailure):
            self.skills.validate_required_procedure(ref, actor=self.admin)

    def test_current_security_policy_change_blocks_next_observation(self):
        from codex_web.skill_security import SkillSecurityPolicyUpdate
        ref = self.reference()
        self.assertEqual(self.skills.validate_required_procedure(ref, actor=self.admin), ref)
        current = self.security.policy(self.admin).model_dump(mode='python')
        self.security.update_policy(SkillSecurityPolicyUpdate(**{**current, 'scan_locally_authored': True},
            reason='Require current scan for local procedures'), actor=self.admin)
        with self.assertRaises(SkillResolutionFailure):
            self.skills.validate_required_procedure(ref, actor=self.admin)

    def test_unsafe_provenance_fallback_runs_after_view_closed(self):
        ref = self.reference()
        raw = self.state.get(self.definitions.store.namespace)
        from codex_web.definitions import definition_checksum
        record = next(r for r in raw['records'] if r['record_id'] == ref.record_id)
        record['payload']['provenance'] = None
        record['checksum'] = definition_checksum(definition_id=record['definition_id'], kind=record['kind'],
            definition_schema_version=record['definition_schema_version'], payload=record['payload'])
        self.state.put(self.definitions.store.namespace, raw)
        from codex_web.services import definitions as definition_module
        active = []
        original = definition_module._DefinitionReadObservation.close
        def closed(view):
            active.append(view)
            return original(view)
        def fallback(reader):
            self.assertIsNone(reader)
            self.assertEqual(len(active), 1)
            with self.assertRaises(ValueError):
                active[0].get_record(ref.record_id)
            return 'legacy'
        with patch.object(definition_module._DefinitionReadObservation, 'close', closed):
            self.assertEqual(self.skills.observe_definition_reads((ref,), fallback), 'legacy')

    def test_custom_security_and_definition_owners_keep_legacy_signatures(self):
        ref = self.reference()
        original = self.security.assert_allowed
        calls = []
        def custom(view, *, actor, phase):
            calls.append(phase)
            return original(view, actor=actor, phase=phase)
        with patch.object(self.security, 'assert_allowed', custom):
            self.assertFalse(self.skills.can_observe_definition_reads())
            self.assertEqual(self.skills.validate_required_procedure(ref, actor=self.admin), ref)
        self.assertIn('assignment', calls)
        with patch.object(self.skills, 'security', SimpleNamespace(assert_allowed=custom, summary=self.security.summary)):
            self.assertFalse(self.skills.can_observe_definition_reads())
            self.assertEqual(self.skills.validate_required_procedure(ref, actor=self.admin), ref)
        with patch.object(self.skills, 'definitions', SimpleNamespace()):
            self.assertFalse(self.skills.can_observe_definition_reads())


class NativeObservationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = native_fixture.NativeProviderPreparationTests()
        await self.fixture.asyncSetUp()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def test_two_selected_skills_validate_together_without_reader_shadowing(self):
        from codex_web.skills import SkillCreate, SkillPublish
        n = self.fixture
        f = n.fixture
        created = f.skills.create(SkillCreate(skill_id='second-native-observation', name='Second native observation',
            instructions='Use the same registered controlled procedure.', operational_procedure=n.content), actor=f.actor)
        published = f.skills.publish(created['skillId'], created['recordId'], SkillPublish(), actor=f.actor)
        second = DefinitionReference.model_validate(published['definitionReference'])
        refs = (n.reference, second)
        original = f.skills.definitions.store.store.get
        reads = []
        def get(namespace):
            if namespace == f.skills.definitions.store.namespace:
                reads.append(namespace)
            return original(namespace)
        with patch.object(f.skills.definitions.store.store, 'get', get):
            proof = n.owner.validate_native_preparation(f.native_assignment, refs, f.actor)
        self.assertTrue(proof)
        self.assertEqual(len(reads), 1)
        self.assertEqual(f.ops.calls, [])
