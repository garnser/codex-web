"""Canonical private worker/broker/Evidence composition; only provider HTTP is fake."""
import json
import time
import unittest
from copy import deepcopy
from pathlib import Path
from urllib.parse import quote
from unittest.mock import patch
from types import SimpleNamespace

import httpx

from codex_web.action_providers import ActionProviderBindingCreate
from codex_web.resources import ResourceAlias, ResourceUpdate
from codex_web.secret_backends import LocalFileSecretBackend
from codex_web.secrets import SecretCreate
from codex_web.services.code_hosts import CodeHostRegistry, CodeHostService
from codex_web.services.github_action_provider import GitHubActionProvider
from codex_web.services.github_code_host import GitHubCodeHostProvider
from codex_web.integrations.github_client import GitHubClient
from codex_web.services.secrets import SecretBroker
from codex_web.storage.secret_state import SecretStateStore
from codex_web.services.data_governance import DataGovernanceService
from codex_web.storage.data_governance import DataGovernanceStore
from tests import test_worker_verifications as verification_fixtures
from tests import test_administrative_broker_runtime as administrative_fixtures
from tests import test_github_merge_requirements as provider_fixtures


METHOD = "code-host-mr-observation.v1"


class ArtifactEvidence14CompatibilityTests(unittest.TestCase):
    def test_prior13_payload_is_preserved_without_legacy_freshness_relabel(self):
        from codex_web.artifact_evidence import ArtifactEvidenceState, Evidence
        from codex_web.storage.artifact_evidence import ArtifactEvidenceStore, ARTIFACT_EVIDENCE_MIGRATIONS
        original = ArtifactEvidenceState(evidence=[Evidence(organization_id="local", workspace_id="default",
            producer_identity_id="original-producer", evidence_type="review", result="pass",
            observed_at=100, metadata={"legacy_source": "retained"})]).model_dump(mode="json")
        original["schema_version"] = "1.3"
        before = deepcopy(original)
        migrated = ARTIFACT_EVIDENCE_MIGRATIONS.migrate(original, from_version="1.3", to_version="1.4")
        self.assertEqual(migrated, {**before, "schema_version": "1.4"})
        self.assertEqual(original, before)
        state = ArtifactEvidenceStore(None)._decode(migrated)
        self.assertEqual(state.evidence[0].model_dump(mode="json"), before["evidence"][0])
        self.assertNotIn("observation_expires_at", state.evidence[0].metadata)

    def test_unchanged_prior_reader_contract_refuses14(self):
        from codex_web.compatibility import ContractSpec, ContractCompatibilityError
        prior = ContractSpec("artifact-evidence-state", "1.3", ("1.0", "1.1", "1.2", "1.3"))
        with self.assertRaises(ContractCompatibilityError):
            prior.require("1.4")


class WorkerMRVerificationTests(unittest.IsolatedAsyncioTestCase):
    _assignment = staticmethod(verification_fixtures.WorkerVerificationTests._assignment)
    _request = verification_fixtures.WorkerVerificationTests._request
    _stop_services = verification_fixtures.WorkerVerificationTests._stop_services
    verification_resources = verification_fixtures.WorkerVerificationTests.verification_resources

    def _publish_worker_authority(self, **kwargs):
        from codex_web.authority import AUTHORITY_ROLE_CATALOG_ID, AUTHORITY_ROLE_CATALOG_KIND
        from codex_web.definitions import DefinitionDraftCreate, DefinitionPublishRequest
        verification_fixtures.WorkerVerificationTests._publish_worker_authority(self, **kwargs)
        active = self.registry.resolve(definition_id=AUTHORITY_ROLE_CATALOG_ID, kind=AUTHORITY_ROLE_CATALOG_KIND)
        payload = active.payload
        role = next(x for x in payload["roles"] if x["id"] == "orchestration-worker")
        role["grants"].append(dict(id="mr-policy-read", capability="repository.pull-request.merge-requirements.read",
            level="read", project_ids=["project-a"]))
        draft = self.registry.create_draft(DefinitionDraftCreate(definition_id=AUTHORITY_ROLE_CATALOG_ID,
            kind=AUTHORITY_ROLE_CATALOG_KIND, definition_schema_version=active.definition_schema_version,
            payload=payload, actor="fixture"))
        self.registry.approve_publication(draft.record_id, actor="fixture-reviewer", reference="PRIVATE-APPROVAL", reason="Private read boundary")
        self.registry.publish(draft.record_id, DefinitionPublishRequest(actor="fixture", expected_active_revision=active.revision))

    def _publish_profile(self, payload, version, project=None):
        for profile in payload["profiles"]:
            if profile["id"] == "repository-write":
                op = "repository.read.merge-requirements"
                if op not in profile["control_plane_operations"]:
                    profile["control_plane_operations"].append(op)
        administrative_fixtures.AdministrativeBrokerRuntimeTests._publish_profile(self, payload, version, project)

    async def asyncSetUp(self):
        await verification_fixtures.WorkerVerificationTests.asyncSetUp(self)
        self.server = provider_fixtures._Provider()
        self.target = self.resources.update(self.target.id, ResourceUpdate(aliases=(
            ResourceAlias(namespace="github-repository", value="acme/widgets", provider="github"),)), actor=self.admin)
        self.secret = SecretBroker(SecretStateStore(self.sqlite), {
            "local": LocalFileSecretBackend(Path(self.temp.name) / "read-credentials")})
        credential = self.secret.create(SecretCreate(name="Synthetic provider read", value="fixture-token",
            provider="github", purpose="code-host", allowed_identity_ids=[self.worker_actor.identity_id]), actor=self.admin)
        registry = CodeHostRegistry()
        registry.register_provider(GitHubCodeHostProvider(GitHubClient(transport=httpx.MockTransport(self.server.response))))
        self.service.code_hosts = CodeHostService(registry, self.resources, secrets=self.secret, identity=self.identity)
        actions = self.intents.execution.registry
        actions.register(GitHubActionProvider(self.resources, provider_instance="github.example",
            api_base="https://api.github.example", web_base="https://github.example"))
        actions.bind(ActionProviderBindingCreate(provider_type="github", provider_instance="github.example",
            project_id="project-a", resource_ids=(self.target.id,), credential_ref=credential.id),
            actor=self.admin, resources=self.resources)
        self.intents.execution.secret_broker = self.secret
        def deadline(state):
            state.assignments[0].deadline_at = time.time() + 300
            return state
        self.workers.store.update_assignment(self.worker.id, self.assignment.id, deadline)
        self.request = {"method": METHOD, "repository_resource_id": self.target.id,
            "pull_request_number": 7, "expected_head_sha": self.server.head, "request_id": "a" * 32}
        self.evidence.governance = DataGovernanceService(DataGovernanceStore(self.sqlite))
        self.evidence.governance.store.load()
        self.initial = self.evidence.store.load()
        self.initial_governance = self.evidence.governance.store.load()

    async def post(self, payload=None):
        return await self._request("POST", "/api/verifications", payload=payload or self.request)

    async def test_actual_broker_provider_read_creates_three_attributed_governed_observations(self):
        status, _, body = await self.post()
        self.assertEqual(status, 200, body)
        self.assertEqual(set(body["evidence"]), {"independent_review", "exact_head_ci", "merge_policy_snapshot"})
        self.assertEqual(body["item"]["verifier_identity_id"], self.worker_actor.identity_id)
        # Same producer verifies its own observations: no invented independence.
        self.assertFalse(body["item"]["independent"])
        for proof in body["evidence"].values():
            self.assertEqual(proof["producer_identity_id"], self.worker_actor.identity_id)
            self.assertEqual(proof["resource_ids"], [self.target.id])
            self.assertIsNotNone(proof["governance_record_id"])
            self.assertEqual(proof["metadata"]["head_sha"], self.server.head)
            self.assertNotIn("fixture-token", json.dumps(proof))
        self.assertTrue(self.server.calls)
        self.assertTrue(all(request.method in {"GET", "POST"} for request in self.server.calls))
        # GitHub POSTs here are the fixed GraphQL read endpoint, never mutations.
        for request in self.server.calls:
            if request.method == "POST":
                self.assertNotIn("mutation", json.loads(request.content)["query"])
        count = len(self.server.calls)
        source = body["item"]["source"]
        status, _, found = await self._request("GET", "/api/verifications?method=" + METHOD + "&source=" + quote(source, safe=""))
        self.assertEqual(status, 200, found)
        self.assertEqual(found["outcome"], "unique")
        self.assertEqual(found["original"]["item"]["id"], body["item"]["id"])
        self.assertEqual(len(self.server.calls), count)

    async def test_caller_pass_or_identity_override_refuses_before_any_provider_read(self):
        for key, value in (("result", "verified"), ("producer_identity_id", "local-admin"), ("independent", True)):
            status, _, body = await self.post({**self.request, key: value})
            self.assertEqual(status, 403, body)
        self.assertEqual(self.server.calls, [])
        self.assertEqual(self.evidence.store.load(), self.initial)

    async def test_same_request_identity_cannot_publish_changed_content(self):
        status, _, original = await self.post()
        self.assertEqual(status, 200, original)
        before = self.evidence.store.load()
        governance = self.evidence.governance.store.load()
        reads = len(self.server.calls)
        status, _, duplicate = await self.post()
        self.assertEqual(status, 200, duplicate)
        self.assertEqual(duplicate["item"]["id"], original["item"]["id"])
        status, _, refused = await self.post({**self.request, "expected_head_sha": "c" * 40})
        self.assertEqual(status, 403, refused)
        self.assertEqual(len(self.server.calls), reads)
        self.assertEqual(self.evidence.store.load(), before)
        self.assertEqual(self.evidence.governance.store.load(), governance)

    async def test_wrong_repository_refuses_without_provider_or_proof(self):
        status, _, body = await self.post({**self.request, "repository_resource_id": "foreign"})
        self.assertEqual(status, 403, body)
        self.assertEqual(self.server.calls, [])
        self.assertEqual(self.evidence.store.load(), self.initial)

    async def test_changed_head_cannot_publish_proof(self):
        status, _, body = await self.post({**self.request, "expected_head_sha": "c" * 40})
        self.assertNotEqual(status, 200, body)
        self.assertEqual(self.evidence.store.load(), self.initial)

    async def test_late_profile_change_refuses_all_domain_and_governance_writes(self):
        from codex_web.agent_profiles import AgentProfileUpdate
        changed = False
        def hook(request, count):
            nonlocal changed
            if not changed:
                changed = True
                self.agent_profiles.update("developer", AgentProfileUpdate(
                    description="Canonical private profile revision changed during provider read"), actor=self.admin)
        self.server.hook = hook
        status, _, body = await self.post()
        # The existing credential-bound CodeHost boundary may classify its
        # interrupted read as a safe internal refusal; no proof can commit.
        self.assertNotEqual(status, 200, body)
        self.assertTrue(changed)
        self.assertEqual(self.evidence.store.load(), self.initial)
        self.assertEqual(self.evidence.governance.store.load(), self.initial_governance)

    async def test_second_governance_registration_failure_rolls_back_every_proof(self):
        original = self.evidence.governance.register_domain_record
        calls = 0
        def register(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("Controlled second governance registration failure")
            return original(*args, **kwargs)
        with patch.object(self.evidence.governance, "register_domain_record", side_effect=register):
            status, _, body = await self.post()
        self.assertNotEqual(status, 200, body)
        self.assertEqual(calls, 2)
        self.assertEqual(self.evidence.store.load(), self.initial)
        self.assertEqual(self.evidence.governance.store.load(), self.initial_governance)

    async def test_observation_freshness_expiry_does_not_expire_or_redact_original_records(self):
        status, _, body = await self.post()
        self.assertEqual(status, 200, body)
        proofs = body["evidence"]
        self.assertTrue(all(x["retention_expires_at"] is None for x in proofs.values()))
        expiry = max(x["metadata"]["observation_expires_at"] for x in proofs.values())
        self.evidence.expire_retention(now=expiry + 1)
        for original in proofs.values():
            record = self.evidence.get_evidence(original["id"], actor=self.admin)
            self.assertEqual(record.model_dump(mode="json"), original)
        before = len(self.server.calls)
        with patch.object(self.service, "_clock", return_value=expiry + 1):
            status, _, found = await self._request("GET", "/api/verifications?method=" + METHOD
                + "&source=" + quote(body["item"]["source"], safe=""))
        self.assertEqual(status, 200, found)
        self.assertFalse(found["original"]["observation_fresh"])
        self.assertEqual(len(self.server.calls), before)
        from codex_web.services.procedure_execution import ProcedureExecutionService, ProcedureExecutionError
        from codex_web.artifact_evidence import EvidenceRequirement
        ci = proofs["exact_head_ci"]
        self.assertEqual(ci["result"], "pass")
        current = SimpleNamespace(evidence=self.evidence, clock=lambda: ci["observed_at"] + 1,
            mr_observation_configuration=None)
        ProcedureExecutionService._evidence(current, ci["id"], self.admin, self.assignment)
        current.clock = lambda: expiry + 1
        with self.assertRaisesRegex(ProcedureExecutionError, "procedure_evidence_invalid"):
            ProcedureExecutionService._evidence(current, ci["id"], self.admin, self.assignment)
        requirement = EvidenceRequirement(id="current-ci", evidence_type="ci_check")
        self.assertTrue(self.evidence.evaluate(self.ref, [requirement], actor=self.admin,
            now=ci["observed_at"] + 1).satisfied)
        self.assertFalse(self.evidence.evaluate(self.ref, [requirement], actor=self.admin, now=expiry + 1).satisfied)

    async def test_governance_denial_blocks_original_lookup_without_new_provider_reads(self):
        status, _, body = await self.post()
        self.assertEqual(status, 200, body)
        ids = {p["governance_record_id"] for p in body["evidence"].values()}
        def deny(state):
            state.records = [r.model_copy(update={"deny_model_context": True}) if r.id in ids else r
                             for r in state.records]
            return state
        self.evidence.governance.store.update(deny)
        before = len(self.server.calls)
        status, _, found = await self._request("GET", "/api/verifications?method=" + METHOD
            + "&source=" + quote(body["item"]["source"], safe=""))
        self.assertEqual(status, 403, found)
        self.assertEqual(len(self.server.calls), before)
