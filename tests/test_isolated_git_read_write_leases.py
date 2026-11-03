"""#1987: real private Git worktrees and canonical lease reservation, no native runtime."""
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from codex_web.execution_workspace_backend import LocalGitWorkspaceBackend
from codex_web.execution_workspaces import ExecutionWorkspaceAcquire, LeaseMode
from codex_web.services.execution_workspaces import ExecutionWorkspaceConflictError
from tests import test_execution_workspaces as fixtures


class IsolatedGitReadWriteLeaseTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ExecutionWorkspaceTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.service = self.f.service
        self.actor = self.f.actor
        self.repo = self.f.repo
        self.source = Path(self.f.project.path)
        for source in (self.source, self.f.repo2_source):
            self.git(source, 'init')
            (source / 'fixture.txt').write_text('pinned private fixture\n')
            self.git(source, 'add', 'fixture.txt')
            self.git(source, 'commit', '-m', 'Private fixture')
        self.backend = LocalGitWorkspaceBackend(Path(self.f.temp.name) / 'actual-git-worktrees')
        self.service.backend = self.backend

    def git(self, path, *args):
        return subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'user.name=Private fixture',
            '-c', 'user.email=private@example.invalid', *args], cwd=path, check=True,
            capture_output=True, text=True).stdout.strip()

    def acquire(self, execution, *, read=False, repository=None, shared=False):
        repository = repository or self.repo
        resources = (repository.id, self.f.database.id) if shared else (repository.id,)
        return self.service.acquire(ExecutionWorkspaceAcquire(work_item_ref=self.f.work_item.ref,
            execution_id=execution, project_id=self.f.project.id, resource_ids=resources,
            repository_resource_id=repository.id,
            read_only_repository_ids=(repository.id,) if read else (),
            lease_mode=LeaseMode.READ if read else LeaseMode.WRITE, ttl_seconds=300), actor=self.actor)

    def coexist(self, first_read):
        first = self.acquire('first-reader' if first_read else 'first-writer', read=first_read)
        try:
            second = self.acquire('second-writer' if first_read else 'second-reader', read=not first_read)
        except ExecutionWorkspaceConflictError as exc:
            self.assertIn('conflicting active resource lease', str(exc))
            self.fail('isolated READ/WRITE refused despite distinct canonical Git worktrees')
        reader, writer = (first, second) if first_read else (second, first)
        self.assertNotEqual(reader.id, writer.id)
        self.assertNotEqual(Path(reader.path).resolve(), Path(writer.path).resolve())
        self.assertEqual(reader.writable_repository_ids, ())
        self.assertEqual(reader.read_only_primary_repository_id, self.repo.id)
        self.assertTrue(all(member.access_mode == LeaseMode.READ for member in reader.repository_members))
        self.assertEqual(self.git(reader.path, 'rev-parse', 'HEAD'), reader.base_revision)
        # Git worktrees share common metadata, but per-worktree HEAD/index and
        # checkout are distinct. Reader remains pinned as writer advances.
        self.assertEqual(self.git(reader.path, 'rev-parse', '--path-format=absolute', '--git-common-dir'),
            self.git(writer.path, 'rev-parse', '--path-format=absolute', '--git-common-dir'))
        self.assertNotEqual(self.git(reader.path, 'rev-parse', '--absolute-git-dir'),
            self.git(writer.path, 'rev-parse', '--absolute-git-dir'))
        self.assertNotEqual(self.git(reader.path, 'rev-parse', '--git-path', 'index'),
            self.git(writer.path, 'rev-parse', '--git-path', 'index'))
        (Path(writer.path) / 'fixture.txt').write_text('writer changed its private checkout\n')
        self.git(writer.path, 'add', 'fixture.txt')
        self.git(writer.path, 'commit', '-m', 'Writer advances')
        self.assertNotEqual(self.git(writer.path, 'rev-parse', 'HEAD'), reader.base_revision)
        self.assertEqual(self.git(reader.path, 'rev-parse', 'HEAD'), reader.base_revision)
        self.assertEqual((Path(reader.path) / 'fixture.txt').read_text(), 'pinned private fixture\n')
        self.assertEqual(self.git(self.source, 'rev-parse', 'HEAD'), reader.base_revision)
        lease = self.service.store.lease(reader.lease_id)
        self.assertEqual(lease.resource_modes[self.repo.id], LeaseMode.READ)
        self.assertIsNone(lease.released_at)

    def test_readonly_supervisor_then_writer_uses_distinct_pinned_git_worktrees(self):
        self.coexist(True)

    def test_writer_then_readonly_supervisor_uses_distinct_pinned_git_worktrees(self):
        self.coexist(False)

    def test_nonisolated_shared_checkout_plan_still_conflicts(self):
        reader = self.acquire('reader', read=True)
        original = self.backend.git_workspace_path
        with patch.object(self.backend, 'git_workspace_path', side_effect=lambda key:
                original(key) if key == reader.id else Path(reader.path)):
            with self.assertRaisesRegex(ExecutionWorkspaceConflictError, self.repo.id):
                self.acquire('shared-path-writer')
        self.assertEqual((Path(reader.path) / 'fixture.txt').read_text(), 'pinned private fixture\n')

    def test_parent_child_checkout_plan_still_conflicts(self):
        reader = self.acquire('reader', read=True)
        original = self.backend.git_workspace_path
        with patch.object(self.backend, 'git_workspace_path', side_effect=lambda key:
                original(key) if key == reader.id else Path(reader.path) / 'nested-writer'):
            with self.assertRaisesRegex(ExecutionWorkspaceConflictError, self.repo.id):
                self.acquire('nested-writer')
        self.assertFalse((Path(reader.path) / 'nested-writer').exists())

    def test_shared_nonrepository_resource_still_conflicts(self):
        self.acquire('reader-with-database', read=True, shared=True)
        with self.assertRaisesRegex(ExecutionWorkspaceConflictError, self.f.database.id):
            self.acquire('writer-with-database', shared=True)

    def test_repository_read_member_coexists_with_writer_without_widening_scope(self):
        mixed = self.service.acquire(ExecutionWorkspaceAcquire(work_item_ref=self.f.work_item.ref,
            execution_id='primary-write-secondary-read', project_id=self.f.project.id,
            resource_ids=(self.repo.id, self.f.repo2.id), repository_resource_id=self.repo.id,
            read_only_repository_ids=(self.f.repo2.id,), lease_mode=LeaseMode.WRITE, ttl_seconds=300), actor=self.actor)
        writer = self.acquire('secondary-writer', repository=self.f.repo2)
        self.assertEqual(mixed.writable_repository_ids, (self.repo.id,))
        read_member = next(member for member in mixed.repository_members if member.resource_id == self.f.repo2.id)
        self.assertEqual(read_member.access_mode, LeaseMode.READ)
        self.assertNotEqual(Path(read_member.workspace_path), Path(writer.path))
        self.assertEqual(self.git(read_member.workspace_path, 'rev-parse', 'HEAD'), read_member.base_revision)

    def test_backend_without_physical_path_contract_remains_fail_closed(self):
        self.acquire('reader', read=True)
        with patch.object(self.backend, 'git_workspace_path', None):
            with self.assertRaisesRegex(ExecutionWorkspaceConflictError, self.repo.id):
                self.acquire('unproven-writer')
