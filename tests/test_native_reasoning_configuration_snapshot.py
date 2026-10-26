"""Fresh canonical configuration snapshots; private SQLite, no native/app calls."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_web.configuration import (ConfigurationContext, ConfigurationDraftCreate,
    ConfigurationPublishRequest, ConfigurationSpec,
    ConfigurationValueKind, FeatureTargeting)
from codex_web.services.configuration import ConfigurationService
from codex_web.services.native_reasoning_configuration import (FIELDS, PREFIX,
    install_native_reasoning_configuration, resolve_native_reasoning_settings)
from codex_web.services.codex_reasoning_policy import NativeReasoningPolicyError
from codex_web.storage.configuration_registry import ConfigurationRegistryStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class NativeConfigurationSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.private = tempfile.TemporaryDirectory()
        self.addCleanup(self.private.cleanup)
        self.state = SQLiteStateStore(Path(self.private.name) / 'private.sqlite')
        self.registry = ConfigurationRegistryStore(self.state)
        self.service = ConfigurationService(self.registry)
        install_native_reasoning_configuration(self.service)

    def publish(self, key, value, **kwargs):
        draft = self.service.create_draft(ConfigurationDraftCreate(
            key=key, value=value, actor='fixture-admin', reason='private fixture', **kwargs))
        self.service.validate_record(draft.id)
        return self.service.publish(draft.id, ConfigurationPublishRequest(
            actor='fixture-admin', reason='private fixture'))

    def configured(self):
        values = dict(enabled=True, provider_id='fixture-native', model_id='fixture-model',
            concrete_model='fixture-model', agent_profile_id='fixture-profile',
            agent_profile_revision=2, execution_profile_id='fixture-reasoning',
            worker_id='fixture-worker', catalog_path='/private/public-catalog.json',
            catalog_sha256='a' * 64)
        for name, value in values.items():
            self.publish(PREFIX + name, value, scope_type='global')
        return values

    def seed_catalog(self, count):
        self.configured()
        # Eleven native history rows and forty total match Root's closed count,
        # not live state content. Stress rows remain canonical validated DTOs.
        self.publish(PREFIX + 'model_id', 'fixture-model', scope_type='global')
        self.service.register_spec(ConfigurationSpec(key='fixture.unrelated',
            value_kind=ConfigurationValueKind.STRING, default=None))
        base = self.publish('fixture.unrelated', 'private-value', scope_type='global')
        records = self.registry.load()
        for index in range(count - len(records)):
            records.append(base.model_copy(update={'id':f'private-unrelated-{index}',
                'key':f'fixture.unrelated.{index}', 'publish_reason':'private metadata ' + 'x' * 180}))
        self.registry.replace(records)
        self.assertEqual(len(self.registry.load()), count)

    def check_four_fresh_settings(self, count):
        self.seed_catalog(count)
        with patch.object(self.registry, 'load', wraps=self.registry.load) as loads:
            started = time.perf_counter()
            results = [resolve_native_reasoning_settings(self.service) for _ in range(4)]
            elapsed = time.perf_counter() - started
        print('NATIVE_SETTINGS_PROFILE ' + json.dumps({'records':count,
            'current_checks':4, 'catalog_loads':loads.call_count,
            'elapsed_seconds':elapsed, 'private_fixture_only':True}), flush=True)
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(results[0][0].model_id, 'fixture-model')
        self.assertEqual(loads.call_count, 4)

    def test_four_fresh_native_settings_checks_read_one_40_record_catalog_each(self):
        self.check_four_fresh_settings(40)

    def test_four_fresh_native_settings_checks_read_one_4000_record_catalog_each(self):
        self.check_four_fresh_settings(4000)

    def test_fresh_revocation_and_changed_pins_are_observed_next_call(self):
        self.configured()
        first = resolve_native_reasoning_settings(self.service)
        self.publish(PREFIX + 'worker_id', 'replacement-worker', scope_type='global')
        changed = resolve_native_reasoning_settings(self.service)
        self.assertNotEqual(first, changed)
        self.assertEqual(changed[0].worker_id, 'replacement-worker')
        self.publish(PREFIX + 'enabled', False, scope_type='global')
        self.assertIsNone(resolve_native_reasoning_settings(self.service))
        self.publish(PREFIX + 'enabled', True, scope_type='global')
        self.assertEqual(resolve_native_reasoning_settings(self.service)[0].worker_id,
                         'replacement-worker')

    def test_disabled_partial_and_read_errors_remain_fail_closed(self):
        with patch.object(self.registry, 'load', wraps=self.registry.load) as loads:
            self.assertIsNone(resolve_native_reasoning_settings(self.service))
        self.assertEqual(loads.call_count, 1)
        self.publish(PREFIX + 'enabled', True, scope_type='global')
        with self.assertRaises(NativeReasoningPolicyError):
            resolve_native_reasoning_settings(self.service)
        with patch.object(self.registry, 'load', side_effect=OSError('private unavailable')):
            with self.assertRaises(OSError):
                resolve_native_reasoning_settings(self.service)

    def test_batch_preserves_full_resolution_metadata_scope_kill_and_expiry(self):
        specs = (ConfigurationSpec(key='fixture.limit', value_kind='integer', default=1),
            ConfigurationSpec(key='fixture.feature', value_kind='boolean', default=False,
                feature_flag=True, kill_switch_capable=True),
            ConfigurationSpec(key='fixture.expiring', value_kind='boolean', default=False,
                feature_flag=True))
        for spec in specs:
            self.service.register_spec(spec)
        self.publish('fixture.limit', 2, scope_type='global')
        self.publish('fixture.limit', 3, scope_type='project', scope_id='project-one')
        self.publish('fixture.feature', True, scope_type='global')
        self.publish('fixture.feature', False, scope_type='project', scope_id='project-one',
                     force_disabled=True)
        self.publish('fixture.expiring', True, scope_type='global',
            feature_targeting=FeatureTargeting(expires_at=100, cohorts=['selected']))
        keys = tuple(spec.key for spec in specs)
        for project, cohort, now in (('project-one','selected',99),
            ('project-two','selected',99),('project-one','other',99),
            ('project-one','selected',100)):
            with self.subTest(project=project,cohort=cohort,now=now):
                context = ConfigurationContext(project_id=project, cohort=cohort)
                expected = {key:self.service.resolve(key, context, now=now).model_dump()
                            for key in keys}
                with patch.object(self.registry, 'load', wraps=self.registry.load) as loads:
                    batch = self.service.resolve_many(keys, context, now=now)
                self.assertEqual({key:value.model_dump() for key,value in batch.items()}, expected)
                self.assertEqual(loads.call_count, 1)
                self.assertEqual(batch['fixture.limit'].value, 3 if project=='project-one' else 2)
                self.assertEqual(batch['fixture.feature'].value, project!='project-one')
                self.assertEqual(batch['fixture.expiring'].value, cohort=='selected' and now<100)
        self.assertEqual(self.service.resolve_many((), now=99), {})
        with patch.object(self.registry, 'load', wraps=self.registry.load) as loads:
            with self.assertRaises(LookupError):
                self.service.resolve_many(('fixture.limit','fixture.unknown'))
        loads.assert_not_called()

    def test_each_batch_uses_one_coherent_snapshot_not_mixed_publications(self):
        self.configured()
        prior = resolve_native_reasoning_settings(self.service)
        original_load = self.registry.load
        # A publication after this read belongs to the next fresh call. It must
        # not splice a disabled bit into another revision's settings/pins.
        def snapshot_then_revoke():
            snapshot = original_load()
            with patch.object(self.registry, 'load', original_load):
                self.publish(PREFIX + 'enabled', False, scope_type='global')
            return snapshot
        with patch.object(self.registry, 'load', side_effect=snapshot_then_revoke) as loads:
            observed = resolve_native_reasoning_settings(self.service)
        self.assertEqual(loads.call_count, 1)
        self.assertEqual(observed, prior)
        self.assertIsNone(resolve_native_reasoning_settings(self.service))
