from __future__ import annotations

import time
import uuid
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.compatibility import ContractSpec
from codex_web.execution_subjects import (
    ExecutionSubject,
    normalize_execution_subject,
)
from codex_web.models import ApprovalPolicy, SandboxMode


EXECUTION_WORKER_CONTRACT = ContractSpec(
    "execution-worker-state",
    "1.3",
    ("1.0", "1.1", "1.2", "1.3"),
)


class WorkerLifecycle(StrEnum):
    ACTIVE = "active"
    DRAINING = "draining"
    QUARANTINED = "quarantined"
    REVOKED = "revoked"
    OFFLINE = "offline"


class AssignmentStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    LOST = "lost"


class WorkerCapability(StrEnum):
    GIT = "git"
    COMMAND_EXECUTION = "command_execution"
    CONTAINER = "container"
    NETWORK = "network"
    ARTIFACT_UPLOAD = "artifact_upload"


class NetworkPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    allowed_hosts: tuple[str, ...] = ()

    @model_validator(mode="after")
    def normalize(self) -> "NetworkPolicy":
        normalized = tuple(
            sorted({value.strip().lower() for value in self.allowed_hosts if value.strip()})
        )
        object.__setattr__(self, "allowed_hosts", normalized)
        if not self.enabled and self.allowed_hosts:
            raise ValueError("network allowlist requires network enabled")
        return self


class WorkerResourceLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cpu_seconds: int = Field(default=900, ge=1, le=86400)
    memory_bytes: int = Field(default=2 * 1024 * 1024 * 1024, ge=64 * 1024 * 1024)
    disk_bytes: int = Field(default=20 * 1024 * 1024 * 1024, ge=64 * 1024 * 1024)
    process_count: int = Field(default=256, ge=1, le=65536)
    wall_seconds: int = Field(default=1800, ge=1, le=86400)


class ExecutionWorker(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(default_factory=lambda: f"worker-{uuid.uuid4().hex}")
    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    service_identity_id: str = Field(min_length=1)
    pool: str = Field(default="local", min_length=1)
    version: str = Field(min_length=1)
    capabilities: tuple[WorkerCapability, ...]
    supported_execution_contract_versions: tuple[str, ...] = ("1.0",)
    lifecycle: WorkerLifecycle = WorkerLifecycle.ACTIVE
    max_concurrency: int = Field(default=1, ge=1, le=128)
    registered_by: str = Field(min_length=1)
    registered_at: float = Field(default_factory=time.time)
    last_heartbeat_at: float = Field(default_factory=time.time)
    quarantine_reason: str | None = None
    revoked_at: float | None = None

    @model_validator(mode="after")
    def normalize(self) -> "ExecutionWorker":
        self.capabilities = tuple(sorted(set(self.capabilities), key=lambda value: value.value))
        self.supported_execution_contract_versions = tuple(
            dict.fromkeys(
                item.strip()
                for item in self.supported_execution_contract_versions
                if item.strip()
            )
        )
        if not self.supported_execution_contract_versions:
            raise ValueError("worker requires at least one supported execution contract version")
        if not self.capabilities:
            raise ValueError("worker requires at least one capability")
        return self


class ExecutionWorkerRegister(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    service_identity_id: str = Field(min_length=1)
    pool: str = Field(default="local", min_length=1)
    version: str = Field(min_length=1)
    capabilities: tuple[WorkerCapability, ...]
    supported_execution_contract_versions: tuple[str, ...] = ("1.0",)
    max_concurrency: int = Field(default=1, ge=1, le=128)


class ExecutionRuntimeBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    provider_id: str = Field(min_length=1)
    runtime_id: str = Field(min_length=1)
    capability_revision: int = Field(ge=1)


class ExecutionAssignmentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    subject: ExecutionSubject | None = None
    work_item_ref: str | None = None
    execution_id: str = Field(min_length=1)
    project_id: str | None = None
    resource_ids: tuple[str, ...]
    base_revision: str | None = None
    execution_contract_version: str = Field(min_length=1)
    required_capabilities: tuple[WorkerCapability, ...]
    sandbox: SandboxMode = "workspace-write"
    approval_policy: ApprovalPolicy = "on-request"
    network: NetworkPolicy = Field(default_factory=NetworkPolicy)
    limits: WorkerResourceLimits = Field(default_factory=WorkerResourceLimits)
    secret_refs: tuple[str, ...] = ()
    deadline_at: float | None = None
    expected_artifact_types: tuple[str, ...] = ()
    expected_evidence_types: tuple[str, ...] = ()
    execution_workspace_id: str | None = None
    runtime_binding: ExecutionRuntimeBinding | None = None

    @model_validator(mode="after")
    def normalize(self) -> "ExecutionAssignmentCreate":
        self.subject, self.work_item_ref = normalize_execution_subject(
            self.subject,
            self.work_item_ref,
        )
        self.resource_ids = tuple(dict.fromkeys(item for item in self.resource_ids if item))
        self.required_capabilities = tuple(
            sorted(set(self.required_capabilities), key=lambda value: value.value)
        )
        self.secret_refs = tuple(dict.fromkeys(item for item in self.secret_refs if item))
        if not self.resource_ids:
            raise ValueError("assignment requires at least one resource")
        if not self.required_capabilities:
            raise ValueError("assignment requires at least one worker capability")
        if self.network.enabled and WorkerCapability.NETWORK not in self.required_capabilities:
            raise ValueError("network-enabled assignment requires network capability")
        return self


class AssignmentLease(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    worker_id: str
    fence: int = Field(ge=1)
    lease_token: str = Field(min_length=20)
    acquired_at: float
    expires_at: float
    renewed_at: float | None = None


class ExecutionAssignment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"assignment-{uuid.uuid4().hex}")
    organization_id: str
    workspace_id: str
    subject: ExecutionSubject | None = None
    work_item_ref: str | None = None
    execution_id: str
    project_id: str | None = None
    resource_ids: tuple[str, ...]
    base_revision: str | None = None
    execution_contract_version: str
    required_capabilities: tuple[WorkerCapability, ...]
    sandbox: SandboxMode
    approval_policy: ApprovalPolicy
    network: NetworkPolicy
    limits: WorkerResourceLimits
    secret_refs: tuple[str, ...] = ()
    deadline_at: float | None = None
    expected_artifact_types: tuple[str, ...] = ()
    expected_evidence_types: tuple[str, ...] = ()
    execution_workspace_id: str | None = None
    runtime_binding: ExecutionRuntimeBinding | None = None
    status: AssignmentStatus = AssignmentStatus.PENDING
    fence: int = Field(default=0, ge=0)
    lease: AssignmentLease | None = None
    assigned_worker_id: str | None = None
    created_by: str
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None
    failure_code: str | None = None
    failure_message: str | None = None
    artifact_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def normalize_subject(self) -> "ExecutionAssignment":
        self.subject, self.work_item_ref = normalize_execution_subject(
            self.subject,
            self.work_item_ref,
        )
        return self


class AssignmentClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lease_seconds: int = Field(default=120, ge=10, le=3600)


class AssignmentRenewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lease_token: str = Field(min_length=20)
    fence: int = Field(ge=1)
    lease_seconds: int = Field(default=120, ge=10, le=3600)


class AssignmentStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lease_token: str = Field(min_length=20)
    fence: int = Field(ge=1)


class AssignmentCompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    lease_token: str = Field(min_length=20)
    fence: int = Field(ge=1)
    succeeded: bool
    failure_code: str | None = None
    failure_message: str | None = None
    artifact_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()


class WorkerHeartbeatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str | None = None


class WorkerLifecycleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = None


class WorkerEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"workerevent-{uuid.uuid4().hex}")
    organization_id: str
    workspace_id: str
    event_type: str
    worker_id: str | None = None
    assignment_id: str | None = None
    actor_id: str | None = None
    details: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    occurred_at: float = Field(default_factory=time.time)


class ExecutionWorkerState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = EXECUTION_WORKER_CONTRACT.current
    workers: list[ExecutionWorker] = Field(default_factory=list)
    assignments: list[ExecutionAssignment] = Field(default_factory=list)
    events: list[WorkerEvent] = Field(default_factory=list)
