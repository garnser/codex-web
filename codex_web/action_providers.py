from __future__ import annotations

import time
import uuid
from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.compatibility import ContractSpec
from codex_web.resources import ResourceType
from codex_web.security import ExecutionSecurityPolicy


ACTION_PROVIDER_CONTRACT = ContractSpec(
    "action-provider",
    "1.1",
    ("1.0", "1.1"),
    deprecated=("1.0",),
)


class ActionRiskClass(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ActionCapability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    read: bool = True
    prepare: bool = True
    execute: bool = True
    dry_run: bool = False
    idempotency: bool = False
    rollback: bool = False
    verification: bool = False
    progress: bool = False
    evidence: bool = False

    def require(self, capability: str) -> None:
        if not hasattr(self, capability) or not bool(getattr(self, capability)):
            raise UnsupportedActionCapabilityError(capability)


class ActionDefinition(BaseModel):
    """Machine-readable pre-execution contract for one external action."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    action_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str | None = None
    capabilities: ActionCapability = Field(default_factory=ActionCapability)
    risk_class: ActionRiskClass = ActionRiskClass.MEDIUM
    required_resource_types: tuple[ResourceType, ...] = ()
    required_authority: tuple[str, ...] = ()
    required_authority_level: Literal["read", "recommend", "prepare", "execute", "approve"] = "execute"
    credential_required: bool = False
    credential_purpose: str | None = None
    expected_evidence: tuple[str, ...] = ()
    timeout_seconds: float = Field(default=60.0, gt=0.0)
    retry_max_attempts: int = Field(default=1, ge=1, le=100)
    reversible: bool = False
    network_access: bool = False
    filesystem_access: Literal["none", "read", "write"] = "none"
    process_access: bool = False

    @model_validator(mode="after")
    def validate_capabilities(self) -> "ActionDefinition":
        if self.reversible and not self.capabilities.rollback:
            raise ValueError("reversible actions must declare rollback capability")
        if self.capabilities.rollback and not self.capabilities.execute:
            raise ValueError("rollback capability requires execute capability")
        if self.credential_required and not self.credential_purpose:
            raise ValueError("credential-required actions must declare credential purpose")
        return self


class ActionRequest(BaseModel):
    """Provider-neutral invocation request. Contains references, never secret material."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    action_id: str = Field(min_length=1)
    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    project_id: str | None = None
    goal_id: str | None = None
    work_item_ref: str | None = None
    resource_ids: tuple[str, ...] = ()
    parameters: dict[str, Any] = Field(default_factory=dict)
    credential_ref: str | None = None
    idempotency_key: str | None = None
    dry_run: bool = False
    correlation_id: str | None = None
    requested_by: str | None = None

    @model_validator(mode="after")
    def normalize_targets(self) -> "ActionRequest":
        self.resource_ids = tuple(dict.fromkeys(self.resource_ids))
        return self


class ActionEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_type: str = Field(min_length=1)
    reference: str | None = None
    summary: str | None = None
    observed_at: float = Field(default_factory=time.time)
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class ActionPreparation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_binding_id: str
    action: ActionDefinition
    request: ActionRequest
    ready: bool = True
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    provider_plan: dict[str, Any] = Field(default_factory=dict)


class ActionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    execution_id: str = Field(default_factory=lambda: f"action-exec-{uuid.uuid4().hex}")
    provider_binding_id: str
    action_id: str
    status: Literal["succeeded", "failed", "dry_run", "rolled_back"]
    started_at: float
    completed_at: float
    idempotency_key: str | None = None
    external_id: str | None = None
    output: dict[str, Any] = Field(default_factory=dict)
    evidence: tuple[ActionEvidence, ...] = ()
    rollback_token: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class ActionVerification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    verified: bool
    evidence: tuple[ActionEvidence, ...] = ()
    findings: tuple[str, ...] = ()


class ActionProviderBinding(BaseModel):
    """Persisted provider configuration; implementation code is registered separately."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(default_factory=lambda: f"action-provider-{uuid.uuid4().hex}")
    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    provider_type: str = Field(min_length=1)
    provider_instance: str = Field(min_length=1)
    project_id: str | None = None
    resource_ids: tuple[str, ...] = ()
    credential_ref: str | None = None
    security_policy: ExecutionSecurityPolicy = Field(default_factory=ExecutionSecurityPolicy)
    enabled: bool = True
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    @model_validator(mode="after")
    def normalize_resources(self) -> "ActionProviderBinding":
        self.resource_ids = tuple(dict.fromkeys(self.resource_ids))
        return self


class ActionProviderBindingCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    provider_type: str = Field(min_length=1)
    provider_instance: str = Field(min_length=1)
    project_id: str | None = None
    resource_ids: tuple[str, ...] = ()
    credential_ref: str | None = None
    security_policy: ExecutionSecurityPolicy = Field(default_factory=ExecutionSecurityPolicy)
    enabled: bool = True

    @model_validator(mode="after")
    def normalize_resources(self) -> "ActionProviderBindingCreate":
        self.resource_ids = tuple(dict.fromkeys(self.resource_ids))
        return self


class ActionProviderState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    bindings: list[ActionProviderBinding] = Field(default_factory=list)


class UnsupportedActionCapabilityError(RuntimeError):
    def __init__(self, capability: str) -> None:
        super().__init__(f"action provider capability {capability!r} is not supported")
        self.capability = capability


@runtime_checkable
class ActionProvider(Protocol):
    contract_version: str
    provider_type: str
    provider_instance: str

    def actions(self) -> tuple[ActionDefinition, ...]: ...

    async def prepare(
        self,
        request: ActionRequest,
        *,
        binding: ActionProviderBinding,
    ) -> dict[str, Any]: ...

    async def execute(
        self,
        request: ActionRequest,
        *,
        binding: ActionProviderBinding,
        credential: str | None = None,
    ) -> ActionResult: ...

    async def verify(
        self,
        result: ActionResult,
        *,
        binding: ActionProviderBinding,
    ) -> ActionVerification: ...

    async def rollback(
        self,
        result: ActionResult,
        *,
        binding: ActionProviderBinding,
        credential: str | None = None,
    ) -> ActionResult: ...
