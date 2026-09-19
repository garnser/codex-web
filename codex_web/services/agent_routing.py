from __future__ import annotations

from codex_web.agent_providers import (
    AgentProviderHealth,
)
from codex_web.agent_routing import (
    AgentRoutingRequest,
    AgentRoutingResult,
    AgentRuntimeRouteCandidate,
)
from codex_web.agent_runtime import AgentRuntimeHealth
from codex_web.identity import AuthenticationActor
from codex_web.services.agent_providers import AgentProviderService
from codex_web.services.agent_runtime import AgentRuntimeRegistry
from codex_web.services.model_gateway import ModelGatewayService


class AgentRoutingError(RuntimeError):
    pass


class AgentRoutingService:
    """Deterministic capability/policy routing for model and execution runtimes."""

    def __init__(
        self,
        providers: AgentProviderService,
        runtimes: AgentRuntimeRegistry,
        *,
        model_gateway: ModelGatewayService | None = None,
    ) -> None:
        self.providers = providers
        self.runtimes = runtimes
        self.model_gateway = model_gateway

    async def route(
        self,
        request: AgentRoutingRequest,
        *,
        actor: AuthenticationActor,
    ) -> AgentRoutingResult:
        required = set(request.required_capabilities)
        discoveries = {
            item.provider.id: item
            for item in self.providers.discover(
                actor,
                required_capabilities=request.required_capabilities,
            )
        }
        provider_preferences = {
            provider_id: index
            for index, provider_id in enumerate(request.preferred_provider_ids)
        }
        runtime_preferences = {
            runtime_id: index
            for index, runtime_id in enumerate(request.preferred_runtime_ids)
        }
        rejected: list[str] = []
        ranked: list[
            tuple[int, int, int, str, str, AgentRuntimeRouteCandidate]
        ] = []

        for registration in self.runtimes.list_registrations():
            provider = discoveries.get(registration.provider_id)
            key = f"{registration.provider_id}/{registration.runtime_id}"
            if provider is None:
                rejected.append(f"{key}:provider_not_registered")
                continue
            if not provider.eligible:
                detail = "|".join(provider.reasons) or "provider_ineligible"
                rejected.append(f"{key}:{detail}")
                continue
            if request.allowed_provider_ids and (
                registration.provider_id not in request.allowed_provider_ids
            ):
                rejected.append(f"{key}:provider_not_allowed")
                continue
            if request.allowed_runtime_ids and (
                registration.runtime_id not in request.allowed_runtime_ids
            ):
                rejected.append(f"{key}:runtime_not_allowed")
                continue
            if not request.allow_fallback:
                if (
                    request.preferred_provider_ids
                    and registration.provider_id
                    != request.preferred_provider_ids[0]
                ):
                    rejected.append(f"{key}:provider_fallback_disabled")
                    continue
                if (
                    request.preferred_runtime_ids
                    and registration.runtime_id
                    != request.preferred_runtime_ids[0]
                ):
                    rejected.append(f"{key}:runtime_fallback_disabled")
                    continue

            runtime_capabilities = set(registration.capabilities)
            if not required.issubset(runtime_capabilities):
                rejected.append(f"{key}:runtime_capability_mismatch")
                continue

            provider_record = provider.provider
            required_residency = set(request.required_residency_tags)
            if required_residency and not required_residency.issubset(
                set(provider_record.residency_tags)
            ):
                rejected.append(f"{key}:provider_residency_mismatch")
                continue
            if (
                required_residency
                and registration.residency_tags
                and not required_residency.issubset(set(registration.residency_tags))
            ):
                rejected.append(f"{key}:runtime_residency_mismatch")
                continue

            required_compliance = set(request.required_compliance_tags)
            if required_compliance and not required_compliance.issubset(
                set(provider_record.compliance_tags)
            ):
                rejected.append(f"{key}:provider_compliance_mismatch")
                continue
            if (
                required_compliance
                and registration.compliance_tags
                and not required_compliance.issubset(set(registration.compliance_tags))
            ):
                rejected.append(f"{key}:runtime_compliance_mismatch")
                continue

            if request.required_sandbox_profile:
                if (
                    not registration.sandbox_profiles
                    or request.required_sandbox_profile
                    not in registration.sandbox_profiles
                ):
                    rejected.append(f"{key}:sandbox_profile_mismatch")
                    continue
            if request.required_network_profile:
                if (
                    not registration.network_profiles
                    or request.required_network_profile
                    not in registration.network_profiles
                ):
                    rejected.append(f"{key}:network_profile_mismatch")
                    continue

            if request.max_runtime_cost_usd is not None:
                if registration.max_session_cost_usd is None:
                    rejected.append(f"{key}:runtime_pricing_required_for_budget")
                    continue
                if registration.max_session_cost_usd > request.max_runtime_cost_usd:
                    rejected.append(f"{key}:runtime_budget_exceeded")
                    continue

            adapter = self.runtimes.get(
                registration.provider_id,
                registration.runtime_id,
            )
            runtime_health = await adapter.health()
            if runtime_health == AgentRuntimeHealth.UNAVAILABLE:
                rejected.append(f"{key}:runtime_unavailable")
                continue

            provider_rank = provider_preferences.get(
                registration.provider_id,
                len(provider_preferences),
            )
            runtime_rank = runtime_preferences.get(
                registration.runtime_id,
                len(runtime_preferences),
            )
            health_penalty = 0
            if provider_record.health == AgentProviderHealth.DEGRADED:
                health_penalty += 1
            if runtime_health == AgentRuntimeHealth.DEGRADED:
                health_penalty += 1

            candidate = AgentRuntimeRouteCandidate(
                provider_id=registration.provider_id,
                runtime_id=registration.runtime_id,
                runtime_type=registration.runtime_type,
                provider_revision=provider_record.revision,
                capability_revision=registration.capability_revision,
                effective_capabilities=tuple(
                    capability
                    for capability in registration.capabilities
                    if capability in set(provider.effective_capabilities)
                ),
                provider_health=provider_record.health,
                runtime_health=runtime_health,
                residency_tags=tuple(
                    registration.residency_tags or provider_record.residency_tags
                ),
                compliance_tags=tuple(
                    registration.compliance_tags or provider_record.compliance_tags
                ),
                sandbox_profiles=registration.sandbox_profiles,
                network_profiles=registration.network_profiles,
                estimated_session_cost_usd=registration.max_session_cost_usd,
                routing_reason=(
                    f"provider_revision={provider_record.revision};"
                    f"capability_revision={registration.capability_revision};"
                    f"provider_health={provider_record.health.value};"
                    f"runtime_health={runtime_health.value}"
                ),
            )
            ranked.append(
                (
                    provider_rank,
                    runtime_rank,
                    health_penalty,
                    registration.provider_id,
                    registration.runtime_id,
                    candidate,
                )
            )

        ranked.sort(key=lambda item: item[:5])
        candidates = tuple(item[5] for item in ranked)
        if not candidates:
            detail = ",".join(rejected[:30]) or "no runtimes registered"
            raise AgentRoutingError(f"no eligible agent runtime: {detail}")

        if not request.allow_fallback:
            candidates = (candidates[0],)

        model_route = None
        if request.model_request is not None:
            if self.model_gateway is None:
                raise AgentRoutingError("model routing requested but ModelGateway is unavailable")
            model_route = self.model_gateway.route(
                request.model_request,
                actor=actor,
            )

        return AgentRoutingResult(
            selected_runtime=candidates[0],
            runtime_candidates=candidates,
            model_route=model_route,
            fallback_allowed=request.allow_fallback,
            rejected_reasons=tuple(dict.fromkeys(rejected)),
        )
