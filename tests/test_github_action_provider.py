from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_web.action_providers import ActionProviderBindingCreate, ActionRequest
from codex_web.resources import ResourceAlias, ResourceCreate, ResourceType
from codex_web.secret_backends import LocalFileSecretBackend
from codex_web.secrets import SecretCreate
from codex_web.services.action_providers import ActionExecutionService, ActionProviderRegistry
from codex_web.services.github_action_provider import (
    CODE_HOST_ISSUE_COMMENT_ACTION_ID,
    CODE_HOST_ISSUE_UPDATE_ACTION_ID,
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
        return {
            "id": 17,
            "state": payload["state"],
            "html_url": f"https://github.com/{repo}/issues/{number}",
        }


class GitHubActionProviderTests(unittest.IsolatedAsyncioTestCase):
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
        self.provider = GitHubActionProvider(self.resources, self.client)
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

        with self.assertRaisesRegex(ValueError, "validated owner/repository"):
            await self.execution.prepare(binding.id, request, actor=self.actor)

        self.assertEqual(self.client.issue_updates, [])


if __name__ == "__main__":
    unittest.main()
