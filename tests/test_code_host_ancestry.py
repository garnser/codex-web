"""Real Git graphs; no provider credentials, network or live workspaces."""
import subprocess
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

    def test_materializer_refuses_shallow_fetch_before_import_or_head_change(self):
        checkout = self.root / 'checkout'
        self.git('clone', '--no-local', str(self.repo), str(checkout))
        before = self.git('-C', str(checkout), 'rev-parse', 'HEAD')
        real_run = subprocess.run
        calls = []
        def local_transport(command, **kwargs):
            calls.append(command)
            if 'fetch' in command:
                command = list(command)
                index = command.index('fetch')
                command[index:index] = ['-c', 'protocol.file.allow=always']
                command[command.index('https://fixture.invalid/repo.git')] = self.repo.as_uri()
            return real_run(command, **kwargs)
        with patch('codex_web.services.code_hosts.subprocess.run', side_effect=local_transport):
            with self.assertRaisesRegex(CodeHostError, 'ancestry is incomplete'):
                CodeHostService._materialize_git_sync(checkout,
                    'https://fixture.invalid/repo.git', 'main', self.tip,
                    'synthetic-unused', 'github', 'fixture-resource')
        self.assertFalse(any('index-pack' in command for command in calls))
        self.assertEqual(self.git('-C', str(checkout), 'rev-parse', 'HEAD'), before)
