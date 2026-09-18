from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from codex_web.api.action_providers import build_action_providers_router
from codex_web.action_providers import (
    ActionCapability,
    ActionDefinition,
    ActionProviderBinding,
    ActionProviderBindingCreate,
    ActionRequest,
    ActionResult,
    ActionRiskClass,
    ActionVerification,
    UnsupportedActionCapabilityError,
)
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
    TenantScope,
)
from codex_web.resources import ResourceCreate, ResourceType
from codex_web.secret_backends import LocalFileSecretBackend
from codex_web.secrets import SecretCreate
from codex_web.services.action_provider_conformance import ActionProviderConformanceSuite
from codex_web.services.action_providers import (
    ActionExecutionService,
    ActionProviderRegistry,
    ActionResolutionError,
)
from codex_web.services.identity import IdentityService
from codex_web.services.reference_action_provider import ReferenceActionProvider
from codex_web.services.resources import ResourceCatalogService
from codex_web.services.secrets import SecretBroker
from codex_web.storage.action_providers import ActionProviderStateStore
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.resource_catalog import ResourceCatalogStore
from codex_web.storage.secret_state import SecretStateStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class _CredentialProvider:
    contract_version = "1.0"
    provider_type = "credential-reference"
    provider_instance = "test"
    seen_credential: str | None = None

    def actions(self):
        return (
            ActionDefinition(
                action_id="credential.call",
                title="Credential call",
                capabilities=ActionCapability(
                    prepare=True,
                    execute=True,
                    evidence=True,
                ),
                risk_class=ActionRiskClass.MEDIUM,
                required_resource_types=(ResourceType.OTHER,),
                required_authority=("action.credential.call",),
                credential_required=True,
                credential_purpose="test-api-token",
                expected_evidence=("provider-call",),
            ),
        )

    async def prepare(self, request, *, binding):
        return {"ready": True}

    async def execute(self, request, *, binding, credential=None):
        self.seen_credential = credential
        now = time.time()
        return ActionResult(
            provider_binding_id=binding.id,
            action_id=request.action_id,
            status="succeeded",
            started_at=now,
            completed_at=now,
            output={"credential_seen": credential is not None},
        )

    async def verify(self, result, *, binding):
        return ActionVerification(verified=True)

    async def rollback(self, result, *, binding, credential=None):
        raise AssertionError("rollback is not supported")


class _NoDryRunProvider:
    contract_version = "1.0"
    provider_type = "no-dry-run"
    provider_instance = "test"

    def actions(self):
        return (
            ActionDefinition(
                action_id="mutate",
                title="Mutate",
                capabilities=ActionCapability(prepare=True, execute=True, dry_run=False),
                required_resource_types=(ResourceType.OTHER,),
            ),
        )

    async def prepare(self, request, *, binding):
        return {}

    async def execute(self, request, *, binding, credential=None):
        now = time.time()
        return ActionResult(
            provider_binding_id=binding.id,
            action_id=request.action_id,
            status="succeeded",
            started_at=now,
            completed_at=now,
        )

    async def verify(self, result, *, binding):
        raise AssertionError

    async def rollback(self, result, *, binding, credential=None):
        raise AssertionError


class ActionProviderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.sqlite = SQLiteStateStore(root / "state.sqlite3")
        self.identity = IdentityService(IdentityStateStore(self.sqlite))
        self.identity.bootstrap_local()
        self.actor = self.identity.local_trusted_actor()
        self.resources = ResourceCatalogService(ResourceCatalogStore(self.sqlite))
        self.secret_broker = SecretBroker(
            SecretStateStore(self.sqlite),
            {"local": LocalFileSecretBackend(root / "secrets")},
        )
        self.registry = ActionProviderRegistry(ActionProviderStateStore(self.sqlite))
        self.reference = ReferenceActionProvider()
        self.registry.register(self.reference)
        self.execution = ActionExecutionService(
            self.registry,
            self.resources,
            secret_broker=self.secret_broker,
        )
        self.resource = self.resources.create(
            ResourceCreate(resource_type=ResourceType.OTHER, name="Reference target"),
            actor=self.actor,
        )

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    def _bind(self, provider_type="reference", provider_instance="local-reference", **overrides):
        payload = {
            "provider_type": provider_type,
            "provider_instance": provider_instance,
            "resource_ids": (self.resource.id,),
        }
        payload.update(overrides)
        return self.registry.bind(
            ActionProviderBindingCreate(**payload),
            actor=self.actor,
            resources=self.resources,
        )

    async def test_reference_provider_passes_shared_conformance_cycle(self) -> None:
        binding = self._bind()
        request = ActionRequest(
            action_id="reference.set",
            organization_id="local",
            workspace_id="default",
            resource_ids=(self.resource.id,),
            parameters={"key": "feature", "value": "on"},
            idempotency_key="idempotent-1",
        )
        result, verification, rollback = await ActionProviderConformanceSuite().exercise(
            self.reference,
            binding=binding,
            request=request,
        )
        self.assertEqual(result.status, "succeeded")
        self.assertTrue(verification and verification.verified)
        self.assertEqual(rollback.status, "rolled_back")
        self.assertNotIn("feature", self.reference.values)

    async def test_prepare_exposes_machine_readable_risk_authority_and_evidence(self) -> None:
        binding = self._bind()
        request = ActionRequest(
            action_id="reference.set",
            organization_id="local",
            workspace_id="default",
            project_id=None,
            resource_ids=(self.resource.id,),
            parameters={"key": "feature", "value": "on"},
            dry_run=True,
        )
        prepared = await self.execution.prepare(binding.id, request, actor=self.actor)
        self.assertEqual(prepared.action.risk_class, ActionRiskClass.LOW)
        self.assertIn("action.reference.set", prepared.action.required_authority)
        self.assertIn("reference-state", prepared.action.expected_evidence)
        self.assertTrue(prepared.action.capabilities.rollback)
        self.assertTrue(prepared.action.capabilities.verification)
        self.assertTrue(prepared.action.capabilities.idempotency)

    async def test_unsupported_capability_fails_explicitly(self) -> None:
        provider = _NoDryRunProvider()
        self.registry.register(provider)
        binding = self._bind(provider_type=provider.provider_type, provider_instance=provider.provider_instance)
        request = ActionRequest(
            action_id="mutate",
            organization_id="local",
            workspace_id="default",
            resource_ids=(self.resource.id,),
            parameters={},
            dry_run=True,
        )
        with self.assertRaises(UnsupportedActionCapabilityError):
            await self.execution.prepare(binding.id, request, actor=self.actor)

    async def test_resource_scoped_binding_requires_explicit_in_scope_target(self) -> None:
        binding = self._bind()
        missing_target = ActionRequest(
            action_id="reference.set",
            organization_id="local",
            workspace_id="default",
            parameters={"key": "x", "value": 1},
        )
        with self.assertRaises(ActionResolutionError):
            await self.execution.prepare(binding.id, missing_target, actor=self.actor)

        other = self.resources.create(
            ResourceCreate(resource_type=ResourceType.OTHER, name="Other"),
            actor=self.actor,
        )
        outside = missing_target.model_copy(update={"resource_ids": (other.id,)})
        with self.assertRaises(ActionResolutionError):
            await self.execution.prepare(binding.id, outside, actor=self.actor)

    async def test_credential_reference_is_resolved_only_at_execution_boundary(self) -> None:
        provider = _CredentialProvider()
        self.registry.register(provider)
        secret = self.secret_broker.create(
            SecretCreate(name="provider token", value="raw-provider-secret"),
            actor=self.actor,
        )
        binding = self._bind(
            provider_type=provider.provider_type,
            provider_instance=provider.provider_instance,
            credential_ref=secret.id,
        )
        request = ActionRequest(
            action_id="credential.call",
            organization_id="local",
            workspace_id="default",
            resource_ids=(self.resource.id,),
            parameters={"operation": "test"},
        )
        serialized = request.model_dump_json()
        self.assertNotIn("raw-provider-secret", serialized)

        result = await self.execution.execute(binding.id, request, actor=self.actor)
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(provider.seen_credential, "raw-provider-secret")
        self.assertNotIn("raw-provider-secret", result.model_dump_json())

    async def test_request_cannot_override_binding_credential(self) -> None:
        provider = _CredentialProvider()
        self.registry.register(provider)
        first = self.secret_broker.create(
            SecretCreate(name="first", value="one"),
            actor=self.actor,
        )
        second = self.secret_broker.create(
            SecretCreate(name="second", value="two"),
            actor=self.actor,
        )
        binding = self._bind(
            provider_type=provider.provider_type,
            provider_instance=provider.provider_instance,
            credential_ref=first.id,
        )
        request = ActionRequest(
            action_id="credential.call",
            organization_id="local",
            workspace_id="default",
            resource_ids=(self.resource.id,),
            credential_ref=second.id,
        )
        with self.assertRaises(ActionResolutionError):
            await self.execution.prepare(binding.id, request, actor=self.actor)

    async def test_cross_tenant_request_is_denied_before_provider_resolution(self) -> None:
        binding = self._bind()
        request = ActionRequest(
            action_id="reference.set",
            organization_id="other",
            workspace_id="other",
            resource_ids=(self.resource.id,),
            parameters={"key": "x", "value": 1},
        )
        with self.assertRaises(Exception):
            await self.execution.prepare(binding.id, request, actor=self.actor)


class ActionProviderApiAssuranceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.sqlite = SQLiteStateStore(root / "state.sqlite3")
        identity = IdentityService(IdentityStateStore(self.sqlite))
        identity.bootstrap_local()
        bootstrap_actor = identity.local_trusted_actor()
        self.resources = ResourceCatalogService(ResourceCatalogStore(self.sqlite))
        self.resource = self.resources.create(
            ResourceCreate(resource_type=ResourceType.OTHER, name="API target"),
            actor=bootstrap_actor,
        )
        self.registry = ActionProviderRegistry(ActionProviderStateStore(self.sqlite))
        self.registry.register(ReferenceActionProvider())
        self.execution = ActionExecutionService(self.registry, self.resources)
        self.actor = AuthenticationActor(
            identity_id="admin",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="local",
            workspace_id="default",
            roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.PRIMARY,
        )
        app = FastAPI()

        @app.middleware("http")
        async def inject_actor(request: Request, call_next):
            request.state.identity_actor = self.actor
            return await call_next(request)

        app.include_router(build_action_providers_router(self.registry, self.execution))
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        self.temp.cleanup()

    def _binding_payload(self):
        return {
            "provider_type": "reference",
            "provider_instance": "local-reference",
            "resource_ids": [self.resource.id],
        }

    def test_low_assurance_human_can_read_catalog_but_cannot_create_binding(self) -> None:
        self.assertEqual(self.client.get("/api/action-providers").status_code, 200)

        response = self.client.post(
            "/api/action-providers/bindings",
            json=self._binding_payload(),
        )

        self.assertEqual(response.status_code, 403)
        self.assertIn("mfa", response.json()["detail"].lower())

    def test_mfa_human_can_create_binding(self) -> None:
        self.actor = self.actor.model_copy(
            update={"assurance": AuthenticationAssurance.MFA}
        )

        response = self.client.post(
            "/api/action-providers/bindings",
            json=self._binding_payload(),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["item"]["resource_ids"], [self.resource.id])

    def test_action_provider_admin_service_scope_remains_supported(self) -> None:
        self.actor = AuthenticationActor(
            identity_id="action-provider-admin",
            principal_kind=PrincipalKind.SERVICE,
            organization_id="local",
            workspace_id="default",
            assurance=AuthenticationAssurance.SERVICE_TOKEN,
            service_scopes=("action-provider:admin",),
        )

        response = self.client.post(
            "/api/action-providers/bindings",
            json=self._binding_payload(),
        )

        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
