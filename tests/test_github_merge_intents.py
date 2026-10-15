"""Real GitHub HTTP adapter and durable ActionIntent merge qualification.

Only HTTP I/O is controlled. No network, live credentials, or live race is used.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import httpx

from codex_web.action_intents import (
    ActionDecisionOutcome, ActionDecisionSnapshot,
    ActionIntentClaimRequest, ActionIntentCreate, ActionIntentReconcileRequest,
    ActionIntentRetryRequest, ActionIntentStatus,
)
from codex_web.action_providers import ActionProviderBindingCreate, ActionRequest
from codex_web.identity import AuthenticationActor, AuthenticationAssurance, PrincipalKind
from codex_web.integrations.github_client import GitHubClient
from codex_web.resources import ResourceAlias, ResourceCreate, ResourceType
from codex_web.secret_backends import LocalFileSecretBackend
from codex_web.secrets import SecretCreate
from codex_web.security import ExecutionSecurityPolicy, NetworkEgressPolicy
from codex_web.services.action_intents import ActionIntentService, ActionIntentConflictError
from codex_web.services.action_providers import ActionExecutionService, ActionProviderRegistry
from codex_web.services.authority_roles import install_authority_roles
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.services.github_action_provider import GitHubActionProvider
from codex_web.services.identity import IdentityService
from codex_web.services.resources import ResourceCatalogService
from codex_web.services.secrets import SecretBroker
from codex_web.services.security_boundary import SecurityBoundaryService
from codex_web.storage.action_intents import ActionIntentStore
from codex_web.storage.action_providers import ActionProviderStateStore
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.resource_catalog import ResourceCatalogStore
from codex_web.storage.secret_state import SecretStateStore
from codex_web.storage.security_events import SecurityEventStore
from codex_web.storage.sqlite_state import SQLiteStateStore


A, B, MERGE = "a" * 40, "b" * 40, "c" * 40
ACTION = "code-host.pull-request.merge"


class GitHubMergeIntentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.make_fixture()

    def make_fixture(self):
        # Retain each isolated database for inspection if qualification fails.
        root = Path(tempfile.mkdtemp(prefix="codex-1455-"))
        sqlite = SQLiteStateStore(root / "state.sqlite3")
        identity = IdentityService(IdentityStateStore(sqlite))
        identity.bootstrap_local()
        self.actor = identity.local_trusted_actor()
        self.worker = AuthenticationActor(
            identity_id="merge-worker", principal_kind=PrincipalKind.SERVICE,
            organization_id="local", workspace_id="default",
            assurance=AuthenticationAssurance.SERVICE_TOKEN,
            service_scopes=("action-intent:worker", "secret:use"),
        )
        resources = ResourceCatalogService(ResourceCatalogStore(sqlite))
        self.repository = resources.create(ResourceCreate(
            resource_type=ResourceType.REPOSITORY, name="Source",
            aliases=[ResourceAlias(namespace="repository", provider="github", value="owner/repo")],
        ), actor=self.actor)
        secrets = SecretBroker(SecretStateStore(sqlite), {"local": LocalFileSecretBackend(root / "secrets")})
        secret = secrets.create(SecretCreate(name="Fixture", value="fixture-only-token", allowed_identity_ids=["merge-worker"]), actor=self.actor)
        authority = install_authority_roles(DefinitionRegistryService(DefinitionRegistryStore(sqlite)), resources)
        self.current = {"number": 42, "head": {"sha": A}, "merged": False,
                        "mergeable": True, "mergeable_state": "clean",
                        "html_url": "https://github.com/owner/repo/pull/42"}
        self.puts = []
        self.reads = []
        self.advance_at_put = False
        self.refusal = None
        self.lose_response = False
        self.bad_response = False
        self.declined = False
        self.read_error = False
        provider = GitHubActionProvider(resources, GitHubClient(transport=httpx.MockTransport(self.http)))
        self.provider = provider
        registry = ActionProviderRegistry(ActionProviderStateStore(sqlite))
        registry.register(provider)
        self.binding = registry.bind(ActionProviderBindingCreate(
            provider_type="github", provider_instance="github.com",
            resource_ids=(self.repository.id,), credential_ref=secret.id,
            security_policy=ExecutionSecurityPolicy(network=NetworkEgressPolicy(
                enabled=True, allowed_hosts=("api.github.com",),
            )),
        ), actor=self.actor, resources=resources)
        execution = ActionExecutionService(registry, resources, secret_broker=secrets)
        self.store = ActionIntentStore(sqlite)
        self.service = ActionIntentService(self.store, execution, authority=authority, identity=identity,
            security_boundary=SecurityBoundaryService(SecurityEventStore(sqlite)))

    def http(self, request):
        self.assertEqual(request.url.host, "api.github.com")
        self.assertEqual(request.headers["Authorization"], "Bearer fixture-only-token")
        if request.method == "GET":
            self.assertEqual(request.url.path, "/repos/owner/repo/pulls/42")
            self.reads.append(request.url.path)
            if self.read_error:
                raise httpx.ReadError("controlled read loss", request=request)
            return httpx.Response(200, json=self.current)
        self.assertEqual(request.method, "PUT")
        self.assertEqual(request.url.path, "/repos/owner/repo/pulls/42/merge")
        payload = json.loads(request.content)
        self.puts.append(payload)
        if self.advance_at_put:
            self.current["head"]["sha"] = B
        if payload.get("sha", self.current["head"]["sha"]) != self.current["head"]["sha"]:
            return httpx.Response(409, json={"message": "controlled stale head"})
        if self.refusal:
            return httpx.Response(self.refusal, json={"message": "must not echo fixture-only-token"})
        if self.declined:
            return httpx.Response(200, json={"merged": False})
        if self.bad_response:
            return httpx.Response(200, text="not-json")
        self.current.update(merged=True, merge_commit_sha=MERGE)
        if self.lose_response:
            raise httpx.ReadError("controlled response loss", request=request)
        return httpx.Response(200, json={"merged": True, "sha": MERGE})

    async def execute(self, *, pin=A, method="squash"):
        parameters = {"pull_request_number": 42, "merge_method": method}
        if pin is not None:
            parameters["expected_head_sha"] = pin
        self.intent = self.service.create(ActionIntentCreate(
            binding_id=self.binding.id,
            policy_decision=ActionDecisionSnapshot(outcome=ActionDecisionOutcome.ALLOW,
                source="policy:fixture", reason="Controlled merge qualification"),
            request=ActionRequest(action_id=ACTION, organization_id="local", workspace_id="default",
                                  resource_ids=(self.repository.id,), parameters=parameters,
                                  idempotency_key="fixture-merge"),
        ), actor=self.actor)
        claim = self.service.claim(ActionIntentClaimRequest(worker_id="worker"),
                                   actor=self.worker, intent_id=self.intent.id)
        self.assertIsNotNone(claim, self.intent.model_dump_json())
        result = await self.service.execute_claimed(self.intent.id, "worker", actor=self.worker)
        self.assertEqual(result.request.parameters, parameters)
        self.assertEqual(result.idempotency_key, "fixture-merge")
        self.assertEqual(result.resource_ids, (self.repository.id,))
        self.assertEqual(result.attempt, 1, result.model_dump_json())
        self.history = self.service.history(result.id, self.actor)
        self.assertNotIn("fixture-only-token", json.dumps(self.history))
        return result

    async def reconcile(self):
        return await self.service.reconcile(self.intent.id,
            ActionIntentReconcileRequest(retry_if_idempotent=False), actor=self.worker)

    async def test_stable_pin_reaches_http_and_verified_durable_receipt(self):
        result = await self.execute(pin=A.upper(), method="rebase")
        self.assertEqual(result.status, ActionIntentStatus.SUCCEEDED)
        self.assertEqual(self.puts, [{"merge_method": "rebase", "sha": A}])
        receipt = self.history["receipts"][-1]
        self.assertEqual(receipt["outcome"], "completed")
        self.assertEqual(receipt["result"]["output"]["head_sha"], A)
        self.assertEqual(receipt["result"]["output"]["merge_commit_sha"], MERGE)
        self.assertTrue(self.history["verifications"][-1]["verified"])
        evidence = self.history["verifications"][-1]["provider_verification"]["evidence"][0]
        self.assertEqual(evidence["metadata"]["expected_head_sha"], A)
        self.assertEqual(evidence["metadata"]["observed_head_sha"], A)
        self.assertEqual(evidence["metadata"]["merge_commit_sha"], MERGE)

    async def test_invalid_pin_fails_before_any_provider_read_or_write(self):
        for pin in ["", "a" * 39, "z" * 40]:
            with self.subTest(pin=pin):
                self.make_fixture()
                self.assertEqual((await self.execute(pin=pin)).status, ActionIntentStatus.FAILED)
                self.assertEqual(self.reads, [])
                self.assertEqual(self.puts, [])

    async def test_post_mutation_verification_mismatch_cannot_claim_success(self):
        original_http = self.http

        def handler(request):
            if request.method == "GET" and self.puts:
                self.current["head"]["sha"] = B
            return original_http(request)

        # Keep the real client; replace only the controlled HTTP transport.
        self.provider.client = GitHubClient(transport=httpx.MockTransport(handler))
        result = await self.execute()
        self.assertEqual(result.status, ActionIntentStatus.REQUIRES_RECONCILIATION)
        self.assertEqual(self.history["receipts"][-1]["result"]["output"]["head_sha"], A)
        verification = self.history["verifications"][-1]
        self.assertFalse(verification["verified"])
        metadata = verification["provider_verification"]["evidence"][0]["metadata"]
        self.assertEqual(metadata["expected_head_sha"], A)
        self.assertEqual(metadata["observed_head_sha"], B)
        self.assertEqual(len(self.puts), 1)

    async def test_changed_head_after_preflight_is_known_refusal_never_success(self):
        self.advance_at_put = True
        result = await self.execute()
        self.assertEqual(self.puts, [{"merge_method": "squash", "sha": A}])
        self.assertEqual(self.current["head"]["sha"], B)
        self.assertFalse(self.current["merged"])
        self.assertEqual(result.status, ActionIntentStatus.FAILED)
        self.assertIn("HTTP 409", result.last_error)
        self.assertEqual(self.history["receipts"][-1]["outcome"], "failed")
        self.assertFalse(self.history["verifications"])
        await self.reconcile()
        self.assertEqual(len(self.puts), 1)

    async def test_stale_preflight_and_protection_refuse_before_put(self):
        for changes in [{"head": {"sha": B}}, {"mergeable": False},
                        {"mergeable_state": "blocked"}, {"mergeable_state": "unknown"}]:
            with self.subTest(changes=changes):
                original = dict(self.current)
                self.current.update(changes)
                result = await self.execute()
                self.assertEqual(result.status, ActionIntentStatus.FAILED)
                self.assertEqual(self.puts, [])
                self.assertEqual(self.history["receipts"][-1]["outcome"], "failed")
                self.current = original
                self.make_fixture()

    async def test_documented_provider_refusals_are_failed_not_unknown(self):
        for status in [405, 409, 422]:
            with self.subTest(status=status):
                self.refusal = status
                result = await self.execute()
                self.assertEqual(result.status, ActionIntentStatus.FAILED)
                self.assertFalse(self.current["merged"])
                self.assertEqual(self.history["receipts"][-1]["outcome"], "failed")
                self.make_fixture()

    async def test_explicit_decline_is_known_failure(self):
        self.declined = True
        self.assertEqual((await self.execute()).status, ActionIntentStatus.FAILED)

    async def test_omitted_pin_preserves_payload_compatibility(self):
        self.assertEqual((await self.execute(pin=None)).status, ActionIntentStatus.SUCCEEDED)
        self.assertEqual(self.puts, [{"merge_method": "squash"}])

    async def test_already_merged_exact_head_uses_reads_only(self):
        self.current.update(merged=True, merge_commit_sha=MERGE, mergeable=False)
        self.assertEqual((await self.execute()).status, ActionIntentStatus.SUCCEEDED)
        self.assertEqual(self.puts, [])
        self.assertTrue(self.history["verifications"][-1]["verified"])

    async def test_already_merged_other_head_cannot_be_adopted(self):
        self.current.update(merged=True, merge_commit_sha=MERGE, head={"sha": B})
        self.assertEqual((await self.execute()).status, ActionIntentStatus.FAILED)
        self.assertEqual(self.puts, [])

    async def test_response_loss_reconciles_exact_original_head_without_replay(self):
        self.lose_response = True
        result = await self.execute()
        self.assertEqual(result.status, ActionIntentStatus.UNCERTAIN)
        self.assertEqual(self.history["receipts"][-1]["outcome"], "unknown")
        with self.assertRaisesRegex(ActionIntentConflictError, "retry limit reached"):
            self.service.retry(result.id, ActionIntentRetryRequest(reason="no blind retry"), actor=self.actor)
        self.assertEqual((await self.reconcile()).status, ActionIntentStatus.SUCCEEDED)
        history = self.service.history(result.id, self.actor)
        self.assertEqual([r["outcome"] for r in history["receipts"]], ["unknown", "completed"])
        self.assertFalse(history["receipts"][-1]["details"]["provider_mutation_replayed"])
        self.assertTrue(history["verifications"][-1]["verified"])
        self.assertEqual(len(self.puts), 1)

    async def test_unknown_wrong_head_or_unmerged_or_unreadable_never_verifies(self):
        self.lose_response = True
        await self.execute()
        for changes in [{"head": {"sha": B}}, {"head": {"sha": A}, "merged": False}]:
            self.current.update(changes)
            self.assertEqual((await self.reconcile()).status, ActionIntentStatus.REQUIRES_RECONCILIATION)
        self.read_error = True
        self.assertEqual((await self.reconcile()).status, ActionIntentStatus.REQUIRES_RECONCILIATION)
        self.assertEqual(len(self.puts), 1)
        history = self.service.history(self.intent.id, self.actor)
        self.assertFalse(any(v["verified"] for v in history["verifications"]))
        self.assertEqual([r["outcome"] for r in history["receipts"]], ["unknown"])

    async def test_unknown_unpinned_merge_cannot_synthesize_success(self):
        self.lose_response = True
        await self.execute(pin=None)
        self.assertEqual((await self.reconcile()).status, ActionIntentStatus.REQUIRES_RECONCILIATION)
        self.assertEqual(len(self.puts), 1)

    async def test_5xx_and_invalid_response_remain_unknown(self):
        for kind in ["server", "invalid"]:
            with self.subTest(kind=kind):
                self.refusal = 500 if kind == "server" else None
                self.bad_response = kind == "invalid"
                result = await self.execute()
                self.assertEqual(result.status, ActionIntentStatus.UNCERTAIN)
                self.assertEqual(self.history["receipts"][-1]["outcome"], "unknown")
                self.make_fixture()
