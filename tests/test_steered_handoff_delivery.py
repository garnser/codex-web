from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from fastapi import HTTPException

from codex_web.agent_profiles import AgentProfileExecutionBinding
from codex_web.execution_workers import ExecutionRuntimeBinding
from codex_web.models import Project, QueuedTurn
from tests import test_handoff_delivery_deadlines as delivery_fixtures
from tests import test_turn_execution as execution_fixtures
from tests import test_turn_service as turn_fixtures


class SteeredHandoffDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.delivery = delivery_fixtures.HandoffDeliveryPersistenceTests()
        self.delivery.setUp()
        self.addCleanup(self.delivery.doCleanups)
        state = self.delivery.state.model_copy(update={"project_id": "p1"})
        self.delivery.repository.put(state.ref, state)
        self.ref = state.ref
        self.service, self.queue, self.execution, _, _ = turn_fixtures.TurnServiceTests()._service()
        self.execution.active = False
        self.service.projects.get = lambda _: Project(
            id="p1", organization_id="org", workspace_id="workspace",
            name="Project", path="/workspace/project")
        self.queue.queued = [QueuedTurn(
            id="original-queued", execution_id="original-execution", thread_id="t1",
            project_id="p1", message="Review the original scoped work",
            work_item_ref=state.ref, handoff_requested_at=100,
            repository_resource_id="repo-docs", writable_repository_resource_ids=("repo-docs",),
            execution_profile_id="repository-write", source="work-item-handoff", created_at=100)]
        _, _, self.sessions, self.runtime = execution_fixtures.TurnExecutionStartTests()._service(
            bootstrap_thread_id="t1")
        runtime = ExecutionRuntimeBinding(provider_id="openai", runtime_id="codex", capability_revision=1)
        profile = AgentProfileExecutionBinding(
            profile_id="reviewer", profile_revision=4, profile_record_id="reviewer-r4",
            role_id="quinn", execution_profile_id="repository-write",
            selected_provider_id="openai", selected_runtime_id="codex")
        assignment = self.sessions.session.validate_current()
        assignment.runtime_binding = runtime
        assignment.agent_profile = profile
        assignment.organization_id = "org"
        assignment.workspace_id = "workspace"
        assignment.execution_profile_id = "repository-write"
        assignment.repository_target = SimpleNamespace(mutable_repository_id="repo-docs")
        assignment.repository_scope = SimpleNamespace(
            writable_repository_ids=("repo-docs",), read_only_repository_ids=())
        self.sessions.session.validate_current = MagicMock(return_value=assignment)
        self.runtime._select_runtime_binding = AsyncMock(return_value=(runtime, profile))
        self.runtime.handoff_delivery_recorder = self.delivery.service.record_handoff_delivery
        # Compose production steering, native admission and persisted delivery.
        # Provider transport, custody and runtime/profile selection use fixtures;
        # independently reopened SQLite verifies the resulting canonical state.
        self.execution.start_thread_turn_now = self.runtime.start_thread_turn_now

    def current(self):
        return self.delivery.reopen().load()[self.ref]

    async def test_steered_native_start_persists_scoped_delivery_without_accepting_work(self):
        result = await self.service.steer("t1", "original-queued")
        self.assertTrue(result["ok"])
        current = self.current()
        self.assertEqual(current.handoff.delivery.status, "delivered")
        self.assertEqual(current.handoff.delivery.turn_id, result["turn"]["id"])
        self.assertTrue(current.handoff.delivery.scope_verified)
        self.assertEqual(current.handoff.delivery.repository_resource_id, "repo-docs")
        self.assertEqual(current.handoff.delivery.writable_repository_resource_ids, ("repo-docs",))
        self.assertEqual(current.handoff.requested_at, 100)
        self.assertIsNone(current.handoff.acknowledged_at)
        self.assertEqual(current.current_owner, "nora")
        self.assertEqual(self.queue.queued, [])

    async def test_unknown_native_start_preserves_original_without_delivery(self):
        original = self.queue.queued[0].model_dump(mode="json")
        request = self.sessions.session.request

        async def unknown(method, params=None):
            if method == "turn/start":
                raise asyncio.TimeoutError("unknown provider outcome")
            return await request(method, params)

        self.sessions.session.request = unknown
        with self.assertRaises(HTTPException):
            await self.service.steer("t1", "original-queued")
        self.assertEqual(self.current().handoff.delivery.status, "awaiting_dispatch")
        self.assertEqual([item.model_dump(mode="json") for item in self.queue.queued], [original])

    async def test_original_nonce_cannot_qualify_a_superseding_handoff(self):
        self.delivery.repository.update(self.ref, lambda state: state.model_copy(update={
            "handoff": state.handoff.model_copy(update={"requested_at": 101})}))
        self.assertTrue((await self.service.steer("t1", "original-queued"))["ok"])
        current = self.current()
        self.assertEqual(current.handoff.requested_at, 101)
        self.assertEqual(current.handoff.delivery.status, "awaiting_dispatch")

    async def test_legacy_queue_without_nonce_does_not_invent_delivery(self):
        self.queue.queued[0].handoff_requested_at = None
        self.assertTrue((await self.service.steer("t1", "original-queued"))["ok"])
        self.assertEqual(self.current().handoff.delivery.status, "awaiting_dispatch")

    async def test_steering_does_not_bypass_the_existing_autonomy_pause(self):
        self.runtime.automated_admission_allowed = lambda: False
        with self.assertRaises(HTTPException) as caught:
            await self.service.steer("t1", "original-queued")
        self.assertEqual(caught.exception.detail["code"], "autonomy_paused")
        self.assertFalse(any(method == "turn/start" for method, _ in self.sessions.session.requests))
        self.assertEqual(self.current().handoff.delivery.status, "awaiting_dispatch")


if __name__ == "__main__":
    unittest.main()
