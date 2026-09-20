from __future__ import annotations

import time
import uuid
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.compatibility import ContractSpec
from codex_web.models import SandboxMode


LEGACY_PROJECT_MIGRATION_CONTRACT = ContractSpec(
    "legacy-project-migration-state",
    "1.0",
    ("1.0",),
)


class MigrationDisposition(StrEnum):
    UNCHANGED = "unchanged"
    CONVERT = "convert"
    BLOCKED = "blocked"
    APPROVAL_REQUIRED = "approval_required"


class MigrationApplyStatus(StrEnum):
    PLANNED = "planned"
    APPLYING = "applying"
    APPLIED = "applied"
    PARTIAL = "partial"
    BLOCKED = "blocked"


class LegacyRepositoryProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(min_length=1)
    name: str = Field(min_length=1)
    absolute_path: str = Field(min_length=1)
    relative_path: str = Field(min_length=1)
    existing_resource_id: str | None = None
    proposed_resource_id: str | None = None
    default_candidate: bool = False


class AuthorityDifference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    material_change: bool = False
    summary: tuple[str, ...] = ()
    requires_operator_approval: bool = False


class LegacyThreadProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    thread_id: str = Field(min_length=1)
    name: str | None = None
    indexed_cwd: str | None = None
    bot_binding_ids: tuple[str, ...] = ()
    current_sandbox: SandboxMode | None = None
    current_repository_resource_id: str | None = None
    current_execution_profile_id: str | None = None
    proposed_sandbox: SandboxMode | None = None
    proposed_repository_key: str | None = None
    proposed_repository_resource_id: str | None = None
    proposed_execution_profile_id: str | None = None
    disposition: MigrationDisposition
    reason_code: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    authority_difference: AuthorityDifference = Field(
        default_factory=AuthorityDifference
    )


class LegacyPathCompatibilityMapping(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    legacy_path: str = Field(min_length=1)
    resource_id: str = Field(min_length=1)
    canonical_path: str = Field(min_length=1)
    expires_at: float
    created_at: float = Field(default_factory=time.time)


class LegacyMigrationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: f"migration-plan-{uuid.uuid4().hex}")
    version: str = "1.0"
    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    project_path: str = Field(min_length=1)
    generated_at: float = Field(default_factory=time.time)
    repositories: tuple[LegacyRepositoryProposal, ...] = ()
    threads: tuple[LegacyThreadProposal, ...] = ()
    blockers: tuple[str, ...] = ()
    requires_approval: bool = False
    summary: dict[str, int] = Field(default_factory=dict)
    rollback_boundary: str = (
        "Dry-run is fully reversible. After canonical Resources/bindings or thread "
        "execution settings are applied, rollback is compensating-only; native Codex "
        "thread history/IDs are never rewritten by this migration."
    )

    @model_validator(mode="after")
    def classify(self) -> "LegacyMigrationPlan":
        requires_approval = any(
            item.disposition == MigrationDisposition.APPROVAL_REQUIRED
            for item in self.threads
        )
        summary = {
            "converted": sum(
                item.disposition == MigrationDisposition.CONVERT
                for item in self.threads
            ),
            "blocked": sum(
                item.disposition == MigrationDisposition.BLOCKED
                for item in self.threads
            ),
            "unchanged": sum(
                item.disposition == MigrationDisposition.UNCHANGED
                for item in self.threads
            ),
            "operator_action_required": sum(
                item.disposition == MigrationDisposition.APPROVAL_REQUIRED
                for item in self.threads
            ),
        }
        object.__setattr__(self, "requires_approval", requires_approval)
        object.__setattr__(self, "summary", summary)
        return self


class LegacyMigrationExecution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"migration-{uuid.uuid4().hex}")
    plan_id: str = Field(min_length=1)
    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    status: MigrationApplyStatus = MigrationApplyStatus.PLANNED
    plan: LegacyMigrationPlan
    approved_by: str | None = None
    approved_at: float | None = None
    applied_operation_ids: tuple[str, ...] = ()
    compatibility_mappings: tuple[LegacyPathCompatibilityMapping, ...] = ()
    error: str | None = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


class LegacyProjectMigrationState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = LEGACY_PROJECT_MIGRATION_CONTRACT.current
    executions: list[LegacyMigrationExecution] = Field(default_factory=list)
