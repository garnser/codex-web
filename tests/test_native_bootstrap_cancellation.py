from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace

from codex_web.execution_workers import AssignmentStartRequest, AssignmentStatus
from codex_web.execution_workspaces import ExecutionWorkspaceStatus
from codex_web.services.agent_process_session import AssignmentBoundAgentProcessSessionManager
from tests import test_bootstrap_cancellation as cancellation_fixtures
from tests.test_thread_bootstrap_routing import _BootstrapBindings, _Host, _thread_service


class NativeBootstrapCancellationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = cancellation_fixtures.BootstrapCancellationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.entered = asyncio.Event()
        self.stopped = []
        self.paths = []
        test = self
        f = self.fixture.fixture

        class RegisteredSession:
            def __init__(self, local_worker, host, assignment_id, **kwargs):
                self.assignment_id = assignment_id
                self.fence = None
                self.workspace_path = None

            async def start(self):
                assignment = test.fixture.claim(self.assignment_id)
                self.fence = assignment.fence
                f.workers.start(
                    f.worker.id, assignment.id,
                    AssignmentStartRequest(fence=self.fence, lease_token=assignment.lease.lease_token),
                    actor=test.fixture.worker_actor,
                )
                workspace = f.workspaces.get(assignment.execution_workspace_id, f.actor)
                self.workspace_path = Path(workspace.path)
                for name in ('tracked.txt', 'untracked.txt', '.ignored-evidence'):
                    path = self.workspace_path / name
                    path.write_text('retained bootstrap evidence')
                    test.paths.append(path)

            def status(self):
                return SimpleNamespace(worker_id=f.worker.id, fence=self.fence)

            async def request(self, method, params=None):
                test.assertEqual(method, 'thread/start')
                test.entered.set()
                await asyncio.Event().wait()

            async def stop(self):
                test.stopped.append(self.assignment_id)

        self.manager = AssignmentBoundAgentProcessSessionManager(
            self.fixture.local_worker, None, runtime_factory=None,
            credential_provider=None, session_factory=RegisteredSession,
        )
        host = _Host()
        host.project = f.project
        self.bindings = _BootstrapBindings()
        self.service = _thread_service(
            host, binding_service=f.service, session_manager=self.manager,
            bootstrap_bindings=self.bindings, control_actor=f.actor,
        )
        self.project_id = f.project.id

    async def start_native_wait(self):
        self.entered.clear()
        previous = set(self.manager.sessions)
        task = asyncio.create_task(self.service.create(project_id=self.project_id))
        await asyncio.wait_for(self.entered.wait(), 3)
        assignment_id = next(iter(set(self.manager.sessions) - previous))
        assignment = self.fixture.fixture.workers.store.assignment(assignment_id)
        self.assertEqual(assignment.status, AssignmentStatus.RUNNING)
        self.assertEqual(assignment.fence, self.manager.get(assignment_id).fence)
        self.assertEqual(self.bindings.calls, [])
        return task, assignment

    async def test_repeated_native_create_cancellation_releases_registered_fence_and_retains_files(self):
        f = self.fixture.fixture
        for attempt in range(3):
            task, assignment = await self.start_native_wait()
            task.cancel('native creation deadline')
            with self.assertRaises(asyncio.CancelledError) as caught:
                await task
            self.assertEqual(caught.exception.args, ('native creation deadline',))
            cancelled = f.workers.store.assignment(assignment.id)
            self.assertEqual(cancelled.status, AssignmentStatus.CANCELLED)
            self.assertEqual(cancelled.fence, assignment.fence + 1)
            self.assertIsNone(cancelled.lease)
            self.assertIsNone(self.manager.get(assignment.id))
            self.assertIn(assignment.id, self.stopped)
            workspace = f.workspaces.get(assignment.execution_workspace_id, f.actor)
            self.assertEqual(workspace.status, ExecutionWorkspaceStatus.RELEASED)
            self.assertIsNone(workspace.cleaned_at)
            self.assertEqual(sum(lease.released_at is None for lease in f.workspaces.store.load().leases), 0)
            self.assertTrue(all(path.read_text() == 'retained bootstrap evidence' for path in self.paths))
        self.assertEqual(self.bindings.calls, [])
        cancelled_events = [event for event in f.workers.events(f.actor) if event.event_type == 'assignment_cancelled']
        self.assertEqual(len(cancelled_events), 3)
        self.assertTrue(all(event.actor_id == f.actor.identity_id for event in cancelled_events))

    async def test_outer_deadline_waits_for_registered_native_create_cleanup(self):
        task, assignment = await self.start_native_wait()
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(task, 0.01)
        f = self.fixture.fixture
        self.assertEqual(f.workers.store.assignment(assignment.id).status, AssignmentStatus.CANCELLED)
        self.assertIsNone(self.manager.get(assignment.id))
        self.assertEqual(f.workspaces.get(assignment.execution_workspace_id, f.actor).status, ExecutionWorkspaceStatus.RELEASED)

    async def test_second_cancellation_cannot_interrupt_owned_cleanup_or_replace_original(self):
        task, assignment = await self.start_native_wait()
        entered = asyncio.Event()
        release = asyncio.Event()
        complete = self.manager.complete

        async def blocked_complete(*args, **kwargs):
            entered.set()
            await release.wait()
            return await complete(*args, **kwargs)

        self.manager.complete = blocked_complete
        task.cancel('original native deadline')
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel('second request cancellation')
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.assertIsNotNone(self.manager.get(assignment.id))
        release.set()
        with self.assertRaises(asyncio.CancelledError) as caught:
            await asyncio.wait_for(task, 3)
        self.assertEqual(caught.exception.args, ('original native deadline',))
        self.assertEqual(self.fixture.fixture.workers.store.assignment(assignment.id).status, AssignmentStatus.CANCELLED)
        self.assertIsNone(self.manager.get(assignment.id))

    async def test_cleanup_failure_is_chained_to_original_cancellation_and_can_be_reconciled(self):
        from unittest.mock import patch
        task, assignment = await self.start_native_wait()
        f = self.fixture.fixture
        failure = RuntimeError('workspace release unavailable')
        with patch.object(f.workspaces, 'release', side_effect=failure):
            task.cancel('original cancellation')
            with self.assertRaises(asyncio.CancelledError) as caught:
                await task
        self.assertEqual(caught.exception.args, ('original cancellation',))
        self.assertIs(caught.exception.__cause__, failure)
        self.assertEqual(f.workers.store.assignment(assignment.id).status, AssignmentStatus.CANCELLED)
        self.assertIsNotNone(self.manager.get(assignment.id))
        await self.manager.cancel_bootstrap(assignment.id, reason='reconcile failed workspace release')
        self.assertIsNone(self.manager.get(assignment.id))
        self.assertEqual(f.workspaces.get(assignment.execution_workspace_id, f.actor).status, ExecutionWorkspaceStatus.RELEASED)
        self.assertTrue(all(path.read_text() == 'retained bootstrap evidence' for path in self.paths))
        self.assertEqual(sum(event.event_type == 'assignment_cancelled' for event in f.workers.events(f.actor)), 1)

    async def test_cancelled_native_create_does_not_touch_another_registered_session(self):
        f = self.fixture.fixture
        worker = f.worker
        # Configure only the private fixture to permit two genuine sessions.
        f.worker = f.workers.ensure_local_worker(
            service_identity_id=worker.service_identity_id, version=worker.version,
            capabilities=worker.capabilities, max_concurrency=2, actor=f.actor,
            supported_execution_contract_versions=worker.supported_execution_contract_versions,
            supported_sandbox_profiles=worker.supported_sandbox_profiles,
        )
        unrelated = self.fixture.prepare('unrelated-native-session')
        session = await self.manager.start(unrelated.assignment_id)
        original = f.workers.store.assignment(unrelated.assignment_id)
        task, assignment = await self.start_native_wait()
        task.cancel('native creation deadline')
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIs(self.manager.get(unrelated.assignment_id), session)
        self.assertEqual(f.workers.store.assignment(unrelated.assignment_id), original)
        self.assertEqual(f.workspaces.get(unrelated.workspace_id, f.actor).status, ExecutionWorkspaceStatus.ACTIVE)
        self.assertNotIn(unrelated.assignment_id, self.stopped)
        self.assertEqual(f.workers.store.assignment(assignment.id).status, AssignmentStatus.CANCELLED)
