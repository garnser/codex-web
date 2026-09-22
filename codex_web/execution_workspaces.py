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
    SCRATCH = "scratch"


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


class RepositoryOutcomeStatus(StrEnum):
    PENDING = "pending"
    PARTIAL = "partial"
    COMPLETE = "complete"
    BLOCKED = "blocked"
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
    resource_modes: dict[str, LeaseMode] = Field(default_factory=dict)
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
        normalized = {
            resource_id: self.resource_modes.get(resource_id, self.mode)
            for resource_id in self.resource_ids
        }
        self.resource_modes = normalized
        return self

    @property
    def active(self) -> bool:
        return self.released_at is None and self.expires_at > time.time()


class ExecutionWorkspaceMember(BaseModel):
    """One repository checkout/snapshot included in an execution workspace."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    resource_id: str = Field(min_length=1)
    access_mode: LeaseMode
    source_path: str = Field(min_length=1)
    workspace_path: str = Field(min_length=1)
    sandbox_path: str = Field(min_length=1)
    branch_name: str | None = None
    base_revision: str = Field(min_length=1)
    head_revision: str = Field(min_length=1)
    disk_bytes: int = Field(default=0, ge=0)


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
    writable_repository_ids: tuple[str, ...] = ()
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
    repository_integrations: dict[str, WorkspaceIntegrationState] = Field(default_factory=dict)
    repository_outcome_status: RepositoryOutcomeStatus = RepositoryOutcomeStatus.PENDING
    repository_members: tuple[ExecutionWorkspaceMember, ...] = ()

    @model_validator(mode="after")
    def normalize_subject(self) -> "ExecutionWorkspace":
        self.subject, self.work_item_ref = normalize_execution_subject(
            self.subject,
            self.work_item_ref,
        )
        writable = tuple(
            dict.fromkeys(
                item.strip()
                for item in self.writable_repository_ids
                if item and item.strip()
            )
        )
        if not writable and self.repository_resource_id is not None:
            writable = (self.repository_resource_id,)
        if self.repository_resource_id is None and writable:
            self.repository_resource_id = writable[0]
        if (
            self.repository_resource_id is not None
            and self.repository_resource_id not in writable
        ):
            raise ValueError("primary repository must be included in writable repository ids")
        self.writable_repository_ids = writable

        member_ids = [item.resource_id for item in self.repository_members]
        if len(member_ids) != len(set(member_ids)):
            raise ValueError("execution workspace repository members must be unique")
        if self.repository_members:
            by_id = {item.resource_id: item for item in self.repository_members}
            missing_writable = set(writable) - set(by_id)
            if missing_writable:
                raise ValueError("writable repository member is missing")
            if len(writable) > 1:
                non_writable = [
                    resource_id
                    for resource_id in writable
                    if by_id[resource_id].access_mode != LeaseMode.WRITE
                ]
                if non_writable:
                    raise ValueError(
                        "coordinated writable repository members must use write leases"
                    )
        if self.repository_resource_id is not None and self.repository_members:
            mutable = [
                item for item in self.repository_members
                if item.resource_id == self.repository_resource_id
            ]
            if len(mutable) != 1:
                raise ValueError("mutable repository member is missing")
            if mutable[0].access_mode not in {LeaseMode.READ, LeaseMode.WRITE}:
                raise ValueError("invalid mutable repository member access")
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

    schema_version: str = "1.4"
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
    writable_repository_ids: tuple[str, ...] = ()
    read_only_repository_ids: tuple[str, ...] = ()
    scratch: bool = False
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
        if not self.resource_ids and not self.scratch:
            raise ValueError(
                "execution workspace requires a canonical resource unless scratch mode is explicit"
            )
        if self.scratch and self.resource_ids:
            raise ValueError("scratch execution workspace cannot lease canonical resources")
        self.writable_repository_ids = tuple(
            dict.fromkeys(
                item.strip()
                for item in self.writable_repository_ids
                if item and item.strip()
            )
        )
        self.read_only_repository_ids = tuple(
            dict.fromkeys(
                item.strip()
                for item in self.read_only_repository_ids
                if item and item.strip()
            )
        )
        if not self.writable_repository_ids and self.repository_resource_id is not None:
            self.writable_repository_ids = (self.repository_resource_id,)
        if self.repository_resource_id is None and self.writable_repository_ids:
            self.repository_resource_id = self.writable_repository_ids[0]
        if self.scratch and (
            self.repository_resource_id is not None
            or self.writable_repository_ids
            or self.read_only_repository_ids
            or self.base_revision is not None
        ):
            raise ValueError("scratch execution workspace cannot reference repositories")
        if self.repository_resource_id and self.repository_resource_id not in self.resource_ids:
            raise ValueError("repository_resource_id must be included in resource_ids")
        missing_writable = set(self.writable_repository_ids) - set(self.resource_ids)
        if missing_writable:
            raise ValueError("writable repository ids must be included in resource_ids")
        missing_read_only = set(self.read_only_repository_ids) - set(self.resource_ids)
        if missing_read_only:
            raise ValueError("read-only repository ids must be included in resource_ids")
        if set(self.writable_repository_ids) & set(self.read_only_repository_ids):
            raise ValueError("writable repositories cannot also be read-only context")
        if (
            self.repository_resource_id is not None
            and self.repository_resource_id not in self.writable_repository_ids
        ):
            raise ValueError("primary repository must be included in writable repository ids")
        if len(self.writable_repository_ids) > 1 and self.lease_mode != LeaseMode.WRITE:
            raise ValueError("coordinated writable repositories require a write lease")
        return self


class ExecutionWorkspaceRenew(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ttl_seconds: int = Field(default=1800, ge=30, le=86400)


class ExecutionWorkspaceRelease(BaseModel):
    model_config = ConfigDict(extra="forbid")

    discard: bool = False
    reason: str | None = None


class WorkspaceIntegrationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    repository_id: str | None = None
    strategy: IntegrationStrategy
    outcome: IntegrationOutcome
    target_revision: str | None = None
    resulting_revision: str | None = None
    conflicts: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_outcome(self) -> "WorkspaceIntegrationRecord":
        if self.repository_id == "":
            self.repository_id = None
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
    repository_members: tuple[ExecutionWorkspaceMember, ...] = ()
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
