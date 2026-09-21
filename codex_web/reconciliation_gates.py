from __future__ import annotations

import time
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


RECONCILIATION_GATE_VERSION = "1.0"


class ReconcilerStartupClass(StrEnum):
    ALWAYS_SAFE = "always_safe"
    PRE_READINESS_BOUNDED = "pre_readiness_bounded"
    PROJECT_READINESS_DEPENDENT = "project_readiness_dependent"
    OPERATOR_APPROVAL_REQUIRED = "operator_approval_required"


class ReconciliationGateState(StrEnum):
    WAITING = "waiting"
    ELIGIBLE = "eligible"
    RUNNING = "running"
    PAUSED = "paused"
    BLOCKED = "blocked"


class ReconcilerDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    service_id: str = Field(min_length=1)
    startup_class: ReconcilerStartupClass
    readiness_required: bool = False
    initial_approval_required: bool = False
    maintenance_incompatible: bool = False
    readiness_check: str | None = None
    description: str | None = None


class ReconciliationProjectControl(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    paused: bool = False
    paused_by: str | None = None
    paused_at: float | None = None
    pause_reason: str | None = None
    approved: bool = False
    approved_by: str | None = None
    approved_at: float | None = None
    approval_correlation_id: str | None = None
    last_start_at: float | None = None
    running_until: float | None = None
    last_stop_at: float | None = None
    last_completion_at: float | None = None
    last_cursor: str | None = None
    last_reason: str | None = None
    updated_at: float = Field(default_factory=time.time)


class ReconciliationMaintenanceLease(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1)
    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    owner: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    acquired_at: float = Field(default_factory=time.time)
    expires_at: float


class ReconciliationGateDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    service_id: str
    project_id: str
    state: ReconciliationGateState
    eligible: bool
    reason_code: str
    reason: str
    readiness_check: str | None = None
    approval_required: bool = False
    approved: bool = False
    paused: bool = False
    maintenance_blocked: bool = False
    last_start_at: float | None = None
    last_stop_at: float | None = None
    last_completion_at: float | None = None
    cursor: str | None = None


class ReconciliationGateAuditEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    service_id: str
    project_id: str
    organization_id: str
    workspace_id: str
    actor_identity_id: str
    action: str
    correlation_id: str | None = None
    reason: str | None = None
    created_at: float = Field(default_factory=time.time)


class ReconciliationGateStoreState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = RECONCILIATION_GATE_VERSION
    controls: list[ReconciliationProjectControl] = Field(default_factory=list)
    maintenance: list[ReconciliationMaintenanceLease] = Field(default_factory=list)
    audit: list[ReconciliationGateAuditEvent] = Field(default_factory=list)
