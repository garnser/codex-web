from __future__ import annotations

import time
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


CANONICAL_MATERIALIZATION_VERSION = "1.0"


class MaterializationDisposition(StrEnum):
    MIGRATED = "migrated"
    UNCHANGED = "unchanged"
    SKIPPED = "skipped"
    UNRESOLVED = "unresolved"
    OPERATOR_ACTION_REQUIRED = "operator_action_required"


class MaterializationExecutionStatus(StrEnum):
    PLANNED = "planned"
    APPLYING = "applying"
    PARTIAL = "partial"
    APPLIED = "applied"
    BLOCKED = "blocked"


class CanonicalMaterializationOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    domain: str = Field(min_length=1)
    record_ref: str = Field(min_length=1)
    disposition: MaterializationDisposition
    reason_code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    apply_kind: str | None = None
    dependencies: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)
    operator_action: str | None = None

    @model_validator(mode="after")
    def no_secret_shaped_metadata(self) -> "CanonicalMaterializationOperation":
        forbidden = {
            "token",
            "password",
            "secret",
            "credential",
            "authorization",
            "cookie",
        }
        for key in self.metadata:
            normalized = str(key).casefold().replace("_id", "")
            if any(word == normalized for word in forbidden):
                raise ValueError(
                    "materialization metadata must contain references only, "
                    "not credential-shaped fields"
                )
        return self


class CanonicalMaterializationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    version: str = CANONICAL_MATERIALIZATION_VERSION
    project_id: str = Field(min_length=1)
    source_organization_id: str = Field(min_length=1)
    source_workspace_id: str = Field(min_length=1)
    target_organization_id: str = Field(min_length=1)
    target_workspace_id: str = Field(min_length=1)
    project_path: str = Field(min_length=1)
    operations: tuple[CanonicalMaterializationOperation, ...]
    generated_at: float = Field(default_factory=time.time)
    source_layout: str = "pre-docker/v0.1-compatible"

    @property
    def blockers(self) -> tuple[CanonicalMaterializationOperation, ...]:
        return tuple(
            item
            for item in self.operations
            if item.disposition
            in {
                MaterializationDisposition.UNRESOLVED,
                MaterializationDisposition.OPERATOR_ACTION_REQUIRED,
            }
        )

    def counts(self) -> dict[str, int]:
        return {
            value.value: sum(
                item.disposition == value
                for item in self.operations
            )
            for value in MaterializationDisposition
        }


class CanonicalMaterializationExecution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    plan_id: str = Field(min_length=1)
    version: str = CANONICAL_MATERIALIZATION_VERSION
    project_id: str = Field(min_length=1)
    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    plan: CanonicalMaterializationPlan
    status: MaterializationExecutionStatus = (
        MaterializationExecutionStatus.PLANNED
    )
    applied_operation_ids: tuple[str, ...] = ()
    records: tuple[CanonicalMaterializationOperation, ...] = ()
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    completed_at: float | None = None
    last_error: str | None = None

    def counts(self) -> dict[str, int]:
        records = self.records or self.plan.operations
        return {
            value.value: sum(
                item.disposition == value
                for item in records
            )
            for value in MaterializationDisposition
        }


class CanonicalMaterializationState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = CANONICAL_MATERIALIZATION_VERSION
    executions: list[CanonicalMaterializationExecution] = Field(
        default_factory=list
    )
