from __future__ import annotations

import time
import uuid
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from codex_web.authority import AuthorityLevel
from codex_web.compatibility import ContractSpec
from codex_web.definitions import DefinitionReference


CONTROL_PLANE_BROKER_AUDIT_CONTRACT = ContractSpec(
    "control-plane-broker-audit-state",
    "1.0",
    ("1.0",),
)


class ControlPlaneBrokerDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ERROR = "error"


class ControlPlaneBrokerOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    method: str = Field(min_length=1)
    path_template: str = Field(min_length=1)
    capability: str = Field(min_length=1)
    authority_level: AuthorityLevel


class ControlPlaneBrokerLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_header_bytes: int = Field(default=32 * 1024, ge=1024, le=256 * 1024)
    max_request_bytes: int = Field(default=64 * 1024, ge=1024, le=4 * 1024 * 1024)
    max_response_bytes: int = Field(default=512 * 1024, ge=1024, le=16 * 1024 * 1024)
    max_concurrent_requests: int = Field(default=4, ge=1, le=64)
    max_requests_per_minute: int = Field(default=60, ge=1, le=10000)


class ControlPlaneBrokerAuditEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: f"control-broker-audit-{uuid.uuid4().hex}")
    occurred_at: float = Field(default_factory=time.time)
    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    project_id: str | None = None
    execution_id: str = Field(min_length=1)
    assignment_id: str = Field(min_length=1)
    worker_id: str = Field(min_length=1)
    fence: int = Field(ge=1)
    actor_identity_id: str | None = None
    execution_profile_id: str | None = None
    operation_id: str | None = None
    capability: str | None = None
    method: str = Field(min_length=1)
    path: str = Field(min_length=1)
    target_ref: str | None = None
    decision: ControlPlaneBrokerDecision
    authority_decision_id: str | None = None
    authority_definition: DefinitionReference | None = None
    response_status: int | None = Field(default=None, ge=100, le=599)
    denial_reason: str | None = None
    correlation_id: str = Field(min_length=1, max_length=128)
    causation_id: str | None = Field(default=None, max_length=128)


class ControlPlaneBrokerAuditState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = CONTROL_PLANE_BROKER_AUDIT_CONTRACT.current
    events: list[ControlPlaneBrokerAuditEvent] = Field(default_factory=list)
