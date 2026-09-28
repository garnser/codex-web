from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
import subprocess

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
from codex_web.services.github_action_provider import (
    CODE_HOST_ISSUE_COMMENT_ACTION_ID,
    CODE_HOST_ISSUE_CREATE_ACTION_ID,
    CODE_HOST_ISSUE_UPDATE_ACTION_ID,
    CODE_HOST_BRANCH_PUBLISH_ACTION_ID,
    CODE_HOST_PULL_REQUEST_UPSERT_ACTION_ID,
    CODE_HOST_PULL_REQUEST_MERGE_ACTION_ID,
    GitHubActionProvider,
)
from codex_web.services.identity import IdentityService
from codex_web.services.resources import ResourceCatalogService
from codex_web.services.secrets import SecretBroker
from codex_web.storage.action_providers import ActionProviderStateStore
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.resource_catalog import ResourceCatalogStore
from codex_web.storage.secret_state import SecretStateStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class _GitHubClient:
    def __init__(self) -> None:
        self.comments: list[dict] = []
        self.comment_creates = 0
        self.issue_updates: list[dict] = []
        self.issues: dict[int, dict] = {}
        self.issue_creates = 0
        self.pull_requests: list[dict] = []
        self.pull_request_creates = 0
        self.pull_request_updates = 0
        self.pull_request_merges = 0
        self.branches: dict[str, str] = {}
        self.credentials: list[str] = []

    async def list_issue_comments(self, api_base, repo, number, *, token):
        self.credentials.append(token)
        return list(self.comments)

    async def create_comment(self, api_base, repo, number, *, token, body):
        self.credentials.append(token)
        self.comment_creates += 1
        item = {
            "id": 41,
            "body": body,
            "html_url": f"https://github.com/{repo}/issues/{number}#issuecomment-41",
        }
        self.comments.append(item)
        return item

    async def update_issue(self, api_base, repo, number, *, token, payload):
        self.credentials.append(token)
        self.issue_updates.append(payload)
        item = {
            "id": 17,
            "state": payload["state"],
            "html_url": f"https://github.com/{repo}/issues/{number}",
        }
        self.issues[number] = item
        return item

    async def list_issues(self, api_base, repo, *, token, state="open"):
        self.credentials.append(token)
        return list(self.issues.values())

    async def create_issue(self, api_base, repo, *, token, payload):
        self.credentials.append(token)
        self.issue_creates += 1
        number = 917
        item = {
            "id": 917,
            "number": number,
            "title": payload["title"],
            "body": payload["body"],
            "state": "open",
            "html_url": f"https://github.com/{repo}/issues/{number}",
        }
        self.issues[number] = item
        return item

    async def issue(self, api_base, repo, number, *, token):
        self.credentials.append(token)
        return self.issues.get(
            number,
            {
                "id": 17,
                "state": "open",
                "html_url": f"https://github.com/{repo}/issues/{number}",
            },
        )

    async def list_pull_requests(self, api_base, repo, *, token, head, base):
        self.credentials.append(token)
        return list(self.pull_requests)

    async def create_pull_request(self, api_base, repo, *, token, payload):
        self.credentials.append(token)
        self.pull_request_creates += 1
        item = {
            "id": 52,
            "number": 889,
            "title": payload["title"],
            "body": payload["body"],
            "head": {"ref": payload["head"]},
            "base": {"ref": payload["base"]},
            "draft": payload.get("draft", False),
            "html_url": f"https://github.com/{repo}/pull/889",
        }
        self.pull_requests.append(item)
        return item

    async def update_pull_request(
        self, api_base, repo, number, *, token, payload
    ):
        self.credentials.append(token)
        self.pull_request_updates += 1
        item = next(row for row in self.pull_requests if row["number"] == number)
        item.update({"title": payload["title"], "body": payload["body"]})
        item["base"] = {"ref": payload["base"]}
        return item

    async def pull_request(self, api_base, repo, number, *, token):
        self.credentials.append(token)
        return next(row for row in self.pull_requests if row["number"] == number)

    async def merge_pull_request(self, api_base, repo, number, *, token, payload):
        self.credentials.append(token)
        self.pull_request_merges += 1
        item = next(row for row in self.pull_requests if row["number"] == number)
        item.update({"merged": True, "merge_commit_sha": "a" * 40})
        return {"merged": True, "sha": "a" * 40}

    async def branch(self, api_base, repo, branch, *, token):
        self.credentials.append(token)
        return {"name": branch, "commit": {"sha": self.branches.get(branch)}}


class GitHubActionProviderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.root = root
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
                        namespace="repository",
                        provider="github",
                        value="garnser/codex-web",
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
            SecretCreate(name="GitHub token", value="secret-github-token"),
            actor=self.actor,
        )
        self.client = _GitHubClient()
        branch = "feature/governed-publication"
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
        subprocess.run(
            ["git", "add", "delivery.txt"], cwd=workspace_path, check=True
        )
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
        self.workspaces = SimpleNamespace(
            store=SimpleNamespace(load=lambda: state),
            backend=SimpleNamespace(root=workspace_root),
        )
        self.published_branches: list[tuple] = []

        async def publish(path, repository, branch_name, revision, credential):
            self.published_branches.append(
                (path, repository, branch_name, revision, credential)
            )
            self.client.branches[branch_name] = revision

        self.workspace_path = workspace_path
        self.workspace_branch = branch
        self.workspace_head = head
        self.provider = GitHubActionProvider(
            self.resources,
            self.client,
            self.workspaces,
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
            idempotency_key="intent-stable-key",
        )

    async def test_comment_uses_secret_boundary_and_replays_without_duplicate(self) -> None:
        request = self._request(
            CODE_HOST_ISSUE_COMMENT_ACTION_ID,
            {"issue_number": 890, "body": "Delivery progress"},
        )
        preparation = await self.execution.prepare(
            self.binding.id, request, actor=self.actor
        )

        self.assertEqual(preparation.provider_plan["repository"], "garnser/codex-web")
        self.assertNotIn("Delivery progress", str(preparation.provider_plan))
        first = await self.execution.execute(self.binding.id, request, actor=self.actor)
        second = await self.execution.execute(self.binding.id, request, actor=self.actor)

        self.assertEqual(self.client.comment_creates, 1)
        self.assertEqual(first.external_id, second.external_id)
        self.assertEqual(set(self.client.credentials), {"secret-github-token"})
        self.assertNotIn("secret-github-token", first.model_dump_json())
        self.assertNotIn("Delivery progress", first.model_dump_json())
        self.assertIn("codex-web-action:", self.client.comments[0]["body"])

    async def test_issue_state_update_is_repository_scoped(self) -> None:
        request = self._request(
            CODE_HOST_ISSUE_UPDATE_ACTION_ID,
            {"issue_number": 886, "state": "closed"},
        )

        result = await self.execution.execute(
            self.binding.id, request, actor=self.actor
        )

        self.assertEqual(self.client.issue_updates, [{"state": "closed"}])
        self.assertEqual(result.output["repository"], "garnser/codex-web")
        self.assertEqual(result.output["issue_number"], 886)

    async def test_issue_create_is_idempotently_owned_and_verified(self) -> None:
        request = self._request(
            CODE_HOST_ISSUE_CREATE_ACTION_ID,
            {"title": "Worker delivery regression", "body": "Reproduction details"},
        )
        first = await self.execution.execute(self.binding.id, request, actor=self.actor)
        second = await self.execution.execute(self.binding.id, request, actor=self.actor)

        self.assertEqual(self.client.issue_creates, 1)
        self.assertEqual(first.output["issue_number"], 917)
        self.assertEqual(first.external_id, second.external_id)
        self.assertTrue(
            (await self.execution.verify(self.binding.id, second, actor=self.actor)).verified
        )

    async def test_pull_request_reconciles_unknown_outcome_without_duplicate(self) -> None:
        request = self._request(
            CODE_HOST_PULL_REQUEST_UPSERT_ACTION_ID,
            {
                "title": "Fix delivery traceability",
                "body": "Closes #890",
                "head": "feature/890-governed-github-actions",
                "base": "main",
                "draft": False,
            },
        )
        preparation = await self.execution.prepare(
            self.binding.id, request, actor=self.actor
        )

        self.assertEqual(preparation.provider_plan["head"], request.parameters["head"])
        self.assertNotIn("Fix delivery traceability", str(preparation.provider_plan))
        first = await self.execution.execute(self.binding.id, request, actor=self.actor)
        second = await self.execution.execute(self.binding.id, request, actor=self.actor)

        self.assertEqual(self.client.pull_request_creates, 1)
        self.assertEqual(first.external_id, second.external_id)
        self.assertEqual(first.output["change_request_number"], 889)
        self.assertNotIn("Closes #890", first.model_dump_json())
        self.assertIn("codex-web-action:", self.client.pull_requests[0]["body"])

        verification = await self.execution.verify(
            self.binding.id, second, actor=self.actor
        )
        self.assertTrue(verification.verified)
        self.assertNotIn("secret-github-token", verification.model_dump_json())

    async def test_pull_request_does_not_adopt_unowned_existing_request(self) -> None:
        self.client.pull_requests.append(
            {
                "id": 99,
                "number": 7,
                "body": "Created outside this ActionIntent",
                "state": "open",
                "html_url": "https://github.com/garnser/codex-web/pull/7",
            }
        )
        request = self._request(
            CODE_HOST_PULL_REQUEST_UPSERT_ACTION_ID,
            {
                "title": "Traceable PR",
                "head": "feature/traceable",
                "base": "main",
            },
        )

        with self.assertRaisesRegex(ValueError, "not owned"):
            await self.execution.execute(self.binding.id, request, actor=self.actor)

        self.assertEqual(self.client.pull_request_creates, 0)

    async def test_pull_request_ignores_unowned_closed_request_for_reused_branch(
        self,
    ) -> None:
        self.client.pull_requests.append(
            {
                "id": 99,
                "number": 7,
                "body": "Created by an earlier ActionIntent",
                "state": "closed",
                "merged_at": "2026-09-28T23:10:55Z",
                "html_url": "https://github.com/garnser/codex-web/pull/7",
            }
        )
        request = self._request(
            CODE_HOST_PULL_REQUEST_UPSERT_ACTION_ID,
            {
                "title": "Next governed delivery",
                "head": "feature/reused-canonical-branch",
                "base": "main",
            },
        )

        result = await self.execution.execute(
            self.binding.id, request, actor=self.actor
        )

        self.assertEqual(self.client.pull_request_creates, 1)
        self.assertEqual(result.output["change_request_number"], 889)
        self.assertEqual(len(self.client.pull_requests), 2)

    async def test_pull_request_merge_requires_clean_state_and_verifies(self) -> None:
        self.client.pull_requests.append(
            {
                "id": 889,
                "number": 889,
                "mergeable": True,
                "mergeable_state": "clean",
                "merged": False,
                "html_url": "https://github.com/garnser/codex-web/pull/889",
            }
        )
        request = self._request(
            CODE_HOST_PULL_REQUEST_MERGE_ACTION_ID,
            {"pull_request_number": 889, "merge_method": "squash"},
        )
        result = await self.execution.execute(self.binding.id, request, actor=self.actor)

        self.assertEqual(self.client.pull_request_merges, 1)
        self.assertEqual(result.output["merge_commit_sha"], "a" * 40)
        self.assertTrue(
            (await self.execution.verify(self.binding.id, result, actor=self.actor)).verified
        )

    async def test_branch_publication_attests_workspace_and_brokers_credential(self) -> None:
        request = self._request(
            CODE_HOST_BRANCH_PUBLISH_ACTION_ID,
            {
                "execution_workspace_id": "workspace-a",
                "branch": self.workspace_branch,
                "head_revision": self.workspace_head,
            },
        )

        preparation = await self.execution.prepare(
            self.binding.id, request, actor=self.actor
        )
        result = await self.execution.execute(
            self.binding.id, request, actor=self.actor
        )

        self.assertTrue(preparation.provider_plan["workspace_attested"])
        self.assertEqual(len(self.published_branches), 1)
        self.assertEqual(self.published_branches[0][0], self.workspace_path)
        self.assertEqual(self.published_branches[0][1], "garnser/codex-web")
        self.assertEqual(self.published_branches[0][4], "secret-github-token")
        self.assertEqual(result.output["head_revision"], self.workspace_head)
        self.assertNotIn("secret-github-token", result.model_dump_json())
        self.assertTrue(
            (
                await self.execution.verify(
                    self.binding.id, result, actor=self.actor
                )
            ).verified
        )

    async def test_branch_publication_attests_commits_after_workspace_acquisition(self) -> None:
        (self.workspace_path / "progress.txt").write_text(
            "committed progress\n", encoding="utf-8"
        )
        subprocess.run(
            ["git", "add", "progress.txt"], cwd=self.workspace_path, check=True
        )
        subprocess.run(
            ["git", "commit", "-m", "Commit assignment progress"],
            cwd=self.workspace_path,
            check=True,
            capture_output=True,
        )
        progressed_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.workspace_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertNotEqual(progressed_head, self.workspace_head)

        request = self._request(
            CODE_HOST_BRANCH_PUBLISH_ACTION_ID,
            {
                "execution_workspace_id": "workspace-a",
                "branch": self.workspace_branch,
                "head_revision": progressed_head,
            },
        )
        result = await self.execution.execute(
            self.binding.id, request, actor=self.actor
        )

        self.assertEqual(result.output["head_revision"], progressed_head)
        self.assertEqual(self.published_branches[0][3], progressed_head)

    async def test_branch_publication_rejects_dirty_or_mismatched_workspace(self) -> None:
        (self.workspace_path / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        request = self._request(
            CODE_HOST_BRANCH_PUBLISH_ACTION_ID,
            {
                "execution_workspace_id": "workspace-a",
                "branch": self.workspace_branch,
                "head_revision": self.workspace_head,
            },
        )

        with self.assertRaisesRegex(ValueError, "uncommitted"):
            await self.execution.prepare(self.binding.id, request, actor=self.actor)

        self.assertEqual(self.published_branches, [])

    async def test_unvalidated_repository_locator_fails_before_provider_call(self) -> None:
        invalid = self.resources.create(
            ResourceCreate(
                resource_type=ResourceType.REPOSITORY,
                name="invalid",
                aliases=[
                    ResourceAlias(
                        namespace="repository",
                        provider="github",
                        value="https://evil.example/repository",
                    )
                ],
            ),
            actor=self.actor,
        )
        binding = self.registry.bind(
            ActionProviderBindingCreate(
                provider_type=self.provider.provider_type,
                provider_instance=self.provider.provider_instance,
                resource_ids=(invalid.id,),
                credential_ref=self.binding.credential_ref,
            ),
            actor=self.actor,
            resources=self.resources,
        )
        request = self._request(
            CODE_HOST_ISSUE_UPDATE_ACTION_ID,
            {"issue_number": 1, "state": "open"},
        ).model_copy(update={"resource_ids": (invalid.id,)})

        with self.assertRaisesRegex(ValueError, "validated namespace/project"):
            await self.execution.prepare(binding.id, request, actor=self.actor)

        self.assertEqual(self.client.issue_updates, [])


if __name__ == "__main__":
    unittest.main()
