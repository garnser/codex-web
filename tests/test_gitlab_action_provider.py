from __future__ import annotations

import json
import re
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx

from codex_web.integrations.gitlab_client import GitLabClient
from codex_web.action_providers import ActionProviderBindingCreate, ActionRequest
from codex_web.action_intents import (
    ActionIntentClaimRequest, ActionIntentCreate, ActionIntentStatus,
)
from codex_web.identity import AuthenticationActor, AuthenticationAssurance, PrincipalKind
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
from codex_web.services.action_providers import ActionExecutionService, ActionProviderRegistry, ActionRequirementError
from codex_web.services.code_host_action_contract import (
    CODE_HOST_BRANCH_PUBLISH_ACTION_ID,
    CODE_HOST_CHANGE_REQUEST_UPSERT_ACTION_ID,
    CODE_HOST_ISSUE_COMMENT_ACTION_ID,
    CODE_HOST_ISSUE_UPDATE_ACTION_ID,
    CODE_HOST_PULL_REQUEST_MERGE_ACTION_ID,
)
from codex_web.services.gitlab_action_provider import GitLabActionProvider
from codex_web.services.identity import IdentityService
from codex_web.services.action_intents import ActionIntentService
from codex_web.services.authority_roles import install_authority_roles
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.services.resources import ResourceCatalogService
from codex_web.services.secrets import SecretBroker
from codex_web.storage.action_providers import ActionProviderStateStore
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.resource_catalog import ResourceCatalogStore
from codex_web.storage.secret_state import SecretStateStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.action_intents import ActionIntentStore
from codex_web.storage.definition_registry import DefinitionRegistryStore


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

    async def merge_request_closes_issues(self, api_base, project, iid, *, token):
        self.credentials.append(token)
        return self.closing_issues

    async def branch(self, api_base, project, branch, *, token):
        self.credentials.append(token)
        return {"name": branch, "protected": getattr(self, "branch_protected", False), "default": False, "commit": {"id": self.branches.get(branch)}}


class GitLabActionProviderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        sqlite = SQLiteStateStore(root / "state.sqlite3")
        identity = IdentityService(IdentityStateStore(sqlite))
        identity.bootstrap_local()
        self.sqlite = sqlite
        self.identity = identity
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
            SecretCreate(
                name="GitLab token", value="secret-gitlab-token",
                allowed_identity_ids=["action-worker"],
            ),
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

    def _title_only_rest_client(self):
        rows, writes = [], []

        def respond(request):
            if request.method == "GET":
                return httpx.Response(200, json=rows if request.url.path.endswith("/merge_requests") else rows[0])
            payload = json.loads(request.content)
            writes.append((request.method, payload))
            if request.method == "POST":
                rows.append({"id": 42, "iid": 9, "state": "opened"})
            rows[0].update(payload)
            # Model the real documented title semantics, ignoring any
            # unsupported draft REST parameter sent by the caller.
            rows[0]["draft"] = bool(re.match(r"^(?:Draft:|\[Draft\]|\(Draft\))", rows[0]["title"], re.I))
            return httpx.Response(200, json=rows[0])

        return GitLabClient(transport=httpx.MockTransport(respond)), rows, writes

    async def test_draft_create_ready_update_and_retry_use_native_titles(self):
        client, rows, writes = self._title_only_rest_client()
        self.provider.client = client
        request = self._request(CODE_HOST_CHANGE_REQUEST_UPSERT_ACTION_ID, {
            "title": "Delivery", "body": "Closes #890",
            "head": "feature/890", "base": "main", "draft": True,
        })
        first = await self.execution.execute(self.binding.id, request, actor=self.actor)
        self.assertEqual(rows[0]["title"], "Draft: Delivery")
        self.assertTrue(rows[0]["draft"])
        self.assertTrue((await self.execution.verify(self.binding.id, first, actor=self.actor)).verified)
        await self.execution.execute(self.binding.id, request, actor=self.actor)
        self.assertEqual(len(writes), 1)
        ready = request.model_copy(update={"parameters": {**request.parameters, "draft": False}})
        result = await self.execution.execute(self.binding.id, ready, actor=self.actor)
        self.assertEqual(rows[0]["title"], "Delivery")
        self.assertFalse(rows[0]["draft"])
        self.assertTrue((await self.execution.verify(self.binding.id, result, actor=self.actor)).verified)
        self.assertEqual([method for method, _ in writes], ["POST", "PUT"])
        self.assertTrue(all("draft" not in payload for _, payload in writes))
        self.assertEqual(first.output["change_request_number"], result.output["change_request_number"])

    async def test_native_title_format_preserves_content_and_clears_only_leading_markers(self):
        for title, draft, expected in (
            ("[Draft] Delivery", True, "[Draft] Delivery"),
            ("(Draft) Delivery", True, "(Draft) Delivery"),
            ("draft: Delivery", True, "draft: Delivery"),
            ("[Draft] Delivery", False, "Delivery"),
            ("(Draft) Delivery", False, "Delivery"),
            ("draft: Delivery", False, "Delivery"),
            ("Draft: [Draft] Delivery", False, "Delivery"),
            ("Review Draft: content", False, "Review Draft: content"),
            ("Review [Draft] content", True, "Draft: Review [Draft] content"),
        ):
            with self.subTest(title=title, draft=draft):
                client, rows, writes = self._title_only_rest_client()
                self.provider.client = client
                request = self._request(CODE_HOST_CHANGE_REQUEST_UPSERT_ACTION_ID, {
                    "title": title, "body": "Closes #890", "head": "feature/890",
                    "base": "main", "draft": draft,
                })
                result = await self.execution.execute(self.binding.id, request, actor=self.actor)
                self.assertEqual(rows[0]["title"], expected)
                self.assertEqual(rows[0]["draft"], draft)
                self.assertNotIn("draft", writes[0][1])
                self.assertTrue((await self.execution.verify(self.binding.id, result, actor=self.actor)).verified)

    async def test_draft_normalization_does_not_hide_provider_drift_or_foreign_marker(self):
        client, rows, writes = self._title_only_rest_client()
        self.provider.client = client
        request = self._request(CODE_HOST_CHANGE_REQUEST_UPSERT_ACTION_ID, {
            "title": "Delivery", "body": "Closes #890", "head": "feature/890",
            "base": "main", "draft": True,
        })
        result = await self.execution.execute(self.binding.id, request, actor=self.actor)
        original = dict(rows[0])
        for field, value in (("title", "[Draft] Delivery"), ("description", "changed"),
                             ("source_branch", "foreign"), ("target_branch", "other"),
                             ("draft", False), ("iid", 8)):
            rows[0].clear()
            rows[0].update({**original, field: value})
            with self.subTest(field=field):
                self.assertFalse((await self.execution.verify(self.binding.id, result, actor=self.actor)).verified)
        rows[0].clear()
        rows[0].update({**original, "description": "another action owns this MR"})
        with self.assertRaises(ValueError):
            await self.execution.execute(self.binding.id, request, actor=self.actor)
        self.assertEqual(len(writes), 1)

    async def test_empty_ready_title_after_marker_is_known_invalid_before_rest(self):
        client, _, writes = self._title_only_rest_client()
        self.provider.client = client
        request = self._request(CODE_HOST_CHANGE_REQUEST_UPSERT_ACTION_ID, {
            "title": "Draft: [Draft]", "body": "body", "head": "feature/890",
            "base": "main", "draft": False,
        })
        from codex_web.services.action_providers import ActionRequirementError
        with self.assertRaises(ActionRequirementError):
            await self.execution.execute(self.binding.id, request, actor=self.actor)
        self.assertEqual(writes, [])

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

    async def _execute_upsert_intent(self, parameters: dict):
        authority = install_authority_roles(
            DefinitionRegistryService(DefinitionRegistryStore(self.sqlite)),
            self.resources,
        )
        service = ActionIntentService(
            ActionIntentStore(self.sqlite), self.execution,
            authority=authority, identity=self.identity,
        )
        worker = AuthenticationActor(
            identity_id="action-worker", principal_kind=PrincipalKind.SERVICE,
            organization_id=self.actor.organization_id,
            workspace_id=self.actor.workspace_id,
            assurance=AuthenticationAssurance.SERVICE_TOKEN,
            service_scopes=("action-intent:worker", "secret:use"),
        )
        request = self._request(CODE_HOST_CHANGE_REQUEST_UPSERT_ACTION_ID, parameters)
        intent = service.create(
            ActionIntentCreate(binding_id=self.binding.id, request=request),
            actor=self.actor,
        )
        service.claim(
            ActionIntentClaimRequest(worker_id="worker-1", lease_seconds=30),
            actor=worker, intent_id=intent.id,
        )
        completed = await service.execute_claimed(intent.id, "worker-1", actor=worker)
        self.assertNotEqual(completed.status, ActionIntentStatus.CANCELLED, completed.last_error)
        return completed, service.history(intent.id, self.actor)

    async def test_local_malformed_upsert_is_known_failed_without_provider_calls(self):
        completed, history = await self._execute_upsert_intent({
            "title": "Delivery", "description": "secret-input-not-for-diagnostics",
            "source_branch": "feature/890", "target_branch": "main",
        })
        self.assertEqual(completed.status, ActionIntentStatus.FAILED)
        self.assertEqual(self.client.credentials, [])
        self.assertEqual(self.client.merge_request_creates, 0)
        self.assertEqual(self.client.merge_request_updates, 0)
        self.assertEqual(history["receipts"][-1]["outcome"], "failed")
        self.assertFalse(completed.failure.requires_reconciliation)
        self.assertFalse(completed.failure.automatic_retry_allowed)
        self.assertIn("title, body, head, base, draft", completed.last_error)
        self.assertNotIn(
            "secret-input-not-for-diagnostics",
            completed.last_error + str(history["receipts"]),
        )
        self.assertNotIn("secret-gitlab-token", str(history))

    async def test_value_error_after_mr_write_retains_unknown_receipt(self):
        create = self.client.create_merge_request

        async def write_then_fail(*args, **kwargs):
            await create(*args, **kwargs)
            raise ValueError("response normalization failed after write")

        self.client.create_merge_request = write_then_fail
        completed, history = await self._execute_upsert_intent({
            "title": "Delivery", "body": "Closes #890", "head": "feature/890",
            "base": "main", "draft": False,
        })
        self.assertEqual(self.client.merge_request_creates, 1)
        self.assertEqual(len(self.client.merge_request_rows), 1)
        self.assertEqual(completed.status, ActionIntentStatus.UNCERTAIN)
        self.assertEqual(history["receipts"][-1]["outcome"], "unknown")
        self.assertTrue(completed.failure.requires_reconciliation)
        self.assertNotIn("secret-gitlab-token", str(history))

    async def test_local_upsert_field_validation_never_calls_mr_api(self):
        valid = {"title": "Delivery", "body": "", "head": "feature/890", "base": "main"}
        for invalid in ({"title": ""}, {"draft": "false"}, {"head": "-unsafe"}):
            with self.subTest(invalid=invalid):
                request = self._request(
                    CODE_HOST_CHANGE_REQUEST_UPSERT_ACTION_ID, {**valid, **invalid},
                )
                with self.assertRaises(ActionRequirementError):
                    await self.execution.execute(self.binding.id, request, actor=self.actor)
                self.assertEqual(self.client.credentials, [])

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

    def _existing_mr_request(self):
        state = self.provider.contract.workspaces.store.load()
        self.recovery_state = state
        ref = "group/platform/codex-web#289"
        state.workspaces[0].work_item_ref = ref
        state.leases[0].work_item_ref = ref
        old_head = self.head
        path = Path(state.workspaces[0].path)
        (path / "delivery.txt").write_text("fixed\n")
        subprocess.run(["git", "add", "delivery.txt"], cwd=path, check=True)
        subprocess.run(["git", "commit", "-m", "Recover existing MR"], cwd=path, check=True, capture_output=True)
        self.recovery_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=path, text=True).strip()
        self.legacy_branch = "james/saas-app-289-signed-saml-validation"
        self.client.issues[289] = {"iid": 289, "project_id": 5}
        self.client.closing_issues = [{"iid": 289, "project_id": 5}]
        self.client.merge_request_rows = [{"iid": 258, "state": "opened", "source_project_id": 5, "target_project_id": 5, "target_branch": "main", "source_branch": self.legacy_branch, "sha": old_head}]
        self.client.branches[self.legacy_branch] = old_head
        async def existing_mr(*args, **kwargs):
            return self.client.merge_request_rows[0]
        self.client.merge_request = existing_mr
        return self._request(CODE_HOST_BRANCH_PUBLISH_ACTION_ID, {
            "execution_workspace_id": "workspace-a", "branch": self.legacy_branch,
            "head_revision": self.recovery_head, "expected_remote_revision": old_head,
            "existing_change_request_number": 258,
        })

    async def test_existing_mr_legacy_branch_recovery_requires_remote_issue_proof(self):
        request = self._existing_mr_request()
        result = await self.execution.execute(self.binding.id, request, actor=self.actor)
        self.assertEqual(self.published[2], self.legacy_branch)
        self.assertEqual(self.published[3], self.recovery_head)
        self.assertEqual(self.published[5], self.head)
        self.assertTrue((await self.execution.verify(self.binding.id, result, actor=self.actor)).verified)

    async def test_existing_mr_recovery_rejects_unrelated_or_stale_provider_data(self):
        request = self._existing_mr_request()
        mr = self.client.merge_request_rows[0]
        for key, value in [("iid", 259), ("state", "merged"), ("source_project_id", 6), ("target_project_id", 6), ("source_branch", "main"), ("target_branch", self.legacy_branch), ("sha", "f" * 40)]:
            with self.subTest(field=key):
                old = mr[key]; mr[key] = value
                with self.assertRaisesRegex(Exception, "could not be attested"):
                    await self.execution.execute(self.binding.id, request, actor=self.actor)
                self.assertIsNone(self.published)
                mr[key] = old
        self.client.closing_issues = [{"iid": 289, "project_id": 6}]
        with self.assertRaisesRegex(Exception, "could not be attested"):
            await self.execution.execute(self.binding.id, request, actor=self.actor)
        self.client.closing_issues = [{"iid": 290, "project_id": 5}]
        with self.assertRaisesRegex(Exception, "could not be attested"):
            await self.execution.execute(self.binding.id, request, actor=self.actor)
        self.client.closing_issues = [{"iid": 289, "project_id": 5}]
        self.client.branch_protected = True
        with self.assertRaisesRegex(ActionRequirementError, "could not be attested"):
            await self.execution.execute(self.binding.id, request, actor=self.actor)
        self.client.branch_protected = False
        self.client.branches[self.legacy_branch] = "f" * 40
        with self.assertRaisesRegex(Exception, "could not be attested"):
            await self.execution.execute(self.binding.id, request, actor=self.actor)
        self.assertIsNone(self.published)

    async def test_existing_mr_recovery_requires_matching_issue_lease(self):
        request = self._existing_mr_request()
        for ref in [None, "thread:bootstrap", "other/repo#289", "group/platform/codex-web#0"]:
            with self.subTest(ref=ref):
                self.recovery_state.workspaces[0].work_item_ref = ref
                self.recovery_state.leases[0].work_item_ref = ref
                with self.assertRaisesRegex(ActionRequirementError, "issue-scoped workspace lease"):
                    await self.execution.execute(self.binding.id, request, actor=self.actor)
        self.assertIsNone(self.published)

    async def test_legacy_branch_still_rejected_without_explicit_mr(self):
        request = self._existing_mr_request()
        request.parameters.pop("existing_change_request_number")
        with self.assertRaisesRegex(ActionRequirementError, "recovery publication requires"):
            await self.execution.execute(self.binding.id, request, actor=self.actor)
        self.assertIsNone(self.published)

    async def test_existing_mr_recovery_rechecks_lease_after_provider_reads(self):
        request = self._existing_mr_request()
        original = self.client.branch
        async def expire(*args, **kwargs):
            result = await original(*args, **kwargs)
            self.recovery_state.leases[0].expires_at = time.time() - 1
            return result
        self.client.branch = expire
        with self.assertRaisesRegex(ValueError, "lease is not active"):
            await self.execution.execute(self.binding.id, request, actor=self.actor)
        self.assertIsNone(self.published)

    async def test_existing_mr_recovery_rejects_changed_issue_scope_after_reads(self):
        request = self._existing_mr_request()
        original = self.client.branch
        async def rescope(*args, **kwargs):
            result = await original(*args, **kwargs)
            self.recovery_state.workspaces[0].work_item_ref = "group/platform/codex-web#290"
            self.recovery_state.leases[0].work_item_ref = "group/platform/codex-web#290"
            return result
        self.client.branch = rescope
        with self.assertRaisesRegex(ActionRequirementError, "attestation changed"):
            await self.execution.execute(self.binding.id, request, actor=self.actor)
        self.assertIsNone(self.published)

    async def test_existing_mr_recovery_preserves_parameter_and_local_head_guards(self):
        request = self._existing_mr_request()
        for number in [True, 0, -1, "258"]:
            request.parameters["existing_change_request_number"] = number
            with self.assertRaisesRegex(ActionRequirementError, "positive number"):
                await self.execution.execute(self.binding.id, request, actor=self.actor)
        request.parameters["existing_change_request_number"] = 258
        request.parameters.pop("expected_remote_revision")
        with self.assertRaisesRegex(ActionRequirementError, "expected remote revision"):
            await self.execution.execute(self.binding.id, request, actor=self.actor)
        request.parameters["expected_remote_revision"] = self.head
        request.parameters["head_revision"] = self.head
        with self.assertRaisesRegex(ActionRequirementError, "Git head does not match"):
            await self.execution.execute(self.binding.id, request, actor=self.actor)
        request.parameters["head_revision"] = self.recovery_head
        (Path(self.recovery_state.workspaces[0].path) / "delivery.txt").write_text("uncommitted\n")
        with self.assertRaisesRegex(ActionRequirementError, "uncommitted changes"):
            await self.execution.execute(self.binding.id, request, actor=self.actor)
        self.assertIsNone(self.published)

    async def test_existing_mr_recovery_rejects_cross_scope_and_readonly_lease(self):
        request = self._existing_mr_request()
        lease = self.recovery_state.leases[0]
        original_owner = lease.owner_identity_id
        lease.owner_identity_id = "other-actor"
        with self.assertRaisesRegex(ActionRequirementError, "writable workspace scope"):
            await self.execution.execute(self.binding.id, request, actor=self.actor)
        lease.owner_identity_id = original_owner
        lease.resource_modes[self.repository.id] = LeaseMode.READ
        with self.assertRaisesRegex(ActionRequirementError, "writable workspace scope"):
            await self.execution.execute(self.binding.id, request, actor=self.actor)
        self.assertIsNone(self.published)

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
