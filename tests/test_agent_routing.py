from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_web.agent_providers import (
    AgentProviderCapability,
    AgentProviderHealth,
    AgentProviderUpsert,
)
from codex_web.agent_routing import AgentRoutingRequest
from codex_web.agent_routing_definitions import (
    AGENT_ROUTING_POLICY_ID,
    AGENT_ROUTING_POLICY_KIND,
    AGENT_ROUTING_POLICY_SCHEMA_VERSION,
)
from codex_web.agent_runtime import AgentRuntimeHealth
from codex_web.configuration import (
    ConfigurationDraftCreate,
    ConfigurationPublishRequest,
    ConfigurationScope,
)
from codex_web.definitions import (
    DefinitionDraftCreate,
    DefinitionPublishRequest,
    DefinitionScope,
)
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.model_gateway import (
    ModelInvocationRequest,
    ModelLatencyClass,
    ModelRouteCandidate,
    ModelRouteResult,
)
from codex_web.services.agent_providers import AgentProviderService
from codex_web.services.agent_routing import AgentRoutingError, AgentRoutingService
from codex_web.services.model_gateway import ModelRoutingError
from codex_web.services.agent_routing_configuration import (
    AGENT_ROUTING_PREFERRED_PROVIDERS,
    MODEL_ROUTING_ALLOW_FALLBACK,
    MODEL_ROUTING_MAX_COST_USD,
    MODEL_ROUTING_PINNED_MODEL,
    MODEL_ROUTING_PREFERRED_LATENCIES,
    MODEL_ROUTING_PREFERRED_PROVIDERS,
    MODEL_ROUTING_PREFER_LOWER_COST,
    install_agent_routing_configuration,
)
from codex_web.services.agent_routing_definitions import install_agent_routing_definitions
from codex_web.provider_capacity import (
    ProviderCapacityReport,
    ProviderCapacityStatus,
)
from codex_web.services.agent_runtime import AgentRuntimeRegistry
from codex_web.services.configuration import ConfigurationService
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.services.provider_capacity import ProviderCapacityService
from codex_web.storage.agent_providers import AgentProviderStore
from codex_web.storage.configuration_registry import ConfigurationRegistryStore
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.provider_capacity import ProviderCapacityStore
from codex_web.storage.sqlite_state import SQLiteStateStore


def _actor() -> AuthenticationActor:
    return AuthenticationActor(
        identity_id="admin-a",
        principal_kind=PrincipalKind.HUMAN,
        organization_id="org-a",
        workspace_id="workspace-a",
        roles=(MembershipRole.ADMIN,),
        assurance=AuthenticationAssurance.MFA,
    )


class _Runtime:
    runtime_type = "test-runtime"

    def __init__(
        self,
        provider_id: str,
        runtime_id: str,
        capabilities: tuple[AgentProviderCapability, ...],
        *,
        health: AgentRuntimeHealth = AgentRuntimeHealth.HEALTHY,
    ) -> None:
        self.provider_id = provider_id
        self.runtime_id = runtime_id
        self.capabilities = capabilities
        self._health = health

    async def health(self) -> AgentRuntimeHealth:
        return self._health


class _ModelGateway:
    def __init__(self, provider_id: str = "openai") -> None:
        self.provider_id = provider_id
        self.calls = []

    def route(self, request, *, actor):
        self.calls.append((request, actor))
        if request.allowed_provider_ids and self.provider_id not in request.allowed_provider_ids:
            raise ModelRoutingError("runtime_provider_incompatible")
        return ModelRouteResult(
            model_class=request.model_class,
            prompt_template_id="generic.system",
            prompt_template_version="1",
            prompt_template_checksum_sha256="abc123",
            candidates=(
                ModelRouteCandidate(
                    provider_id=self.provider_id,
                    model_id="model-a",
                    concrete_model="model-a",
                    estimated_input_tokens=1,
                    max_output_tokens=128,
                    routing_reason="test",
                ),
            ),
            policy_max_attempts=1,
            policy_fingerprint_sha256="policy123",
        )


class AgentRoutingServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.providers = AgentProviderService(AgentProviderStore(self.sqlite))
        self.runtimes = AgentRuntimeRegistry()
        self.configuration = ConfigurationService(
            ConfigurationRegistryStore(self.sqlite)
        )
        install_agent_routing_configuration(self.configuration)
        self.definitions = DefinitionRegistryService(
            DefinitionRegistryStore(self.sqlite)
        )
        self.role_defaults = install_agent_routing_definitions(self.definitions)
        self.actor = _actor()
        self.capacity = ProviderCapacityService(
            ProviderCapacityStore(self.sqlite),
            clock=lambda: 1_900_000_000.0,
        )

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    def _provider(
        self,
        provider_id: str,
        capabilities: tuple[AgentProviderCapability, ...],
        *,
        health: AgentProviderHealth = AgentProviderHealth.HEALTHY,
        residency_tags: tuple[str, ...] = (),
        compliance_tags: tuple[str, ...] = (),
        model_provider_ids: tuple[str, ...] = (),
    ) -> None:
        record = self.providers.upsert(
            AgentProviderUpsert(
                id=provider_id,
                display_name=provider_id,
                declared_capabilities=capabilities,
                granted_capabilities=capabilities,
                health=health,
                residency_tags=residency_tags,
                compliance_tags=compliance_tags,
            ),
            actor=self.actor,
        )
        if model_provider_ids:
            self.providers.store.upsert(
                record.model_copy(update={"model_provider_ids": model_provider_ids}),
                expected_revision=record.revision,
            )

    def _runtime(
        self,
        provider_id: str,
        runtime_id: str,
        capabilities: tuple[AgentProviderCapability, ...],
        *,
        health: AgentRuntimeHealth = AgentRuntimeHealth.HEALTHY,
        capability_revision: int = 1,
        sandbox_profiles: tuple[str, ...] = (),
        network_profiles: tuple[str, ...] = (),
        residency_tags: tuple[str, ...] = (),
        compliance_tags: tuple[str, ...] = (),
        max_session_cost_usd: float | None = None,
    ) -> None:
        self.runtimes.register(
            _Runtime(
                provider_id,
                runtime_id,
                capabilities,
                health=health,
            ),
            capability_revision=capability_revision,
            sandbox_profiles=sandbox_profiles,
            network_profiles=network_profiles,
            residency_tags=residency_tags,
            compliance_tags=compliance_tags,
            max_session_cost_usd=max_session_cost_usd,
        )

    async def test_model_and_execution_runtime_route_independently(self) -> None:
        capabilities = (
            AgentProviderCapability.AGENT_EXECUTION,
            AgentProviderCapability.SHELL_TOOLS,
        )
        self._provider("anthropic", capabilities)
        self._runtime(
            "anthropic",
            "claude-code",
            capabilities,
            capability_revision=4,
        )
        models = _ModelGateway("openai")
        service = AgentRoutingService(
            self.providers,
            self.runtimes,
            model_gateway=models,
        )

        result = await service.route(
            AgentRoutingRequest(
                project_id="project-a",
                required_capabilities=(AgentProviderCapability.SHELL_TOOLS,),
                model_request=ModelInvocationRequest(
                    model_class="primary-coding",
                    messages=(),
                ),
            ),
            actor=self.actor,
        )

        self.assertEqual(result.selected_runtime.provider_id, "anthropic")
        self.assertEqual(result.selected_runtime.runtime_id, "claude-code")
        self.assertEqual(result.selected_runtime.capability_revision, 4)
        self.assertEqual(
            result.selected_runtime.execution_binding().model_dump(mode="json"),
            {
                "provider_id": "anthropic",
                "runtime_id": "claude-code",
                "capability_revision": 4,
            },
        )
        self.assertEqual(result.model_route.candidates[0].provider_id, "openai")
        self.assertEqual(len(models.calls), 1)

    async def test_model_selection_intersects_each_runtime_provider_binding(self) -> None:
        capabilities = (AgentProviderCapability.AGENT_EXECUTION,)
        self._provider(
            "a-first-runtime",
            capabilities,
            model_provider_ids=("anthropic-api",),
        )
        self._provider(
            "z-codex-runtime",
            capabilities,
            model_provider_ids=("openai-chatgpt",),
        )
        self._runtime("a-first-runtime", "first", capabilities)
        self._runtime("z-codex-runtime", "codex", capabilities)
        models = _ModelGateway("openai-chatgpt")
        service = AgentRoutingService(
            self.providers,
            self.runtimes,
            model_gateway=models,
        )

        result = await service.route(
            AgentRoutingRequest(
                project_id="project-a",
                model_request=ModelInvocationRequest(
                    model_class="primary-coding",
                    messages=(),
                ),
            ),
            actor=self.actor,
        )

        self.assertEqual(result.selected_runtime.provider_id, "z-codex-runtime")
        self.assertEqual(len(models.calls), 2)
        self.assertEqual(
            models.calls[0][0].allowed_provider_ids,
            ("anthropic-api",),
        )
        self.assertEqual(
            models.calls[1][0].allowed_provider_ids,
            ("openai-chatgpt",),
        )

    async def test_codex_routes_every_supported_execution_sandbox_profile(self) -> None:
        capabilities = (
            AgentProviderCapability.AGENT_EXECUTION,
            AgentProviderCapability.SHELL_TOOLS,
        )
        supported = ("read-only", "workspace-write", "danger-full-access")
        self._provider("openai", capabilities)
        self._runtime(
            "openai",
            "codex",
            capabilities,
            sandbox_profiles=supported,
        )
        service = AgentRoutingService(self.providers, self.runtimes)

        for sandbox in supported:
            with self.subTest(sandbox=sandbox):
                result = await service.route(
                    AgentRoutingRequest(
                        project_id="project-a",
                        required_capabilities=(AgentProviderCapability.SHELL_TOOLS,),
                        required_sandbox_profile=sandbox,
                        preferred_provider_ids=("openai",),
                        allow_fallback=False,
                    ),
                    actor=self.actor,
                )
                self.assertEqual(result.selected_runtime.provider_id, "openai")
                self.assertEqual(result.selected_runtime.runtime_id, "codex")
                self.assertEqual(
                    set(result.selected_runtime.sandbox_profiles),
                    set(supported),
                )
                self.assertEqual(
                    set(result.selected_runtime.execution_binding().sandbox_profiles),
                    set(supported),
                )

    def test_application_codex_registration_matches_supported_sandbox_profiles(self) -> None:
        source = (
            Path(__file__).resolve().parents[1] / "codex_web" / "application.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'sandbox_profiles=("read-only", "workspace-write", "danger-full-access"),',
            source,
        )

    async def test_unavailable_preference_falls_back_only_when_allowed(self) -> None:
        capabilities = (AgentProviderCapability.AGENT_EXECUTION,)
        self._provider("provider-a", capabilities)
        self._provider("provider-b", capabilities)
        self._runtime(
            "provider-a",
            "runtime-a",
            capabilities,
            health=AgentRuntimeHealth.UNAVAILABLE,
        )
        self._runtime("provider-b", "runtime-b", capabilities)
        service = AgentRoutingService(self.providers, self.runtimes)

        result = await service.route(
            AgentRoutingRequest(
                project_id="project-a",
                preferred_provider_ids=("provider-a", "provider-b"),
            ),
            actor=self.actor,
        )
        self.assertEqual(result.selected_runtime.provider_id, "provider-b")
        self.assertIn(
            "provider-a/runtime-a:runtime_unavailable",
            result.rejected_reasons,
        )

        with self.assertRaisesRegex(AgentRoutingError, "no eligible agent runtime"):
            await service.route(
                AgentRoutingRequest(
                    project_id="project-a",
                    preferred_provider_ids=("provider-a", "provider-b"),
                    allow_fallback=False,
                ),
                actor=self.actor,
            )

    async def test_depleted_preferred_runtime_falls_back_to_eligible_runtime(self) -> None:
        capabilities = (
            AgentProviderCapability.AGENT_EXECUTION,
            AgentProviderCapability.PERSISTENT_SESSIONS,
        )
        self._provider("openai", capabilities)
        self._provider("anthropic", capabilities)
        self._runtime("openai", "codex", capabilities)
        self._runtime("anthropic", "claude-code", capabilities)
        self.capacity.report(
            ProviderCapacityReport(
                provider_id="openai",
                runtime_id="codex",
                status=ProviderCapacityStatus.DEPLETED,
                retry_at=1_900_000_120.0,
                reason="Codex allocation exhausted",
                source="test",
                observed_at=1_900_000_000.0,
            ),
            actor=self.actor,
        )
        service = AgentRoutingService(
            self.providers,
            self.runtimes,
            provider_capacity=self.capacity,
        )

        result = await service.route(
            AgentRoutingRequest(
                project_id="project-a",
                require_persistent_session=True,
                preferred_provider_ids=("openai", "anthropic"),
            ),
            actor=self.actor,
        )

        self.assertEqual(result.selected_runtime.provider_id, "anthropic")
        self.assertEqual(result.selected_runtime.runtime_id, "claude-code")
        self.assertEqual(
            result.earliest_capacity_retry_at,
            1_900_000_120.0,
        )
        self.assertIn(
            "openai/codex:capacity_depleted:retry_at=1900000120.000",
            result.rejected_reasons,
        )

    async def test_allowed_network_profiles_accept_brokered_or_direct_egress(self) -> None:
        capabilities = (
            AgentProviderCapability.AGENT_EXECUTION,
            AgentProviderCapability.PERSISTENT_SESSIONS,
        )
        self._provider("openai", capabilities)
        self._runtime(
            "openai",
            "codex",
            capabilities,
            network_profiles=("brokered-model-egress",),
        )
        self._runtime(
            "openai",
            "codex-cli",
            capabilities,
            network_profiles=("direct-provider-egress",),
        )
        service = AgentRoutingService(self.providers, self.runtimes)

        for runtime_id in ("codex", "codex-cli"):
            with self.subTest(runtime_id=runtime_id):
                result = await service.route(
                    AgentRoutingRequest(
                        project_id="project-a",
                        allowed_runtime_ids=(runtime_id,),
                        require_persistent_session=True,
                        allowed_network_profiles=(
                            "brokered-model-egress",
                            "direct-provider-egress",
                        ),
                    ),
                    actor=self.actor,
                )
                self.assertEqual(
                    result.selected_runtime.runtime_id,
                    runtime_id,
                )

        self._runtime(
            "openai",
            "isolated-only",
            capabilities,
            network_profiles=("no-egress",),
        )
        with self.assertRaisesRegex(
            AgentRoutingError,
            "network_profile_mismatch",
        ):
            await service.route(
                AgentRoutingRequest(
                    project_id="project-a",
                    allowed_runtime_ids=("isolated-only",),
                    allowed_network_profiles=(
                        "brokered-model-egress",
                        "direct-provider-egress",
                    ),
                ),
                actor=self.actor,
            )

    async def test_constraints_and_runtime_budget_fail_closed(self) -> None:
        capabilities = (
            AgentProviderCapability.AGENT_EXECUTION,
            AgentProviderCapability.PERSISTENT_SESSIONS,
            AgentProviderCapability.SHELL_TOOLS,
        )
        self._provider(
            "provider-a",
            capabilities,
            residency_tags=("eu",),
            compliance_tags=("iso27001",),
        )
        self._runtime(
            "provider-a",
            "runtime-a",
            capabilities,
            capability_revision=7,
            sandbox_profiles=("isolated",),
            network_profiles=("no-egress",),
            residency_tags=("eu",),
            compliance_tags=("iso27001",),
            max_session_cost_usd=1.5,
        )
        service = AgentRoutingService(self.providers, self.runtimes)

        result = await service.route(
            AgentRoutingRequest(
                project_id="project-a",
                require_persistent_session=True,
                required_capabilities=(AgentProviderCapability.SHELL_TOOLS,),
                required_residency_tags=("eu",),
                required_compliance_tags=("iso27001",),
                required_sandbox_profile="isolated",
                required_network_profile="no-egress",
                max_runtime_cost_usd=2.0,
            ),
            actor=self.actor,
        )
        self.assertEqual(result.selected_runtime.capability_revision, 7)

        with self.assertRaisesRegex(AgentRoutingError, "runtime_budget_exceeded"):
            await service.route(
                AgentRoutingRequest(
                    project_id="project-a",
                    max_runtime_cost_usd=1.0,
                ),
                actor=self.actor,
            )

    async def test_live_runtime_capability_loss_fails_closed(self) -> None:
        capabilities = (
            AgentProviderCapability.AGENT_EXECUTION,
            AgentProviderCapability.SHELL_TOOLS,
        )
        self._provider("provider-a", capabilities)
        runtime = _Runtime("provider-a", "runtime-a", capabilities)
        self.runtimes.register(runtime, capability_revision=5)
        service = AgentRoutingService(self.providers, self.runtimes)

        first = await service.route(
            AgentRoutingRequest(
                project_id="project-a",
                required_capabilities=(AgentProviderCapability.SHELL_TOOLS,),
            ),
            actor=self.actor,
        )
        self.assertEqual(first.selected_runtime.capability_revision, 5)

        runtime.capabilities = (AgentProviderCapability.AGENT_EXECUTION,)
        with self.assertRaisesRegex(
            AgentRoutingError,
            "runtime_capability_mismatch",
        ):
            await service.route(
                AgentRoutingRequest(
                    project_id="project-a",
                    required_capabilities=(AgentProviderCapability.SHELL_TOOLS,),
                ),
                actor=self.actor,
            )

    async def test_missing_persistent_capability_is_rejected(self) -> None:
        capabilities = (AgentProviderCapability.AGENT_EXECUTION,)
        self._provider("provider-a", capabilities)
        self._runtime("provider-a", "runtime-a", capabilities)
        service = AgentRoutingService(self.providers, self.runtimes)

        with self.assertRaisesRegex(AgentRoutingError, "missing required capabilities"):
            await service.route(
                AgentRoutingRequest(
                    project_id="project-a",
                    require_persistent_session=True,
                ),
                actor=self.actor,
            )

    async def test_project_workspace_and_role_defaults_are_resolved_with_provenance(self) -> None:
        capabilities = (
            AgentProviderCapability.AGENT_EXECUTION,
            AgentProviderCapability.SHELL_TOOLS,
        )
        self._provider("provider-a", capabilities)
        self._provider("provider-b", capabilities)
        self._runtime("provider-a", "runtime-a", capabilities, capability_revision=2)
        self._runtime("provider-b", "runtime-b", capabilities, capability_revision=3)

        workspace_preference = self.configuration.create_draft(
            ConfigurationDraftCreate(
                key=AGENT_ROUTING_PREFERRED_PROVIDERS,
                scope_type=ConfigurationScope.WORKSPACE,
                scope_id="workspace-a",
                value=["provider-b"],
                actor="admin-a",
            )
        )
        self.configuration.publish(
            workspace_preference.id,
            ConfigurationPublishRequest(actor="admin-a"),
        )

        role_policy = self.definitions.create_draft(
            DefinitionDraftCreate(
                definition_id=AGENT_ROUTING_POLICY_ID,
                kind=AGENT_ROUTING_POLICY_KIND,
                definition_schema_version=AGENT_ROUTING_POLICY_SCHEMA_VERSION,
                scope_type=DefinitionScope.PROJECT,
                scope_id="project-a",
                payload={
                    "roles": [
                        {
                            "role_id": "developer",
                            "required_capabilities": ["shell_tools"],
                            "preferred_provider_ids": ["provider-a"],
                        }
                    ]
                },
                actor="admin-a",
            )
        )
        self.definitions.publish(
            role_policy.record_id,
            DefinitionPublishRequest(actor="admin-a"),
        )

        service = AgentRoutingService(
            self.providers,
            self.runtimes,
            configuration=self.configuration,
            role_defaults=self.role_defaults,
        )
        result = await service.route(
            AgentRoutingRequest(
                project_id="project-a",
                role_id="developer",
            ),
            actor=self.actor,
        )

        self.assertEqual(result.selected_runtime.provider_id, "provider-a")
        self.assertIsNotNone(result.role_definition_ref)
        self.assertEqual(
            result.role_definition_ref.record_id,
            role_policy.record_id,
        )
        preference_source = next(
            item
            for item in result.configuration_sources
            if item.key == AGENT_ROUTING_PREFERRED_PROVIDERS
        )
        self.assertEqual(preference_source.scope_type, "workspace")
        self.assertEqual(preference_source.scope_id, "workspace-a")

        without_role = await service.route(
            AgentRoutingRequest(project_id="project-a"),
            actor=self.actor,
        )
        self.assertEqual(without_role.selected_runtime.provider_id, "provider-b")

    async def test_model_configuration_is_merged_with_turn_precedence_and_provenance(self) -> None:
        capabilities = (AgentProviderCapability.AGENT_EXECUTION,)
        self._provider("provider-a", capabilities)
        self._runtime("provider-a", "runtime-a", capabilities)
        configured = {
            MODEL_ROUTING_PINNED_MODEL: "configuration-model",
            MODEL_ROUTING_PREFERRED_PROVIDERS: ["configuration-provider"],
            MODEL_ROUTING_PREFERRED_LATENCIES: ["standard", "high"],
            MODEL_ROUTING_PREFER_LOWER_COST: True,
            MODEL_ROUTING_ALLOW_FALLBACK: False,
            MODEL_ROUTING_MAX_COST_USD: 0.04,
        }
        records = {}
        for key, value in configured.items():
            draft = self.configuration.create_draft(
                ConfigurationDraftCreate(
                    key=key,
                    scope_type=ConfigurationScope.PROJECT,
                    scope_id="project-a",
                    value=value,
                    actor="admin-a",
                )
            )
            records[key] = self.configuration.publish(
                draft.id,
                ConfigurationPublishRequest(actor="admin-a"),
            )

        models = _ModelGateway()
        service = AgentRoutingService(
            self.providers,
            self.runtimes,
            configuration=self.configuration,
            model_gateway=models,
        )
        result = await service.route(
            AgentRoutingRequest(
                project_id="project-a",
                model_request=ModelInvocationRequest(
                    model_class="primary-coding",
                    pinned_model_id="turn-model",
                    preferred_provider_ids=("turn-provider",),
                    preferred_latency_classes=(ModelLatencyClass.LOW,),
                    max_cost_usd=0.08,
                    messages=(),
                ),
            ),
            actor=self.actor,
        )

        effective = models.calls[0][0]
        self.assertEqual(effective.pinned_model_id, "turn-model")
        self.assertEqual(
            effective.preferred_provider_ids,
            ("turn-provider", "configuration-provider"),
        )
        self.assertEqual(
            effective.preferred_latency_classes,
            (
                ModelLatencyClass.LOW,
                ModelLatencyClass.STANDARD,
                ModelLatencyClass.HIGH,
            ),
        )
        self.assertTrue(effective.prefer_lower_cost)
        self.assertEqual(effective.max_cost_usd, 0.04)
        self.assertFalse(effective.allow_fallback)
        self.assertEqual(
            result.effective_model_preferences.model_dump(mode="json"),
            {
                "model_class": "primary-coding",
                "workload_class": None,
                "pinned_model_id": "turn-model",
                "preferred_provider_ids": [
                    "turn-provider",
                    "configuration-provider",
                ],
                "preferred_latency_classes": ["low", "standard", "high"],
                "prefer_lower_cost": True,
                "max_cost_usd": 0.04,
                "allow_fallback": False,
                "source_precedence": [
                    "workflow_or_turn",
                    "agent_profile_revision",
                    "scoped_configuration",
                    "defaults",
                ],
            },
        )
        sources = {item.key: item for item in result.configuration_sources}
        for key, record in records.items():
            self.assertEqual(sources[key].scope_type, "project")
            self.assertEqual(sources[key].scope_id, "project-a")
            self.assertEqual(sources[key].record_id, record.id)
            self.assertEqual(sources[key].revision, record.revision)


    async def test_allowlist_never_expands_during_fallback(self) -> None:
        capabilities = (AgentProviderCapability.AGENT_EXECUTION,)
        self._provider("provider-a", capabilities)
        self._provider("provider-b", capabilities)
        self._runtime(
            "provider-a",
            "runtime-a",
            capabilities,
            health=AgentRuntimeHealth.UNAVAILABLE,
        )
        self._runtime("provider-b", "runtime-b", capabilities)
        service = AgentRoutingService(self.providers, self.runtimes)

        with self.assertRaisesRegex(AgentRoutingError, "provider_not_allowed"):
            await service.route(
                AgentRoutingRequest(
                    project_id="project-a",
                    allowed_provider_ids=("provider-a",),
                    preferred_provider_ids=("provider-a", "provider-b"),
                ),
                actor=self.actor,
            )


if __name__ == "__main__":
    unittest.main()
