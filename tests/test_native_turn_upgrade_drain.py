from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

from codex_web.models import Project
from tests import test_turn_execution as turns
from tests import test_upgrades as upgrades


class NativeUpgradeDrainTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.upgrade = upgrades.UpgradeTests(methodName="runTest")
        await self.upgrade.asyncSetUp()
        self.plan = self.upgrade.create_plan(require_drain=True)
        self.host, self.binding, self.sessions, self.service = turns.TurnExecutionStartTests()._service(bootstrap_thread_id="t1")
        self.service.upgrade_turn_admission_guard = self.upgrade.service.worker_assignment_allowed
        original = self.sessions.session.validate_current
        def current():
            assignment = original()
            assignment.organization_id = "org-a"
            assignment.workspace_id = "ws-a"
            return assignment
        self.sessions.session.validate_current = current
        self.service._assignment_record = lambda _id: current()
        self.project = Project(id="p1", name="Project", path="/workspace/project", organization_id="org-b", workspace_id="ws-b")

    async def asyncTearDown(self):
        await self.upgrade.asyncTearDown()

    async def start(self):
        return await self.service.start_thread_turn_now("t1", project=self.project, message="review", sandbox="workspace-write", approval_policy="on-request", actor=SimpleNamespace(identity_id="caller-b", organization_id="org-b", workspace_id="ws-b"))

    async def test_existing_bound_turn_uses_assignment_scope_not_caller_or_project(self):
        self.upgrade.service.start_drain(self.plan.id, actor=self.upgrade.actor)
        with self.assertRaises(HTTPException) as caught:
            await self.start()
        self.assertEqual(caught.exception.detail["code"], "upgrade_maintenance")
        self.assertEqual(self.sessions.session.requests, [])
        self.assertEqual(self.host.active, {})
        self.assertEqual(self.sessions.stopped, [])

    async def test_drain_entered_during_native_resume_blocks_turn_start(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.sessions.session.request
        async def request(method, params=None):
            if method == "thread/resume":
                entered.set()
                await release.wait()
            return await original(method, params)
        self.sessions.session.request = request
        task = asyncio.create_task(self.start())
        await asyncio.wait_for(entered.wait(), 2)
        self.upgrade.service.start_drain(self.plan.id, actor=self.upgrade.actor)
        release.set()
        with self.assertRaises(HTTPException) as caught:
            await task
        self.assertEqual(caught.exception.detail["code"], "upgrade_maintenance")
        self.assertNotIn("turn/start", [name for name, _ in self.sessions.session.requests])
        self.assertEqual(self.host.active, {})
        self.assertEqual(self.sessions.stopped, [])

    async def test_drain_in_other_scope_does_not_pause_existing_owner(self):
        other = self.upgrade.actor.model_copy(update={"organization_id": "org-b", "workspace_id": "ws-b"})
        plan = self.upgrade.service.create(upgrades.UpgradePlanCreate(release_id="release-target", current_app_version="1.0.0", target_app_version="1.1.0", compatibility=self.upgrade.profile(), observed_control_plane_versions=("1.0.0", "1.1.0"), steps=(), require_drain=True), actor=other)
        self.upgrade.service.start_drain(plan.id, actor=other)
        result = await self.start()
        self.assertEqual(result["turn"]["id"], "turn-1")

    async def test_rollback_releases_scope_and_retained_fifo_can_continue(self):
        self.upgrade.service.start_drain(self.plan.id, actor=self.upgrade.actor)
        self.host._project = lambda _id: self.project
        queued = self.service.enqueue_turn(thread_id="t1", project_id="p1", message="retained", source="slack")
        await self.service.drain_thread_queue("t1")
        self.assertEqual(self.host.queues["t1"][0].id, queued.id)
        self.upgrade.service.mark_rolled_back(self.plan.id, actor=self.upgrade.actor, reason="qualified source restored")
        await self.service.drain_thread_queue("t1")
        self.assertNotIn("t1", self.host.queues)
        self.assertIn("turn/start", [name for name, _ in self.sessions.session.requests])
        self.assertEqual(self.sessions.stopped, [])

    async def test_unknown_assignment_scope_fails_closed_without_starting_or_killing(self):
        self.service._assignment_record = lambda _id: None
        with self.assertRaises(HTTPException) as caught:
            await self.start()
        self.assertEqual(caught.exception.detail["code"], "upgrade_turn_scope_unknown")
        self.assertEqual(self.sessions.session.requests, [])
        self.assertEqual(self.sessions.stopped, [])

    async def test_unknown_scope_retains_fifo_without_automatic_retry(self):
        self.service._assignment_record = lambda _id: None
        self.host._project = lambda _id: self.project
        first = self.service.enqueue_turn(thread_id="t1", project_id="p1", message="first", source="slack")
        second = self.service.enqueue_turn(thread_id="t1", project_id="p1", message="second", source="slack")
        with patch.object(asyncio.get_running_loop(), "call_later") as retry:
            await self.service.drain_thread_queue("t1")
        self.assertEqual([item.id for item in self.host.queues["t1"]], [first.id, second.id])
        self.assertEqual(first.attempts, 0)
        retry.assert_not_called()
        self.assertEqual(self.sessions.stopped, [])

    async def test_pruned_old_assignment_preserves_authorized_profile_recovery(self):
        self.sessions.session = None
        runtime = turns.ExecutionRuntimeBinding(provider_id="openai", runtime_id="codex", capability_revision=1)
        profile = turns.AgentProfileExecutionBinding(profile_id="reviewer", profile_revision=4, profile_record_id="reviewer-r4", selected_provider_id="openai", selected_runtime_id="codex")
        actor = SimpleNamespace(identity_id="requesting-human")
        self.service._select_runtime_binding = AsyncMock(return_value=(runtime, profile))
        recovered = turns._Session()
        fresh = recovered.validate_current()
        fresh.id = "assignment-new"
        fresh.organization_id = "org-b"
        fresh.workspace_id = "ws-b"
        fresh.runtime_binding = runtime
        fresh.agent_profile = profile
        recovered.validate_current = lambda: fresh
        self.sessions.start = AsyncMock(side_effect=[RuntimeError("assignment not found"), recovered])
        self.service._assignment_record = lambda aid: fresh if aid == "assignment-new" else None
        bootstrap = self.service.bootstrap_bindings.get_by_thread("t1", self.service.control_actor)
        bootstrap.assignment_id = "assignment-new"
        self.service._supersede_thread_bootstrap = AsyncMock(return_value=bootstrap)
        try:
            result = await self.service.start_thread_turn_now("t1", project=self.project, message="recover", sandbox="workspace-write", approval_policy="on-request", actor=actor, agent_profile_id="reviewer", agent_profile_revision=4)
        except HTTPException as exc:
            self.fail(f"Pruned old row prevented canonical profile recovery: {exc.status_code} {exc.detail}")
        self.assertEqual(result["turn"]["id"], "turn-1")
        self.service._select_runtime_binding.assert_awaited_once_with(project_id="p1", sandbox="workspace-write", trusted_local_codex_session=False, actor=actor, agent_profile_id="reviewer", agent_profile_revision=4)
        healing = self.service._supersede_thread_bootstrap.await_args.kwargs
        self.assertIs(healing["agent_profile"], profile)
        self.assertEqual(healing["previous_assignment_id"], "assignment-1")
        self.assertEqual(self.host.active["t1"].assignment_id, "assignment-new")
        self.host.codex.request.assert_not_awaited()

    async def test_pruned_old_assignment_does_not_bypass_profile_routing_denial(self):
        self.sessions.session = None
        self.service._assignment_record = lambda _id: None
        self.service._select_runtime_binding = AsyncMock(side_effect=HTTPException(status_code=403, detail="profile access denied"))
        self.sessions.start = AsyncMock(side_effect=RuntimeError("assignment not found"))
        self.service._supersede_thread_bootstrap = AsyncMock()
        with self.assertRaises(HTTPException) as caught:
            await self.service.start_thread_turn_now("t1", project=self.project, message="recover", sandbox="workspace-write", approval_policy="on-request", agent_profile_id="reviewer")
        self.assertEqual(caught.exception.status_code, 403)
        self.service._supersede_thread_bootstrap.assert_not_awaited()
        self.assertEqual(self.host.active, {})
        self.host.codex.request.assert_not_awaited()

    async def test_live_native_process_with_real_stale_validation_cannot_fallback_or_lose_fifo(self):
        from codex_web.services.agent_process_session import AssignmentBoundAgentProcessSession
        from codex_web.services.agent_worker_session import AssignmentBoundAgentSessionStaleError
        from codex_web.execution_workers import WorkerLifecycle
        worker = SimpleNamespace(id="worker-1", organization_id="org-a", workspace_id="ws-a", lifecycle=WorkerLifecycle.ACTIVE)
        local_worker = SimpleNamespace(worker=worker, worker_actor=SimpleNamespace(organization_id="org-a", workspace_id="ws-a"), worker_service=SimpleNamespace(store=SimpleNamespace(worker=MagicMock(return_value=worker))), _pending_assignment=MagicMock(side_effect=AssignmentBoundAgentSessionStaleError("canonical assignment row is missing")))
        session = AssignmentBoundAgentProcessSession(local_worker, self.host, "assignment-1", runtime_factory=MagicMock(), credential_provider=SimpleNamespace())
        session.fence = 7
        session.delegation = SimpleNamespace(expires_at=9999999999)
        proc = SimpleNamespace(poll=MagicMock(return_value=None))
        session.runtime = SimpleNamespace(proc=proc, ready=SimpleNamespace(is_set=lambda: True))
        with self.assertRaises(AssignmentBoundAgentSessionStaleError):
            session.validate_current()
        local_worker._pending_assignment.assert_called_once_with("assignment-1")
        self.sessions.session = session
        self.service._assignment_record = lambda _id: None
        with self.assertRaises(HTTPException):
            await self.service._require_upgrade_turn_admission("t1", self.project, "assignment-1", allow_bootstrap_recovery=True)
        with self.assertRaises(HTTPException) as caught:
            await self.start()
        self.assertEqual(caught.exception.detail["code"], "upgrade_turn_scope_unknown")
        self.assertEqual(self.sessions.stopped, [])
        self.assertIsNone(proc.poll())
        self.assertEqual(self.host.active, {})
        self.host._project = lambda _id: self.project
        first = self.service.enqueue_turn(thread_id="t1", project_id="p1", message="first", source="slack")
        second = self.service.enqueue_turn(thread_id="t1", project_id="p1", message="second", source="slack")
        with patch.object(asyncio.get_running_loop(), "call_later") as retry:
            await self.service.drain_thread_queue("t1")
        self.assertEqual([x.id for x in self.host.queues["t1"]], [first.id, second.id])
        self.assertEqual(first.attempts, 0)
        self.assertEqual(self.sessions.stopped, [])
        retry.assert_not_called()
        self.assertIsNone(proc.poll())

    async def test_unknown_process_exit_does_not_allow_pruned_scope_fallback(self):
        self.service._assignment_record = lambda _id: None
        for poll in [None, MagicMock(side_effect=RuntimeError("unknown")), MagicMock(return_value=None)]:
            with self.subTest(poll_available=poll is not None):
                self.sessions.session.runtime = SimpleNamespace(proc=SimpleNamespace(poll=poll))
                with self.assertRaises(HTTPException) as caught:
                    await self.start()
                self.assertEqual(caught.exception.detail["code"], "upgrade_turn_scope_unknown")
                self.assertEqual(self.sessions.stopped, [])

    async def test_observed_exited_process_allows_canonical_project_recovery_gate(self):
        self.service._assignment_record = lambda _id: None
        self.sessions.session.runtime = SimpleNamespace(proc=SimpleNamespace(poll=lambda: 0))
        await self.service._require_upgrade_turn_admission("t1", self.project, "assignment-1", allow_bootstrap_recovery=True)
        with self.assertRaises(HTTPException):
            await self.service._require_upgrade_turn_admission("t1", self.project, "assignment-1")
        self.assertEqual(self.sessions.stopped, [])

    async def test_fifo_maintenance_wait_preserves_order_attempts_and_runtime(self):
        self.upgrade.service.start_drain(self.plan.id, actor=self.upgrade.actor)
        self.host._project = lambda _id: self.project
        first = self.service.enqueue_turn(thread_id="t1", project_id="p1", message="first", source="slack")
        second = self.service.enqueue_turn(thread_id="t1", project_id="p1", message="second", source="slack")
        with patch.object(self.service, "schedule_queue_drain") as schedule:
            await self.service.drain_thread_queue("t1")
        self.assertEqual([item.id for item in self.host.queues["t1"]], [first.id, second.id])
        self.assertEqual(first.attempts, 0)
        self.assertEqual(self.sessions.stopped, [])
        self.assertEqual(self.host.active, {})
        schedule.assert_not_called()


if __name__ == "__main__":
    unittest.main()
