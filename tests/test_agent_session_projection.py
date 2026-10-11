from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from codex_web.agent_runtime import AgentRuntimeSessionRequest, AgentRuntimeHealth
from codex_web.agent_runtime_usage import AgentRuntimeUsage
from codex_web.api.agent_sessions import build_agent_sessions_router
from tests import test_agent_runtime_telemetry as fixtures
from pydantic import ValidationError


class AgentSessionProjectionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.AgentRuntimeTelemetryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.actor = self.fixture.actor
        self.runtime = self.fixture.runtime
        self.runtime.health = AsyncMock(return_value=AgentRuntimeHealth.HEALTHY)
        self.authenticated = True

    def session(self, number, *, actor=None, runtime=None):
        runtime = runtime or self.runtime
        return self.fixture.session_service.adopt(
            provider_id=runtime.provider_id, runtime_id=runtime.runtime_id,
            runtime_type=runtime.runtime_type,
            provider_native_session_id=f"native-{number}",
            request=AgentRuntimeSessionRequest(project_id="project-a"),
            actor=actor or self.actor, capability_snapshot=runtime.capabilities,
            capability_revision=4,
        )

    def usage(self, session, record_id, observed_at, **changes):
        raw = dict(id=record_id, organization_id=session.organization_id,
                   workspace_id=session.workspace_id, agent_session_id=session.id,
                   provider_id=session.provider_id, runtime_id=session.runtime_id,
                   runtime_type=session.runtime_type, observed_at=observed_at)
        raw.update(changes)
        record = AgentRuntimeUsage(**raw)
        self.fixture.usage.upsert(record)
        return record

    def client(self, *, telemetry=True):
        app = FastAPI()
        @app.middleware("http")
        async def actor(request, call_next):
            if self.authenticated:
                request.state.identity_actor = self.actor
            return await call_next(request)
        app.include_router(build_agent_sessions_router(
            self.fixture.session_service, self.fixture.registry,
            self.fixture.service if telemetry else None,
        ))
        return TestClient(app)

    def test_list_batches_reads_preserves_latest_order_and_refreshes(self):
        sessions = [self.fixture.session] + [self.session(i) for i in range(10)]
        for session in sessions[:-1]:
            self.usage(session, "old-" + session.id, 1)
            self.usage(session, "a-" + session.id, 2)
            self.usage(session, "z-" + session.id, 2)
        with self.client() as client, patch.object(
            self.fixture.service, "list", wraps=self.fixture.service.list,
        ) as read:
            response = client.get("/api/agent-sessions")
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["count"], len(sessions))
            read.assert_called_once_with(self.actor)
            self.runtime.health.assert_awaited_once()
            by_id = {item["id"]: item for item in data["items"]}
            for session in sessions[:-1]:
                self.assertEqual(by_id[session.id]["latest_usage"]["id"], "z-" + session.id)
            self.assertIsNone(by_id[sessions[-1].id]["latest_usage"])
            self.usage(sessions[0], "newest", 3)
            self.runtime.health.return_value = AgentRuntimeHealth.UNAVAILABLE
            second = client.get("/api/agent-sessions").json()
            by_id = {item["id"]: item for item in second["items"]}
            self.assertEqual(by_id[sessions[0].id]["latest_usage"]["id"], "newest")
            self.assertEqual(by_id[sessions[0].id]["runtime_health"], "unavailable")
            self.assertEqual(read.call_count, 2)
            self.assertEqual(self.runtime.health.await_count, 2)

    def test_tenant_scope_empty_and_unauthenticated_requests(self):
        visible = self.fixture.session
        self.usage(visible, "own", 1)
        foreign = self.session("foreign", actor=fixtures._actor("org-b", "workspace-b"))
        self.usage(foreign, "foreign", 100)
        # Even a foreign record naming the visible session cannot cross scope.
        self.usage(visible, "foreign-alias", 200, organization_id="org-b")
        with self.client() as client, patch.object(
            self.fixture.service, "list", wraps=self.fixture.service.list,
        ) as read:
            result = client.get("/api/agent-sessions").json()
            self.assertEqual(result["count"], 1)
            self.assertEqual(result["items"][0]["latest_usage"]["id"], "own")
            read.reset_mock()
            self.actor = fixtures._actor("empty", "empty")
            self.assertEqual(client.get("/api/agent-sessions").json(), {"items": [], "count": 0})
            read.assert_not_called()
            self.authenticated = False
            self.assertEqual(client.get("/api/agent-sessions").status_code, 401)
            read.assert_not_called()

    def test_runtime_health_is_grouped_and_failures_remain_request_local(self):
        self.session(1)
        alternate = fixtures._Runtime()
        alternate.runtime_id = "alternate"
        alternate.health = AsyncMock(side_effect=RuntimeError("offline"))
        self.fixture.registry.register(alternate, capability_revision=4)
        self.session(2, runtime=alternate)
        self.session(3, runtime=alternate)
        with self.client(telemetry=False) as client:
            response = client.get("/api/agent-sessions")
            self.assertEqual(response.status_code, 200)
            for item in response.json()["items"]:
                self.assertIsNone(item["latest_usage"])
                expected = "unavailable" if item["runtime_id"] == "alternate" else "healthy"
                self.assertEqual(item["runtime_health"], expected)
            self.runtime.health.assert_awaited_once()
            alternate.health.assert_awaited_once()
            alternate.health.side_effect = None
            alternate.health.return_value = AgentRuntimeHealth.HEALTHY
            again = client.get("/api/agent-sessions").json()
            self.assertTrue(all(item["runtime_health"] == "healthy" for item in again["items"]))

    def test_single_session_and_invalid_telemetry_keep_existing_behavior(self):
        session = self.fixture.session
        record = self.usage(session, "latest", 10)
        with self.client() as client, patch.object(
            self.fixture.service, "list", wraps=self.fixture.service.list,
        ) as read:
            result = client.get("/api/agent-sessions/" + session.id)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()["item"]["latest_usage"], record.model_dump(mode="json"))
            read.assert_called_once_with(self.actor, agent_session_id=session.id)
            foreign = self.session("foreign", actor=fixtures._actor("org-b", "workspace-b"))
            read.reset_mock()
            self.assertEqual(client.get("/api/agent-sessions/" + foreign.id).status_code, 404)
            read.assert_not_called()
        self.fixture.usage.store.record_apply(self.fixture.usage.namespace, upserts={"invalid": {}})
        with self.client() as client:
            with self.assertRaises(ValidationError):
                client.get("/api/agent-sessions")
