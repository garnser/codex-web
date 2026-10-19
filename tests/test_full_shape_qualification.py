"""Ownership, fail-closed evidence and allocation admission for the scale owner."""
import fcntl
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.ci import run_full_shape_preservation as owner


class FullShapeQualificationTests(unittest.TestCase):
    def test_inventory_distinguishes_scale_targets_from_inherited_cases(self):
        from tests import test_recovery_storage_operator as encrypted
        loader = unittest.TestLoader()
        suite = loader.loadTestsFromNames(owner.TARGETS)
        self.assertFalse(loader.errors)
        self.assertEqual(suite.countTestCases(), 3)
        full = encrypted.FullShapeEncryptedStorageTests
        inherited = set(loader.getTestCaseNames(encrypted.PostgresStorageOperatorTests))
        self.assertEqual(len(inherited), 26)
        self.assertEqual(set(loader.getTestCaseNames(full)) - inherited,
                         {'test_full_current_shape_governed_encrypted_capture_and_isolated_verify'})
        self.assertEqual(len(loader.getTestCaseNames(full)), 27)
        self.assertIn(owner.TARGETS[-1].rsplit('.', 1)[1], full.__dict__)

    def test_skips_missing_and_unexpected_cases_never_qualify(self):
        good = {'tests': list(owner.TARGETS), 'successful': list(owner.TARGETS),
                'skipped': [], 'failures': [], 'errors': []}
        self.assertTrue(owner.successful(good))
        for field, value in [('tests', []), ('successful', list(owner.TARGETS[:-1])),
                             ('tests', list(owner.TARGETS) + ['unowned']),
                             ('skipped', ['missing_backend']), ('failures', ['failed']),
                             ('errors', ['broken_import'])]:
            with self.subTest(field=field, value=value):
                self.assertFalse(owner.successful({**good, field: value}))

    def test_status_is_read_only_with_deployment_data_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / 'scripts/ci/run_full_shape_preservation.py'
            script.parent.mkdir(parents=True)
            shutil.copyfile(Path(owner.__file__), script)
            shared = root / 'protected'
            shared.mkdir()
            (shared / 'marker').write_text('unchanged')
            (root / 'data').symlink_to(shared, target_is_directory=True)
            result = subprocess.run([sys.executable, str(script), '--status'], cwd='/',
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('NOT EXECUTED', result.stdout)
            report = json.loads(result.stdout.splitlines()[-1])
            self.assertEqual(report['status'], 'not_executed')
            self.assertEqual(report['distinct_targets'], list(owner.TARGETS))
            self.assertTrue((root / 'data').is_symlink())
            self.assertEqual([p.name for p in shared.iterdir()], ['marker'])
            self.assertEqual((shared / 'marker').read_text(), 'unchanged')

    def test_inadequate_disk_and_existing_owner_refuse_before_privileged_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            with patch.object(owner.shutil, 'disk_usage', return_value=SimpleNamespace(free=0)), \
                    patch.object(owner.subprocess, 'run', side_effect=AssertionError('privileged operation')):
                with self.assertRaisesRegex(RuntimeError, '22 GiB'):
                    owner.qualify(parent, 'docker', False)
            with (parent / '.qualification.lock').open('w') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with patch.object(owner.shutil, 'disk_usage', return_value=SimpleNamespace(free=100 * owner.GIB)), \
                        patch.object(owner.subprocess, 'run', side_effect=AssertionError('privileged operation')):
                    with self.assertRaisesRegex(RuntimeError, 'another full-shape qualification'):
                        owner.qualify(parent, 'docker', False)

    def test_internal_child_refuses_ordinary_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, 'isolated data|allocated filesystem'):
                owner.child(Path(directory) / 'tests.json')
