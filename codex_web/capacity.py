from __future__ import annotations

import time
import uuid
from enum import IntEnum, StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class WorkloadKind(StrEnum):
    API = "api"
    CANONICAL_EVENT = "canonical_event"
    SCHEDULER = "scheduler"
    MODEL = "model"
    ACTION = "action"
    EVIDENCE = "evidence"
    RECONCILIATION = "reconciliation"
    RECOVERY = "recovery"
    INCIDENT = "incident"
    APPROVAL = "approval"
    SECURITY = "security"


class WorkloadPriority(IntEnum):
    LOW = 10
    NORMAL = 20
    HIGH = 30
    CRITICAL = 40


class CircuitStatus(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CapacityPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_global_inflight: int = Field(default=128, ge=1, le=100000)
    max_tenant_inflight: int = Field(default=32, ge=1, le=10000)
    critical_global_reserve: int = Field(default=16, ge=0, le=10000)
    critical_tenant_reserve: int = Field(default=4, ge=0, le=1000)
    load_shed_threshold: float = Field(default=0.85, gt=0.0, le=1.0)
    low_priority_shed_threshold: float = Field(default=0.65, gt=0.0, le=1.0)
    default_lease_seconds: int = Field(default=120, ge=5, le=86400)
    workload_limits: dict[WorkloadKind, int] = Field(
        default_factory=lambda: {
            WorkloadKind.MODEL: 16,
            WorkloadKind.ACTION: 16,
            WorkloadKind.EVIDENCE: 16,
            WorkloadKind.RECOVERY: 4,
            WorkloadKind.INCIDENT: 16,
            WorkloadKind.RECONCILIATION: 16,
        }
    )
    recovery_admissions_per_minute: int = Field(default=30, ge=1, le=10000)
    circuit_failure_threshold: int = Field(default=5, ge=1, le=1000)
    circuit_cooldown_seconds: int = Field(default=60, ge=1, le=86400)
    max_history: int = Field(default=5000, ge=100, le=100000)

    @model_validator(mode="after")
    def validate_reserves(self) -> "CapacityPolicy":
        if self.critical_global_reserve >= self.max_global_inflight:
            raise ValueError(
                "critical_global_reserve must be below max_global_inflight"
            )
        if self.critical_tenant_reserve >= self.max_tenant_inflight:
            raise ValueError(
                "critical_tenant_reserve must be below max_tenant_inflight"
            )
        return self


class CapacityLease(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: f"capacity-lease-{uuid.uuid4().hex}")
    organization_id: str
    workspace_id: str
    workload: WorkloadKind
    priority: WorkloadPriority
    owner_ref: str
    component_key: str | None = None
    acquired_at: float = Field(default_factory=time.time)
    expires_at: float


class CapacityAdmissionEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    organization_id: str
    workspace_id: str
    workload: WorkloadKind
    priority: WorkloadPriority
    outcome: str
    reason: str | None = None
    observed_at: float = Field(default_factory=time.time)


class CircuitRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    organization_id: str
    workspace_id: str
    component_key: str
    status: CircuitStatus = CircuitStatus.CLOSED
    consecutive_failures: int = Field(default=0, ge=0)
    opened_at: float | None = None
    retry_at: float | None = None
    last_failure_reason: str | None = None
    last_success_at: float | None = None
    updated_at: float = Field(default_factory=time.time)


class CapacityQualificationReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    deployment_mode: str = Field(min_length=1, max_length=100)
    workload: WorkloadKind
    target_concurrency: int = Field(ge=1)
    burst_size: int = Field(ge=1)
    duration_seconds: float = Field(gt=0)
    p95_latency_seconds: float = Field(ge=0)
    error_rate: float = Field(ge=0.0, le=1.0)
    lost_work_count: int = Field(ge=0)
    retry_amplification: float = Field(ge=0.0)
    max_tenant_share: float = Field(ge=0.0, le=1.0)
    queue_or_admission_saturation_observed: bool = False
    source: str = Field(default="capacity-test", min_length=1)
    observed_at: float = Field(default_factory=time.time)


class CapacityQualification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"capacity-qualification-{uuid.uuid4().hex}")
    report: CapacityQualificationReport
    passed: bool
    blockers: tuple[str, ...] = ()
    evidence_id: str | None = None
    evaluated_at: float = Field(default_factory=time.time)


class CapacityHealth(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    active_global: int = Field(ge=0)
    active_tenant: int = Field(ge=0)
    global_utilization: float = Field(ge=0.0)
    tenant_utilization: float = Field(ge=0.0)
    load_shed_mode: bool
    open_circuits: tuple[str, ...] = ()
    recent_admitted: int = Field(ge=0)
    recent_deferred: int = Field(ge=0)
    recent_rejected: int = Field(ge=0)


class CapacityState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    policy: CapacityPolicy = Field(default_factory=CapacityPolicy)
    leases: dict[str, CapacityLease] = Field(default_factory=dict)
    circuits: dict[str, CircuitRecord] = Field(default_factory=dict)
    history: list[CapacityAdmissionEvent] = Field(default_factory=list)
    qualifications: dict[str, CapacityQualification] = Field(default_factory=dict)
