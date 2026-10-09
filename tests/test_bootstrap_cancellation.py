from __future__ import annotations

import asyncio
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from codex_web.execution_workers import (
    AssignmentCancelRequest, AssignmentClaimRequest, AssignmentCompleteRequest,
    AssignmentStartRequest, AssignmentStatus,
)
from codex_web.execution_workspaces import (
    ExecutionWorkspaceStatus, WorkspaceQuota,
)
from codex_web.identity import AuthenticationAssurance, MembershipRole, PrincipalKind
from codex_web.services.agent_process_session import AssignmentBoundAgentProcessSessionManager
from codex_web.services.execution_workers import WorkerConflictError, WorkerLeaseError
from codex_web.services.identity import AuthorizationError
from tests import test_turn_execution_binding as binding_fixtures


class BootstrapCancellationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = binding_fixtures.TurnExecutionBindingTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        f = self.fixture
        f._publish_secret()
        f.workspaces.quota = WorkspaceQuota(max_active_per_identity=2, max_active_per_tenant=2)
        self.worker_actor = f.actor.model_copy(update={
            "identity_id": f.worker.service_identity_id,
            "principal_kind": PrincipalKind.SERVICE,
            "service_scopes": ("execution-worker:run",),
            "roles": (MembershipRole.MEMBER,),
        })
        self.local_worker = SimpleNamespace(
            worker_service=f.workers, workspace_service=f.workspaces,
            control_actor=f.actor, worker_actor=self.worker_actor,
            _pending_assignment=lambda key: f.workers.store.assignment(key),
        )

    def prepare(self, token):
        f = self.fixture
        return f.service.prepare_bootstrap(
            bootstrap_id=f"bootstrap-{token}", execution_id=f"execution-{token}",
            project_id=f.project.id, sandbox="workspace-write", approval_policy="on-request",
        )

    def claim(self, assignment_id):
        f = self.fixture
        return f.workers.claim(f.worker.id, AssignmentClaimRequest(),
                               actor=self.worker_actor, assignment_id=assignment_id)

    def manager(self, *, fail_after_claim):
        test = self
        class FailingSession:
            def __init__(self, local_worker, host, assignment_id, **kwargs):
                self.assignment_id = assignment_id
                self.fence = None
            async def start(self):
                assignment = test.claim(self.assignment_id)
                if assignment is None:
                    raise RuntimeError("worker capacity exhausted")
                self.fence = assignment.fence
                if fail_after_claim:
                    test.fixture.workers.start(
                        test.fixture.worker.id, assignment.id,
                        AssignmentStartRequest(fence=self.fence, lease_token=assignment.lease.lease_token),
                        actor=test.worker_actor,
                    )
                    raise RuntimeError("runtime adapter startup failed")
            async def stop(self):
                pass
        return AssignmentBoundAgentProcessSessionManager(
            self.local_worker, None, runtime_factory=None, credential_provider=None,
            session_factory=FailingSession,
        )

    async def test_repeated_capacity_rejections_free_reservations_without_touching_live_claim(self):
        busy = self.prepare("busy")
        live = self.claim(busy.assignment_id)
        manager = self.manager(fail_after_claim=False)
        for attempt in range(5):
            binding = self.prepare(f"capacity-{attempt}")
            with self.assertRaisesRegex(RuntimeError, "worker capacity"):
                await manager.start(binding.assignment_id)
            assignment = self.fixture.workers.store.assignment(binding.assignment_id)
            self.assertEqual(assignment.status, AssignmentStatus.CANCELLED)
            self.assertIsNone(assignment.lease)
            self.assertIsNone(manager.get(binding.assignment_id))
            workspace = self.fixture.workspaces.get(binding.workspace_id, self.fixture.actor)
            self.assertEqual(workspace.status, ExecutionWorkspaceStatus.RELEASED)
            self.assertIsNone(workspace.cleaned_at)
        self.assertEqual(self.fixture.workers.store.assignment(live.id), live)
        self.assertEqual(sum(lease.released_at is None for lease in self.fixture.workspaces.store.load().leases), 1)

    async def test_repeated_runtime_start_failures_preserve_every_file_and_free_both_capacities(self):
        manager = self.manager(fail_after_claim=True)
        f = self.fixture
        with patch.object(f.workspaces, "_cleanup_git_workspace", side_effect=AssertionError("must retain files")):
            for attempt in range(5):
                binding = self.prepare(f"runtime-{attempt}")
                workspace = f.workspaces.get(binding.workspace_id, f.actor)
                paths = [Path(workspace.path) / name for name in ("tracked.txt", "untracked.txt", ".ignored-secret")]
                for path in paths:
                    path.write_text("recovery artifact")
                with self.assertRaisesRegex(RuntimeError, "runtime adapter"):
                    await manager.start(binding.assignment_id)
                assignment = f.workers.store.assignment(binding.assignment_id)
                self.assertEqual(assignment.status, AssignmentStatus.CANCELLED)
                self.assertEqual(assignment.fence, 2)
                self.assertIsNone(assignment.lease)
                self.assertTrue(all(path.read_text() == "recovery artifact" for path in paths))
                self.assertEqual(sum(lease.released_at is None for lease in f.workspaces.store.load().leases), 0)
        events = f.workers.events(f.actor)
        self.assertEqual(sum(event.event_type == "assignment_cancelled" for event in events), 5)
        self.assertTrue(all(event.actor_id == f.actor.identity_id for event in events if event.event_type == "assignment_cancelled"))

    async def test_cancellation_requires_admin_tenant_and_observed_fence_and_rejects_late_completion(self):
        f = self.fixture
        binding = self.prepare("guard")
        claimed = self.claim(binding.assignment_id)
        f.workers.start(f.worker.id, claimed.id,
                       AssignmentStartRequest(fence=claimed.fence, lease_token=claimed.lease.lease_token),
                       actor=self.worker_actor)
        request = AssignmentCancelRequest(expected_fence=claimed.fence, reason="operator recovery")
        with self.assertRaises(AuthorizationError):
            f.workers.cancel_bootstrap(claimed.id, request, actor=self.worker_actor)
        with self.assertRaises(WorkerConflictError):
            f.workers.cancel_bootstrap(claimed.id, request.model_copy(update={"expected_fence": 0}), actor=f.actor)
        foreign = f.actor.model_copy(update={"workspace_id": "other-tenant"})
        with self.assertRaisesRegex(Exception, "not found"):
            f.workers.cancel_bootstrap(claimed.id, request, actor=foreign)
        cancelled = f.workers.cancel_bootstrap(claimed.id, request, actor=f.actor)
        self.assertEqual(cancelled.fence, claimed.fence + 1)
        with self.assertRaises(WorkerLeaseError):
            f.workers.complete(f.worker.id, claimed.id,
                              AssignmentCompleteRequest(fence=claimed.fence, lease_token=claimed.lease.lease_token, succeeded=True),
                              actor=self.worker_actor)
        # The same guarded command can reconcile the separate workspace write.
        self.assertEqual(f.workers.cancel_bootstrap(claimed.id, request, actor=f.actor), cancelled)
        self.assertEqual(sum(event.event_type == "assignment_cancelled" for event in f.workers.events(f.actor)), 1)

    async def test_claim_race_prevents_failed_start_cleanup_from_cancelling_another_claim(self):
        f = self.fixture
        binding = self.prepare("race")
        test = self
        class RacingSession:
            def __init__(self, *args, **kwargs):
                self.fence = None
            async def start(self):
                test.claim(binding.assignment_id)
                raise RuntimeError("another claimant won")
            async def stop(self):
                pass
        manager = AssignmentBoundAgentProcessSessionManager(
            self.local_worker, None, runtime_factory=None, credential_provider=None,
            session_factory=RacingSession,
        )
        with self.assertRaisesRegex(WorkerConflictError, "fence changed"):
            await manager.start(binding.assignment_id)
        self.assertEqual(f.workers.store.assignment(binding.assignment_id).status, AssignmentStatus.CLAIMED)
        self.assertEqual(f.workspaces.get(binding.workspace_id, f.actor).status, ExecutionWorkspaceStatus.ACTIVE)

    async def test_cancelled_scratch_reservation_retains_ignored_and_untracked_artifacts(self):
        f = self.fixture
        binding = f.service.prepare_bootstrap(
            bootstrap_id="scratch-bootstrap", execution_id="scratch-execution",
            project_id=f.project.id, sandbox="workspace-write", approval_policy="on-request",
            execution_profile_id="orchestration-only",
        )
        workspace = f.workspaces.get(binding.workspace_id, f.actor)
        artifact = Path(workspace.path) / ".ignored-artifact"
        artifact.write_text("preserve scratch evidence")
        f.workers.cancel_bootstrap(binding.assignment_id, AssignmentCancelRequest(expected_fence=0, reason="scratch failed"), actor=f.actor)
        self.assertEqual(artifact.read_text(), "preserve scratch evidence")
        self.assertEqual(sum(lease.released_at is None for lease in f.workspaces.store.load().leases), 0)

    async def test_cancel_endpoint_requires_step_up_and_returns_canonical_recovery_evidence(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from codex_web.api.execution_workers import build_execution_workers_router
        f = self.fixture
        binding = self.prepare("api")
        actor = f.actor.model_copy(update={"assurance": AuthenticationAssurance.PRIMARY})
        app = FastAPI()
        @app.middleware("http")
        async def identity(request, call_next):
            request.state.identity_actor = actor
            return await call_next(request)
        app.include_router(build_execution_workers_router(f.workers))
        endpoint = f"/api/execution-workers/assignments/{binding.assignment_id}/cancel-bootstrap"
        with TestClient(app) as client:
            self.assertEqual(client.post(endpoint, json={"expected_fence": 0, "reason": "operator recovery"}).status_code, 403)
            actor = f.actor
            response = client.post(endpoint, json={"expected_fence": 0, "reason": "operator recovery"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["item"]["status"], "cancelled")
            self.assertEqual(response.json()["item"]["fence"], 1)
            self.assertIsNone(response.json()["item"]["lease"])
            self.assertEqual(client.post(endpoint, json={"expected_fence": 9, "reason": "stale request"}).status_code, 409)

    async def test_cancel_endpoint_slow_storage_keeps_event_loop_responsive_and_preserves_authority(self):
        from concurrent.futures import ThreadPoolExecutor
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from codex_web.api.execution_workers import build_execution_workers_router
        f = self.fixture
        binding = self.prepare("api-backpressure")
        entered = threading.Event()
        release = threading.Event()
        ping_served = threading.Event()
        calls = []
        actor = f.actor
        original = f.workers.cancel_bootstrap
        def slow_cancel(*args, **kwargs):
            calls.append(kwargs["actor"])
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test storage barrier timed out")
            return original(*args, **kwargs)
        app = FastAPI()
        @app.middleware("http")
        async def identity(request, call_next):
            request.state.identity_actor = actor
            return await call_next(request)
        @app.get("/ping")
        async def ping():
            ping_served.set()
            return {"ok": True}
        app.include_router(build_execution_workers_router(f.workers))
        endpoint = f"/api/execution-workers/assignments/{binding.assignment_id}/cancel-bootstrap"
        with TestClient(app) as client, ThreadPoolExecutor(max_workers=2) as executor:
            with patch.object(f.workers, "cancel_bootstrap", side_effect=slow_cancel):
                pending = executor.submit(client.post, endpoint, json={"expected_fence": 0, "reason": "recovery"})
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    ping_request = executor.submit(client.get, "/ping")
                    self.assertTrue(await asyncio.to_thread(ping_served.wait, 2), "canonical storage must not block the event loop")
                finally:
                    release.set()
                self.assertEqual(pending.result(timeout=3).status_code, 200)
                self.assertEqual(ping_request.result(timeout=3).status_code, 200)
                self.assertEqual(calls, [f.actor])
                actor = f.actor.model_copy(update={"assurance": AuthenticationAssurance.PRIMARY})
                self.assertEqual(client.post(endpoint, json={"expected_fence": 0, "reason": "denied"}).status_code, 403)
                self.assertEqual(calls, [f.actor], "denied authority cannot reach cancellation storage")

    async def test_failed_completion_normalizes_long_and_blank_reasons_before_freeing_reservations(self):
        from codex_web.services.cli_worker_session import AssignmentBoundCliSessionManager
        f = self.fixture
        managers = [
            AssignmentBoundAgentProcessSessionManager(
                self.local_worker, None, runtime_factory=None, credential_provider=None,
            ),
            AssignmentBoundCliSessionManager(
                self.local_worker, runtime_binding=f.service.runtime_binding,
            ),
        ]
        for index, manager in enumerate(managers):
            for tag, message, expected in [("long", "x" * 600, "x" * 500), ("blank", "   ", "bootstrap failed")]:
                binding = self.prepare(f"reason-{index}-{tag}")
                claim = self.claim(binding.assignment_id)
                f.workers.start(f.worker.id, claim.id,
                               AssignmentStartRequest(fence=claim.fence, lease_token=claim.lease.lease_token),
                               actor=self.worker_actor)
                session = SimpleNamespace(fence=claim.fence, stop=AsyncMock())
                manager.sessions[binding.assignment_id] = session
                cancelled = await manager.complete(binding.assignment_id, succeeded=False, failure_message=message)
                self.assertEqual(cancelled.status, AssignmentStatus.CANCELLED)
                self.assertEqual(cancelled.failure_message, expected)
                session.stop.assert_awaited_once()
                self.assertEqual(sum(lease.released_at is None for lease in f.workspaces.store.load().leases), 0)

    async def test_start_timeout_settles_executor_claim_then_fences_and_stops_unregistered_session(self):
        f = self.fixture
        binding = self.prepare("timeout")
        entered = threading.Event()
        release = threading.Event()
        stopped = []
        test = self
        class DelayedSession:
            def __init__(self, *args, **kwargs):
                self.fence = None
            def claim_in_executor(self):
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("test claim gate timed out")
                assignment = test.claim(binding.assignment_id)
                self.fence = assignment.fence
            async def start(self):
                await asyncio.to_thread(self.claim_in_executor)
            async def stop(self):
                stopped.append(self.fence)
        manager = AssignmentBoundAgentProcessSessionManager(
            self.local_worker, None, runtime_factory=None, credential_provider=None,
            session_factory=DelayedSession,
        )
        start_task = asyncio.create_task(manager.start(binding.assignment_id))
        task = None
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 5), "executor claim must enter before triggering timeout")
            task = asyncio.create_task(asyncio.wait_for(start_task, 0.05))
            await asyncio.sleep(0.1)
            self.assertFalse(task.done(), "timeout must settle the executor claim before cleanup")
            release.set()
            with self.assertRaises(TimeoutError):
                await task
        finally:
            release.set()
            await asyncio.gather(start_task, *([task] if task is not None else []), return_exceptions=True)
        assignment = f.workers.store.assignment(binding.assignment_id)
        self.assertEqual(assignment.status, AssignmentStatus.CANCELLED)
        self.assertEqual(assignment.fence, 2)
        self.assertIsNone(assignment.lease)
        self.assertEqual(stopped, [1])
        self.assertIsNone(manager.get(binding.assignment_id))
        self.assertEqual(sum(lease.released_at is None for lease in f.workspaces.store.load().leases), 0)

    async def test_supersession_reconciles_cancelled_predecessor_after_release_failure(self):
        from codex_web.runtime.execution import TurnExecutionService
        f = self.fixture
        binding = self.prepare("supersede-retry")
        request = AssignmentCancelRequest(expected_fence=0, reason="failed startup")
        with patch.object(f.workspaces, "release", side_effect=RuntimeError("store unavailable")):
            with self.assertRaises(RuntimeError):
                f.workers.cancel_bootstrap(binding.assignment_id, request, actor=f.actor)
        manager = SimpleNamespace(local_worker=self.local_worker, get=lambda key: None)
        rebindings = []
        service = TurnExecutionService(
            SimpleNamespace(_append_bot_event=lambda event: None),
            binding_service=f.service, control_actor=f.actor,
            bootstrap_bindings=SimpleNamespace(rebind=lambda **kwargs: rebindings.append(kwargs)),
            session_managers={("openai", "codex"): manager},
        )
        service._bootstrap_binding_for_thread = lambda key: rebindings[-1]
        result = await service._supersede_thread_bootstrap(
            thread_id="thread-existing", project=f.project,
            runtime_binding=f.service.runtime_binding, sandbox="workspace-write",
            approval_policy="on-request", execution_profile_id=None,
            agent_profile=None, explicit_repository_id=None,
            previous_assignment_id=binding.assignment_id,
        )
        self.assertNotEqual(result["assignment_id"], binding.assignment_id)
        previous_workspace = f.workspaces.get(binding.workspace_id, f.actor)
        self.assertEqual(previous_workspace.status, ExecutionWorkspaceStatus.RELEASED)
        self.assertIsNone(previous_workspace.cleaned_at)
        self.assertEqual(sum(lease.released_at is None for lease in f.workspaces.store.load().leases), 1)

    async def test_workspace_release_failure_is_reconciled_by_repeating_canonical_cancellation(self):
        f = self.fixture
        binding = self.prepare("release-retry")
        request = AssignmentCancelRequest(expected_fence=0, reason="failed startup")
        with patch.object(f.workspaces, "release", side_effect=RuntimeError("store unavailable")):
            with self.assertRaisesRegex(RuntimeError, "store unavailable"):
                f.workers.cancel_bootstrap(binding.assignment_id, request, actor=f.actor)
        self.assertEqual(f.workers.store.assignment(binding.assignment_id).status, AssignmentStatus.CANCELLED)
        f.workers.cancel_bootstrap(binding.assignment_id, request, actor=f.actor)
        self.assertEqual(sum(lease.released_at is None for lease in f.workspaces.store.load().leases), 0)
