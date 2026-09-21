from __future__ import annotations

import hashlib
import json
import time
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from codex_web.canonical_materialization import CanonicalMaterializationPlan
from codex_web.legacy_project_migration import LegacyMigrationPlan
from codex_web.project_bootstrap import ProjectBootstrapManifest

PROJECT_BOOTSTRAP_ENGINE_VERSION = "1.0"


class BootstrapDisposition(StrEnum):
    READY = "ready"
    CREATE = "create"
    MIGRATE = "migrate"
    UPDATE = "update"
    SKIP = "skip"
    WARNING = "warning"
    BLOCKED = "blocked"


class BootstrapRollbackClass(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    REVERSIBLE = "reversible"
    COMPENSATING = "compensating"
    IRREVERSIBLE = "irreversible"


class BootstrapExecutionStatus(StrEnum):
    PLANNED = "planned"
    APPLYING = "applying"
    PARTIAL = "partial"
    APPLIED = "applied"
    BLOCKED = "blocked"
    FAILED = "failed"


class BootstrapCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(min_length=1)
    domain: str = Field(min_length=1)
    resource_ref: str = Field(min_length=1)
    disposition: BootstrapDisposition
    reason_code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    details: dict[str, Any] = Field(default_factory=dict)
    operator_action_required: bool = False


class BootstrapOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(min_length=1)
    domain: str = Field(min_length=1)
    resource_ref: str = Field(min_length=1)
    disposition: BootstrapDisposition
    reason_code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    current: dict[str, Any] = Field(default_factory=dict)
    desired: dict[str, Any] = Field(default_factory=dict)
    dependencies: tuple[str, ...] = ()
    operator_action_required: bool = False
    rollback: BootstrapRollbackClass = BootstrapRollbackClass.NOT_APPLICABLE
    provider: str = "none"
    provider_operation_id: str | None = None

    @property
    def applicable(self) -> bool:
        return self.disposition in {
            BootstrapDisposition.CREATE,
            BootstrapDisposition.MIGRATE,
            BootstrapDisposition.UPDATE,
        }


class ProjectBootstrapPreflight(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: str = PROJECT_BOOTSTRAP_ENGINE_VERSION
    project_id: str = Field(min_length=1)
    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    manifest_digest: str = Field(min_length=1)
    checks: tuple[BootstrapCheck, ...]
    generated_at: float = Field(default_factory=time.time)

    @property
    def blocked(self) -> bool:
        return any(
            item.disposition == BootstrapDisposition.BLOCKED
            for item in self.checks
        )


class ProjectBootstrapPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(min_length=1)
    version: str = PROJECT_BOOTSTRAP_ENGINE_VERSION
    project_id: str = Field(min_length=1)
    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    manifest: ProjectBootstrapManifest
    manifest_digest: str = Field(min_length=1)
    snapshot_digest: str = Field(min_length=1)
    migrate_legacy: bool = True
    preflight: ProjectBootstrapPreflight
    operations: tuple[BootstrapOperation, ...]
    canonical_materialization_plan: CanonicalMaterializationPlan | None = None
    legacy_migration_plan: LegacyMigrationPlan | None = None
    generated_at: float = Field(default_factory=time.time)

    @property
    def blockers(self) -> tuple[BootstrapOperation, ...]:
        return tuple(
            item
            for item in self.operations
            if item.disposition == BootstrapDisposition.BLOCKED
        )

    @property
    def applicable_operations(self) -> tuple[BootstrapOperation, ...]:
        return tuple(item for item in self.operations if item.applicable)

    def counts(self) -> dict[str, int]:
        return {
            value.value: sum(item.disposition == value for item in self.operations)
            for value in BootstrapDisposition
        }


class BootstrapAuditEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(min_length=1)
    execution_id: str = Field(min_length=1)
    operation_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    actor_identity_id: str = Field(min_length=1)
    outcome: str = Field(min_length=1)
    provider_execution_id: str | None = None
    recorded_at: float = Field(default_factory=time.time)


class ProjectBootstrapExecution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    plan_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    manifest_digest: str = Field(min_length=1)
    plan: ProjectBootstrapPlan
    status: BootstrapExecutionStatus = BootstrapExecutionStatus.PLANNED
    completed_operation_ids: tuple[str, ...] = ()
    audit_events: tuple[BootstrapAuditEvent, ...] = ()
    lease_owner: str | None = None
    lease_expires_at: float | None = None
    readiness: dict[str, Any] | None = None
    warnings: tuple[str, ...] = ()
    last_error_code: str | None = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    completed_at: float | None = None


class ProjectBootstrapState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = PROJECT_BOOTSTRAP_ENGINE_VERSION
    executions: list[ProjectBootstrapExecution] = Field(default_factory=list)


def stable_payload_digest(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def stable_bootstrap_id(prefix: str, payload: Any) -> str:
    return f"{prefix}-{stable_payload_digest(payload)[:24]}"
