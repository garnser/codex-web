from __future__ import annotations

import time
import uuid
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator
from codex_web.agent_profiles import AgentProfileExecutionBinding

from codex_web.compatibility import ContractSpec
from codex_web.definitions import DefinitionReference
from codex_web.execution_subjects import (
    ExecutionSubject,
    normalize_execution_subject,
)
from codex_web.failures import FailureRecord
from codex_web.models import ApprovalPolicy, SandboxMode
from codex_web.resources import RepositoryExecutionScope, RepositoryExecutionTarget


EXECUTION_WORKER_CONTRACT = ContractSpec(
    "execution-worker-state",
    "1.7",
    ("1.0", "1.1", "1.2", "1.3", "1.4", "1.5", "1.6", "1.7"),
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


class ExecutionWorkerEnrollmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    service_identity_id: str = Field(min_length=1)
    pool: str = Field(default="local", min_length=1)
    expires_in_seconds: int = Field(default=300, ge=30, le=1800)
    allowed_capabilities: tuple[WorkerCapability, ...]
    max_concurrency_ceiling: int = Field(default=1, ge=1, le=128)

    @model_validator(mode="after")
    def normalize(self) -> "ExecutionWorkerEnrollmentRequest":
        self.allowed_capabilities = tuple(
            sorted(set(self.allowed_capabilities), key=lambda value: value.value)
        )
        if not self.allowed_capabilities:
            raise ValueError("enrollment requires at least one allowed capability")
        return self


class ExecutionWorkerEnrollmentGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"worker-enrollment-{uuid.uuid4().hex}")
    organization_id: str
    workspace_id: str
    service_identity_id: str
    pool: str
    token_digest: str = Field(min_length=64, max_length=64)
    allowed_capabilities: tuple[WorkerCapability, ...]
    max_concurrency_ceiling: int = Field(ge=1, le=128)
    created_by: str
    created_at: float = Field(default_factory=time.time)
    expires_at: float
    used_at: float | None = None
    worker_id: str | None = None


class ExecutionWorkerEnroll(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    token: str = Field(min_length=20)
    version: str = Field(min_length=1)
    capabilities: tuple[WorkerCapability, ...]
    supported_execution_contract_versions: tuple[str, ...] = ("1.0",)
    max_concurrency: int = Field(default=1, ge=1, le=128)
    probe_results: dict[str, bool] = Field(default_factory=dict)

    @model_validator(mode="after")
    def normalize(self) -> "ExecutionWorkerEnroll":
        self.capabilities = tuple(
            sorted(set(self.capabilities), key=lambda value: value.value)
        )
        self.supported_execution_contract_versions = tuple(
            dict.fromkeys(
                item.strip()
                for item in self.supported_execution_contract_versions
                if item.strip()
            )
        )
        if not self.capabilities:
            raise ValueError("enrolled worker requires at least one capability")
        if not self.supported_execution_contract_versions:
            raise ValueError("enrolled worker requires an execution contract version")
        return self


class WorkerExecutionReadiness(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ready: bool
    code: str
    required_capabilities: tuple[WorkerCapability, ...] = ()
    available_capabilities: tuple[WorkerCapability, ...] = ()
    eligible_worker_ids: tuple[str, ...] = ()
    active_worker_ids: tuple[str, ...] = ()
    execution_contract_version: str
    reason: str
    remediation: str | None = None


class CodexExecutionAuthenticationMode(StrEnum):
    TRUSTED_LOCAL_SESSION = "trusted_local_session"
    DELEGATED_WORKER_TOKEN = "delegated_worker_token"
    API_KEY = "api_key"


class ExecutionRuntimeBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    provider_id: str = Field(min_length=1)
    runtime_id: str = Field(min_length=1)
    capability_revision: int = Field(ge=1)
    authentication_mode: CodexExecutionAuthenticationMode | None = None

    @model_serializer(mode="wrap")
    def _serialize_compatibly(self, handler):
        data = handler(self)
        if self.authentication_mode is None:
            data.pop("authentication_mode", None)
        return data


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
    repository_target: RepositoryExecutionTarget | None = None
    repository_scope: RepositoryExecutionScope | None = None
    execution_profile_id: str | None = None
    execution_profile_definition: DefinitionReference | None = None
    agent_profile: AgentProfileExecutionBinding | None = None

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
        if not self.resource_ids and not self.execution_profile_id:
            raise ValueError(
                "resource-free assignment requires an explicit execution profile"
            )
        if bool(self.execution_profile_id) != bool(self.execution_profile_definition):
            raise ValueError(
                "execution profile id and definition reference must be supplied together"
            )
        if not self.required_capabilities:
            raise ValueError("assignment requires at least one worker capability")
        if self.network.enabled and WorkerCapability.NETWORK not in self.required_capabilities:
            raise ValueError("network-enabled assignment requires network capability")
        if self.repository_target is not None:
            mutable_repository_id = self.repository_target.mutable_repository_id
            if (
                mutable_repository_id is not None
                and mutable_repository_id not in self.resource_ids
            ):
                raise ValueError(
                    "mutable repository target must be included in assignment resources"
                )
            if (
                self.project_id is not None
                and self.repository_target.project_id != self.project_id
            ):
                raise ValueError("repository target project does not match assignment")
        if self.repository_scope is not None:
            scope_ids = set(self.repository_scope.writable_repository_ids) | set(
                self.repository_scope.read_only_repository_ids
            )
            if scope_ids - set(self.resource_ids):
                raise ValueError(
                    "repository scope must be included in assignment resources"
                )
            if (
                self.project_id is not None
                and self.repository_scope.project_id != self.project_id
            ):
                raise ValueError("repository scope project does not match assignment")
            if self.repository_target is not None:
                if self.repository_scope.write_mode.value == "single":
                    expected = RepositoryExecutionScope.from_target(self.repository_target)
                    if self.repository_scope != expected:
                        raise ValueError(
                            "repository target and repository scope describe different authority"
                        )
                else:
                    target_repository_id = self.repository_target.mutable_repository_id
                    if (
                        target_repository_id is None
                        or target_repository_id
                        not in self.repository_scope.writable_repository_ids
                    ):
                        raise ValueError(
                            "repository target must identify a writable repository in coordinated scope"
                        )
                    if (
                        self.repository_target.organization_id
                        != self.repository_scope.organization_id
                        or self.repository_target.workspace_id
                        != self.repository_scope.workspace_id
                        or self.repository_target.project_id
                        != self.repository_scope.project_id
                    ):
                        raise ValueError(
                            "repository target and coordinated scope tenant/project do not match"
                        )
                    if set(self.repository_target.read_only_repository_ids) - set(
                        self.repository_scope.read_only_repository_ids
                    ):
                        raise ValueError(
                            "repository target read-only context exceeds coordinated scope"
                        )
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
    repository_target: RepositoryExecutionTarget | None = None
    repository_scope: RepositoryExecutionScope | None = None
    execution_profile_id: str | None = None
    execution_profile_definition: DefinitionReference | None = None
    agent_profile: AgentProfileExecutionBinding | None = None
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
    failure: FailureRecord | None = None
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
    enrollments: list[ExecutionWorkerEnrollmentGrant] = Field(default_factory=list)
