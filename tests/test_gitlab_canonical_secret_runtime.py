from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.models import TaskSourceConfiguration, TaskSourceIdentity
from codex_web.secret_backends import LocalFileSecretBackend
from codex_web.secrets import SecretCreate
from codex_web.services.builtin_task_source_runtime import (
    SecretBoundTaskSource,
)
from codex_web.services.secrets import SecretBroker
from codex_web.services.work_items import WorkItemService
from codex_web.storage.secret_state import SecretStateStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class _Identity:
    @staticmethod
    def bootstrap_service_actor(
        *,
        identity_id,
        name,
        scope,
        service_scopes,
    ):
        del name
        return AuthenticationActor(
            identity_id=identity_id,
            principal_kind=PrincipalKind.SERVICE,
            organization_id=scope.organization_id,
            workspace_id=scope.workspace_id,
            roles=(),
            assurance=AuthenticationAssurance.SERVICE,
            service_scopes=tuple(service_scopes),
        )


class _GitLab:
    def __init__(self) -> None:
        self.tokens = []

    async def project_issue(
        self,
        api_base,
        project_path,
        iid,
        *,
        token,
    ):
        self.tokens.append(token)
        return {
            "id": 1001,
            "iid": iid,
            "title": "Canonical credential read",
            "state": "opened",
            "labels": [],
            "assignees": [],
            "web_url": f"https://gitlab.example/{project_path}/-/issues/{iid}",
            "updated_at": "2026-09-21T00:00:00Z",
        }


class GitLabCanonicalSecretRuntimeTests(unittest.TestCase):
    def test_gitlab_project_binding_uses_secret_broker_not_legacy_token(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        sqlite = SQLiteStateStore(
            Path(temp.name) / "state.sqlite3"
        )
        broker = SecretBroker(
            SecretStateStore(sqlite),
            {
                "local": LocalFileSecretBackend(
                    Path(temp.name) / "secrets"
                )
            },
        )
        admin = AuthenticationActor(
            identity_id="admin",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="org-a",
            workspace_id="ws-a",
            roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.MFA,
        )
        reference = broker.create(
            SecretCreate(
                name="GitLab",
                value="CANONICAL_GITLAB_TOKEN",
                provider="gitlab",
                purpose="legacy-materialization:project-a:task-source",
                allowed_identity_ids=[
                    "service-task-source-runtime"
                ],
            ),
            actor=admin,
        )

        service = WorkItemService.__new__(WorkItemService)
        service.identity_service = _Identity()
        service.secret_broker = broker
        service.gitlab = _GitLab()

        configuration = TaskSourceConfiguration(
            source_type="gitlab",
            source_instance="https://gitlab.example/api/v4",
            scope="group",
            credential_secret_id=reference.id,
        )
        source = service._canonical_gitlab_source(
            configuration,
            scope=admin.tenant,
        )

        self.assertIsInstance(source, SecretBoundTaskSource)
        self.assertFalse(hasattr(source, "token"))
        snapshot = asyncio.run(
            source.read(
                TaskSourceIdentity(
                    source_type="gitlab",
                    source_instance="https://gitlab.example/api/v4",
                    external_id="group/app#1",
                )
            )
        )
        self.assertEqual(
            snapshot.identity.external_id,
            "group/app#1",
        )
        self.assertEqual(
            service.gitlab.tokens,
            ["CANONICAL_GITLAB_TOKEN"],
        )
        self.assertNotIn(
            "CANONICAL_GITLAB_TOKEN",
            repr(source.__dict__),
        )


if __name__ == "__main__":
    unittest.main()
