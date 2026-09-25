from __future__ import annotations

import time
import uuid
from enum import StrEnum
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.agent_providers import AgentProviderCapability
from codex_web.compatibility import ContractSpec
from codex_web.failures import FailureReason, FailureRecord


AGENT_SESSION_STATE_CONTRACT = ContractSpec(
    "agent-session-state",
    "1.1",
    ("1.1",),
)


class AgentSessionStatus(StrEnum):
    STARTING = "starting"
    READY = "ready"
    RUNNING = "running"
    INTERRUPTED = "interrupted"
    CLOSED = "closed"
    FAILED = "failed"


class AgentRuntimeHealth(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class AgentRuntimeRegistration(BaseModel):
    """Code-owned runtime capability metadata used for deterministic routing."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    provider_id: str = Field(min_length=1)
    runtime_id: str = Field(min_length=1)
    runtime_type: str = Field(min_length=1)
    capabilities: tuple[AgentProviderCapability, ...] = ()
    capability_revision: int = Field(default=1, ge=1)
    sandbox_profiles: tuple[str, ...] = ()
    network_profiles: tuple[str, ...] = ()
    residency_tags: tuple[str, ...] = ()
    compliance_tags: tuple[str, ...] = ()
    max_session_cost_usd: float | None = Field(default=None, ge=0.0)


class AgentRuntimeUnsupportedCapability(RuntimeError):
    reason_code = FailureReason.CAPABILITY_UNAVAILABLE

    def __init__(self, capability: AgentProviderCapability) -> None:
        self.capability = capability
        super().__init__(f"agent runtime capability is unsupported: {capability.value}")


class AgentRuntimeSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    project_id: str = Field(min_length=1)
    sandbox: str | None = None
    approval_policy: str | None = None
    approval_reviewer: str | None = None
    workspace_cwd: str | None = None
    sandbox_policy: dict[str, Any] | None = None
    execution_id: str | None = None
    assignment_id: str | None = None
    execution_workspace_id: str | None = None
    worker_id: str | None = None
    model: str | None = None
    model_class: str | None = None
    developer_instructions: str | None = None
    resource_ids: tuple[str, ...] = ()


class AgentRuntimeListRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    workspace_cwd: str | None = None
    archived: bool = False
    search: str | None = None
    limit: int = Field(default=100, ge=1, le=1000)


class AgentRuntimeObjectiveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    objective: str | None = Field(default=None, min_length=1)
    status: str | None = None
    token_budget: int | None = Field(default=None, ge=1)


class AgentRuntimeTurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    message: str = Field(min_length=1)
    model: str | None = None
    reasoning_effort: str | None = None
    workspace_cwd: str | None = None
    approval_policy: str | None = None
    approval_reviewer: str | None = None
    sandbox_policy: dict[str, Any] | None = None
    developer_instructions: str | None = None


class AgentRuntimeEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_type: str
    provider_native_session_id: str | None = None
    provider_native_turn_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class AgentRuntimeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_native_session_id: str | None = None
    provider_native_turn_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class AgentSession(BaseModel):
    """Canonical codex-web identity for one provider-backed agent session."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(default_factory=lambda: f"agent-session-{uuid.uuid4().hex}")
    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    provider_id: str = Field(min_length=1)
    runtime_id: str = Field(min_length=1)
    runtime_type: str = Field(min_length=1)
    provider_native_session_id: str | None = None
    project_id: str = Field(min_length=1)
    resource_ids: tuple[str, ...] = ()
    execution_id: str | None = None
    assignment_id: str | None = None
    execution_workspace_id: str | None = None
    worker_id: str | None = None
    model: str | None = None
    model_class: str | None = None
    capability_snapshot: tuple[AgentProviderCapability, ...] = ()
    capability_revision: int = Field(default=1, ge=1)
    status: AgentSessionStatus = AgentSessionStatus.STARTING
    recovery_attempts: int = Field(default=0, ge=0)
    last_recovered_at: float | None = None
    failure_reason: str | None = None
    failure: FailureRecord | None = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    @model_validator(mode="after")
    def normalize(self) -> "AgentSession":
        self.resource_ids = tuple(sorted({item for item in self.resource_ids if item}))
        self.capability_snapshot = tuple(dict.fromkeys(self.capability_snapshot))
        return self


class AgentSessionState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = AGENT_SESSION_STATE_CONTRACT.current
    sessions: list[AgentSession] = Field(default_factory=list)

    def model_post_init(self, __context: Any) -> None:
        AGENT_SESSION_STATE_CONTRACT.require(self.schema_version)


@runtime_checkable
class AgentRuntimeAdapter(Protocol):
    """Provider-neutral runtime boundary.

    Adapters translate provider-native session/turn protocol into canonical
    operations. They do not grant authority, choose a broader workspace, or
    persist provider-native IDs as canonical identity.
    """

    provider_id: str
    runtime_id: str
    runtime_type: str
    capabilities: tuple[AgentProviderCapability, ...]

    async def health(self) -> AgentRuntimeHealth: ...

    def subscribe_events(
        self,
        listener: Callable[[AgentRuntimeEvent], None],
    ) -> Callable[[], None]: ...

    async def recover(self) -> AgentRuntimeHealth: ...

    async def shutdown(self) -> None: ...

    async def list_sessions(
        self,
        request: AgentRuntimeListRequest,
    ) -> AgentRuntimeResult: ...

    async def create_session(
        self,
        request: AgentRuntimeSessionRequest,
    ) -> AgentRuntimeResult: ...

    async def resume_session(
        self,
        provider_native_session_id: str,
        request: AgentRuntimeSessionRequest,
    ) -> AgentRuntimeResult: ...

    async def read_session(
        self,
        provider_native_session_id: str,
    ) -> AgentRuntimeResult: ...

    async def close_session(
        self,
        provider_native_session_id: str,
    ) -> AgentRuntimeResult: ...

    async def restore_session(
        self,
        provider_native_session_id: str,
    ) -> AgentRuntimeResult: ...

    async def compact_session(
        self,
        provider_native_session_id: str,
    ) -> AgentRuntimeResult: ...

    async def read_objective(
        self,
        provider_native_session_id: str,
    ) -> AgentRuntimeResult: ...

    async def set_objective(
        self,
        provider_native_session_id: str,
        request: AgentRuntimeObjectiveRequest,
    ) -> AgentRuntimeResult: ...

    async def clear_objective(
        self,
        provider_native_session_id: str,
    ) -> AgentRuntimeResult: ...

    async def respond_approval(
        self,
        request_id: int | str,
        result: dict[str, Any],
    ) -> None: ...

    async def start_turn(
        self,
        provider_native_session_id: str,
        request: AgentRuntimeTurnRequest,
    ) -> AgentRuntimeResult: ...

    async def interrupt(
        self,
        provider_native_session_id: str,
    ) -> AgentRuntimeResult: ...
