from __future__ import annotations

import time
import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


PROJECT_READINESS_VERSION = "1.0"


class ReadinessCheckStatus(StrEnum):
    READY = "ready"
    WARNING = "warning"
    BLOCKED = "blocked"
    NOT_APPLICABLE = "not_applicable"


class ProjectReadinessCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    domain: str = Field(min_length=1)
    status: ReadinessCheckStatus
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    affected_type: str | None = None
    affected_id: str | None = None
    remediation: str | None = None
    remediation_route: str | None = None
    required: bool = True
    details: dict[str, Any] = Field(default_factory=dict)


class ProjectReadinessSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = PROJECT_READINESS_VERSION
    correlation_id: str = Field(
        default_factory=lambda: f"readiness-{uuid.uuid4().hex}"
    )
    project_id: str = Field(min_length=1)
    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    semantic_ready: bool
    execution_ready: bool
    status: ReadinessCheckStatus
    checks: tuple[ProjectReadinessCheck, ...]
    bootstrap_version: str | None = None
    bootstrap_execution_id: str | None = None
    migration_version: str | None = None
    last_successful_verification_at: float | None = None
    generated_at: float = Field(default_factory=time.time)

    @property
    def blockers(self) -> tuple[ProjectReadinessCheck, ...]:
        return tuple(
            item
            for item in self.checks
            if item.status == ReadinessCheckStatus.BLOCKED
        )

    @property
    def warnings(self) -> tuple[ProjectReadinessCheck, ...]:
        return tuple(
            item
            for item in self.checks
            if item.status == ReadinessCheckStatus.WARNING
        )


class ProjectReadinessRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    organization_id: str
    workspace_id: str
    last_successful_verification_at: float | None = None
    last_status: ReadinessCheckStatus | None = None
    last_correlation_id: str | None = None
    updated_at: float = Field(default_factory=time.time)


class ProjectReadinessState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = PROJECT_READINESS_VERSION
    records: list[ProjectReadinessRecord] = Field(default_factory=list)
