from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from codex_web.api.model_gateway import build_model_gateway_router
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.model_gateway import (
    MODEL_CLASS_STRATEGIC,
    MODEL_GATEWAY_CONTRACT,
    ModelDefinitionUpsert,
    ModelInvocationRequest,
    ModelMessage,
    ModelProviderResult,
    ModelProviderUpsert,
    ModelProviderUsage,
    PromptTemplateUpsert,
    TenantModelPolicyUpdate,
)
from codex_web.input_plugins import (
    InputContextBlock,
    InputPatch,
    InputPhase,
    InputPluginPipeline,
    InputPluginRegistration,
)
from codex_web.model_providers import (
    ModelProviderAdapter,
    ModelProviderTransientError,
    OpenAIModelProviderAdapter,
)
from codex_web.secret_backends import LocalFileSecretBackend
from codex_web.secrets import SecretCreate
from codex_web.services.identity import IdentityService
from codex_web.services.model_gateway import (
    ModelGatewayService,
    ModelProviderUnavailableError,
    ModelRegistryConflictError,
    ModelRoutingError,
)
from codex_web.services.secrets import SecretBroker
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.model_gateway import ModelGatewayStore
from codex_web.storage.secret_state import SecretStateStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class _FakeAdapter:
    adapter_type = "fake"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.requests: list[ModelInvocationRequest] = []
        self.transient_models: set[str] = set()

    async def invoke(self, provider, model, request, *, credential):
        self.calls.append((model.id, credential))
        self.requests.append(request)
        if model.id in self.transient_models:
            raise ModelProviderTransientError("temporary provider outage")
        return ModelProviderResult(
            text=f"reply:{model.id}",
            usage=ModelProviderUsage(input_tokens=100, output_tokens=20),
            provider_request_id=f"request:{model.id}",
        )


class _GatewayInputPlugin:
    id = "builtin.gateway-test"
    version = "1.0.0"
    transport = "builtin"

    async def transform(self, envelope, context):
        del context
        return InputPatch(
            plugin_id=self.id,
            plugin_version=self.version,
            phase=InputPhase.COMPOSE,
            changes={
                "system_prompt": "PLUGIN COMPOSED SYSTEM",
                "context_blocks": (
                    InputContextBlock(
                        id="plugin-context",
                        source="test-plugin",
                        content="PLUGIN CONTEXT BODY",
                    ),
                ),
                "output_contract": {"format": "concise-json"},
                "max_output_tokens": 500,
            },
        )


class ModelGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.sqlite = SQLiteStateStore(root / "state.sqlite3")
        identity = IdentityService(IdentityStateStore(self.sqlite))
        identity.bootstrap_local()
        self.actor = identity.local_trusted_actor()
        self.secret_broker = SecretBroker(
            SecretStateStore(self.sqlite),
            {"local": LocalFileSecretBackend(root / "secrets")},
        )
        self.service = ModelGatewayService(
            ModelGatewayStore(self.sqlite),
            secret_broker=self.secret_broker,
        )
        self.adapter = _FakeAdapter()
        self.service.register_adapter(self.adapter)
        self.service.upsert_template(
            PromptTemplateUpsert(
                template_id="executive.system",
                version="1.0",
                content="{{ instructions }}",
            ),
            actor=self.actor,
        )

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    def _provider(self, provider_id: str = "p1", **overrides):
        payload = {
            "id": provider_id,
            "adapter_type": "fake",
            "display_name": provider_id,
            "credential_required": False,
            "residency_tags": ("eu",),
            "compliance_tags": ("gdpr",),
        }
        payload.update(overrides)
        return self.service.upsert_provider(
            ModelProviderUpsert(**payload),
            actor=self.actor,
        )

    def _model(self, model_id: str, provider_id: str = "p1", **overrides):
        payload = {
            "id": model_id,
            "provider_id": provider_id,
            "concrete_model": f"concrete-{model_id}",
            "model_version": "2026-09",
            "model_classes": (MODEL_CLASS_STRATEGIC,),
            "capabilities": ("text", "reasoning"),
            "context_window_tokens": 32000,
            "max_output_tokens": 4096,
            "residency_tags": ("eu",),
            "compliance_tags": ("gdpr",),
            "input_price_per_million_usd": 1.0,
            "output_price_per_million_usd": 2.0,
        }
        payload.update(overrides)
        return self.service.upsert_model(
            ModelDefinitionUpsert(**payload),
            actor=self.actor,
        )

    @staticmethod
    def _request(**overrides):
        payload = {
            "model_class": MODEL_CLASS_STRATEGIC,
            "messages": (ModelMessage(role="user", content="What should we do?"),),
            "system_prompt": "Sensitive system context",
            "prompt_template_id": "executive.system",
            "prompt_template_version": "1.0",
            "required_capabilities": ("text", "reasoning"),
            "required_residency_tags": ("eu",),
            "required_compliance_tags": ("gdpr",),
            "max_output_tokens": 1000,
            "purpose": "executive-advice",
        }
        payload.update(overrides)
        return ModelInvocationRequest(**payload)

    async def test_model_gateway_v1_state_migrates_with_empty_input_plugin_provenance(self) -> None:
        migrated = self.service.store._decode(
            {
                "schema_version": "1.0",
                "providers": [],
                "models": [],
                "prompt_templates": [],
                "policies": [],
                "invocations": [],
            }
        )

        self.assertEqual(migrated.schema_version, MODEL_GATEWAY_CONTRACT.current)
        self.assertEqual(MODEL_GATEWAY_CONTRACT.current, "1.1")
        self.assertIn("1.0", MODEL_GATEWAY_CONTRACT.supported)

    async def test_routing_is_deterministic_by_class_policy_health_and_priority(self) -> None:
        self._provider("p1")
        self._provider("p2")
        self._model("slow", "p1", route_priority=50)
        self._model("fast", "p2", route_priority=10)

        route = self.service.route(self._request(), actor=self.actor)

        self.assertEqual([item.model_id for item in route.candidates], ["fast", "slow"])
        self.assertEqual(route.policy_max_attempts, 2)
        self.assertEqual(route.prompt_template_version, "1.0")

    async def test_residency_and_allowlist_fail_before_adapter_invocation(self) -> None:
        self._provider("p1")
        self._model("m1")
        self.service.set_policy(
            TenantModelPolicyUpdate(
                allowed_provider_ids=("p1",),
                required_residency_tags=("se",),
            ),
            actor=self.actor,
        )

        with self.assertRaises(ModelRoutingError):
            self.service.route(self._request(), actor=self.actor)

        self.assertEqual(self.adapter.calls, [])

    async def test_budget_requires_known_pricing_and_rejects_over_budget(self) -> None:
        self._provider("p1")
        self._model(
            "expensive",
            input_price_per_million_usd=1000,
            output_price_per_million_usd=1000,
        )

        with self.assertRaises(ModelRoutingError):
            self.service.route(
                self._request(max_cost_usd=0.0001),
                actor=self.actor,
            )

    async def test_prompt_template_revision_is_immutable_and_policy_is_pinned(self) -> None:
        self._provider("p1")
        self._model("m1")
        first_route = self.service.route(self._request(), actor=self.actor)

        with self.assertRaises(ModelRegistryConflictError):
            self.service.upsert_template(
                PromptTemplateUpsert(
                    template_id="executive.system",
                    version="1.0",
                    content="changed content for same version",
                ),
                actor=self.actor,
            )

        self.service.set_policy(
            TenantModelPolicyUpdate(
                allowed_provider_ids=("p1",),
                max_attempts=1,
            ),
            actor=self.actor,
        )
        second_route = self.service.route(self._request(), actor=self.actor)

        self.assertNotEqual(
            first_route.policy_fingerprint_sha256,
            second_route.policy_fingerprint_sha256,
        )
        self.assertEqual(len(second_route.policy_fingerprint_sha256), 64)

    async def test_transient_failure_falls_back_with_bounded_attempts(self) -> None:
        self._provider("p1")
        self._provider("p2")
        self._model("first", "p1", route_priority=1)
        self._model("second", "p2", route_priority=2)
        self.adapter.transient_models.add("first")

        result = await self.service.invoke(self._request(), actor=self.actor)

        self.assertEqual(result.text, "reply:second")
        self.assertEqual([item[0] for item in self.adapter.calls], ["first", "second"])
        self.assertEqual(len(result.invocation.attempts), 2)
        self.assertEqual(result.invocation.attempts[0].outcome, "transient_failure")
        self.assertEqual(result.invocation.attempts[1].outcome, "success")
        self.assertEqual(result.invocation.selected_model_id, "second")

    async def test_policy_bounds_fallback_attempts(self) -> None:
        self._provider("p1")
        self._provider("p2")
        self._model("first", "p1", route_priority=1)
        self._model("second", "p2", route_priority=2)
        self.adapter.transient_models.add("first")
        self.service.set_policy(
            TenantModelPolicyUpdate(max_attempts=1),
            actor=self.actor,
        )

        with self.assertRaises(ModelProviderUnavailableError):
            await self.service.invoke(self._request(), actor=self.actor)

        self.assertEqual([item[0] for item in self.adapter.calls], ["first"])
        rows = self.service.invocations(self.actor)
        self.assertEqual(rows[0].status, "failed")
        self.assertEqual(len(rows[0].attempts), 1)

    async def test_input_budget_is_enforced_after_plugin_composition(self) -> None:
        self._provider("p1")
        self._model("m1")
        self.service.input_pipeline = InputPluginPipeline(
            [
                InputPluginRegistration(
                    plugin=_GatewayInputPlugin(),
                    phase=InputPhase.COMPOSE,
                )
            ]
        )

        with self.assertRaisesRegex(
            ModelRoutingError,
            "input token budget exceeded",
        ):
            await self.service.invoke(
                self._request(max_input_tokens=20),
                actor=self.actor,
            )

        self.assertEqual(self.adapter.calls, [])

    async def test_goal_usage_aggregates_gateway_attempt_usage(self) -> None:
        self._provider("p1")
        self._model("m1")

        for _ in range(2):
            await self.service.invoke(
                self._request(goal_id="goal-budget-test"),
                actor=self.actor,
            )

        usage = self.service.goal_usage(
            "goal-budget-test",
            actor=self.actor,
        )
        self.assertEqual(usage.calls, 2)
        self.assertEqual(usage.input_tokens, 200)
        self.assertEqual(usage.output_tokens, 40)
        self.assertGreater(usage.cost_usd, 0.0)
        self.assertEqual(
            self.service.goal_usage("other-goal", actor=self.actor).calls,
            0,
        )

    async def test_secret_reference_is_resolved_only_at_provider_boundary(self) -> None:
        secret = self.secret_broker.create(
            SecretCreate(
                name="model provider key",
                value="super-secret-provider-key",
                provider="fake",
                purpose="model invocation",
            ),
            actor=self.actor,
        )
        self._provider(
            "p1",
            credential_ref=secret.id,
            credential_required=True,
        )
        self._model("m1")

        result = await self.service.invoke(self._request(), actor=self.actor)

        self.assertEqual(result.text, "reply:m1")
        self.assertEqual(self.adapter.calls[-1], ("m1", "super-secret-provider-key"))
        registry_json = self.service.store.load().model_dump_json()
        self.assertNotIn("super-secret-provider-key", registry_json)
        self.assertIn(secret.id, registry_json)

    async def test_input_pipeline_composes_before_policy_routing_and_persists_hash_only_provenance(self) -> None:
        self._provider("p1")
        self._model("m1")
        self.service.input_pipeline = InputPluginPipeline(
            [
                InputPluginRegistration(
                    plugin=_GatewayInputPlugin(),
                    phase=InputPhase.COMPOSE,
                )
            ],
            gated_validator=lambda field, value, current, context: (
                field == "max_output_tokens"
                and int(value) <= current.max_output_tokens
            ),
        )

        result = await self.service.invoke(
            self._request(
                system_prompt="ORIGINAL SYSTEM",
                max_output_tokens=1000,
            ),
            actor=self.actor,
        )

        provider_request = self.adapter.requests[-1]
        self.assertIn("PLUGIN COMPOSED SYSTEM", provider_request.system_prompt)
        self.assertIn("PLUGIN CONTEXT BODY", provider_request.system_prompt)
        self.assertIn('zone="TOOL_OUTPUT"', provider_request.system_prompt)
        self.assertIn("concise-json", provider_request.system_prompt)
        self.assertEqual(provider_request.max_output_tokens, 500)

        record = result.invocation
        self.assertEqual(len(record.input_plugin_provenance), 1)
        self.assertEqual(
            record.input_plugin_provenance[0].plugin_id,
            "builtin.gateway-test",
        )
        self.assertEqual(
            record.input_plugin_provenance[0].applied_fields,
            (
                "context_blocks",
                "max_output_tokens",
                "output_contract",
                "system_prompt",
            ),
        )
        self.assertEqual(len(record.input_gated_proposals), 1)
        self.assertEqual(
            record.input_gated_proposals[0].field,
            "max_output_tokens",
        )

        durable = self.service.store.load().model_dump_json()
        self.assertNotIn("PLUGIN COMPOSED SYSTEM", durable)
        self.assertNotIn("PLUGIN CONTEXT BODY", durable)
        self.assertNotIn("concise-json", durable)
        self.assertIn("builtin.gateway-test", durable)

    async def test_input_pipeline_cannot_expand_gateway_budget_without_core_acceptance(self) -> None:
        class _BudgetExpansionPlugin:
            id = "builtin.budget-expansion"
            version = "1.0.0"
            transport = "builtin"

            async def transform(self, envelope, context):
                del context
                return InputPatch(
                    plugin_id=self.id,
                    plugin_version=self.version,
                    phase=InputPhase.OPTIMIZE,
                    changes={
                        "max_output_tokens": envelope.max_output_tokens * 10,
                        "max_cost_usd": 1000.0,
                    },
                )

        self._provider("p1")
        self._model("m1")
        self.service.input_pipeline = InputPluginPipeline(
            [
                InputPluginRegistration(
                    plugin=_BudgetExpansionPlugin(),
                    phase=InputPhase.OPTIMIZE,
                )
            ]
        )

        result = await self.service.invoke(
            self._request(max_output_tokens=1000, max_cost_usd=1.0),
            actor=self.actor,
        )

        provider_request = self.adapter.requests[-1]
        self.assertEqual(provider_request.max_output_tokens, 1000)
        self.assertEqual(provider_request.max_cost_usd, 1.0)
        self.assertEqual(
            result.invocation.input_plugin_provenance[0].rejected_fields,
            ("max_cost_usd", "max_output_tokens"),
        )

    async def test_invocation_ledger_contains_hashes_not_prompt_or_output_content(self) -> None:
        self._provider("p1")
        self._model("m1")
        request = self._request(
            system_prompt="TOP SECRET SYSTEM TEXT",
            messages=(ModelMessage(role="user", content="SENSITIVE USER MESSAGE"),),
        )

        result = await self.service.invoke(request, actor=self.actor)
        serialized = result.invocation.model_dump_json()

        self.assertNotIn("TOP SECRET SYSTEM TEXT", serialized)
        self.assertNotIn("SENSITIVE USER MESSAGE", serialized)
        self.assertNotIn("reply:m1", serialized)
        self.assertEqual(len(result.invocation.rendered_prompt_sha256), 64)
        self.assertEqual(result.invocation.prompt_template_version, "1.0")
        self.assertEqual(result.invocation.selected_concrete_model, "concrete-m1")

    async def test_cross_tenant_actor_cannot_resolve_registry_entries(self) -> None:
        self._provider("p1")
        self._model("m1")
        other: AuthenticationActor = self.actor.model_copy(
            update={
                "organization_id": "other",
                "workspace_id": "other",
            }
        )

        with self.assertRaises(ModelRoutingError):
            self.service.route(self._request(), actor=other)

    async def test_reference_openai_adapter_satisfies_provider_protocol_and_classifies_429(self) -> None:
        adapter = OpenAIModelProviderAdapter()
        self.assertIsInstance(adapter, ModelProviderAdapter)

        class _RateLimit(Exception):
            status_code = 429

        classified = adapter._classify(_RateLimit("too many requests"))
        self.assertIsInstance(classified, ModelProviderTransientError)


class ModelGatewayApiAssuranceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.service = ModelGatewayService(ModelGatewayStore(SQLiteStateStore(root / "state.sqlite3")))
        self.actor = AuthenticationActor(
            identity_id="admin",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="org-a",
            workspace_id="ws-a",
            roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.PRIMARY,
        )
        app = FastAPI()

        @app.middleware("http")
        async def inject_actor(request: Request, call_next):
            request.state.identity_actor = self.actor
            return await call_next(request)

        app.include_router(build_model_gateway_router(self.service))
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        self.temp.cleanup()

    def test_low_assurance_human_can_read_and_route_but_cannot_mutate_registry(self) -> None:
        self.assertEqual(self.client.get("/api/model-gateway/providers").status_code, 200)

        provider = self.client.put(
            "/api/model-gateway/providers/p1",
            json={
                "id": "p1",
                "adapter_type": "fake",
                "display_name": "Provider",
                "credential_required": False,
            },
        )
        policy = self.client.put(
            "/api/model-gateway/policy",
            json={"max_attempts": 1},
        )

        self.assertEqual(provider.status_code, 403)
        self.assertEqual(policy.status_code, 403)
        self.assertIn("mfa", provider.json()["detail"].lower())

    def test_mfa_human_can_mutate_provider_model_prompt_and_policy(self) -> None:
        self.actor = self.actor.model_copy(
            update={"assurance": AuthenticationAssurance.MFA}
        )
        provider = self.client.put(
            "/api/model-gateway/providers/p1",
            json={
                "id": "p1",
                "adapter_type": "fake",
                "display_name": "Provider",
                "credential_required": False,
                "residency_tags": ["eu"],
                "compliance_tags": ["gdpr"],
            },
        )
        self.assertEqual(provider.status_code, 200)

        model = self.client.put(
            "/api/model-gateway/models/m1",
            json={
                "id": "m1",
                "provider_id": "p1",
                "concrete_model": "concrete-m1",
                "model_classes": ["primary-coding"],
                "capabilities": ["text"],
                "modalities": ["text"],
            },
        )
        prompt = self.client.put(
            "/api/model-gateway/prompts/generic.system/1.0",
            json={
                "template_id": "generic.system",
                "version": "1.0",
                "content": "{{ instructions }}",
                "active": True,
            },
        )
        policy = self.client.put(
            "/api/model-gateway/policy",
            json={
                "allowed_provider_ids": ["p1"],
                "allowed_model_ids": ["m1"],
                "max_attempts": 1,
            },
        )

        self.assertEqual(model.status_code, 200)
        self.assertEqual(prompt.status_code, 200)
        self.assertEqual(policy.status_code, 200)

    def test_model_gateway_admin_service_scope_remains_supported(self) -> None:
        self.actor = AuthenticationActor(
            identity_id="model-admin-service",
            principal_kind=PrincipalKind.SERVICE,
            organization_id="org-a",
            workspace_id="ws-a",
            assurance=AuthenticationAssurance.SERVICE_TOKEN,
            service_scopes=("model-gateway:admin",),
        )

        response = self.client.put(
            "/api/model-gateway/providers/p1",
            json={
                "id": "p1",
                "adapter_type": "fake",
                "display_name": "Provider",
                "credential_required": False,
            },
        )

        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
