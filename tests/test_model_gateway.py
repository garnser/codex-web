from __future__ import annotations

import tempfile
import types
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
    ModelCatalogEntry,
    ModelDefinitionUpsert,
    ModelInvocationRequest,
    ModelLatencyClass,
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
    ModelProviderCapacityError,
    ModelProviderTransientError,
    OpenAIModelProviderAdapter,
)
from codex_web.provider_capacity import (
    ProviderCapacityReport,
    ProviderCapacityStatus,
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
from codex_web.services.provider_capacity import ProviderCapacityService
from codex_web.services.secrets import SecretBroker
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.model_gateway import ModelGatewayStore
from codex_web.storage.provider_capacity import ProviderCapacityStore
from codex_web.storage.secret_state import SecretStateStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class _FakeAdapter:
    adapter_type = "fake"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.requests: list[ModelInvocationRequest] = []
        self.transient_models: set[str] = set()
        self.capacity_models: set[str] = set()
        self.catalogs: dict[str, tuple[ModelCatalogEntry, ...]] = {}
        self.catalog_error: set[str] = set()

    async def discover_models(self, provider, *, credential):
        if provider.id in self.catalog_error:
            raise ModelProviderTransientError("catalog temporarily unavailable")
        return self.catalogs.get(provider.id, ())

    async def invoke(self, provider, model, request, *, credential):
        self.calls.append((model.id, credential))
        self.requests.append(request)
        if model.id in self.capacity_models:
            raise ModelProviderCapacityError(
                "usage limit reached: quota exhausted",
                status=ProviderCapacityStatus.DEPLETED,
                retry_at=1_900_000_120.0,
            )
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
        self.capacity = ProviderCapacityService(
            ProviderCapacityStore(self.sqlite),
            clock=lambda: 1_900_000_000.0,
        )
        self.now = 1_900_000_000.0
        self.service = ModelGatewayService(
            ModelGatewayStore(self.sqlite),
            secret_broker=self.secret_broker,
            provider_capacity=self.capacity,
            clock=lambda: self.now,
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
        self.assertEqual(MODEL_GATEWAY_CONTRACT.current, "1.4")
        self.assertIn("1.0", MODEL_GATEWAY_CONTRACT.supported)
        self.assertIn("1.1", MODEL_GATEWAY_CONTRACT.supported)
        self.assertIn("1.2", MODEL_GATEWAY_CONTRACT.supported)

    async def test_discovered_catalog_fences_routing_and_records_upstream_revision(self) -> None:
        self._provider("aggregate", catalog_discovery_enabled=True, catalog_ttl_seconds=60)
        self.adapter.catalogs["aggregate"] = (
            ModelCatalogEntry(
                concrete_model="openai/gpt-coding",
                upstream_provider_id="openai",
                upstream_model_id="gpt-coding",
                model_version="2026-10",
            ),
        )
        catalog = await self.service.refresh_catalog("aggregate", actor=self.actor)
        self._model(
            "aggregate-coding", "aggregate",
            concrete_model="openai/gpt-coding",
            availability_source="discovered",
        )

        route = self.service.route(self._request(), actor=self.actor)
        candidate = route.candidates[0]
        self.assertEqual(candidate.upstream_provider_id, "openai")
        self.assertEqual(candidate.upstream_model_id, "gpt-coding")
        self.assertEqual(candidate.catalog_revision, catalog.revision)
        result = await self.service.invoke(self._request(), actor=self.actor)
        self.assertEqual(result.invocation.selected_catalog_revision, catalog.revision)
        self.assertEqual(result.invocation.selected_upstream_provider_id, "openai")

        self.adapter.catalogs["aggregate"] = (
            ModelCatalogEntry(concrete_model="anthropic/claude"),
        )
        await self.service.refresh_catalog("aggregate", actor=self.actor)
        with self.assertRaisesRegex(ModelRoutingError, "not_in_provider_catalog"):
            self.service.route(self._request(), actor=self.actor)

    async def test_catalog_ttl_and_error_fail_closed_while_static_fallback_stays_explicit(self) -> None:
        self._provider("p1", catalog_discovery_enabled=True, catalog_ttl_seconds=30)
        self.adapter.catalogs["p1"] = (
            ModelCatalogEntry(concrete_model="concrete-discovered"),
        )
        await self.service.refresh_catalog("p1", actor=self.actor)
        self._model("discovered", concrete_model="concrete-discovered", availability_source="discovered")
        self._model("static", route_priority=200)
        self.now += 31

        route = self.service.route(self._request(), actor=self.actor)
        self.assertEqual([item.model_id for item in route.candidates], ["static"])
        self.assertEqual(self.service.list_catalogs(self.actor)[0].status.value, "stale")

        self.adapter.catalog_error.add("p1")
        failed = await self.service.refresh_catalog("p1", actor=self.actor)
        self.assertEqual(failed.status.value, "error")
        self.assertIn("catalog temporarily unavailable", failed.error)
        self.assertEqual(
            [item.model_id for item in self.service.route(self._request(), actor=self.actor).candidates],
            ["static"],
        )

    async def test_v1_2_state_migrates_task_routing_provenance(self) -> None:
        migrated = self.service.store._decode(
            {
                "schema_version": "1.2",
                "providers": [],
                "models": [
                    {
                        **self._model_payload("legacy-model"),
                        "organization_id": self.actor.organization_id,
                        "workspace_id": self.actor.workspace_id,
                        "updated_by": self.actor.identity_id,
                    }
                ],
                "prompt_templates": [],
                "policies": [],
                "invocations": [],
            }
        )

        self.assertEqual(migrated.schema_version, "1.4")
        self.assertEqual(migrated.models[0].workload_classes, ())

    @staticmethod
    def _model_payload(model_id: str, **overrides):
        payload = {
            "id": model_id,
            "provider_id": "p1",
            "concrete_model": f"concrete-{model_id}",
            "model_classes": (MODEL_CLASS_STRATEGIC,),
        }
        payload.update(overrides)
        return payload

    async def test_routing_is_deterministic_by_class_policy_health_and_priority(self) -> None:
        self._provider("p1")
        self._provider("p2")
        self._model("slow", "p1", route_priority=50)
        self._model("fast", "p2", route_priority=10)

        route = self.service.route(self._request(), actor=self.actor)

        self.assertEqual([item.model_id for item in route.candidates], ["fast", "slow"])
        self.assertEqual(route.policy_max_attempts, 2)
        self.assertEqual(route.prompt_template_version, "1.0")

    async def test_workload_specific_model_precedes_generic_provider_catalog_entry(self) -> None:
        self._provider("p1")
        self._provider("p2")
        self._model("generic", "p1", route_priority=1)
        self._model(
            "code-review",
            "p2",
            workload_classes=("code_review",),
            route_priority=100,
        )
        self._model(
            "summarizer",
            "p2",
            workload_classes=("summarization",),
            route_priority=0,
        )

        route = self.service.route(
            self._request(workload_class="code_review"),
            actor=self.actor,
        )

        self.assertEqual(
            [item.model_id for item in route.candidates],
            ["code-review", "generic"],
        )
        self.assertEqual(route.workload_class, "code_review")
        self.assertIn("workload_match=exact", route.candidates[0].routing_reason)

    async def test_latency_and_cost_preferences_rank_only_eligible_models(self) -> None:
        self._provider("p1")
        self._model(
            "fast-expensive",
            workload_classes=("summarization",),
            latency_class=ModelLatencyClass.LOW,
            input_price_per_million_usd=10.0,
            output_price_per_million_usd=10.0,
            route_priority=1,
        )
        self._model(
            "slow-cheap",
            workload_classes=("summarization",),
            latency_class=ModelLatencyClass.HIGH,
            input_price_per_million_usd=0.1,
            output_price_per_million_usd=0.1,
            route_priority=100,
        )

        low_cost = self.service.route(
            self._request(
                workload_class="summarization",
                prefer_lower_cost=True,
            ),
            actor=self.actor,
        )
        low_latency = self.service.route(
            self._request(
                workload_class="summarization",
                preferred_latency_classes=(ModelLatencyClass.LOW,),
            ),
            actor=self.actor,
        )

        self.assertEqual(low_cost.candidates[0].model_id, "slow-cheap")
        self.assertEqual(low_latency.candidates[0].model_id, "fast-expensive")

    async def test_strict_model_pin_cannot_bypass_tenant_policy(self) -> None:
        self._provider("p1")
        self._model("allowed")
        self._model("denied")
        self.service.set_policy(
            TenantModelPolicyUpdate(allowed_model_ids=("allowed",)),
            actor=self.actor,
        )

        with self.assertRaisesRegex(ModelRoutingError, "pinned_model=denied"):
            self.service.route(
                self._request(pinned_model_id="denied"),
                actor=self.actor,
            )

        self.assertEqual(self.adapter.calls, [])

    async def test_invocation_persists_task_routing_and_pin_provenance(self) -> None:
        self._provider("p1")
        self._model("reviewer", workload_classes=("code_review",))

        result = await self.service.invoke(
            self._request(
                workload_class="code_review",
                pinned_model_id="reviewer",
                preferred_latency_classes=(ModelLatencyClass.LOW,),
                prefer_lower_cost=True,
            ),
            actor=self.actor,
        )

        self.assertEqual(result.invocation.workload_class, "code_review")
        self.assertEqual(result.invocation.pinned_model_id, "reviewer")
        self.assertEqual(
            result.invocation.preferred_latency_classes,
            (ModelLatencyClass.LOW,),
        )
        self.assertTrue(result.invocation.prefer_lower_cost)
        self.assertIn("pin=reviewer", result.invocation.route_reason)

    async def test_task_routing_selects_distinct_model_classes(self) -> None:
        self._provider("p1")
        self._provider("p2")
        for model_class, workload, provider in (
            ("lightweight", "summarization", "p1"),
            ("primary-coding", "implementation", "p2"),
            ("high-reasoning", "architecture", "p2"),
        ):
            self._model(
                model_class, provider, model_classes=(model_class,),
                workload_classes=(workload,),
            )
            result = self.service.route(
                self._request(model_class=model_class, workload_class=workload),
                actor=self.actor,
            )
            self.assertEqual([c.model_id for c in result.candidates], [model_class])
            self.assertEqual(result.candidates[0].provider_id, provider)
        self.assertEqual(self.adapter.calls, [])

    async def test_missing_or_unavailable_pin_never_routes_to_other_provider(self) -> None:
        self._provider("p1", status="disabled")
        self._provider("p2")
        self._model("unavailable", "p1")
        self._model("available", "p2")
        for pin in ("missing", "unavailable"):
            with self.subTest(pin=pin), self.assertRaises(ModelRoutingError):
                await self.service.invoke(
                    self._request(pinned_model_id=pin), actor=self.actor,
                )
        self.assertEqual(self.adapter.calls, [])

    async def test_workload_outage_fallback_is_bounded_and_policy_safe(self) -> None:
        self._provider("p1")
        self._provider("p2")
        self._model("specific", "p1", workload_classes=("code_review",))
        self._model("generic", "p2")
        self._model(
            "restricted", "p2", workload_classes=("code_review",),
            residency_tags=("us",), route_priority=0,
        )
        self.adapter.transient_models.add("specific")
        self.service.set_policy(TenantModelPolicyUpdate(max_attempts=2), actor=self.actor)
        result = await self.service.invoke(
            self._request(workload_class="code_review"), actor=self.actor,
        )
        self.assertEqual([call[0] for call in self.adapter.calls], ["specific", "generic"])
        self.assertEqual(result.invocation.selected_model_id, "generic")
        self.adapter.calls.clear()
        with self.assertRaises(ModelProviderUnavailableError):
            await self.service.invoke(
                self._request(workload_class="code_review", pinned_model_id="specific"),
                actor=self.actor,
            )
        self.assertEqual([call[0] for call in self.adapter.calls], ["specific"])

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
        self.assertIsNotNone(result.invocation.attempts[0].failure)
        self.assertEqual(
            result.invocation.attempts[0].failure.reason_code.value,
            "provider_network",
        )
        self.assertTrue(
            result.invocation.attempts[0].failure.automatic_retry_allowed
        )
        self.assertEqual(result.invocation.attempts[1].outcome, "success")
        self.assertEqual(result.invocation.selected_model_id, "second")

    async def test_capacity_failure_falls_back_and_persists_provider_cooldown(self) -> None:
        self._provider("p1")
        self._provider("p2")
        self._model("first", "p1", route_priority=1)
        self._model("second", "p2", route_priority=2)
        self.adapter.capacity_models.add("first")

        result = await self.service.invoke(self._request(), actor=self.actor)

        self.assertEqual(result.text, "reply:second")
        self.assertEqual(
            [item[0] for item in self.adapter.calls],
            ["first", "second"],
        )
        self.assertEqual(
            result.invocation.attempts[0].outcome,
            "capacity_failure",
        )
        self.assertIsNotNone(
            result.invocation.attempts[0].failure
        )
        self.assertEqual(
            result.invocation.attempts[0].failure.reason_code.value,
            "provider_quota_exhausted",
        )
        self.assertEqual(
            result.invocation.attempts[0].failure.retryability.value,
            "after_remediation",
        )
        blocked = self.capacity.blocking_record(
            "p1",
            None,
            actor=self.actor,
        )
        self.assertIsNotNone(blocked)
        self.assertEqual(blocked.status, ProviderCapacityStatus.DEPLETED)
        self.assertEqual(blocked.retry_at, 1_900_000_120.0)

        route = self.service.route(self._request(), actor=self.actor)
        self.assertEqual(
            [item.provider_id for item in route.candidates],
            ["p2"],
        )

    async def test_preexisting_provider_capacity_is_excluded_before_invocation(self) -> None:
        self._provider("p1")
        self._provider("p2")
        self._model("first", "p1", route_priority=1)
        self._model("second", "p2", route_priority=2)
        self.capacity.report(
            ProviderCapacityReport(
                provider_id="p1",
                status=ProviderCapacityStatus.THROTTLED,
                retry_at=1_900_000_060.0,
                reason="retry later",
                source="test",
                observed_at=1_900_000_000.0,
            ),
            actor=self.actor,
        )

        result = await self.service.invoke(self._request(), actor=self.actor)

        self.assertEqual(result.text, "reply:second")
        self.assertEqual(self.adapter.calls, [("second", None)])

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

        with self.assertRaises(
            ModelProviderUnavailableError
        ) as caught:
            await self.service.invoke(
                self._request(),
                actor=self.actor,
            )

        self.assertIsNotNone(caught.exception.failure)
        self.assertEqual(
            caught.exception.failure.reason_code.value,
            "provider_network",
        )
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

    async def test_openai_catalog_discovery_normalizes_and_deduplicates_sdk_models(self) -> None:
        adapter = OpenAIModelProviderAdapter()

        class _Models:
            async def list(self):
                return types.SimpleNamespace(data=(
                    types.SimpleNamespace(id="gpt-z", owned_by="openai"),
                    types.SimpleNamespace(id="gpt-a", owned_by="partner"),
                    types.SimpleNamespace(id="gpt-z", owned_by="replacement"),
                    types.SimpleNamespace(id="", owned_by="ignored"),
                ))

        adapter._client = lambda provider, credential: types.SimpleNamespace(models=_Models())
        provider = self._provider("openai", adapter_type="openai")

        entries = await adapter.discover_models(provider, credential=None)

        self.assertEqual([item.concrete_model for item in entries], ["gpt-a", "gpt-z"])
        self.assertEqual(entries[1].upstream_provider_id, "replacement")
        self.assertEqual(entries[1].upstream_model_id, "gpt-z")


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
                "workload_classes": ["code_review"],
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

        route = self.client.post(
            "/api/model-gateway/route",
            json={
                "model_class": "primary-coding",
                "workload_class": "code_review",
                "pinned_model_id": "m1",
                "preferred_latency_classes": ["low", "standard"],
                "prefer_lower_cost": True,
                "messages": [{"role": "user", "content": "preview"}],
                "prompt_template_id": "generic.system",
                "prompt_template_version": "1.0",
            },
        )

        self.assertEqual(route.status_code, 200)
        self.assertEqual(route.json()["workload_class"], "code_review")
        self.assertEqual(route.json()["pinned_model_id"], "m1")
        self.assertEqual(
            route.json()["preferred_latency_classes"],
            ["low", "standard"],
        )
        self.assertTrue(route.json()["prefer_lower_cost"])
        self.assertEqual(route.json()["candidates"][0]["model_id"], "m1")

    def test_mfa_human_can_refresh_and_inspect_provider_scoped_catalog(self) -> None:
        self.actor = self.actor.model_copy(
            update={"assurance": AuthenticationAssurance.MFA}
        )
        adapter = _FakeAdapter()
        adapter.catalogs["aggregate"] = (
            ModelCatalogEntry(
                concrete_model="openai/gpt-coding",
                upstream_provider_id="openai",
                upstream_model_id="gpt-coding",
            ),
        )
        self.service.register_adapter(adapter)
        provider = self.client.put(
            "/api/model-gateway/providers/aggregate",
            json={
                "id": "aggregate", "adapter_type": "fake",
                "display_name": "Aggregate", "credential_required": False,
                "catalog_discovery_enabled": True, "catalog_ttl_seconds": 60,
            },
        )
        refreshed = self.client.post(
            "/api/model-gateway/providers/aggregate/catalog/refresh"
        )
        catalogs = self.client.get("/api/model-gateway/catalogs")

        self.assertEqual(provider.status_code, 200)
        self.assertEqual(refreshed.status_code, 200)
        self.assertEqual(refreshed.json()["item"]["status"], "ready")
        self.assertEqual(catalogs.json()["items"][0]["provider_id"], "aggregate")
        self.assertEqual(
            catalogs.json()["items"][0]["entries"][0]["upstream_provider_id"],
            "openai",
        )

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
