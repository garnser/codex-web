from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import ValidationError

from codex_web.api.execution_workspaces import build_execution_workspaces_router
from codex_web.execution_subjects import ExecutionSubject
from codex_web.execution_workspaces import ExecutionWorkspaceAcquire, ExecutionWorkspaceRelease, WorkspaceQuota
from codex_web.models import BotBinding, BotBindingCreate
from codex_web.services.bot_bindings import BotBindingLifecycleService
from codex_web.services.execution_workspaces import ExecutionWorkspaceQuotaError, ExecutionWorkspaceService
from tests import test_execution_workspaces as workspace_fixtures


class ConfiguredWorkspaceAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = workspace_fixtures.ExecutionWorkspaceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.service = self.fixture.service

    def acquire(self, number):
        return self.service.acquire(ExecutionWorkspaceAcquire(
            subject=ExecutionSubject(kind='thread_bootstrap', ref=f'quota-{number}'),
            execution_id=f'quota-{number}', project_id='home', resource_ids=(), scratch=True,
            ttl_seconds=300,
        ), actor=self.fixture.actor)

    def test_configured_identity_limit_allows_ninth_then_refuses_without_losing_files(self):
        with patch.dict('os.environ', {
            'CODEX_WEB_MAX_ACTIVE_WORKSPACES_PER_IDENTITY': '9',
            'CODEX_WEB_MAX_ACTIVE_WORKSPACES_PER_TENANT': '12',
        }):
            self.service = ExecutionWorkspaceService(
                self.service.store, self.service.backend, self.service.resources,
                self.service.project_lookup,
            )
        admitted = [self.acquire(i) for i in range(9)]
        retained = Path(admitted[0].path) / 'uncommitted-work'
        retained.write_bytes(b'original recovery work')
        before = self.service.store.load()
        with self.assertRaisesRegex(ExecutionWorkspaceQuotaError, r'identity.*active=9.*limit=9'):
            self.acquire(9)
        self.assertEqual(self.service.store.load(), before)
        self.assertEqual(retained.read_bytes(), b'original recovery work')
        self.service.release(admitted[0].id, ExecutionWorkspaceRelease(reason='owned retirement'),
                             actor=self.fixture.actor, preserve_files=True)
        self.assertEqual(retained.read_bytes(), b'original recovery work')
        self.assertEqual(self.acquire(9).execution_id, 'quota-9')

    def test_tenant_limit_applies_even_when_identity_limit_is_higher(self):
        self.service.quota = WorkspaceQuota(max_active_per_identity=12, max_active_per_tenant=2)
        self.acquire(0)
        self.acquire(1)
        with self.assertRaisesRegex(ExecutionWorkspaceQuotaError, r'tenant.*active=2.*limit=2'):
            self.acquire(2)

    def test_inspector_returns_the_enforced_limits_and_quota_refusal_is_actionable(self):
        self.service.quota = WorkspaceQuota(max_active_per_identity=1, max_active_per_tenant=12)
        self.acquire(0)
        app = FastAPI()

        @app.middleware('http')
        async def actor(request: Request, call_next):
            request.state.identity_actor = self.fixture.actor
            return await call_next(request)

        app.include_router(build_execution_workspaces_router(self.service))
        with TestClient(app) as client:
            response = client.get('/api/execution-workspaces/inspection')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['quota'], self.service.quota.model_dump(mode='json'))
            self.assertEqual(response.json()['count'], 1)
            refused = client.post('/api/execution-workspaces', json={
                'subject': {'kind': 'thread_bootstrap', 'ref': 'next-owner'},
                'execution_id': 'next-owner', 'project_id': 'home',
                'resource_ids': [], 'scratch': True,
            })
            self.assertEqual(refused.status_code, 429)
            self.assertIn('active=1, limit=1', refused.json()['detail'])


class WorkspaceQuotaConfigurationTests(unittest.TestCase):
    def service(self, quota=None):
        return ExecutionWorkspaceService(None, None, None, lambda _: None, quota=quota)

    def test_configured_limits_allow_more_than_default_eight_owner_sessions(self):
        with patch.dict("os.environ", {
            "CODEX_WEB_MAX_ACTIVE_WORKSPACES_PER_IDENTITY": "32",
            "CODEX_WEB_MAX_ACTIVE_WORKSPACES_PER_TENANT": "64",
        }, clear=True):
            quota = self.service().quota
        self.assertEqual(quota.max_active_per_identity, 32)
        self.assertEqual(quota.max_active_per_tenant, 64)
        self.assertEqual(quota.max_resources_per_workspace, 16)

    def test_absent_configuration_keeps_defaults(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(self.service().quota, WorkspaceQuota())

    def test_invalid_configuration_fails_closed(self):
        for value in ("0", "-1", "unlimited", ""):
            with self.subTest(value=value), patch.dict("os.environ", {
                "CODEX_WEB_MAX_ACTIVE_WORKSPACES_PER_IDENTITY": value,
            }, clear=True):
                with self.assertRaises(ValidationError):
                    self.service()

    def test_explicit_quota_takes_precedence_over_environment(self):
        quota = WorkspaceQuota(max_active_per_identity=3)
        with patch.dict("os.environ", {
            "CODEX_WEB_MAX_ACTIVE_WORKSPACES_PER_IDENTITY": "invalid",
        }, clear=True):
            self.assertIs(self.service(quota).quota, quota)


class ExistingBindingRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_control_update_preserves_route_without_contacting_stopped_runtime(self):
        original = BotBinding(
            id="existing", provider="slack", external_conversation_id="channel",
            thread_id="thread", project_id="project", thread_name="Dana",
            route_prefix="Dana", sandbox="danger-full-access", created_at=1, updated_at=1,
        )
        stored = [original]
        name = AsyncMock(side_effect=RuntimeError("Codex app-server stopped"))
        service = BotBindingLifecycleService(
            load_bindings=lambda: stored,
            save_bindings=lambda values: stored.__setitem__(slice(None), values),
            connections=SimpleNamespace(dedupe_integrations=lambda: None),
            selection=None, targets=None, presentation=None,
            projects=SimpleNamespace(get=lambda _: SimpleNamespace(id="project")),
            runtime_request=AsyncMock(), set_thread_name=name,
        )
        result = await service.start(BotBindingCreate(
            provider="slack", external_conversation_id="channel", thread_id="thread",
            project_id="project", thread_name="Dana", route_prefix="Dana",
            sandbox="workspace-write", approval_policy="never",
        ))
        self.assertEqual(result.id, "existing")
        self.assertEqual(result.sandbox, "workspace-write")
        self.assertEqual(len(stored), 1)
        name.assert_not_awaited()
