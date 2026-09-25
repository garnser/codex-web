from __future__ import annotations

import time
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.compatibility import ContractSpec


AGENT_PROVIDER_STATE_CONTRACT = ContractSpec(
    "agent-provider-state",
    "1.0",
    ("1.0",),
)


class AgentProviderCapability(StrEnum):
    MODEL_INFERENCE = "model_inference"
    AGENT_EXECUTION = "agent_execution"
    PERSISTENT_SESSIONS = "persistent_sessions"
    STREAMING = "streaming"
    INTERRUPT_CANCEL = "interrupt_cancel"
    FILESYSTEM_EDITING = "filesystem_editing"
    SHELL_TOOLS = "shell_tools"
    GIT_OPERATIONS = "git_operations"
    INTERACTIVE_APPROVALS = "interactive_approvals"
    NATIVE_CONTEXT_COMPACTION = "native_context_compaction"
    NATIVE_EXECUTION_OBJECTIVES = "native_execution_objectives"
    SUBAGENTS = "subagents"
    MCP_TOOL_SERVERS = "mcp_tool_servers"
    USAGE_EXACT = "usage_exact"
    USAGE_PARTIAL = "usage_partial"


class AgentProviderLifecycle(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class AgentProviderHealth(StrEnum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class AgentProviderCompatibility(StrEnum):
    COMPATIBLE = "compatible"
    INCOMPATIBLE = "incompatible"


class AgentProviderUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(min_length=1, max_length=200)
    display_name: str = Field(min_length=1, max_length=300)
    declared_capabilities: tuple[AgentProviderCapability, ...] = ()
    granted_capabilities: tuple[AgentProviderCapability, ...] = ()
    model_provider_ids: tuple[str, ...] = ()
    extension_installation_id: str | None = Field(default=None, max_length=500)
    configuration_refs: tuple[str, ...] = ()
    credential_refs: tuple[str, ...] = ()
    residency_tags: tuple[str, ...] = ()
    compliance_tags: tuple[str, ...] = ()
    lifecycle: AgentProviderLifecycle = AgentProviderLifecycle.ACTIVE
    health: AgentProviderHealth = AgentProviderHealth.UNKNOWN
    compatibility: AgentProviderCompatibility = AgentProviderCompatibility.COMPATIBLE

    @model_validator(mode="after")
    def normalize(self) -> "AgentProviderUpsert":
        self.declared_capabilities = tuple(dict.fromkeys(self.declared_capabilities))
        self.granted_capabilities = tuple(dict.fromkeys(self.granted_capabilities))
        if set(self.granted_capabilities) - set(self.declared_capabilities):
            raise ValueError("granted capabilities must be declared by the provider")
        self.model_provider_ids = tuple(sorted({item.strip() for item in self.model_provider_ids if item.strip()}))
        self.configuration_refs = tuple(sorted({item.strip() for item in self.configuration_refs if item.strip()}))
        self.credential_refs = tuple(sorted({item.strip() for item in self.credential_refs if item.strip()}))
        self.residency_tags = tuple(sorted({item.strip() for item in self.residency_tags if item.strip()}))
        self.compliance_tags = tuple(sorted({item.strip() for item in self.compliance_tags if item.strip()}))
        return self


class AgentProviderRecord(AgentProviderUpsert):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    organization_id: str
    workspace_id: str
    extension_id: str | None = None
    extension_version: str | None = None
    extension_digest: str | None = None
    synthesized_from_model_gateway: bool = False
    created_at: float = Field(default_factory=time.time)
    created_by: str
    updated_at: float = Field(default_factory=time.time)
    updated_by: str
    revision: int = Field(default=1, ge=1)


class AgentProviderDiscovery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: AgentProviderRecord
    effective_capabilities: tuple[AgentProviderCapability, ...]
    eligible: bool
    reasons: tuple[str, ...] = ()


class AgentProviderState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = AGENT_PROVIDER_STATE_CONTRACT.current
    providers: list[AgentProviderRecord] = Field(default_factory=list)

    def model_post_init(self, __context: Any) -> None:
        AGENT_PROVIDER_STATE_CONTRACT.require(self.schema_version)
