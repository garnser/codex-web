from __future__ import annotations

from codex_web.agent_providers import (
    AgentProviderHealth,
)
from codex_web.agent_routing import (
    AgentRoutingConfigurationSource,
    AgentRoutingRequest,
    AgentRoutingResult,
    AgentRuntimeRouteCandidate,
)
from codex_web.agent_runtime import AgentRuntimeHealth
from codex_web.provider_capacity import ProviderCapacityStatus
from codex_web.configuration import ConfigurationContext
from codex_web.identity import AuthenticationActor
from codex_web.services.agent_profiles import AgentProfileService
from codex_web.services.agent_providers import AgentProviderService
from codex_web.services.agent_routing_configuration import (
    AGENT_ROUTING_ALLOW_FALLBACK,
    AGENT_ROUTING_ALLOWED_PROVIDERS,
    AGENT_ROUTING_ALLOWED_RUNTIMES,
    AGENT_ROUTING_CONFIGURATION_KEYS,
    AGENT_ROUTING_MAX_RUNTIME_COST_USD,
    AGENT_ROUTING_PREFERRED_PROVIDERS,
    AGENT_ROUTING_PREFERRED_RUNTIMES,
    AGENT_ROUTING_REQUIRED_COMPLIANCE,
    AGENT_ROUTING_REQUIRED_RESIDENCY,
)
from codex_web.services.agent_routing_definitions import AgentRoutingDefinitionService
from codex_web.services.agent_runtime import AgentRuntimeRegistry
from codex_web.services.configuration import ConfigurationService
from codex_web.services.model_gateway import ModelGatewayService
from codex_web.services.provider_capacity import ProviderCapacityService
from codex_web.services.skills import SkillError, SkillService


class AgentRoutingError(RuntimeError):
    pass


class AgentRoutingBlockedError(AgentRoutingError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        rejected_reasons: tuple[str, ...] = (),
        target_type: str = "agent_runtime",
        target_id: str | None = None,
        retryable: bool = False,
        remediation_route: str = "/api/agent-providers",
    ) -> None:
        self.code = code
        self.rejected_reasons = rejected_reasons
        self.target_type = target_type
        self.target_id = target_id
        self.retryable = retryable
        self.remediation_route = remediation_route
        super().__init__(message)

    def public(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": str(self),
            "retryable": self.retryable,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "rejected_reasons": list(self.rejected_reasons),
            "remediation_route": self.remediation_route,
        }


class AgentCapacityRoutingError(AgentRoutingError):
    def __init__(
        self,
        message: str,
        *,
        retry_at: float | None,
        provider_keys: tuple[str, ...],
    ) -> None:
        self.retry_at = retry_at
        self.provider_keys = provider_keys
        super().__init__(message)


class AgentRoutingService:
    """Deterministic capability/policy routing for model and execution runtimes."""

    def __init__(
        self,
        providers: AgentProviderService,
        runtimes: AgentRuntimeRegistry,
        *,
        model_gateway: ModelGatewayService | None = None,
        configuration: ConfigurationService | None = None,
        role_defaults: AgentRoutingDefinitionService | None = None,
        provider_capacity: ProviderCapacityService | None = None,
        profiles: AgentProfileService | None = None,
        skills: SkillService | None = None,
    ) -> None:
        self.providers = providers
        self.runtimes = runtimes
        self.model_gateway = model_gateway
        self.configuration = configuration
        self.role_defaults = role_defaults
        self.provider_capacity = provider_capacity
        self.profiles = profiles
        self.skills = skills

    @staticmethod
    def _ordered(*groups: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                item
                for group in groups
                for item in group
                if item
            )
        )

    @staticmethod
    def _allowlist(
        label: str,
        *groups: tuple[str, ...],
    ) -> tuple[str, ...]:
        constrained = [set(group) for group in groups if group]
        if not constrained:
            return ()
        allowed = set.intersection(*constrained)
        if not allowed:
            raise AgentRoutingError(
                f"agent routing {label} allowlists have no common value"
            )
        ordered = [
            item
            for group in groups
            for item in group
            if item in allowed
        ]
        return tuple(dict.fromkeys(ordered))

    def _apply_profile(
        self,
        request: AgentRoutingRequest,
        actor: AuthenticationActor,
    ):
        if request.agent_profile_id is None:
            return request, None
        if self.profiles is None:
            raise AgentRoutingError(
                "agent profile routing requested but Agent Profile service is unavailable"
            )
        profile, decision = self.profiles.resolve_for_execution(
            request.agent_profile_id,
            actor=actor,
            project_id=request.project_id,
            revision=request.agent_profile_revision,
        )
        runtime = profile.runtime_policy
        skill_provider_capabilities = ()
        if profile.skill_refs:
            if self.skills is None:
                raise AgentRoutingBlockedError(
                    "Agent Profile references Skills but Skill service is unavailable",
                    code="skill_definition_unavailable",
                    target_type="agent_profile",
                    target_id=profile.profile_id,
                    remediation_route="/api/skills",
                )
            try:
                skill_provider_capabilities, _worker = (
                    self.skills.requirements_for_refs(
                        profile.skill_refs,
                        actor=actor,
                    )
                )
            except SkillError as exc:
                raise AgentRoutingBlockedError(
                    str(exc),
                    code="skill_definition_unavailable",
                    target_type="agent_profile",
                    target_id=profile.profile_id,
                    remediation_route="/api/skills",
                ) from exc

        if (
            request.role_id
            and profile.role_id
            and request.role_id != profile.role_id
        ):
            raise AgentRoutingError(
                "routing request role conflicts with Agent Profile role"
            )

        def exact_optional(label: str, requested, profiled):
            if requested and profiled and requested != profiled:
                raise AgentRoutingError(
                    f"routing request {label} conflicts with Agent Profile"
                )
            return requested or profiled

        allowed_providers = self._allowlist(
            "provider",
            request.allowed_provider_ids,
            runtime.allowed_provider_ids,
        )
        allowed_runtimes = self._allowlist(
            "runtime",
            request.allowed_runtime_ids,
            runtime.allowed_runtime_ids,
        )

        model_request = request.model_request
        if model_request is not None:
            policy = profile.model_policy
            if (
                policy.model_class
                and model_request.model_class != policy.model_class
            ):
                raise AgentRoutingError(
                    "model request class conflicts with Agent Profile model policy"
                )
            max_input = model_request.max_input_tokens
            if profile.budgets.max_input_tokens is not None:
                max_input = (
                    min(max_input, profile.budgets.max_input_tokens)
                    if max_input is not None
                    else profile.budgets.max_input_tokens
                )
            max_output = model_request.max_output_tokens
            if profile.budgets.max_output_tokens is not None:
                max_output = min(
                    max_output,
                    profile.budgets.max_output_tokens,
                )
            max_cost = model_request.max_cost_usd
            if profile.budgets.max_cost_usd is not None:
                max_cost = (
                    min(max_cost, profile.budgets.max_cost_usd)
                    if max_cost is not None
                    else profile.budgets.max_cost_usd
                )
            model_request = model_request.model_copy(
                update={
                    "preferred_provider_ids": self._ordered(
                        model_request.preferred_provider_ids,
                        policy.preferred_provider_ids,
                    ),
                    "max_input_tokens": max_input,
                    "max_output_tokens": max_output,
                    "max_cost_usd": max_cost,
                    "allow_fallback": (
                        model_request.allow_fallback
                        and policy.allow_fallback
                    ),
                }
            )

        effective = request.model_copy(
            update={
                "role_id": request.role_id or profile.role_id,
                "required_capabilities": tuple(
                    dict.fromkeys(
                        (
                            *request.required_capabilities,
                            *runtime.required_capabilities,
                            *skill_provider_capabilities,
                        )
                    )
                ),
                "allowed_provider_ids": allowed_providers,
                "allowed_runtime_ids": allowed_runtimes,
                "preferred_provider_ids": self._ordered(
                    request.preferred_provider_ids,
                    runtime.preferred_provider_ids,
                ),
                "preferred_runtime_ids": self._ordered(
                    request.preferred_runtime_ids,
                    runtime.preferred_runtime_ids,
                ),
                "required_residency_tags": self._ordered(
                    request.required_residency_tags,
                    runtime.required_residency_tags,
                ),
                "required_compliance_tags": self._ordered(
                    request.required_compliance_tags,
                    runtime.required_compliance_tags,
                ),
                "required_sandbox_profile": exact_optional(
                    "sandbox profile",
                    request.required_sandbox_profile,
                    (
                        runtime.required_sandbox_profile
                        or profile.sandbox_requirement
                    ),
                ),
                "required_network_profile": exact_optional(
                    "network profile",
                    request.required_network_profile,
                    runtime.required_network_profile,
                ),
                "require_persistent_session": (
                    request.require_persistent_session
                    or runtime.require_persistent_session
                ),
                "max_runtime_cost_usd": (
                    min(
                        value
                        for value in (
                            request.max_runtime_cost_usd,
                            runtime.max_runtime_cost_usd,
                            profile.budgets.max_cost_usd,
                        )
                        if value is not None
                    )
                    if any(
                        value is not None
                        for value in (
                            request.max_runtime_cost_usd,
                            runtime.max_runtime_cost_usd,
                            profile.budgets.max_cost_usd,
                        )
                    )
                    else None
                ),
                "allow_fallback": (
                    request.allow_fallback
                    and runtime.allow_fallback
                ),
                "model_request": model_request,
            }
        )
        return AgentRoutingRequest.model_validate(
            effective.model_dump(mode="python")
        ), profile

    def _effective_request(
        self,
        request: AgentRoutingRequest,
        actor: AuthenticationActor,
    ) -> tuple[
        AgentRoutingRequest,
        tuple[AgentRoutingConfigurationSource, ...],
        object | None,
    ]:
        config_values: dict[str, object] = {}
        config_sources: list[AgentRoutingConfigurationSource] = []
        if self.configuration is not None:
            context = ConfigurationContext(
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                project_id=request.project_id,
            )
            for key in AGENT_ROUTING_CONFIGURATION_KEYS:
                resolved = self.configuration.resolve(key, context)
                config_values[key] = resolved.value
                config_sources.append(
                    AgentRoutingConfigurationSource(
                        key=key,
                        source=resolved.source,
                        record_id=resolved.record_id,
                        revision=resolved.revision,
                        scope_type=(
                            resolved.scope_type.value
                            if resolved.scope_type is not None
                            else None
                        ),
                        scope_id=resolved.scope_id,
                    )
                )

        role = None
        role_ref = None
        if self.role_defaults is not None and request.role_id:
            role, role_ref = self.role_defaults.resolve(
                role_id=request.role_id,
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                project_id=request.project_id,
            )

        config_preferred_providers = tuple(
            config_values.get(AGENT_ROUTING_PREFERRED_PROVIDERS) or ()
        )
        config_preferred_runtimes = tuple(
            config_values.get(AGENT_ROUTING_PREFERRED_RUNTIMES) or ()
        )
        config_allowed_providers = tuple(
            config_values.get(AGENT_ROUTING_ALLOWED_PROVIDERS) or ()
        )
        config_allowed_runtimes = tuple(
            config_values.get(AGENT_ROUTING_ALLOWED_RUNTIMES) or ()
        )
        role_preferred_providers = role.preferred_provider_ids if role else ()
        role_preferred_runtimes = role.preferred_runtime_ids if role else ()
        role_allowed_providers = role.allowed_provider_ids if role else ()
        role_allowed_runtimes = role.allowed_runtime_ids if role else ()
        role_capabilities = role.required_capabilities if role else ()
        role_residency = role.required_residency_tags if role else ()
        role_compliance = role.required_compliance_tags if role else ()

        costs = [
            value
            for value in (
                request.max_runtime_cost_usd,
                role.max_runtime_cost_usd if role else None,
                config_values.get(AGENT_ROUTING_MAX_RUNTIME_COST_USD),
            )
            if value is not None
        ]
        max_runtime_cost = min(float(value) for value in costs) if costs else None

        effective = request.model_copy(
            update={
                "required_capabilities": tuple(
                    dict.fromkeys(
                        (*request.required_capabilities, *role_capabilities)
                    )
                ),
                "allowed_provider_ids": self._allowlist(
                    "provider",
                    request.allowed_provider_ids,
                    role_allowed_providers,
                    config_allowed_providers,
                ),
                "allowed_runtime_ids": self._allowlist(
                    "runtime",
                    request.allowed_runtime_ids,
                    role_allowed_runtimes,
                    config_allowed_runtimes,
                ),
                "preferred_provider_ids": self._ordered(
                    request.preferred_provider_ids,
                    role_preferred_providers,
                    config_preferred_providers,
                ),
                "preferred_runtime_ids": self._ordered(
                    request.preferred_runtime_ids,
                    role_preferred_runtimes,
                    config_preferred_runtimes,
                ),
                "required_residency_tags": self._ordered(
                    request.required_residency_tags,
                    role_residency,
                    tuple(
                        config_values.get(AGENT_ROUTING_REQUIRED_RESIDENCY) or ()
                    ),
                ),
                "required_compliance_tags": self._ordered(
                    request.required_compliance_tags,
                    role_compliance,
                    tuple(
                        config_values.get(AGENT_ROUTING_REQUIRED_COMPLIANCE) or ()
                    ),
                ),
                "allow_fallback": (
                    request.allow_fallback
                    and (role.allow_fallback if role else True)
                    and bool(
                        config_values.get(AGENT_ROUTING_ALLOW_FALLBACK, True)
                    )
                ),
                "max_runtime_cost_usd": max_runtime_cost,
            }
        )
        effective = AgentRoutingRequest.model_validate(
            effective.model_dump(mode="python")
        )
        return effective, tuple(config_sources), role_ref

    async def route(
        self,
        request: AgentRoutingRequest,
        *,
        actor: AuthenticationActor,
    ) -> AgentRoutingResult:
        request, profile = self._apply_profile(
            request,
            actor,
        )
        request, configuration_sources, role_definition_ref = self._effective_request(
            request,
            actor,
        )
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
        capacity_blocks = []
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

            adapter = self.runtimes.get(
                registration.provider_id,
                registration.runtime_id,
            )
            runtime_capabilities = set(registration.capabilities) & set(
                adapter.capabilities
            )
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
            if request.allowed_network_profiles:
                if (
                    not registration.network_profiles
                    or not set(registration.network_profiles).intersection(
                        request.allowed_network_profiles
                    )
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

            capacity_record = None
            if self.provider_capacity is not None:
                try:
                    capacity_record = await self.provider_capacity.refresh_if_due(
                        registration.provider_id,
                        registration.runtime_id,
                        actor=actor,
                    )
                except Exception:
                    capacity_record = self.provider_capacity.get(
                        registration.provider_id,
                        registration.runtime_id,
                        actor=actor,
                    )
                if (
                    capacity_record is not None
                    and capacity_record.blocks(float(self.provider_capacity.clock()))
                ):
                    rejected.append(
                        f"{key}:capacity_{capacity_record.status.value}"
                        + (
                            f":retry_at={capacity_record.retry_at:.3f}"
                            if capacity_record.retry_at is not None
                            else ""
                        )
                    )
                    capacity_blocks.append(capacity_record)
                    continue

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
                    if capability in runtime_capabilities
                    and capability in set(provider.effective_capabilities)
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
                capacity_status=(
                    capacity_record.status
                    if capacity_record is not None
                    else ProviderCapacityStatus.AVAILABLE
                ),
                capacity_retry_at=(
                    capacity_record.retry_at
                    if capacity_record is not None
                    else None
                ),
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
            if capacity_blocks:
                retries = [
                    item.retry_at
                    for item in capacity_blocks
                    if item.retry_at is not None
                ]
                raise AgentCapacityRoutingError(
                    f"no eligible agent runtime: {detail}",
                    retry_at=min(retries) if retries else None,
                    provider_keys=tuple(
                        dict.fromkeys(item.key for item in capacity_blocks)
                    ),
                )
            raise AgentRoutingBlockedError(
                f"no eligible agent runtime: {detail}",
                code="agent_runtime_unavailable",
                rejected_reasons=tuple(
                    dict.fromkeys(rejected)
                ),
                target_type=(
                    "agent_profile"
                    if request.agent_profile_id
                    else "agent_runtime"
                ),
                target_id=request.agent_profile_id,
                retryable=False,
                remediation_route=(
                    "/api/agent-profiles"
                    if request.agent_profile_id
                    else "/api/agent-providers"
                ),
            )

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
            earliest_capacity_retry_at=(
                min(
                    item.retry_at
                    for item in capacity_blocks
                    if item.retry_at is not None
                )
                if any(item.retry_at is not None for item in capacity_blocks)
                else None
            ),
            rejected_reasons=tuple(dict.fromkeys(rejected)),
            configuration_sources=configuration_sources,
            role_definition_ref=role_definition_ref,
            agent_profile=(
                self.profiles.binding_for(
                    profile,
                    selected_provider_id=candidates[0].provider_id,
                    selected_runtime_id=candidates[0].runtime_id,
                    selected_provider_revision=candidates[0].provider_revision,
                    selected_runtime_capability_revision=(
                        candidates[0].capability_revision
                    ),
                    model_provider_id=(
                        model_route.candidates[0].provider_id
                        if model_route is not None
                        and model_route.candidates
                        else None
                    ),
                    model_id=(
                        model_route.candidates[0].model_id
                        if model_route is not None
                        and model_route.candidates
                        else None
                    ),
                )
                if profile is not None and self.profiles is not None
                else None
            ),
        )
