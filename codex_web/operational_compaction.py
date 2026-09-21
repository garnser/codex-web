from __future__ import annotations

import time
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


OPERATIONAL_COMPACTION_VERSION = "1.0"


class CompactionExecutionStatus(StrEnum):
    BACKUP_READY = "backup_ready"
    APPLYING = "applying"
    APPLIED = "applied"
    PARTIAL = "partial"
    ROLLED_BACK = "rolled_back"


class OperationalStoreInspection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    store: str
    source: str
    records: int | None = None
    bytes: int | None = None
    estimated_records: int | None = None
    sampled_bytes: int | None = None
    truncated: bool = False
    pathology: tuple[str, ...] = ()
    details: dict[str, Any] = Field(default_factory=dict)


class OperationalStateInspection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = OPERATIONAL_COMPACTION_VERSION
    project_id: str
    stores: tuple[OperationalStoreInspection, ...]
    generated_at: float = Field(default_factory=time.time)


class DeliveryTargetCompactionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    version: str = OPERATIONAL_COMPACTION_VERSION
    project_id: str
    organization_id: str
    workspace_id: str
    source_revision: float | None = None
    source_checksum: str
    target_checksum: str
    source_records: int
    retained_records: int
    removed_records: int
    created_or_updated_records: int
    protected_records: int
    malformed_records: int
    ambiguous_records: int
    canonical_bindings: int
    bindings_without_target: int
    estimated_bytes_before: int | None = None
    generated_at: float = Field(default_factory=time.time)
    rollback_boundary: str = (
        "Apply replaces only the canonical delivery-target registry after a "
        "verified private backup exists. Rollback restores that exact backup."
    )


class OperationalCompactionAudit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    project_id: str
    organization_id: str
    workspace_id: str
    actor_identity_id: str
    action: str
    plan_id: str
    execution_id: str | None = None
    backup_path: str | None = None
    created_at: float = Field(default_factory=time.time)


class DeliveryTargetCompactionExecution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    plan_id: str
    project_id: str
    organization_id: str
    workspace_id: str
    status: CompactionExecutionStatus
    source_checksum: str
    target_checksum: str
    backup_path: str
    backup_checksum: str
    source_records: int
    target_records: int
    removed_records: int
    rollback_instructions: str
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    completed_at: float | None = None
    last_error: str | None = None


class OperationalCompactionState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = OPERATIONAL_COMPACTION_VERSION
    executions: list[DeliveryTargetCompactionExecution] = Field(
        default_factory=list
    )
    audit: list[OperationalCompactionAudit] = Field(default_factory=list)
