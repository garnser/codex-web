from __future__ import annotations

import hashlib
import re
import time
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.execution_subjects import ExecutionSubject, normalize_execution_subject


class ExecutionWorkspaceKind(StrEnum):
    GIT_WORKTREE = "git_worktree"
    RESOURCE_LEASE = "resource_lease"


class ExecutionWorkspaceStatus(StrEnum):
    PROVISIONING = "provisioning"
    ACTIVE = "active"
    RELEASED = "released"
    ABANDONED = "abandoned"
    INTEGRATED = "integrated"
    CONFLICTED = "conflicted"
    DISCARDED = "discarded"
    ERROR = "error"


class LeaseMode(StrEnum):
    READ = "read"
    WRITE = "write"


class IntegrationStrategy(StrEnum):
    MERGE = "merge"
    REBASE = "rebase"
    FAST_FORWARD = "fast_forward"
    NONE = "none"


class IntegrationOutcome(StrEnum):
    PENDING = "pending"
    MERGED = "merged"
    REBASED = "rebased"
    FAST_FORWARDED = "fast_forwarded"
    CONFLICT = "conflict"
    DISCARDED = "discarded"


class WorkspaceQuota(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_active_per_tenant: int = Field(default=20, ge=1)
    max_active_per_identity: int = Field(default=8, ge=1)
    max_resources_per_workspace: int = Field(default=16, ge=1)
    max_requested_disk_bytes: int = Field(default=20 * 1024 * 1024 * 1024, ge=1)


class ExecutionWorkspaceLease(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    execution_workspace_id: str
    organization_id: str
    workspace_id: str
    subject: ExecutionSubject | None = None
    work_item_ref: str | None = None
    execution_id: str
    owner_identity_id: str
    resource_ids: tuple[str, ...]
    mode: LeaseMode
    acquired_at: float
    expires_at: float
    renewed_at: float | None = None
    released_at: float | None = None
    release_reason: str | None = None

    @model_validator(mode="after")
    def normalize_subject(self) -> "ExecutionWorkspaceLease":
        self.subject, self.work_item_ref = normalize_execution_subject(
            self.subject,
            self.work_item_ref,
        )
        return self

    @property
    def active(self) -> bool:
        return self.released_at is None and self.expires_at > time.time()


class WorkspaceIntegrationState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: IntegrationStrategy = IntegrationStrategy.NONE
    outcome: IntegrationOutcome = IntegrationOutcome.PENDING
    target_revision: str | None = None
    resulting_revision: str | None = None
    conflicts: tuple[str, ...] = ()
    recorded_at: float | None = None
    recorded_by: str | None = None


class ExecutionWorkspace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    organization_id: str
    workspace_id: str
    subject: ExecutionSubject | None = None
    work_item_ref: str | None = None
    execution_id: str
    project_id: str
    owner_identity_id: str
    kind: ExecutionWorkspaceKind
    resource_ids: tuple[str, ...]
    repository_resource_id: str | None = None
    lease_id: str
    path: str | None = None
    branch_name: str | None = None
    base_revision: str | None = None
    head_revision: str | None = None
    status: ExecutionWorkspaceStatus = ExecutionWorkspaceStatus.PROVISIONING
    requested_disk_bytes: int = 0
    actual_disk_bytes: int | None = None
    created_at: float
    updated_at: float
    cleaned_at: float | None = None
    error: str | None = None
    integration: WorkspaceIntegrationState = Field(default_factory=WorkspaceIntegrationState)

    @model_validator(mode="after")
    def normalize_subject(self) -> "ExecutionWorkspace":
        self.subject, self.work_item_ref = normalize_execution_subject(
            self.subject,
            self.work_item_ref,
        )
        return self


class ExecutionWorkspaceEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    event_type: str
    occurred_at: float = Field(default_factory=time.time)
    actor_identity_id: str | None = None
    details: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class ExecutionWorkspaceState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.1"
    workspaces: list[ExecutionWorkspace] = Field(default_factory=list)
    leases: list[ExecutionWorkspaceLease] = Field(default_factory=list)
    events: list[ExecutionWorkspaceEvent] = Field(default_factory=list)


class ExecutionWorkspaceAcquire(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    subject: ExecutionSubject | None = None
    work_item_ref: str | None = None
    execution_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    resource_ids: tuple[str, ...]
    repository_resource_id: str | None = None
    base_revision: str | None = None
    lease_mode: LeaseMode = LeaseMode.WRITE
    ttl_seconds: int = Field(default=1800, ge=30, le=86400)
    requested_disk_bytes: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def normalize(self) -> "ExecutionWorkspaceAcquire":
        self.subject, self.work_item_ref = normalize_execution_subject(
            self.subject,
            self.work_item_ref,
        )
        self.resource_ids = tuple(dict.fromkeys(item.strip() for item in self.resource_ids if item.strip()))
        if not self.resource_ids:
            raise ValueError("execution workspace requires at least one canonical resource")
        if self.repository_resource_id and self.repository_resource_id not in self.resource_ids:
            raise ValueError("repository_resource_id must be included in resource_ids")
        return self


class ExecutionWorkspaceRenew(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ttl_seconds: int = Field(default=1800, ge=30, le=86400)


class ExecutionWorkspaceRelease(BaseModel):
    model_config = ConfigDict(extra="forbid")

    discard: bool = False
    reason: str | None = None


class WorkspaceIntegrationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: IntegrationStrategy
    outcome: IntegrationOutcome
    target_revision: str | None = None
    resulting_revision: str | None = None
    conflicts: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_outcome(self) -> "WorkspaceIntegrationRecord":
        if self.outcome == IntegrationOutcome.CONFLICT and not self.conflicts:
            raise ValueError("conflict outcome requires explicit conflict details")
        if self.outcome in {
            IntegrationOutcome.MERGED,
            IntegrationOutcome.REBASED,
            IntegrationOutcome.FAST_FORWARDED,
        } and not self.resulting_revision:
            raise ValueError("successful integration outcome requires resulting_revision")
        return self


class ExecutionWorkspaceInspection(BaseModel):
    """Read-only operator projection joining workspace state to its lease."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace: ExecutionWorkspace
    lease: ExecutionWorkspaceLease | None = None
    lease_active: bool = False
    lease_expired: bool = False
    observed_at: float


class ExecutionWorkspaceReference(BaseModel):
    """Compact canonical execution-contract reference."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: str
    lease_id: str
    kind: ExecutionWorkspaceKind
    status: ExecutionWorkspaceStatus
    resource_ids: tuple[str, ...]
    path: str | None = None
    branch_name: str | None = None
    base_revision: str | None = None
    head_revision: str | None = None
    lease_expires_at: float | None = None


def deterministic_workspace_id(
    organization_id: str,
    workspace_id: str,
    work_item_ref: str | None,
    execution_id: str,
    *,
    subject: ExecutionSubject | None = None,
) -> str:
    normalized_subject, _ = normalize_execution_subject(subject, work_item_ref)
    raw = (
        f"{organization_id}\n{workspace_id}\n"
        f"{normalized_subject.key}\n{execution_id}"
    ).encode()
    return f"execws-{hashlib.sha256(raw).hexdigest()[:20]}"


def deterministic_branch_name(subject_ref: str, execution_id: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", subject_ref).strip("-._").lower()
    slug = slug[:64] or "work-item"
    suffix = hashlib.sha256(execution_id.encode()).hexdigest()[:10]
    return f"codex/{slug}/{suffix}"
