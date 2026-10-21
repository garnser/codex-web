from __future__ import annotations

import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from codex_web import preinstall_backup_cli as cli


class OfflineHostGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.lock = self.path / 'deploy.lock'
        self.fd = os.open(self.lock, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, self.fd)
        self.options = SimpleNamespace(unit='private-fixture.service',
            expected_runtime_pid=987654321, runtime_checkout=self.path,
            producer_checkout=self.path, expected_runtime_revision='a'*40,
            expected_producer_revision='a'*40)
        self.active = {'ActiveState': 'active', 'MainPID': '987654321',
            'InvocationID': 'original-invocation', 'ExecMainPID': '987654321',
            'ExecMainStartTimestampMonotonic': '1000', 'ExecMainExitTimestampMonotonic': '0',
            'Result': 'success', 'ExecMainCode': '0', 'ExecMainStatus': '0'}
        self.stopped = {**self.active, 'ActiveState': 'inactive', 'MainPID': '0',
            'InvocationID': '', 'ExecMainExitTimestampMonotonic': '2000', 'ExecMainCode': '1'}
        self.lock_patch = patch.object(cli, 'LOCAL_DEPLOY_LOCK', self.lock)
        self.lock_patch.start()
        self.addCleanup(self.lock_patch.stop)
        self.git_patch = patch.object(cli, 'git', side_effect=lambda _root, *args:
            'a'*40 if args == ('rev-parse', 'HEAD') else '')
        self.git_patch.start()
        self.addCleanup(self.git_patch.stop)
        with patch.object(cli.OfflineHostGuard, 'service', return_value=self.active):
            self.guard = cli.OfflineHostGuard(self.options, self.fd)
        self.guard.stopped = True

    def check(self, values=None):
        with patch.object(self.guard, 'service', return_value=values or self.stopped):
            self.guard.check()

    def test_clean_same_invocation_stop_is_accepted_without_controlling_service(self):
        self.check()
        self.check({**self.stopped, 'InvocationID': 'original-invocation'})

    def test_restart_crash_failed_stop_or_different_main_process_refuses(self):
        for changes in ({'InvocationID': 'new-invocation'}, {'MainPID': '123'},
                {'ExecMainPID': '123'}, {'ExecMainStartTimestampMonotonic': '1001'},
                {'ActiveState': 'failed'}, {'Result': 'core-dump'},
                {'ExecMainCode': '2', 'ExecMainStatus': '11'},
                {'ExecMainExitTimestampMonotonic': '0'}):
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                self.check({**self.stopped, **changes})

    def test_source_or_lock_replacement_refuses(self):
        with patch.object(cli, 'git', return_value='changed'), self.assertRaises(RuntimeError):
            self.check()
        self.lock.unlink()
        self.lock.write_text('replacement')
        with self.assertRaises(RuntimeError):
            self.check()

    def test_wait_for_clean_stop_has_a_finite_deadline(self):
        with patch.object(self.guard, 'service', return_value=self.active), \
                patch.object(cli.time, 'time', return_value=101), \
                patch.object(cli.time, 'sleep') as sleep:
            with self.assertRaisesRegex(RuntimeError, 'expired'):
                self.guard.wait_stopped(100)
        sleep.assert_not_called()


class RecoveryObservationTests(unittest.TestCase):
    def test_actual_installed_executor_requires_typed_zero_counts(self):
        executor = {'accepting': True, 'waiting_or_running': 0, 'inflight_work': 0,
            'quiescence_guard': False}
        observation = {'scheduled_work': {'local_executor': executor}}
        self.assertEqual(cli.require_recovery_idle(observation), executor)
        for changes in ({'accepting': False}, {'waiting_or_running': 1},
                {'waiting_or_running': False}, {'inflight_work': 1},
                {'quiescence_guard': True}):
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                cli.require_recovery_idle({'scheduled_work': {'local_executor': {**executor, **changes}}})
        with self.assertRaises(RuntimeError):
            cli.require_recovery_idle({})

    def test_api_refuses_remote_credentials_redirects_and_non_origin_urls(self):
        for base in ('https://example.com', 'http://localhost', 'http://127.0.0.1/path',
                'http://user:password@127.0.0.1', 'http://127.0.0.1?query=1'):
            with self.subTest(base=base), patch.object(cli, 'build_opener') as opener:
                with self.assertRaises(ValueError):
                    cli.local_api(base, 'private-fixture-token', 'GET', '/api/version')
                opener.assert_not_called()

    def test_durable_checkpoint_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cli.record(root, 'capture-started', {'plan_id': 'private-fixture'})
            original = (root/'capture-started.json').read_bytes()
            with self.assertRaises(FileExistsError):
                cli.record(root, 'capture-started', {'plan_id': 'other'})
            self.assertEqual((root/'capture-started.json').read_bytes(), original)
