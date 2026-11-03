"""#1985: canonical readonly bootstrap crosses the real native mount policy.

Only process I/O and optional tool-environment acquisition are controlled.
No real ambient HOME, credentials, provider, native executable or network.
"""
import asyncio
import dataclasses
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from codex_web import local_execution_backend as backend_module
from codex_web.services.codex_worker_session import (
    AssignmentBoundCodexSession, TrustedLocalCodexCredentialProvider,
    AssignmentBoundCodexSessionStaleError,
)
from codex_web.services.local_execution_worker import LocalExecutionWorkerRuntime
from tests import test_codex_worker_session as session_fixtures
from tests import test_procedure_automation_preparation as bootstrap_fixtures


class ReadonlyTrustedLocalRuntimeHomeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = bootstrap_fixtures.FreshSupervisorPreparationTests()
        await self.fixture.asyncSetUp()
        # Forward the borrowed fixture's actual async cleanup callbacks to
        # this running test case (never nest a second unittest event loop).
        for function, args, kwargs in self.fixture._cleanups:
            self.addCleanup(function, *args, **kwargs)
        self.fixture._cleanups.clear()
        f = self.fixture.f
        self.assignment = self.fixture.assignment
        self.workspace = Path(f.workspaces.get(self.assignment.execution_workspace_id, f.actor).path)
        self.home = Path(f.temp.name) / 'ambient-signin-home'
        self.home.mkdir()
        self.bin = Path(f.temp.name) / 'native-bin'
        self.bin.mkdir()
        executable = self.bin / 'codex'
        executable.write_text('# inert, never executed\n')
        self.provider = TrustedLocalCodexCredentialProvider(codex_home=self.home,
            executable=str(executable))
        self.commands = []
        self.processes = []

        def popen(command, **kwargs):
            self.commands.append(tuple(command))
            process = session_fixtures._FakeProcess()
            self.processes.append(process)
            return process

        self.backend = backend_module.BubblewrapExecutionBackend(executable='/usr/bin/bwrap',
            probe_runner=lambda *a, **k: SimpleNamespace(returncode=0, stderr=''), popen=popen)
        self.local = LocalExecutionWorkerRuntime(f.workers, f.workspaces, self.backend,
            worker=self.fixture.c.worker, worker_actor=self.fixture.c.worker_actor,
            control_actor=f.actor, trusted_local_codex_delegation=self.provider)
        # Dependency acquisition is not part of this mount-policy regression.
        self.local.python_environment = Mock(return_value=None)
        async def idle(_seconds):
            await asyncio.Future()
        self.session = AssignmentBoundCodexSession(self.local, SimpleNamespace(), self.assignment.id,
            credential_provider=self.provider, runtime_factory=session_fixtures._FakeCodexRuntime,
            sleep=idle)
        self.addAsyncCleanup(self.session.stop)

    def mount(self):
        return backend_module.TrustedLocalRuntimeHome(self.home.resolve(), self.assignment.id,
            self.assignment.assigned_worker_id, self.assignment.fence)

    def build(self, home=None, **kwargs):
        return self.backend.build_command(self.assignment, argv=('codex', 'app-server'),
            workspace_path=self.workspace, trusted_local_runtime_home=home or self.mount(), **kwargs)

    async def test_readonly_bootstrap_trusted_local_home_crosses_real_policy(self):
        self.assertEqual(self.assignment.sandbox, 'read-only')
        self.assertEqual(self.assignment.repository_scope.writable_repository_ids, ())
        self.assertEqual(self.assignment.repository_scope.read_only_repository_ids,
            (self.fixture.f.repo.id,))
        try:
            await self.session.start()
        except backend_module.LocalExecutionPolicyError as exc:
            # OLD must reach the actual policy refusal, not a new constructor
            # or type missing from the baseline.
            self.assertEqual(str(exc), 'read-only assignment cannot receive writable repository mounts')
            self.assertEqual(self.commands, [])
            self.fail('canonical readonly trusted-local bootstrap was refused by repository mount policy')
        self.assertTrue(self.session.status().ready)
        self.assertEqual(len(self.commands), 1)
        triples = [self.commands[0][i:i+3] for i in range(len(self.commands[0])-2)]
        self.assertIn(('--bind', str(self.home), str(self.home)), triples)
        self.assertIn(('--ro-bind', str(self.workspace), str(self.workspace)), triples)
        self.assertNotIn(('--bind', str(self.workspace), str(self.workspace)), triples)
        current = self.fixture.f.workers.store.assignment(self.assignment.id)
        self.assertEqual(current.repository_scope, self.assignment.repository_scope)
        self.assertEqual(current.fence, self.assignment.fence)
        self.assertFalse(any('ACCESS_TOKEN' in key for key in self.backend.minimal_environment()))

    async def test_secondary_repository_write_remains_forbidden(self):
        sibling = Path(self.fixture.f.temp.name) / 'sibling-repository'
        sibling.mkdir()
        with self.assertRaisesRegex(backend_module.LocalExecutionPolicyError, 'writable repository mounts'):
            self.build(trusted_writable_mounts=((sibling, Path('/mnt/secondary')),))
        self.assertEqual(self.commands, [])

    async def test_runtime_home_cannot_overlap_workspace_repository_or_metadata(self):
        sibling = Path(self.fixture.f.temp.name) / 'read-repository'
        sibling.mkdir()
        child = sibling / 'nested-home'
        child.mkdir()
        metadata = Path(self.fixture.f.temp.name) / 'repository-metadata'
        metadata.mkdir()
        cases = [self.workspace, self.workspace.parent, sibling, child, metadata, Path('/')]
        for path in cases:
            with self.subTest(path=path.name):
                mount = dataclasses.replace(self.mount(), path=path.resolve())
                with self.assertRaises(backend_module.LocalExecutionPolicyError):
                    self.build(home=mount, git_metadata_path=metadata,
                        trusted_readonly_mounts=((sibling, Path('/mnt/read-repository')),))
        self.assertEqual(self.commands, [])

    async def test_foreign_assignment_worker_or_fence_mount_is_refused(self):
        for changes in ({'assignment_id':'foreign'}, {'worker_id':'foreign'},
                {'fence':self.assignment.fence+1}):
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(backend_module.LocalExecutionPolicyError, 'custody mismatch'):
                    self.build(home=dataclasses.replace(self.mount(), **changes))
        self.assertEqual(self.commands, [])

    async def test_current_grant_rejects_foreign_home_expired_current_lease_and_deadline(self):
        lease = self.assignment.lease
        grant = self.provider._grant(self.assignment, worker_id=lease.worker_id, fence=lease.fence)
        self.provider._clock = lambda: lease.expires_at - 1
        foreign = dataclasses.replace(grant, codex_home=str(self.bin))
        with self.assertRaises(AssignmentBoundCodexSessionStaleError):
            self.provider.validate_current(foreign, self.assignment, actor=self.local.worker_actor)
        stale = self.assignment.model_copy(update={'fence':self.assignment.fence+1})
        with self.assertRaises(AssignmentBoundCodexSessionStaleError):
            self.provider.validate_current(grant, stale, actor=self.local.worker_actor)
        expired_lease = self.assignment.model_copy(update={
            'lease':lease.model_copy(update={'expires_at':lease.expires_at-2})})
        with self.assertRaisesRegex(AssignmentBoundCodexSessionStaleError, 'lease/fence is stale'):
            self.provider.validate_current(grant, expired_lease, actor=self.local.worker_actor)
        expired_deadline = self.assignment.model_copy(update={'deadline_at':lease.expires_at-2})
        with self.assertRaisesRegex(AssignmentBoundCodexSessionStaleError, 'deadline has expired'):
            self.provider.validate_current(grant, expired_deadline, actor=self.local.worker_actor)
        self.assertEqual(self.commands, [])

    async def test_renewed_canonical_lease_survives_original_launch_grant_expiry(self):
        await self.session.start()
        original_grant = self.session.delegation
        current = self.fixture.f.workers.store.assignment(self.assignment.id)
        self.assertEqual(original_grant.expires_at, current.lease.expires_at)
        # Renew through the real canonical owner BEFORE the old lease expires.
        # No sleeps, grant replacement or direct Store mutation.
        renew_at = original_grant.expires_at - 10
        self.provider._clock = lambda: renew_at
        self.session._clock = lambda: renew_at
        with patch('codex_web.services.execution_workers.time.time', return_value=renew_at):
            renewed = self.session._heartbeat_and_renew(current)
        # Mirror _watchdog: keep the same launch grant, but retain its newly
        # validated canonical lease snapshot for the next supervision budget.
        self.session._bound_assignment = renewed
        observed_at = original_grant.expires_at + 1
        self.assertLess(observed_at, renewed.lease.expires_at)
        self.provider._clock = lambda: observed_at
        self.session._clock = lambda: observed_at
        self.assertIs(self.session.delegation, original_grant)
        try:
            observed = self.session._validate_current_authority()
        except AssignmentBoundCodexSessionStaleError as exc:
            self.assertEqual(str(exc), 'trusted-local Codex grant assignment changed')
            self.fail('valid renewed canonical lease was refused using the original launch-grant expiry')
        self.assertEqual(observed.lease, renewed.lease)
        self.assertEqual(observed.fence, current.fence)
        self.assertTrue(self.session.status().ready)
        self.assertIs(self.session.delegation, original_grant)

    async def test_session_refuses_runtime_home_different_from_configured_grant(self):
        original_use = self.provider.use
        def substituted(assignment, **kwargs):
            consumer = kwargs.pop('consumer')
            def change(launch):
                mount = dataclasses.replace(launch.trusted_local_runtime_home, path=self.bin.resolve())
                return consumer(dataclasses.replace(launch, trusted_local_runtime_home=mount))
            return original_use(assignment, consumer=change, **kwargs)
        self.provider.use = substituted
        with self.assertRaisesRegex(AssignmentBoundCodexSessionStaleError, 'differs from canonical grant'):
            await self.session.start()
        self.assertEqual(self.commands, [])
