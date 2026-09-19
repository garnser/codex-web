from __future__ import annotations

import unittest

import httpx

from codex_web.canonical_events import CanonicalEventType
from codex_web.code_hosts import (
    CodeHostCapability,
    CodeHostProvider,
    CodeHostProviderBinding,
    CodeHostTransientError,
)
from codex_web.integrations.github_client import GitHubClient
from codex_web.integrations.gitlab_client import GitLabClient
from codex_web.resources import (
    Resource,
    ResourceAlias,
    ResourceProvenance,
    ResourceType,
)
from codex_web.services.github_code_host import GitHubCodeHostProvider
from codex_web.services.gitlab_code_host import GitLabCodeHostProvider


class CodeHostAdapterTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _resource(provider: str) -> Resource:
        return Resource(
            id=f"resource-{provider}",
            organization_id="org-a",
            workspace_id="ws-a",
            resource_type=ResourceType.REPOSITORY,
            name="Canonical repository",
            aliases=[
                ResourceAlias(
                    namespace="provider",
                    provider=provider,
                    value="acme/widgets",
                )
            ],
            provenance=ResourceProvenance(
                provider=provider,
                provider_instance=f"{provider}.example",
                external_id="42",
                external_url=f"https://{provider}.example/acme/widgets",
            ),
        )

    @staticmethod
    def _binding(provider: str) -> CodeHostProviderBinding:
        base_url = (
            "https://api.github.example"
            if provider == "github"
            else "https://gitlab.example/api/v4"
        )
        return CodeHostProviderBinding(
            id=f"{provider}-main",
            organization_id="org-a",
            workspace_id="ws-a",
            provider_type=provider,
            provider_instance=f"{provider}.example",
            base_url=base_url,
            credential_ref=f"secret-{provider}",
            capabilities=tuple(CodeHostCapability),
        )

    async def test_github_normalizes_repository_pr_checks_and_webhook(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.raw_path.decode().split("?", 1)[0]
            if path == "/repos/acme/widgets":
                return httpx.Response(
                    200,
                    json={
                        "id": 42,
                        "name": "widgets",
                        "full_name": "acme/widgets",
                        "default_branch": "main",
                        "html_url": "https://github.example/acme/widgets",
                        "archived": False,
                        "visibility": "private",
                    },
                )
            if path == "/repos/acme/widgets/pulls/7":
                return httpx.Response(
                    200,
                    json={
                        "id": 700,
                        "number": 7,
                        "title": "Change",
                        "state": "open",
                        "draft": False,
                        "html_url": "https://github.example/acme/widgets/pull/7",
                        "head": {"ref": "feature"},
                        "base": {"ref": "main"},
                        "user": {"id": 11},
                        "merge_commit_sha": None,
                    },
                )
            if path == "/repos/acme/widgets/commits/abc/check-runs":
                return httpx.Response(
                    200,
                    json={
                        "check_runs": [
                            {
                                "id": 9,
                                "name": "tests",
                                "status": "completed",
                                "conclusion": "success",
                                "head_sha": "abc",
                                "html_url": "https://github.example/check/9",
                            }
                        ]
                    },
                )
            return httpx.Response(404, json={"message": "not found"})

        provider = GitHubCodeHostProvider(
            GitHubClient(transport=httpx.MockTransport(handler))
        )
        binding = self._binding("github")
        resource = self._resource("github")

        self.assertIsInstance(provider, CodeHostProvider)
        repository = await provider.repository(
            binding,
            resource,
            credential="token",
        )
        pull_request = await provider.pull_request(
            binding,
            resource,
            "7",
            credential="token",
        )
        checks = await provider.checks(
            binding,
            resource,
            "abc",
            credential="token",
        )
        event = provider.normalize_webhook(
            binding,
            {
                "action": "opened",
                "repository": {"id": 42, "full_name": "acme/widgets"},
                "pull_request": {
                    "id": 700,
                    "state": "open",
                    "html_url": "https://github.example/acme/widgets/pull/7",
                },
            },
            event_kind="pull_request",
            event_id="delivery-1",
        )

        self.assertEqual(repository.resource_id, resource.id)
        self.assertEqual(repository.external_id, "42")
        self.assertEqual(pull_request.number, 7)
        self.assertEqual(pull_request.source_ref, "feature")
        self.assertEqual(checks[0].conclusion, "success")
        self.assertEqual(
            event.canonical_event_type,
            CanonicalEventType.PULL_REQUEST.value,
        )
        self.assertEqual(event.repository_external_id, "42")

    async def test_gitlab_normalizes_equivalent_repository_pr_checks_and_webhook(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.raw_path.decode().split("?", 1)[0]
            if path == "/api/v4/projects/acme%2Fwidgets":
                return httpx.Response(
                    200,
                    json={
                        "id": 42,
                        "name": "widgets",
                        "path_with_namespace": "acme/widgets",
                        "default_branch": "main",
                        "web_url": "https://gitlab.example/acme/widgets",
                        "archived": False,
                        "visibility": "private",
                    },
                )
            if path == "/api/v4/projects/acme%2Fwidgets/merge_requests/7":
                return httpx.Response(
                    200,
                    json={
                        "id": 700,
                        "iid": 7,
                        "title": "Change",
                        "state": "opened",
                        "source_branch": "feature",
                        "target_branch": "main",
                        "author": {"id": 11},
                        "web_url": "https://gitlab.example/acme/widgets/-/merge_requests/7",
                        "draft": False,
                    },
                )
            if path == "/api/v4/projects/acme%2Fwidgets/repository/commits/abc/statuses":
                return httpx.Response(
                    200,
                    json=[
                        {
                            "id": 9,
                            "name": "tests",
                            "status": "success",
                            "sha": "abc",
                            "target_url": "https://gitlab.example/job/9",
                        }
                    ],
                )
            return httpx.Response(404, json={"message": "not found"})

        provider = GitLabCodeHostProvider(
            GitLabClient(transport=httpx.MockTransport(handler))
        )
        binding = self._binding("gitlab")
        resource = self._resource("gitlab")

        self.assertIsInstance(provider, CodeHostProvider)
        repository = await provider.repository(
            binding,
            resource,
            credential="token",
        )
        pull_request = await provider.pull_request(
            binding,
            resource,
            "7",
            credential="token",
        )
        checks = await provider.checks(
            binding,
            resource,
            "abc",
            credential="token",
        )
        event = provider.normalize_webhook(
            binding,
            {
                "object_kind": "merge_request",
                "project": {
                    "id": 42,
                    "path_with_namespace": "acme/widgets",
                },
                "object_attributes": {
                    "id": 700,
                    "iid": 7,
                    "action": "open",
                    "state": "opened",
                    "url": "https://gitlab.example/acme/widgets/-/merge_requests/7",
                },
            },
            event_kind="merge_request",
            event_id="delivery-1",
        )

        self.assertEqual(repository.resource_id, resource.id)
        self.assertEqual(repository.external_id, "42")
        self.assertEqual(pull_request.number, 7)
        self.assertEqual(pull_request.source_ref, "feature")
        self.assertEqual(checks[0].conclusion, "success")
        self.assertEqual(
            event.canonical_event_type,
            CanonicalEventType.PULL_REQUEST.value,
        )
        self.assertEqual(event.repository_external_id, "42")

    async def test_provider_rate_limits_are_normalized_as_transient(self) -> None:
        github = GitHubCodeHostProvider(
            GitHubClient(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(429, json={"message": "slow down"})
                )
            )
        )
        with self.assertRaises(CodeHostTransientError):
            await github.repository(
                self._binding("github"),
                self._resource("github"),
                credential="token",
            )

        gitlab = GitLabCodeHostProvider(
            GitLabClient(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(429, json={"message": "slow down"})
                )
            )
        )
        with self.assertRaises(CodeHostTransientError):
            await gitlab.repository(
                self._binding("gitlab"),
                self._resource("gitlab"),
                credential="token",
            )


if __name__ == "__main__":
    unittest.main()
