from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

from codex_web.models import BotBinding, BotBindingCreate
from codex_web.execution_workspaces import WorkspaceQuota
from codex_web.services.execution_workspaces import ExecutionWorkspaceService
from codex_web.services.bot_bindings import BotBindingLifecycleService


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
