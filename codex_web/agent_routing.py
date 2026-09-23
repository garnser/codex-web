from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.agent_profiles import AgentProfileExecutionBinding
from codex_web.agent_providers import AgentProviderCapability, AgentProviderHealth
from codex_web.agent_runtime import AgentRuntimeHealth
from codex_web.definitions import DefinitionReference
from codex_web.execution_workers import ExecutionRuntimeBinding
from codex_web.model_gateway import ModelInvocationRequest, ModelRouteResult
from codex_web.provider_capacity import ProviderCapacityStatus


class AgentRoutingRequest(BaseModel):
    """Deterministic model/runtime routing constraints for one execution request."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    project_id: str = Field(min_length=1)
    agent_profile_id: str | None = None
    agent_profile_revision: int | None = Field(default=None, ge=1)
    role_id: str | None = None
    required_capabilities: tuple[AgentProviderCapability, ...] = (
        AgentProviderCapability.AGENT_EXECUTION,
    )
    allowed_provider_ids: tuple[str, ...] = ()
    allowed_runtime_ids: tuple[str, ...] = ()
    preferred_provider_ids: tuple[str, ...] = ()
    preferred_runtime_ids: tuple[str, ...] = ()
    required_residency_tags: tuple[str, ...] = ()
    required_compliance_tags: tuple[str, ...] = ()
    required_sandbox_profile: str | None = None
    required_network_profile: str | None = None
    allowed_network_profiles: tuple[str, ...] = ()
    require_persistent_session: bool = False
    max_runtime_cost_usd: float | None = Field(default=None, ge=0.0)
    allow_fallback: bool = True
    model_request: ModelInvocationRequest | None = None

    @model_validator(mode="after")
    def normalize(self) -> "AgentRoutingRequest":
        if self.agent_profile_revision is not None and not self.agent_profile_id:
            raise ValueError(
                "agent_profile_revision requires agent_profile_id"
            )
        required = list(dict.fromkeys(self.required_capabilities))
        if AgentProviderCapability.AGENT_EXECUTION not in required:
            required.insert(0, AgentProviderCapability.AGENT_EXECUTION)
        if (
            self.require_persistent_session
            and AgentProviderCapability.PERSISTENT_SESSIONS not in required
        ):
            required.append(AgentProviderCapability.PERSISTENT_SESSIONS)
        self.required_capabilities = tuple(required)

        for field_name in (
            "allowed_provider_ids",
            "allowed_runtime_ids",
            "preferred_provider_ids",
            "preferred_runtime_ids",
            "required_residency_tags",
            "required_compliance_tags",
            "allowed_network_profiles",
        ):
            values = getattr(self, field_name)
            setattr(
                self,
                field_name,
                tuple(dict.fromkeys(value.strip() for value in values if value.strip())),
            )
        return self


class AgentRuntimeRouteCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_id: str
    runtime_id: str
    runtime_type: str
    provider_revision: int = Field(ge=1)
    capability_revision: int = Field(ge=1)
    effective_capabilities: tuple[AgentProviderCapability, ...]
    provider_health: AgentProviderHealth
    runtime_health: AgentRuntimeHealth
    residency_tags: tuple[str, ...] = ()
    compliance_tags: tuple[str, ...] = ()
    sandbox_profiles: tuple[str, ...] = ()
    network_profiles: tuple[str, ...] = ()
    estimated_session_cost_usd: float | None = None
    capacity_status: ProviderCapacityStatus = ProviderCapacityStatus.AVAILABLE
    capacity_retry_at: float | None = None
    routing_reason: str

    def execution_binding(self) -> ExecutionRuntimeBinding:
        return ExecutionRuntimeBinding(
            provider_id=self.provider_id,
            runtime_id=self.runtime_id,
            capability_revision=self.capability_revision,
            sandbox_profiles=self.sandbox_profiles,
        )


class AgentRoutingConfigurationSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    source: str
    record_id: str | None = None
    revision: int | None = None
    scope_type: str | None = None
    scope_id: str | None = None


class AgentRoutingResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    selected_runtime: AgentRuntimeRouteCandidate
    runtime_candidates: tuple[AgentRuntimeRouteCandidate, ...]
    model_route: ModelRouteResult | None = None
    fallback_allowed: bool
    earliest_capacity_retry_at: float | None = None
    rejected_reasons: tuple[str, ...] = ()
    configuration_sources: tuple[AgentRoutingConfigurationSource, ...] = ()
    role_definition_ref: DefinitionReference | None = None
    agent_profile: AgentProfileExecutionBinding | None = None
