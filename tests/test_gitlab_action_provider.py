from __future__ import annotations

import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from codex_web.action_providers import ActionProviderBindingCreate, ActionRequest
from codex_web.execution_workspaces import (
    ExecutionWorkspace,
    ExecutionWorkspaceKind,
    ExecutionWorkspaceLease,
    ExecutionWorkspaceMember,
    ExecutionWorkspaceStatus,
    LeaseMode,
)
from codex_web.resources import ResourceAlias, ResourceCreate, ResourceType
from codex_web.secret_backends import LocalFileSecretBackend
from codex_web.secrets import SecretCreate
from codex_web.services.action_providers import ActionExecutionService, ActionProviderRegistry
from codex_web.services.code_host_action_contract import (
    CODE_HOST_BRANCH_PUBLISH_ACTION_ID,
    CODE_HOST_CHANGE_REQUEST_UPSERT_ACTION_ID,
    CODE_HOST_ISSUE_COMMENT_ACTION_ID,
    CODE_HOST_ISSUE_UPDATE_ACTION_ID,
    CODE_HOST_PULL_REQUEST_MERGE_ACTION_ID,
)
from codex_web.services.gitlab_action_provider import GitLabActionProvider
from codex_web.services.identity import IdentityService
from codex_web.services.resources import ResourceCatalogService
from codex_web.services.secrets import SecretBroker
from codex_web.storage.action_providers import ActionProviderStateStore
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.resource_catalog import ResourceCatalogStore
from codex_web.storage.secret_state import SecretStateStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class _GitLabClient:
    def __init__(self) -> None:
        self.credentials: list[str] = []
        self.notes: list[dict] = []
        self.note_creates = 0
        self.issues: dict[int, dict] = {}
        self.issue_updates: list[dict] = []
        self.merge_request_rows: list[dict] = []
        self.merge_request_creates = 0
        self.merge_request_updates = 0
        self.merge_request_merges = 0
        self.branches: dict[str, str] = {}

    async def project_issue_notes(self, api_base, project, iid, *, token):
        self.credentials.append(token)
        return list(self.notes)

    async def create_project_issue_note(
        self, api_base, project, iid, *, token, body
    ):
        self.credentials.append(token)
        self.note_creates += 1
        item = {
            "id": 31,
            "body": body,
            "web_url": f"https://gitlab.example/{project}/-/issues/{iid}#note_31",
        }
        self.notes.append(item)
        return item

    async def project_issue(self, api_base, project, iid, *, token):
        self.credentials.append(token)
        return self.issues.get(
            iid,
            {
                "id": 11,
                "iid": iid,
                "state": "opened",
                "web_url": f"https://gitlab.example/{project}/-/issues/{iid}",
            },
        )

    async def update_project_issue(
        self, api_base, project, iid, *, token, payload
    ):
        self.credentials.append(token)
        self.issue_updates.append(payload)
        state = "closed" if payload["state_event"] == "close" else "opened"
        item = {
            "id": 11,
            "iid": iid,
            "state": state,
            "web_url": f"https://gitlab.example/{project}/-/issues/{iid}",
        }
        self.issues[iid] = item
        return item

    async def merge_requests(
        self,
        api_base,
        project,
        *,
        token,
        source_branch,
        target_branch,
    ):
        self.credentials.append(token)
        return [
            item
            for item in self.merge_request_rows
            if item["source_branch"] == source_branch
            and item["target_branch"] == target_branch
        ]

    async def create_merge_request(
        self, api_base, project, *, token, payload
    ):
        self.credentials.append(token)
        self.merge_request_creates += 1
        item = {
            "id": 42,
            "iid": 9,
            **payload,
            "web_url": f"https://gitlab.example/{project}/-/merge_requests/9",
        }
        self.merge_request_rows.append(item)
        return item

    async def update_merge_request(
        self, api_base, project, iid, *, token, payload
    ):
        self.credentials.append(token)
        self.merge_request_updates += 1
        item = next(row for row in self.merge_request_rows if row["iid"] == iid)
        item.update(payload)
        return item

    async def merge_request(self, api_base, project, iid, *, token):
        self.credentials.append(token)
        return next(row for row in self.merge_request_rows if row["iid"] == iid)

    async def accept_merge_request(
        self, api_base, project, iid, *, token, payload
    ):
        self.credentials.append(token)
        self.merge_request_merges += 1
        item = next(row for row in self.merge_request_rows if row["iid"] == iid)
        if payload["sha"] != item["sha"]:
            raise RuntimeError("stale merge request head")
        item.update(
            {
                "state": "merged",
                "merge_commit_sha": "b" * 40,
                "squash": payload["squash"],
            }
        )
        return item

    async def branch(self, api_base, project, branch, *, token):
        self.credentials.append(token)
        return {"name": branch, "commit": {"id": self.branches.get(branch)}}


class GitLabActionProviderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        sqlite = SQLiteStateStore(root / "state.sqlite3")
        identity = IdentityService(IdentityStateStore(sqlite))
        identity.bootstrap_local()
        self.actor = identity.local_trusted_actor()
        self.resources = ResourceCatalogService(ResourceCatalogStore(sqlite))
        self.repository = self.resources.create(
            ResourceCreate(
                resource_type=ResourceType.REPOSITORY,
                name="codex-web",
                aliases=[
                    ResourceAlias(
                        namespace="gitlab-project",
                        provider="gitlab",
                        value="group/platform/codex-web",
                    )
                ],
            ),
            actor=self.actor,
        )
        self.secrets = SecretBroker(
            SecretStateStore(sqlite),
            {"local": LocalFileSecretBackend(root / "secrets")},
        )
        secret = self.secrets.create(
            SecretCreate(name="GitLab token", value="secret-gitlab-token"),
            actor=self.actor,
        )
        branch = "feature/governed-gitlab-publication"
        workspace_root = root / "workspaces"
        workspace_path = workspace_root / "workspace-a"
        workspace_path.mkdir(parents=True)
        subprocess.run(
            ["git", "init", "-b", branch],
            cwd=workspace_path,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=workspace_path,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=workspace_path,
            check=True,
        )
        (workspace_path / "delivery.txt").write_text("ready\n", encoding="utf-8")
        subprocess.run(["git", "add", "delivery.txt"], cwd=workspace_path, check=True)
        subprocess.run(
            ["git", "commit", "-m", "Ready for publication"],
            cwd=workspace_path,
            check=True,
            capture_output=True,
        )
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=workspace_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        lease = ExecutionWorkspaceLease(
            id="lease-a",
            execution_workspace_id="workspace-a",
            organization_id=self.actor.organization_id,
            workspace_id=self.actor.workspace_id,
            work_item_ref="WI-1",
            execution_id="execution-a",
            owner_identity_id=self.actor.identity_id,
            resource_ids=(self.repository.id,),
            mode=LeaseMode.WRITE,
            acquired_at=time.time(),
            expires_at=time.time() + 300,
        )
        workspace = ExecutionWorkspace(
            id="workspace-a",
            organization_id=self.actor.organization_id,
            workspace_id=self.actor.workspace_id,
            work_item_ref="WI-1",
            execution_id="execution-a",
            project_id="project-a",
            owner_identity_id=self.actor.identity_id,
            kind=ExecutionWorkspaceKind.GIT_WORKTREE,
            resource_ids=(self.repository.id,),
            repository_resource_id=self.repository.id,
            writable_repository_ids=(self.repository.id,),
            lease_id=lease.id,
            path=str(workspace_path),
            branch_name=branch,
            base_revision=head,
            head_revision=head,
            status=ExecutionWorkspaceStatus.ACTIVE,
            created_at=time.time(),
            updated_at=time.time(),
            repository_members=(
                ExecutionWorkspaceMember(
                    resource_id=self.repository.id,
                    access_mode=LeaseMode.WRITE,
                    source_path=str(workspace_path),
                    workspace_path=str(workspace_path),
                    sandbox_path="/mnt/codex-repositories/repository",
                    branch_name=branch,
                    base_revision=head,
                    head_revision=head,
                ),
            ),
        )
        state = SimpleNamespace(workspaces=[workspace], leases=[lease])
        workspaces = SimpleNamespace(
            store=SimpleNamespace(load=lambda: state),
            backend=SimpleNamespace(root=workspace_root),
        )
        self.client = _GitLabClient()

        async def publish(
            path,
            project,
            branch_name,
            revision,
            credential,
            expected_remote_revision,
        ):
            self.client.branches[branch_name] = revision
            self.published = (
                path,
                project,
                branch_name,
                revision,
                credential,
                expected_remote_revision,
            )

        self.published = None
        self.branch = branch
        self.head = head
        self.provider = GitLabActionProvider(
            self.resources,
            self.client,
            workspaces,
            provider_instance="gitlab.example",
            api_base="https://gitlab.example/api/v4",
            branch_publisher=publish,
        )
        self.registry = ActionProviderRegistry(ActionProviderStateStore(sqlite))
        self.registry.register(self.provider)
        self.execution = ActionExecutionService(
            self.registry,
            self.resources,
            secret_broker=self.secrets,
        )
        self.binding = self.registry.bind(
            ActionProviderBindingCreate(
                provider_type=self.provider.provider_type,
                provider_instance=self.provider.provider_instance,
                resource_ids=(self.repository.id,),
                credential_ref=secret.id,
            ),
            actor=self.actor,
            resources=self.resources,
        )

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    def _request(self, action_id: str, parameters: dict) -> ActionRequest:
        return ActionRequest(
            action_id=action_id,
            organization_id=self.actor.organization_id,
            workspace_id=self.actor.workspace_id,
            resource_ids=(self.repository.id,),
            parameters=parameters,
            idempotency_key="stable-action-key",
        )

    async def test_comment_is_idempotent_and_verifies_through_secret_broker(self) -> None:
        request = self._request(
            CODE_HOST_ISSUE_COMMENT_ACTION_ID,
            {"issue_number": 890, "body": "Delivery progress"},
        )
        first = await self.execution.execute(self.binding.id, request, actor=self.actor)
        second = await self.execution.execute(self.binding.id, request, actor=self.actor)
        verification = await self.execution.verify(
            self.binding.id, second, actor=self.actor
        )

        self.assertEqual(self.client.note_creates, 1)
        self.assertEqual(first.external_id, second.external_id)
        self.assertTrue(verification.verified)
        self.assertEqual(set(self.client.credentials), {"secret-gitlab-token"})
        self.assertNotIn("secret-gitlab-token", second.model_dump_json())
        self.assertNotIn("Delivery progress", second.model_dump_json())

    async def test_issue_state_update_and_verification(self) -> None:
        request = self._request(
            CODE_HOST_ISSUE_UPDATE_ACTION_ID,
            {"issue_number": 886, "state": "closed"},
        )
        result = await self.execution.execute(self.binding.id, request, actor=self.actor)

        self.assertEqual(self.client.issue_updates, [{"state_event": "close"}])
        self.assertTrue(
            (
                await self.execution.verify(
                    self.binding.id, result, actor=self.actor
                )
            ).verified
        )

    async def test_merge_request_is_created_then_reconciled_and_verified(self) -> None:
        request = self._request(
            CODE_HOST_CHANGE_REQUEST_UPSERT_ACTION_ID,
            {
                "title": "Governed delivery",
                "body": "Closes #890",
                "head": "feature/890",
                "base": "main",
                "draft": False,
            },
        )
        first = await self.execution.execute(self.binding.id, request, actor=self.actor)
        updated = request.model_copy(
            update={"parameters": {**request.parameters, "title": "Governed delivery ready"}}
        )
        second = await self.execution.execute(self.binding.id, updated, actor=self.actor)

        self.assertEqual(self.client.merge_request_creates, 1)
        self.assertEqual(self.client.merge_request_updates, 1)
        self.assertEqual(first.output["change_request_number"], 9)
        self.assertEqual(second.output["change_request_number"], 9)
        self.assertTrue(
            (
                await self.execution.verify(
                    self.binding.id, second, actor=self.actor
                )
            ).verified
        )

    async def test_unowned_merge_request_is_not_adopted(self) -> None:
        self.client.merge_request_rows.append(
            {
                "id": 99,
                "iid": 7,
                "title": "Other",
                "description": "Created elsewhere",
                "source_branch": "feature/890",
                "target_branch": "main",
                "draft": False,
            }
        )
        request = self._request(
            CODE_HOST_CHANGE_REQUEST_UPSERT_ACTION_ID,
            {
                "title": "Governed delivery",
                "head": "feature/890",
                "base": "main",
            },
        )

        with self.assertRaisesRegex(ValueError, "not owned"):
            await self.execution.execute(self.binding.id, request, actor=self.actor)

    async def test_merge_request_requires_exact_head_and_green_pipeline(self) -> None:
        head_sha = "a" * 40
        self.client.merge_request_rows.append(
            {
                "id": 42,
                "iid": 33,
                "state": "opened",
                "sha": head_sha,
                "source_branch": "docs-fix",
                "target_branch": "main",
                "draft": False,
                "merge_status": "can_be_merged",
                "detailed_merge_status": "mergeable",
                "head_pipeline": {"id": 3323, "status": "success"},
                "web_url": "https://gitlab.example/group/platform/codex-web/-/merge_requests/33",
            }
        )
        request = self._request(
            CODE_HOST_PULL_REQUEST_MERGE_ACTION_ID,
            {
                "pull_request_number": 33,
                "merge_method": "merge",
                "expected_head_sha": head_sha,
            },
        )

        result = await self.execution.execute(
            self.binding.id, request, actor=self.actor
        )

        self.assertEqual(self.client.merge_request_merges, 1)
        self.assertEqual(result.output["head_sha"], head_sha)
        self.assertTrue(
            (
                await self.execution.verify(
                    self.binding.id, result, actor=self.actor
                )
            ).verified
        )

    async def test_merge_request_rejects_changed_head(self) -> None:
        self.client.merge_request_rows.append(
            {
                "id": 42,
                "iid": 33,
                "state": "opened",
                "sha": "a" * 40,
                "source_branch": "docs-fix",
                "target_branch": "main",
                "draft": False,
                "merge_status": "can_be_merged",
                "detailed_merge_status": "mergeable",
                "head_pipeline": {"id": 3323, "status": "success"},
            }
        )
        request = self._request(
            CODE_HOST_PULL_REQUEST_MERGE_ACTION_ID,
            {
                "pull_request_number": 33,
                "merge_method": "merge",
                "expected_head_sha": "c" * 40,
            },
        )

        with self.assertRaisesRegex(ValueError, "no longer matches"):
            await self.execution.execute(
                self.binding.id, request, actor=self.actor
            )

    async def test_branch_publication_attests_and_verifies_exact_revision(self) -> None:
        request = self._request(
            CODE_HOST_BRANCH_PUBLISH_ACTION_ID,
            {
                "execution_workspace_id": "workspace-a",
                "branch": self.branch,
                "head_revision": self.head,
            },
        )
        result = await self.execution.execute(self.binding.id, request, actor=self.actor)

        self.assertEqual(self.published[1], "group/platform/codex-web")
        self.assertEqual(self.published[4], "secret-gitlab-token")
        self.assertTrue(
            (
                await self.execution.verify(
                    self.binding.id, result, actor=self.actor
                )
            ).verified
        )
        self.assertNotIn("secret-gitlab-token", result.model_dump_json())


if __name__ == "__main__":
    unittest.main()
