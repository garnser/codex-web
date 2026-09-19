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
from codex_web.agent_runtime import AgentRuntimeHealth
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.model_gateway import (
    ModelInvocationRequest,
    ModelRouteCandidate,
    ModelRouteResult,
)
from codex_web.services.agent_providers import AgentProviderService
from codex_web.services.agent_routing import AgentRoutingError, AgentRoutingService
from codex_web.services.agent_runtime import AgentRuntimeRegistry
from codex_web.storage.agent_providers import AgentProviderStore
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
        sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.providers = AgentProviderService(AgentProviderStore(sqlite))
        self.runtimes = AgentRuntimeRegistry()
        self.actor = _actor()

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
    ) -> None:
        self.providers.upsert(
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
        self.assertEqual(result.model_route.candidates[0].provider_id, "openai")
        self.assertEqual(len(models.calls), 1)

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
