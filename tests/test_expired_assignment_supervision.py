from __future__ import annotations

import asyncio
import contextvars
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codex_web.execution_workers import AssignmentRenewRequest, AssignmentStartRequest, AssignmentStatus
from codex_web.execution_workspaces import ExecutionWorkspaceStatus
from codex_web.services.runtime_supervisor import RuntimeSupervisor
from codex_web.services.identity import AuthorizationError
from tests import test_bootstrap_cancellation as fixtures


class ExpiredAssignmentRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.BootstrapCancellationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.f = self.fixture.fixture

    def running(self):
        binding = self.fixture.prepare('lease-loss')
        claimed = self.fixture.claim(binding.assignment_id)
        running = self.f.workers.start(
            self.f.worker.id, claimed.id,
            AssignmentStartRequest(fence=claimed.fence, lease_token=claimed.lease.lease_token),
            actor=self.fixture.worker_actor,
        )
        return binding, running

    def test_expiry_records_canonical_lease_loss_and_retains_all_workspace_files(self):
        binding, running = self.running()
        workspace = self.f.workspaces.get(binding.workspace_id, self.f.actor)
        paths = [Path(workspace.path) / name for name in ('tracked.txt', 'untracked.txt', '.ignored-artifact')]
        for path in paths:
            path.write_text('retain lease-loss evidence')
        with patch.object(self.f.workspaces, '_cleanup_git_workspace', side_effect=AssertionError('must preserve files')):
            lost = self.f.workers.recover_expired(actor=self.f.actor, now=running.lease.expires_at + 1)
        self.assertEqual(lost, [running.id])
        assignment = self.f.workers.store.assignment(running.id)
        self.assertEqual(assignment.status, AssignmentStatus.LOST)
        self.assertEqual(assignment.fence, running.fence)
        self.assertIsNone(assignment.lease)
        self.assertEqual(assignment.failure.reason_code.value, 'worker_lease_lost')
        released = self.f.workspaces.get(binding.workspace_id, self.f.actor)
        self.assertEqual(released.status, ExecutionWorkspaceStatus.RELEASED)
        self.assertIsNone(released.cleaned_at)
        self.assertTrue(all(path.read_text() == 'retain lease-loss evidence' for path in paths))
        events = [event for event in self.f.workers.events(self.f.actor) if event.event_type == 'assignment_lost']
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].actor_id, self.f.actor.identity_id)
        self.assertEqual(events[0].details['fence'], running.fence)

    def test_expired_scratch_workspace_retains_ignored_artifacts(self):
        binding = self.f.service.prepare_bootstrap(
            bootstrap_id="scratch-expiry", execution_id="scratch-expiry",
            project_id=self.f.project.id, sandbox="workspace-write", approval_policy="on-request",
            execution_profile_id="orchestration-only",
        )
        claimed = self.fixture.claim(binding.assignment_id)
        workspace = self.f.workspaces.get(binding.workspace_id, self.f.actor)
        artifact = Path(workspace.path) / ".ignored-scratch-evidence"
        artifact.write_text("retain scratch evidence")
        self.f.workers.recover_expired(actor=self.f.actor, now=claimed.lease.expires_at + 1)
        self.assertEqual(artifact.read_text(), "retain scratch evidence")
        released = self.f.workspaces.get(binding.workspace_id, self.f.actor)
        self.assertEqual(released.status, ExecutionWorkspaceStatus.RELEASED)
        self.assertIsNone(released.cleaned_at)

    def test_unexpired_and_foreign_scope_do_not_load_or_rewrite_history(self):
        _, running = self.running()
        store = self.f.workers.store
        with patch.object(store, 'load', side_effect=AssertionError('no history load')), patch.object(store, 'update', side_effect=AssertionError('no rewrite')):
            self.assertEqual(self.f.workers.recover_expired(actor=self.f.actor, now=running.lease.expires_at - 1), [])
            foreign = self.f.actor.model_copy(update={'workspace_id':'foreign-tenant'})
            self.assertEqual(self.f.workers.recover_expired(actor=foreign, now=running.lease.expires_at + 1), [])
        self.assertEqual(store.assignment(running.id), running)
        with self.assertRaises(AuthorizationError):
            self.f.workers.recover_expired(actor=self.fixture.worker_actor, now=running.lease.expires_at + 1)

    def test_candidate_renewed_before_atomic_recheck_is_not_lost(self):
        _, running = self.running()
        store = self.f.workers.store
        original = store.update
        renewed = []
        def renew_then_update(updater):
            renewed.append(self.f.workers.renew(
                self.f.worker.id, running.id,
                AssignmentRenewRequest(fence=running.fence, lease_token=running.lease.lease_token, lease_seconds=1200),
                actor=self.fixture.worker_actor,
            ))
            return original(updater)
        with patch.object(store, 'update', side_effect=renew_then_update):
            self.assertEqual(self.f.workers.recover_expired(actor=self.f.actor, now=running.lease.expires_at + 1), [])
        self.assertEqual(store.assignment(running.id), renewed[0])
        self.assertEqual(self.f.workspaces.get(running.execution_workspace_id, self.f.actor).status, ExecutionWorkspaceStatus.ACTIVE)
        self.assertFalse(any(event.event_type == 'assignment_lost' for event in self.f.workers.events(self.f.actor)))

    def test_read_only_prefilter_follows_bounded_assignment_pages(self):
        self.fixture.prepare('pending-one')
        self.fixture.prepare('pending-two')
        store = self.f.workers.store
        original = store.assignment_page
        cursors = []
        def page(*, after=None, limit=100):
            self.assertEqual(limit, 100)
            cursors.append(after)
            return original(after=after, limit=1)
        with patch.object(store, 'assignment_page', side_effect=page), patch.object(store, 'update', side_effect=AssertionError('no expiry rewrite')):
            self.assertEqual(self.f.workers.recover_expired(actor=self.f.actor, now=0), [])
        self.assertEqual(len(cursors), 2)
        self.assertIsNotNone(cursors[1])


class ExpiredAssignmentSupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def test_callback_keeps_event_loop_responsive_and_preserves_actor_context(self):
        entered = threading.Event()
        release = threading.Event()
        actor_context = contextvars.ContextVar('expiry_actor')
        actor = object()
        observed = []
        def callback():
            observed.append(actor_context.get())
            entered.set()
            if not release.wait(3):
                raise RuntimeError('storage test barrier timed out')
        service = RuntimeSupervisor(SimpleNamespace(state=SimpleNamespace()), SimpleNamespace(), recover_expired_assignments=callback)
        token = actor_context.set(actor)
        try:
            pending = asyncio.create_task(service._recover_expired_assignments_once())
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            await asyncio.wait_for(asyncio.sleep(0), 1)
            self.assertFalse(pending.done())
        finally:
            release.set()
            actor_context.reset(token)
        await pending
        self.assertEqual(observed, [actor])

    async def test_periodic_callback_failure_retries_without_queue_or_heartbeat(self):
        events = []
        recovered = asyncio.Event()
        loop = asyncio.get_running_loop()
        calls = 0
        def callback():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError('worker store unavailable')
            loop.call_soon_threadsafe(recovered.set)
        service = RuntimeSupervisor(SimpleNamespace(state=SimpleNamespace()), SimpleNamespace(), recover_expired_assignments=callback, event_sink=events.append)
        service.queue_recovery_interval_seconds = lambda: 0.01
        await service.start()
        task = service.tasks['execution-assignment-recovery']
        try:
            await asyncio.wait_for(recovered.wait(), 3)
            self.assertFalse(task.done())
            self.assertGreaterEqual(calls, 2)
            self.assertEqual(events[0]['type'], 'execution_assignment_recovery_failed')
            self.assertIn('worker store unavailable', events[0]['error'])
            self.assertNotIn('local-worker-heartbeat', service.tasks)
        finally:
            await service.stop()
        self.assertTrue(task.done())

    async def test_composed_application_callback_starts_periodic_task_with_real_control_actor(self):
        from codex_web import application
        called = asyncio.Event()
        actor = application.identity_service.local_trusted_actor()
        loop = asyncio.get_running_loop()
        def recover(*, actor):
            loop.call_soon_threadsafe(called.set)
            return []
        callback = application.runtime_supervisor.recover_expired_assignments
        self.assertIs(callback, application._recover_expired_execution_assignments)
        service = RuntimeSupervisor(SimpleNamespace(state=SimpleNamespace()), SimpleNamespace(), recover_expired_assignments=callback)
        with patch.object(application.execution_worker_service, 'recover_expired', side_effect=recover) as canonical:
            await service.start()
            try:
                await asyncio.wait_for(called.wait(), 3)
                self.assertIn('execution-assignment-recovery', service.tasks)
                self.assertFalse(service.tasks['execution-assignment-recovery'].done())
                canonical.assert_called_once()
                observed = canonical.call_args.kwargs["actor"]
                self.assertEqual(observed.model_dump(exclude={"authenticated_at"}), actor.model_dump(exclude={"authenticated_at"}))
            finally:
                await service.stop()
