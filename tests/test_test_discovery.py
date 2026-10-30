"""Fixture reuse must preserve each concrete class/environment's test ownership."""
import importlib
import inspect
from pathlib import Path
import unittest

from scripts.ci.run_unit_tests import selected_tests


class TestDiscoveryOwnershipTests(unittest.TestCase):
    def test_fixture_consumers_collect_only_their_own_cases(self):
        consumers = {
            'test_explicit_repository_target_qualification': 'ExplicitRepositoryTargetQualificationTests',
            'test_project_readiness_refresh': 'ProjectReadinessRefreshTests',
            'test_usage_history_preservation': 'UsageHistoryPreservationTests',
            'test_preservation_crypto_batch': 'PreservationCryptoBatchTests',
            'test_native_reasoning_admission': 'NativeReasoningAdmissionTests',
            'test_native_reasoning_canonical_bridge': 'NativeCanonicalBridgeTests',
            'test_reasoning_coldstart_provisioning': 'ReasoningColdstartProvisioningTests',
            'test_skill_scanner_completion_preparation': 'SkillScannerCompletionPreparationTests',
            'test_paused_queued_preflight_retention': 'PausedQueuedPreflightRetentionTests',
            'test_thread_legacy_native_custody': 'ThreadLegacyNativeCustodyTests',
            'test_native_procedure_credential_custody': 'NativeProcedureCredentialCustodyTests',
            'test_native_credential_custody_compatibility': 'NativeCredentialCustodyCompatibilityTests',
        }
        loader = unittest.TestLoader()
        for name, owner in consumers.items():
            with self.subTest(module=name):
                module = importlib.import_module('tests.' + name)
                actual = list(self._flatten(loader.loadTestsFromModule(module)))
                expected = list(loader.loadTestsFromTestCase(getattr(module, owner)))
                self.assertTrue(actual)
                self.assertEqual([t.id() for t in actual], [t.id() for t in expected])

    def test_broad_inventory_has_unique_concrete_source_class_and_method(self):
        root = Path(__file__).resolve().parents[1]
        loader = unittest.TestLoader()
        discovered = loader.discover(str(root / 'tests'), pattern='test_*.py', top_level_dir=str(root))
        self.assertFalse(loader.errors, loader.errors)
        owners = {}
        for case in selected_tests(discovered):
            cls = type(case)
            key = (Path(inspect.getsourcefile(cls)).resolve(), cls.__qualname__, case._testMethodName)
            self.assertNotIn(key, owners, f'{case.id()} repeats {owners.get(key)}')
            owners[key] = case.id()
            self.assertTrue(cls.__module__.startswith('tests.'), case.id())
        self.assertTrue(owners)

    @staticmethod
    def _flatten(suite):
        for item in suite:
            if isinstance(item, unittest.TestSuite):
                yield from TestDiscoveryOwnershipTests._flatten(item)
            else:
                yield item
