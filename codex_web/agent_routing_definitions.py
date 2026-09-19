from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.agent_providers import AgentProviderCapability


AGENT_ROUTING_POLICY_KIND = "agent-routing-policy"
AGENT_ROUTING_POLICY_ID = "agent-routing.default"
AGENT_ROUTING_POLICY_SCHEMA_VERSION = "1.0"


class AgentRoutingRoleDefault(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    role_id: str = Field(min_length=1)
    required_capabilities: tuple[AgentProviderCapability, ...] = ()
    allowed_provider_ids: tuple[str, ...] = ()
    allowed_runtime_ids: tuple[str, ...] = ()
    preferred_provider_ids: tuple[str, ...] = ()
    preferred_runtime_ids: tuple[str, ...] = ()
    required_residency_tags: tuple[str, ...] = ()
    required_compliance_tags: tuple[str, ...] = ()
    allow_fallback: bool = True
    max_runtime_cost_usd: float | None = Field(default=None, ge=0.0)


class AgentRoutingPolicyDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    roles: tuple[AgentRoutingRoleDefault, ...] = ()

    @model_validator(mode="after")
    def validate_roles(self) -> "AgentRoutingPolicyDefinition":
        role_ids = [item.role_id for item in self.roles]
        if len(role_ids) != len(set(role_ids)):
            raise ValueError("agent routing role defaults must have unique role_id values")
        return self

    @property
    def role_map(self) -> dict[str, AgentRoutingRoleDefault]:
        return {item.role_id: item for item in self.roles}


def validate_agent_routing_policy(payload: dict[str, object]) -> dict[str, object]:
    return AgentRoutingPolicyDefinition.model_validate(payload).model_dump(mode="json")
