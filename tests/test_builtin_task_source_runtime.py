from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from pydantic import ValidationError

from codex_web.identity import TenantScope
from codex_web.models import (
    JiraTaskSourceSettings,
    Project,
    ServiceNowTaskSourceSettings,
    TaskSourceConfiguration,
    TaskSourceIdentity,
    WorkItemState,
)
from codex_web.secret_backends import LocalFileSecretBackend
from codex_web.secrets import SecretCreate
from codex_web.services.builtin_task_source_runtime import (
    BuiltInTaskSourceRuntime,
    SecretBoundTaskSource,
)
from codex_web.services.identity import IdentityService
from codex_web.services.secrets import SecretBroker, SecretUseDeniedError
from codex_web.services.task_source_runtime import (
    TaskSourceRegistry,
    TaskSourceResolutionError,
)
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.secret_state import SecretStateStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class _Host:
    def __init__(self, projects):
        self.projects = projects

    def _load_projects(self):
        return [project.model_copy(deep=True) for project in self.projects]


class _JiraClient:
    def __init__(self):
        self.tokens_seen = []

    async def search_issues(
        self, api_base, *, token, username, jql, start_at=0, max_results=100
    ):
        self.tokens_seen.append(token)
        return {
            "issues": [
                {
                    "key": "OPS-42",
                    "fields": {
                        "summary": "Jira bound work",
                        "description": None,
                        "status": {"name": "In Progress"},
                        "assignee": {"accountId": "acct-1", "displayName": "Dana"},
                        "labels": [],
                        "priority": {"name": "High"},
                        "issuetype": {"name": "Task"},
                        "updated": "2026-09-19T08:00:00.000+0000",
                    },
                }
            ],
            "total": 1,
        }


class _ServiceNowClient:
    def __init__(self):
        self.tokens_seen = []

    async def list_records(
        self,
        api_base,
        table,
        *,
        token,
        query,
        fields,
        limit,
        offset=0,
    ):
        self.tokens_seen.append(token)
        return (
            {
                "sys_id": {"value": "abc123", "display_value": "abc123"},
                "number": {"value": "TASK001", "display_value": "TASK001"},
                "short_description": {
                    "value": "ServiceNow bound work",
                    "display_value": "ServiceNow bound work",
                },
                "description": {"value": "", "display_value": ""},
                "state": {"value": "2", "display_value": "In Progress"},
                "assigned_to": {"value": "user-1", "display_value": "Dana"},
                "priority": {"value": "2", "display_value": "High"},
                "category": {"value": "software", "display_value": "Software"},
                "parent": {"value": "", "display_value": ""},
                "sys_updated_on": {
                    "value": "2026-09-19 08:00:00",
                    "display_value": "2026-09-19 08:00:00",
                },
            },
        )


class BuiltInTaskSourceRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        sqlite = SQLiteStateStore(root / "state.sqlite3")
        self.identity = IdentityService(IdentityStateStore(sqlite))
        self.admin = self.identity.local_trusted_actor()
        self.secrets = SecretBroker(
            SecretStateStore(sqlite),
            {"local": LocalFileSecretBackend(root / "secret-material")},
        )
        self.jira_client = _JiraClient()
        self.servicenow_client = _ServiceNowClient()
        self.registry = TaskSourceRegistry()
        self.scope = TenantScope()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _secret(self, provider: str, value: str = "provider-token"):
        return self.secrets.create(
            SecretCreate(
                name=f"{provider} credential",
                value=value,
                provider=provider,
                allowed_identity_ids=["service-task-source-runtime"],
            ),
            actor=self.admin,
        )

    def _runtime(self, project: Project) -> BuiltInTaskSourceRuntime:
        return BuiltInTaskSourceRuntime(
            self.registry,
            _Host([project]),
            self.identity,
            self.secrets,
            jira_client=self.jira_client,
            servicenow_client=self.servicenow_client,
        ).install()

    async def test_jira_project_and_existing_item_resolve_through_secret_boundary(self) -> None:
        secret = self._secret("jira")
        config = TaskSourceConfiguration(
            source_type="jira",
            source_instance="https://jira.example",
            scope="OPS",
            credential_secret_id=secret.id,
            provider_settings=JiraTaskSourceSettings(
                username="agent@example.com",
            ),
        )
        project = Project(
            id="project-a",
            name="Project A",
            path="/tmp/project-a",
            authoritative_task_source=config,
        )
        self._runtime(project)

        source = self.registry.resolve_project(
            config,
            project_id=project.id,
            scope=self.scope,
            required=True,
        )
        self.assertIsInstance(source, SecretBoundTaskSource)
        self.assertFalse(hasattr(source, "token"))

        discovered = await source.discover(scope=config.scope)
        self.assertEqual(discovered[0].identity.external_id, "OPS-42")
        self.assertEqual(self.jira_client.tokens_seen, ["provider-token"])

        state = WorkItemState(
            ref="jira:OPS-42",
            project_id=project.id,
            source_identity=TaskSourceIdentity(
                source_type="jira",
                source_instance="https://jira.example",
                external_id="OPS-42",
            ),
            last_meaningful_update_at=1.0,
            updated_at=1.0,
            created_at=1.0,
        )
        resolved = self.registry.resolve(state, required=True)
        self.assertIsInstance(resolved, SecretBoundTaskSource)
        read_source = resolved._projection_source
        self.assertFalse(hasattr(read_source, "provider-token"))

    async def test_servicenow_project_resolution_uses_typed_table_and_state_settings(self) -> None:
        secret = self._secret("servicenow", "snow-token")
        config = TaskSourceConfiguration(
            source_type="servicenow",
            source_instance="https://instance.service-now.com",
            scope="active=true^assignment_group=ops",
            credential_secret_id=secret.id,
            provider_settings=ServiceNowTaskSourceSettings(
                table="incident",
                canonical_state_values={
                    "implementation_active": "2",
                    "closed": "7",
                },
            ),
        )
        project = Project(
            id="project-snow",
            name="ServiceNow Project",
            path="/tmp/project-snow",
            authoritative_task_source=config,
        )
        self._runtime(project)

        source = self.registry.resolve_project(
            config,
            project_id=project.id,
            scope=self.scope,
            required=True,
        )
        page = await source.discover_page(scope=config.scope, limit=25)
        self.assertEqual(page.items[0].title, "ServiceNow bound work")
        self.assertEqual(self.servicenow_client.tokens_seen, ["snow-token"])
        self.assertTrue(source.capabilities.supports("state_write"))

    async def test_revoked_secret_fails_closed_after_source_resolution(self) -> None:
        secret = self._secret("jira")
        config = TaskSourceConfiguration(
            source_type="jira",
            source_instance="https://jira.example",
            scope="OPS",
            credential_secret_id=secret.id,
            provider_settings=JiraTaskSourceSettings(),
        )
        project = Project(
            id="project-a",
            name="Project A",
            path="/tmp/project-a",
            authoritative_task_source=config,
        )
        self._runtime(project)
        source = self.registry.resolve_project(
            config,
            project_id=project.id,
            scope=self.scope,
            required=True,
        )

        self.secrets.revoke(secret.id, actor=self.admin, reason="test")
        with self.assertRaises(SecretUseDeniedError):
            await source.discover(scope="OPS")
        self.assertEqual(self.jira_client.tokens_seen, [])

    def test_secret_without_runtime_acl_is_rejected_without_reveal(self) -> None:
        secret = self.secrets.create(
            SecretCreate(
                name="Jira credential",
                value="must-not-escape",
                provider="jira",
            ),
            actor=self.admin,
        )
        config = TaskSourceConfiguration(
            source_type="jira",
            source_instance="https://jira.example",
            scope="OPS",
            credential_secret_id=secret.id,
            provider_settings=JiraTaskSourceSettings(),
        )
        project = Project(
            id="project-a",
            name="Project A",
            path="/tmp/project-a",
            authoritative_task_source=config,
        )
        self._runtime(project)
        with self.assertRaisesRegex(TaskSourceResolutionError, "credential is unavailable"):
            self.registry.resolve_project(
                config,
                project_id=project.id,
                scope=self.scope,
                required=True,
            )

    def test_cross_tenant_project_resolution_is_rejected(self) -> None:
        secret = self._secret("jira")
        config = TaskSourceConfiguration(
            source_type="jira",
            source_instance="https://jira.example",
            scope="OPS",
            credential_secret_id=secret.id,
            provider_settings=JiraTaskSourceSettings(),
        )
        project = Project(
            id="project-a",
            name="Project A",
            path="/tmp/project-a",
            authoritative_task_source=config,
        )
        self._runtime(project)
        with self.assertRaises(TaskSourceResolutionError):
            self.registry.resolve_project(
                config,
                project_id=project.id,
                scope=TenantScope(
                    organization_id="other-org",
                    workspace_id="other-workspace",
                ),
                required=True,
            )

    def test_provider_settings_are_typed_and_must_match_source(self) -> None:
        with self.assertRaises(ValidationError):
            TaskSourceConfiguration(
                source_type="jira",
                source_instance="https://jira.example",
                scope="OPS",
                credential_secret_id="secret-1",
                provider_settings=ServiceNowTaskSourceSettings(),
            )
        with self.assertRaises(ValidationError):
            TaskSourceConfiguration(
                source_type="servicenow",
                source_instance="https://snow.example",
                scope="active=true",
                provider_settings=ServiceNowTaskSourceSettings(),
            )


if __name__ == "__main__":
    unittest.main()
