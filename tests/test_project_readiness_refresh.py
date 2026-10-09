from __future__ import annotations

import unittest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from codex_web.api.project_readiness import build_project_readiness_router
from codex_web.execution_workers import ExecutionRuntimeBinding
from tests.test_project_readiness import ProjectReadinessTests, _actor


class ProjectReadinessRefreshTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = ProjectReadinessTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.available = False
        self.refreshes = 0
        self.service = self.fixture.service(
            runtime_binding=ExecutionRuntimeBinding(
                provider_id='openai', runtime_id='codex', capability_revision=1,
                authentication_mode='trusted_local_session'),
            local_session_probe=lambda: self.available,
        )
        self.service.local_session_refresh = self.refresh

    async def refresh(self):
        self.refreshes += 1
        self.available = True

    async def test_expired_account_evidence_is_refreshed_and_recorded(self):
        value = await self.service.evaluate_with_refresh('project-a', actor=_actor())
        self.assertTrue(value.execution_ready)
        self.assertEqual(self.refreshes, 1)
        self.assertEqual(value.last_successful_verification_at, value.generated_at)
        await self.service.evaluate_with_refresh('project-a', actor=_actor())
        self.assertEqual(self.refreshes, 1)
        self.available = False
        value = await self.service.evaluate_with_refresh('project-a', actor=_actor())
        self.assertTrue(value.execution_ready)
        self.assertEqual(self.refreshes, 2)

    async def test_failed_account_read_keeps_authentication_blocked(self):
        async def fail():
            raise RuntimeError('metadata unavailable')
        self.service.local_session_refresh = fail
        value = await self.service.evaluate_with_refresh('project-a', actor=_actor())
        self.assertFalse(value.execution_ready)
        self.assertIn('local_session_unavailable', [c.code for c in value.checks])

    async def test_negative_account_response_keeps_authentication_blocked(self):
        async def negative():
            self.refreshes += 1
        self.service.local_session_refresh = negative
        value = await self.service.evaluate_with_refresh('project-a', actor=_actor())
        self.assertFalse(value.execution_ready)
        self.assertEqual(self.refreshes, 1)

    async def test_scope_is_checked_before_account_refresh(self):
        with self.assertRaises(LookupError):
            await self.service.evaluate_with_refresh('other-project', actor=_actor())
        self.assertEqual(self.refreshes, 0)

    async def test_readiness_http_route_refreshes_expired_evidence(self):
        app = FastAPI()
        @app.middleware('http')
        async def actor(request, call_next):
            request.state.identity_actor = _actor()
            return await call_next(request)
        app.include_router(build_project_readiness_router(self.service))
        with TestClient(app) as client:
            response = client.get('/api/projects/project-a/readiness')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['blockerCount'], 0)
