from __future__ import annotations

import time
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class CompanyOperationsHealth(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


class CompanyFactDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    business_entity_id: str
    entity_name: str
    entity_type: str
    fact_key: str
    freshness: str
    conflict: bool
    selected_fact_id: str | None = None
    selected_value: str | float | int | bool | None = None
    unit: str | None = None
    provider: str | None = None
    external_record_ref_id: str | None = None
    classification: str | None = None
    candidate_fact_ids: tuple[str, ...] = ()
    conflict_fact_ids: tuple[str, ...] = ()
    stale_fact_ids: tuple[str, ...] = ()
    revoked_source_fact_ids: tuple[str, ...] = ()
    reason: str


class CompanySourceDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str
    name: str
    source_type: str
    provider_id: str
    provider_instance: str
    object_type: str
    entity_type: str
    status: str
    capabilities: tuple[str, ...] = ()
    credential_ref: str | None = None
    cursor: str | None = None
    checkpoint: str | None = None
    last_success_at: float | None = None
    last_error: str | None = None
    last_error_at: float | None = None
    projected_records: int = 0
    stale_events: int = 0
    duplicate_events: int = 0
    capacity_status: str | None = None
    capacity_reason: str | None = None
    retry_at: float | None = None
    consecutive_failures: int = 0
    drift_count: int = 0
    conflict_count: int = 0
    health: CompanyOperationsHealth
    issues: tuple[str, ...] = ()


class CompanyOperationsCounts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    business_entities: int = 0
    external_records: int = 0
    fact_diagnostics: int = 0
    sources: int = 0
    kpis: int = 0
    goals: int = 0
    decisions: int = 0
    executive_activations: int = 0
    attention_items: int = 0
    pending_approvals: int = 0
    action_intents: int = 0


class CompanyOperationsOverview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    organization_id: str
    workspace_id: str
    evaluated_at: float = Field(default_factory=time.time)
    overall_health: CompanyOperationsHealth
    counts: CompanyOperationsCounts
    business_domains: tuple[str, ...] = ()
    business_entities: tuple[dict[str, Any], ...] = ()
    fact_diagnostics: tuple[CompanyFactDiagnostic, ...] = ()
    external_records: tuple[dict[str, Any], ...] = ()
    sources: tuple[CompanySourceDiagnostic, ...] = ()
    kpi_view: dict[str, Any] = Field(default_factory=dict)
    goals: tuple[dict[str, Any], ...] = ()
    decisions: tuple[dict[str, Any], ...] = ()
    executive_activations: tuple[dict[str, Any], ...] = ()
    attention_items: tuple[dict[str, Any], ...] = ()
    approval_requests: tuple[dict[str, Any], ...] = ()
    action_intents: tuple[dict[str, Any], ...] = ()
    blockers: tuple[str, ...] = ()


class CompanyOperationsExplainStage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str
    label: str
    object_id: str | None = None
    status: str | None = None
    summary: str | None = None
    refs: tuple[str, ...] = ()
    details: dict[str, Any] = Field(default_factory=dict)


class CompanyOperationsExplain(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    organization_id: str
    workspace_id: str
    subject_type: str
    subject_id: str
    stages: tuple[CompanyOperationsExplainStage, ...]
    unresolved: tuple[str, ...] = ()
    evaluated_at: float = Field(default_factory=time.time)
