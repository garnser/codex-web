from __future__ import annotations

import asyncio
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from codex_web.canonical_events import CanonicalEventType
from codex_web.code_hosts import (
    CodeHostCapability,
    CodeHostCheckFact,
    CodeHostCommitFact,
    CodeHostCompareFact,
    CodeHostProvider,
    CodeHostProviderBinding,
    CodeHostPullRequestFact,
    CodeHostRefFact,
    CodeHostReleaseFact,
    CodeHostRepositoryFact,
    CodeHostReviewFact,
    CodeHostUnsupportedCapabilityError,
    CodeHostError,
    CodeHostTransientError,
    CodeHostWebhookFact,
)
from codex_web.resources import (
    ResourceCreate,
    ResourceProvenance,
    ResourceType,
)
from codex_web.services.canonical_events import (
    CanonicalEventBus,
    CanonicalEventIngestionService,
)
from codex_web.services.code_hosts import CodeHostRegistry, CodeHostService
from codex_web.services.identity import IdentityService, TenantIsolationError
from codex_web.services.resources import ResourceCatalogService
from codex_web.storage.canonical_events import CanonicalEventStore
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.resource_catalog import ResourceCatalogStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class _SecretBroker:
    def __init__(self) -> None:
        self.calls = []

    async def use_async(self, secret_id, *, actor, operation, consumer, context=None):
        self.calls.append((secret_id, actor.identity_id, operation, context))
        return await consumer("provider-secret")


class _Provider:
    provider_type = "fake"

    def __init__(self) -> None:
        self.credentials = []

    def capabilities(self):
        return frozenset(CodeHostCapability)

    async def merge_requirements(self, binding, resource, external_id, *, credential, checkpoint=None):
        raise CodeHostUnsupportedCapabilityError("Fixture does not implement merge requirements")

    def _credential(self, value):
        self.credentials.append(value)

    async def repository(self, binding, resource, *, credential):
        self._credential(credential)
        return CodeHostRepositoryFact(
            resource_id=resource.id,
            provider_type=self.provider_type,
            provider_instance=binding.provider_instance,
            external_id="stable-42",
            name="repo",
            full_name="org/repo",
            default_branch="main",
        )

    async def refs(self, binding, resource, *, credential):
        self._credential(credential)
        return (CodeHostRefFact(name="main", kind="branch", revision="abc"),)

    async def commit(self, binding, resource, revision, *, credential):
        self._credential(credential)
        return CodeHostCommitFact(revision=revision, message="commit")

    async def pull_request(self, binding, resource, external_id, *, credential):
        self._credential(credential)
        return CodeHostPullRequestFact(
            external_id=external_id,
            number=7,
            title="PR",
            state="open",
        )

    async def reviews(self, binding, resource, external_id, *, credential):
        self._credential(credential)
        return (CodeHostReviewFact(external_id="review-1", state="approved"),)

    async def checks(self, binding, resource, revision, *, credential):
        self._credential(credential)
        return (
            CodeHostCheckFact(
                external_id="check-1",
                name="tests",
                state="completed",
                conclusion="success",
                revision=revision,
            ),
        )

    async def releases(self, binding, resource, *, credential):
        self._credential(credential)
        return (
            CodeHostReleaseFact(
                external_id="release-1",
                tag="v1",
                name="v1",
            ),
        )

    async def compare(
        self,
        binding,
        resource,
        base_revision,
        head_revision,
        *,
        credential,
    ):
        self._credential(credential)
        return CodeHostCompareFact(
            base_revision=base_revision,
            head_revision=head_revision,
            total_commits=1,
            changed_files=("README.md",),
        )

    def normalize_webhook(self, binding, payload, *, event_kind, event_id):
        return CodeHostWebhookFact(
            provider_type=self.provider_type,
            provider_instance=binding.provider_instance,
            event_id=event_id,
            event_kind=event_kind,
            canonical_event_type=CanonicalEventType.PULL_REQUEST.value,
            repository_external_id="stable-42",
            subject_external_id="pr-7",
            action="opened",
            provider_payload={"safe": True},
        )


class CodeHostContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        sqlite = SQLiteStateStore(root / "state.sqlite3")
        identity = IdentityService(IdentityStateStore(sqlite))
        identity.bootstrap_local()
        self.actor = identity.local_trusted_actor()
        self.resources = ResourceCatalogService(ResourceCatalogStore(sqlite))
        self.resource = self.resources.create(
            ResourceCreate(
                resource_type=ResourceType.REPOSITORY,
                name="Repository",
                provenance=ResourceProvenance(
                    provider="fake",
                    provider_instance="fake.example",
                    external_id="stable-42",
                    external_url="https://fake.example/org/repo",
                ),
            ),
            actor=self.actor,
        )
        self.provider = _Provider()
        self.registry = CodeHostRegistry()
        self.registry.register_provider(self.provider)
        self.binding = CodeHostProviderBinding(
            id="fake-main",
            organization_id=self.actor.organization_id,
            workspace_id=self.actor.workspace_id,
            provider_type="fake",
            provider_instance="fake.example",
            base_url="https://fake.example/api",
            credential_ref="secret-ref",
            capabilities=tuple(CodeHostCapability),
        )
        self.registry.register_binding(self.binding)
        self.secrets = _SecretBroker()
        event_store = CanonicalEventStore(sqlite)
        self.service = CodeHostService(
            self.registry,
            self.resources,
            secrets=self.secrets,
            canonical_events=CanonicalEventIngestionService(
                CanonicalEventBus(event_store)
            ),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    async def test_all_reads_share_resource_identity_and_secret_boundary(self) -> None:
        self.assertIsInstance(self.provider, CodeHostProvider)
        repository = await self.service.repository(
            self.binding.id,
            self.resource.id,
            actor=self.actor,
        )
        commit = await self.service.commit(
            self.binding.id,
            self.resource.id,
            "abc",
            actor=self.actor,
        )
        reviews = await self.service.reviews(
            self.binding.id,
            self.resource.id,
            "7",
            actor=self.actor,
        )
        comparison = await self.service.compare(
            self.binding.id,
            self.resource.id,
            "abc",
            "def",
            actor=self.actor,
        )

        self.assertEqual(repository.resource_id, self.resource.id)
        self.assertEqual(repository.external_id, "stable-42")
        self.assertEqual(commit.revision, "abc")
        self.assertEqual(reviews[0].state, "approved")
        self.assertEqual(comparison.changed_files, ("README.md",))
        self.assertEqual(self.provider.credentials, ["provider-secret"] * 4)
        self.assertTrue(
            all(call[0] == "secret-ref" for call in self.secrets.calls)
        )
        self.assertNotIn(
            "provider-secret",
            self.binding.model_dump_json(),
        )

    async def test_binding_capabilities_fail_closed(self) -> None:
        limited = self.binding.model_copy(
            update={
                "id": "limited",
                "capabilities": (CodeHostCapability.REPOSITORY_READ,),
            }
        )
        self.registry.register_binding(limited)

        with self.assertRaises(CodeHostUnsupportedCapabilityError):
            await self.service.checks(
                limited.id,
                self.resource.id,
                "abc",
                actor=self.actor,
            )

    async def test_cross_tenant_binding_access_is_denied(self) -> None:
        other = self.actor.model_copy(
            update={
                "organization_id": "other",
                "workspace_id": "other",
            }
        )
        with self.assertRaises(TenantIsolationError):
            await self.service.repository(
                self.binding.id,
                self.resource.id,
                actor=other,
            )

    def _materialization_service(self, revision: str):
        class GitHubProvider(_Provider):
            provider_type = "github"

            async def refs(inner, binding, resource, *, credential):
                inner._credential(credential)
                return (
                    CodeHostRefFact(
                        name="main", kind="branch", revision=revision
                    ),
                )

        resource = self.resources.create(
            ResourceCreate(
                resource_type=ResourceType.REPOSITORY,
                name="org/materialized",
                provenance=ResourceProvenance(
                    provider="github",
                    provider_instance="github.com",
                    external_id="stable-42",
                    external_url="https://github.com/org/repo",
                ),
            ),
            actor=self.actor,
        )
        provider = GitHubProvider()
        registry = CodeHostRegistry()
        registry.register_provider(provider)
        binding = CodeHostProviderBinding(
            id="github-materialize",
            organization_id=self.actor.organization_id,
            workspace_id=self.actor.workspace_id,
            provider_type="github",
            provider_instance="github.com",
            base_url="https://api.github.com",
            credential_ref="materialize-secret",
            capabilities=tuple(CodeHostCapability),
        )
        registry.register_binding(binding)
        service = CodeHostService(registry, self.resources, secrets=self.secrets)
        return service, provider, binding, resource

    async def test_materialization_requires_exact_attested_default_branch(self) -> None:
        revision = "a" * 40
        service, provider, binding, resource = self._materialization_service(revision)
        checkout = Path(self.temp.name) / "materialize-checkout"
        checkout.mkdir()
        with (
            patch.object(service, "_git_has_complete_history", return_value=False),
            patch.object(service, "_materialize_git_sync") as fetch,
        ):
            fact = await service.materialize_default_branch_revision(
                binding.id,
                resource.id,
                revision,
                checkout=checkout,
                actor=self.actor,
            )

        self.assertEqual(fact.revision, revision)
        self.assertEqual(fact.repository_full_name, "org/repo")
        self.assertTrue(fact.fetched)
        fetch.assert_called_once()
        command_args = fetch.call_args.args
        self.assertEqual(command_args[1], "https://github.com/org/repo.git")
        self.assertEqual(command_args[3], revision)
        self.assertEqual(self.secrets.calls[-1][2], "code-host.revision.materialize")
        self.assertNotIn("provider-secret", fact.model_dump_json())

        with patch.object(service, "_materialize_git_sync") as denied_fetch:
            with self.assertRaisesRegex(CodeHostError, "default-branch tip"):
                await service.materialize_default_branch_revision(
                    binding.id,
                    resource.id,
                    "b" * 40,
                    checkout=checkout,
                    actor=self.actor,
                )
        denied_fetch.assert_not_called()

        foreign = CodeHostRepositoryFact(
            resource_id=resource.id,
            provider_type="github",
            provider_instance="github.com",
            external_id="foreign-99",
            name="foreign",
            full_name="other/foreign",
            default_branch="main",
        )
        with (
            patch.object(provider, "repository", new=AsyncMock(return_value=foreign)),
            patch.object(service, "_materialize_git_sync") as foreign_fetch,
        ):
            with self.assertRaisesRegex(CodeHostError, "Resource provenance"):
                await service.materialize_default_branch_revision(
                    binding.id,
                    resource.id,
                    revision,
                    checkout=checkout,
                    actor=self.actor,
                )
        foreign_fetch.assert_not_called()

        unauthenticated = binding.model_copy(
            update={"id": "github-materialize-unauthenticated", "credential_ref": None}
        )
        service.registry.register_binding(unauthenticated)
        with self.assertRaisesRegex(CodeHostError, "authenticated provider access"):
            await service.materialize_default_branch_revision(
                unauthenticated.id,
                resource.id,
                revision,
                checkout=checkout,
                actor=self.actor,
            )

    async def test_concurrent_materialization_fetches_the_revision_once(self) -> None:
        revision = "c" * 40
        service, _, binding, resource = self._materialization_service(revision)
        checkout = Path(self.temp.name) / "concurrent-checkout"
        checkout.mkdir()
        with (
            patch.object(service, "_git_has_complete_history", side_effect=[False, True]),
            patch.object(service, "_materialize_git_sync") as fetch,
        ):
            first, second = await asyncio.gather(
                *(
                    service.materialize_default_branch_revision(
                        binding.id,
                        resource.id,
                        revision,
                        checkout=checkout,
                        actor=self.actor,
                    )
                    for _ in range(2)
                )
            )

        self.assertEqual((first.fetched, second.fetched), (True, False))
        fetch.assert_called_once()

    def test_materialization_git_command_disables_repository_execution(self) -> None:
        revision = "d" * 40
        calls = []

        def run(command, **kwargs):
            calls.append((command, kwargs))
            if "index-pack" in command:
                packs = Path(command[command.index("--git-dir") + 1]) / "objects/pack"
                packs.mkdir(parents=True)
                (packs / "pack-fixture.pack").write_bytes(b"fixture")
                (packs / "pack-fixture.idx").write_bytes(b"fixture-index")
            stdout = (str(checkout / ".git") + "\n" if "--git-common-dir" in command
                      else "false\n" if "--is-shallow-repository" in command
                      else revision + "\n" if "rev-parse" in command else "")
            return subprocess.CompletedProcess(command, 0, stdout, "")

        checkout = Path(self.temp.name) / "git-command-checkout"
        (checkout / ".git/objects/pack").mkdir(parents=True)
        with patch.object(CodeHostService, "_run_materialization_git", side_effect=run):
            CodeHostService._materialize_git_sync(
                checkout,
                "https://github.com/org/repo.git",
                "main",
                revision,
                "never-print-this-token",
                "github",
                self.resource.id,
            )

        fetch_command, fetch_options = next(item for item in calls if "fetch" in item[0])
        self.assertIn("core.hooksPath=/dev/null", fetch_command)
        self.assertIn("protocol.allow=never", fetch_command)
        self.assertIn("protocol.https.allow=always", fetch_command)
        self.assertIn("--no-recurse-submodules", fetch_command)
        self.assertNotIn("never-print-this-token", " ".join(fetch_command))
        self.assertEqual(
            fetch_options["env"]["CODEX_MATERIALIZE_TOKEN"],
            "never-print-this-token",
        )
        self.assertIn("http.followRedirects=false", fetch_command)
        for command, options in calls:
            if "fetch" not in command:
                self.assertNotIn("CODEX_MATERIALIZE_TOKEN", options["env"])

        with patch(
            "codex_web.services.code_hosts.CodeHostService._run_materialization_git",
            side_effect=subprocess.CalledProcessError(
                128,
                ["git", "fetch"],
                stderr="authentication failed: never-print-this-token",
            ),
        ):
            with self.assertRaises(CodeHostTransientError) as failure:
                CodeHostService._materialize_git_sync(
                    checkout,
                    "https://github.com/org/repo.git",
                    "main",
                    revision,
                    "never-print-this-token",
                    "github",
                    self.resource.id,
                )
        self.assertNotIn("never-print-this-token", str(failure.exception))

    async def test_webhook_normalization_is_canonical_and_idempotent(self) -> None:
        first_fact, first = await self.service.ingest_webhook(
            self.binding.id,
            actor=self.actor,
            event_kind="pull_request",
            event_id="delivery-1",
            payload={"provider": "raw"},
        )
        second_fact, second = await self.service.ingest_webhook(
            self.binding.id,
            actor=self.actor,
            event_kind="pull_request",
            event_id="delivery-1",
            payload={"provider": "raw"},
        )

        self.assertEqual(
            first_fact.canonical_event_type,
            CanonicalEventType.PULL_REQUEST.value,
        )
        self.assertEqual(first.event.event_id, second.event.event_id)
        self.assertTrue(first.inserted)
        self.assertFalse(second.inserted)
        self.assertEqual(second_fact.provider_payload, {"safe": True})

    def test_contract_exposes_no_external_mutation_methods(self) -> None:
        forbidden = {
            "create_branch",
            "open_pull_request",
            "merge_pull_request",
            "comment",
            "approve",
            "create_release",
        }
        self.assertTrue(forbidden.isdisjoint(set(dir(CodeHostService))))


if __name__ == "__main__":
    unittest.main()
