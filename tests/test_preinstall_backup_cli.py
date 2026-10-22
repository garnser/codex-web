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
        (self.path/'data').mkdir()
        (self.path/'data/keys').mkdir()
        (self.path/'data/backups').mkdir()
        self.lock = self.path / 'deploy.lock'
        self.fd = os.open(self.lock, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, self.fd)
        self.options = SimpleNamespace(unit='private-fixture.service',
            expected_runtime_pid=987654321, runtime_checkout=self.path,
            producer_checkout=self.path, expected_runtime_revision='a'*40,
            expected_producer_revision='a'*40, key_material_directory=self.path/'data/keys',
            backup_directory=self.path/'data/backups', sqlite_path=self.path/'data/codex-web.db')
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

    def test_successful_sigterm_stop_is_accepted_with_same_runtime_proof(self):
        # A Root-requested systemd stop can report CLD_KILLED/SIGTERM even
        # after uvicorn finishes its normal shutdown; Result remains success.
        stopped = {**self.stopped, 'ExecMainCode': '2', 'ExecMainStatus': '15'}
        self.check(stopped)
        self.check({**stopped, 'InvocationID': 'original-invocation'})
        with patch.object(self.guard, 'service', return_value=stopped), \
                patch.object(cli.time, 'time', return_value=100):
            self.guard.wait_stopped(101)

    def test_sigterm_cannot_hide_failure_replacement_or_a_live_original_process(self):
        stopped = {**self.stopped, 'ExecMainCode': '2', 'ExecMainStatus': '15'}
        for changes in ({'Result': 'timeout'}, {'ActiveState': 'failed'},
                {'InvocationID': 'replacement'}, {'ExecMainPID': '123'},
                {'ExecMainStartTimestampMonotonic': '1001'},
                {'ExecMainExitTimestampMonotonic': '0'}):
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                self.check({**stopped, **changes})
        with patch.object(cli.Path, 'exists', return_value=True), self.assertRaises(RuntimeError):
            self.check(stopped)

    def test_forced_kill_crash_and_other_exit_pairs_are_refused(self):
        for code, status in (('2', '9'), ('2', '11'), ('3', '15'), ('1', '15'),
                ('2', '0'), ('1', '1')):
            with self.subTest(code=code, status=status), self.assertRaises(RuntimeError):
                self.check({**self.stopped, 'ExecMainCode': code, 'ExecMainStatus': status})

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

    def test_alternate_backup_or_key_or_state_configuration_refuses(self):
        alternate = self.path/'alternate'
        alternate.mkdir()
        for name in ('backup_directory', 'key_material_directory', 'sqlite_path'):
            original = getattr(self.options, name)
            with self.subTest(name=name):
                setattr(self.options, name, alternate)
                with self.assertRaisesRegex(RuntimeError, 'canonical runtime storage'):
                    self.check()
                setattr(self.options, name, original)

    def test_runtime_data_or_backend_symlink_retarget_refuses(self):
        (self.path/'data/backups').rename(self.path/'old-backups')
        (self.path/'alternate-backups').mkdir()
        (self.path/'data/backups').symlink_to(self.path/'alternate-backups')
        self.options.backup_directory = self.path/'data/backups'
        with self.assertRaisesRegex(RuntimeError, 'canonical runtime storage'):
            self.check()
        (self.path/'data').rename(self.path/'old-data')
        (self.path/'new-data').mkdir()
        (self.path/'data').symlink_to(self.path/'new-data')
        with self.assertRaisesRegex(RuntimeError, 'runtime data directory changed'):
            self.check()

    def test_wait_for_clean_stop_has_a_finite_deadline(self):
        with patch.object(self.guard, 'service', return_value=self.active), \
                patch.object(cli.time, 'time', return_value=101), \
                patch.object(cli.time, 'sleep') as sleep:
            with self.assertRaisesRegex(RuntimeError, 'expired'):
                self.guard.wait_stopped(100)
        sleep.assert_not_called()

    def test_stop_observation_and_qualification_cannot_cross_freshness_deadline(self):
        for clock in ([99, 101], [99, 99, 101]):
            with self.subTest(clock=clock), patch.object(self.guard, 'service', return_value=self.stopped), \
                    patch.object(self.guard, 'check') as check, \
                    patch.object(cli.time, 'time', side_effect=clock):
                with self.assertRaisesRegex(RuntimeError, 'expired'):
                    self.guard.wait_stopped(100)
                self.assertEqual(check.call_count, 0 if len(clock) == 2 else 1)


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
