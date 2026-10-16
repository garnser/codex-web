"""Real Git graphs; no provider credentials, network or live workspaces."""
import subprocess
import os
import sys
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from codex_web.services.code_hosts import CodeHostService
from codex_web.code_hosts import CodeHostError


class MaterializationAncestryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'remote'
        self.git('init', '-b', 'main', str(self.repo))
        self.git('-C', str(self.repo), 'config', 'user.name', 'Fixture')
        self.git('-C', str(self.repo), 'config', 'user.email', 'fixture@example.invalid')
        self.commit('base', 'base')
        self.git('-C', str(self.repo), 'checkout', '-b', 'side')
        self.commit('side', 'side')
        self.git('-C', str(self.repo), 'checkout', 'main')
        self.commit('main', 'main')
        self.git('-C', str(self.repo), 'merge', '--no-ff', '-m', 'real merge', 'side')
        self.tip = self.git('-C', str(self.repo), 'rev-parse', 'HEAD').strip()

    @staticmethod
    def git(*args):
        return subprocess.run(['git', *args], check=True, capture_output=True, text=True).stdout

    def commit(self, name, text):
        (self.repo / name).write_text(text)
        self.git('-C', str(self.repo), 'add', name)
        self.git('-C', str(self.repo), 'commit', '-m', name)

    def test_complete_merge_graph_supports_bundle_and_both_parents(self):
        checkout = self.root / 'complete'
        self.git('clone', '--no-local', str(self.repo), str(checkout))
        self.assertTrue(CodeHostService._git_has_complete_history(checkout, self.tip))
        parents = self.git('-C', str(checkout), 'rev-parse', self.tip+'^1', self.tip+'^2').splitlines()
        self.assertEqual(len(parents), 2)
        bundle = self.root / 'complete.bundle'
        self.git('-C', str(checkout), 'bundle', 'create', str(bundle), 'HEAD')
        self.git('-C', str(checkout), 'bundle', 'verify', str(bundle))
        self.assertEqual(self.git('-C', str(checkout), 'rev-parse', 'HEAD').strip(), self.tip)

    def test_tip_presence_and_zero_git_exit_do_not_qualify_shallow_graph(self):
        checkout = self.root / 'shallow'
        self.git('clone', '--depth=1', self.repo.as_uri(), str(checkout))
        self.git('-C', str(checkout), 'cat-file', '-e', self.tip+'^{commit}')
        # rev-list succeeds at a shallow boundary, despite missing parents.
        self.git('-C', str(checkout), 'rev-list', '--quiet', self.tip)
        self.assertFalse(CodeHostService._git_has_complete_history(checkout, self.tip))

    def test_shallow_pack_import_cannot_masquerade_as_connected_normal_repository(self):
        shallow = self.root / 'shallow'
        self.git('clone', '--depth=1', self.repo.as_uri(), str(shallow))
        normal = self.root / 'normal'
        self.git('init', str(normal))
        pack = subprocess.run(['git', '-C', str(shallow), 'pack-objects', '--stdout', '--revs'],
            input=(self.tip+'\n').encode(), check=True, capture_output=True).stdout
        subprocess.run(['git', '-C', str(normal), 'index-pack', '--stdin'],
            input=pack, check=True, capture_output=True)
        self.assertTrue(CodeHostService._git_has_commit(normal, self.tip))
        self.assertFalse(CodeHostService._git_has_complete_history(normal, self.tip))
        # Hydrate an already-present tip whose parents were lost by the old import.
        self.materialize(checkout=normal)
        self.assertTrue(CodeHostService._git_has_complete_history(normal, self.tip))
        self.git('-C', str(normal), 'reset', '--hard', self.tip)
        self.assertEqual(self.git('-C', str(normal), 'rev-parse', 'HEAD').strip(), self.tip)

    def materialize(self, *, depths=(64, 512, 4096), drift=False, checkout=None):
        if checkout is None:
            checkout = self.root / ('fresh-' + str(time.monotonic_ns()))
            self.git('init', '-b', 'local', str(checkout))
        before = self.git('-C', str(checkout), 'symbolic-ref', 'HEAD')
        real_run = CodeHostService._run_materialization_git
        calls = []
        def local_transport(command, **kwargs):
            calls.append(command)
            if 'fetch' in command:
                command = list(command)
                index = command.index('fetch')
                command[index:index] = ['-c', 'protocol.file.allow=always']
                command[command.index('https://fixture.invalid/repo.git')] = self.repo.as_uri()
            result = real_run(command, **kwargs)
            if drift and '--verify' in command:
                return subprocess.CompletedProcess(command, 0, 'f' * 40 + '\n', '')
            return result
        with patch('codex_web.services.code_hosts._MATERIALIZATION_DEPTHS', depths), patch.object(
                CodeHostService, '_run_materialization_git', side_effect=local_transport):
            try:
                CodeHostService._materialize_git_sync(checkout,
                    'https://fixture.invalid/repo.git', 'main', self.tip,
                    'synthetic-unused', 'github', 'fixture-resource')
            finally:
                self.assertEqual(self.git('-C', str(checkout), 'symbolic-ref', 'HEAD'), before)
        return checkout, calls

    def test_fresh_checkout_hydrates_real_merge_graph_then_resets_and_bundles(self):
        checkout, calls = self.materialize()
        self.assertTrue(CodeHostService._git_has_complete_history(checkout, self.tip))
        self.git('-C', str(checkout), 'reset', '--hard', self.tip)
        self.assertEqual(self.git('-C', str(checkout), 'rev-parse', 'HEAD').strip(), self.tip)
        self.assertEqual(self.git('-C', str(checkout), 'rev-parse', 'HEAD^{tree}'),
                         self.git('-C', str(self.repo), 'rev-parse', 'HEAD^{tree}'))
        self.assertEqual(len(self.git('-C', str(checkout), 'rev-parse', 'HEAD^1', 'HEAD^2').splitlines()), 2)
        bundle = self.root / 'hydrated.bundle'
        self.git('-C', str(checkout), 'bundle', 'create', str(bundle), 'HEAD')
        self.git('-C', str(checkout), 'bundle', 'verify', str(bundle))
        self.assertTrue(any('index-pack' in command for command in calls))

    def test_finite_deepening_completes_and_actual_depth_bound_refuses(self):
        checkout, calls = self.materialize(depths=(1, 2, 8))
        self.assertTrue(CodeHostService._git_has_complete_history(checkout, self.tip))
        self.assertEqual([next(arg for arg in command if arg.startswith('--depth='))
                          for command in calls if 'fetch' in command], ['--depth=1', '--depth=2', '--depth=8'])
        with self.assertRaisesRegex(CodeHostError, 'depth bound1'):
            self.materialize(depths=(1,))

    def test_provider_drift_refuses_import(self):
        with self.assertRaisesRegex(CodeHostError, 'default branch changed'):
            self.materialize(drift=True)

    def test_real_subprocess_time_and_scratch_limit_retire_child(self):
        for script, seconds, limit, error in [
            ('import time; time.sleep(10)', .05, 100000, 'time bound'),
            ('from pathlib import Path; import time; Path("oversize").write_bytes(b"x"*200000); time.sleep(10)',
             2, 10000, 'disk bound'),
        ]:
            with self.subTest(error=error), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                if 'oversize' in script:
                    script = script.replace('Path("oversize")', 'Path(' + repr(str(root/'oversize')) + ')')
                with self.assertRaisesRegex(CodeHostError, error):
                    CodeHostService._run_materialization_git([sys.executable, '-c', script],
                        root=root, deadline=time.monotonic()+seconds, disk_bytes=limit,
                        env={'PATH':os.environ['PATH']})
                # Immediate deletion is safe because the primary was reaped by the wrapper.

    def test_insufficient_quota_refuses_before_fetch(self):
        checkout = self.root / 'quota'
        self.git('init', str(checkout))
        with self.assertRaisesRegex(CodeHostError, 'disk quota'):
            CodeHostService._materialize_git_sync(checkout,
                'https://fixture.invalid/repo.git', 'main', self.tip,
                'synthetic-unused', 'github', 'fixture-resource', disk_budget_bytes=1)
