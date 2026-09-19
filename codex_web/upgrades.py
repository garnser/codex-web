from __future__ import annotations

import time
import uuid
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class UpgradeDeploymentMode(StrEnum):
    LOCAL = "local"
    SHARED = "shared"
    REPLICATED = "replicated"


class UpgradePhase(StrEnum):
    EXPAND = "expand"
    ROLLOUT = "rollout"
    VERIFY = "verify"
    CONTRACT = "contract"


class UpgradeStepKind(StrEnum):
    STATE_SCHEMA = "state_schema"
    DEFINITION = "definition"
    CONTROL_PLANE = "control_plane"
    WORKER = "worker"
    EXTENSION = "extension"
    VERIFICATION = "verification"
    CLEANUP = "cleanup"


class UpgradeStepStatus(StrEnum):
    PENDING = "pending"
    AWAITING_APPROVAL = "awaiting_approval"
    READY = "ready"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class UpgradeStatus(StrEnum):
    DRAFT = "draft"
    PREFLIGHT_BLOCKED = "preflight_blocked"
    READY = "ready"
    DRAINING = "draining"
    IN_PROGRESS = "in_progress"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


class UpgradeCompatibilityProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    source_app_version: str = Field(min_length=1)
    target_app_version: str = Field(min_length=1)
    deployment_mode: UpgradeDeploymentMode = UpgradeDeploymentMode.LOCAL
    supported_control_plane_versions_during_rollout: tuple[str, ...]
    supported_worker_versions_during_rollout: tuple[str, ...] = ()
    supported_execution_contract_versions: tuple[str, ...] = ("1.0",)
    source_state_schema_version: int = Field(ge=0)
    target_state_schema_version: int = Field(ge=0)
    supported_event_contract_versions: tuple[str, ...] = ("1.0",)
    supported_api_contract_versions: tuple[str, ...] = ("1.0",)
    target_definition_engine_version: str = Field(min_length=1)
    target_definition_schemas: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    target_extension_host_version: str = Field(min_length=1)
    rollback_supported_to_app_version: str | None = None

    @model_validator(mode="after")
    def normalize(self) -> "UpgradeCompatibilityProfile":
        for name in (
            "supported_control_plane_versions_during_rollout",
            "supported_worker_versions_during_rollout",
            "supported_execution_contract_versions",
            "supported_event_contract_versions",
            "supported_api_contract_versions",
        ):
            object.__setattr__(
                self,
                name,
                tuple(dict.fromkeys(item.strip() for item in getattr(self, name) if item.strip())),
            )
        object.__setattr__(
            self,
            "target_definition_schemas",
            {
                kind.strip(): tuple(dict.fromkeys(version.strip() for version in versions if version.strip()))
                for kind, versions in self.target_definition_schemas.items()
                if kind.strip()
            },
        )
        if self.source_app_version == self.target_app_version:
            raise ValueError("upgrade source and target application versions must differ")
        if not self.supported_control_plane_versions_during_rollout:
            raise ValueError("upgrade must declare control-plane rollout compatibility")
        return self


class UpgradeStepCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    id: str = Field(min_length=1, max_length=200)
    phase: UpgradePhase
    kind: UpgradeStepKind
    description: str = Field(min_length=1, max_length=4000)
    handler_id: str | None = Field(default=None, max_length=200)
    idempotent: bool = True
    reversible: bool = True
    irreversible: bool = False
    requires_drain: bool = False
    requires_backup: bool = False

    @model_validator(mode="after")
    def validate_safety(self) -> "UpgradeStepCreate":
        if self.irreversible and self.reversible:
            raise ValueError("irreversible upgrade step cannot be reversible")
        if self.phase == UpgradePhase.CONTRACT and self.irreversible and not self.requires_backup:
            raise ValueError("irreversible contract/cleanup step must require backup")
        return self


class UpgradeStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    phase: UpgradePhase
    kind: UpgradeStepKind
    description: str
    handler_id: str | None = None
    idempotent: bool
    reversible: bool
    irreversible: bool
    requires_drain: bool
    requires_backup: bool
    status: UpgradeStepStatus = UpgradeStepStatus.PENDING
    approval_request_id: str | None = None
    attempts: int = Field(default=0, ge=0)
    last_error: str | None = None
    result: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    evidence_id: str | None = None
    started_at: float | None = None
    completed_at: float | None = None

    @classmethod
    def from_create(cls, payload: UpgradeStepCreate) -> "UpgradeStep":
        return cls(**payload.model_dump())


class UpgradeDefinitionBaseline(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    record_id: str
    definition_id: str
    kind: str
    revision: int = Field(ge=1)
    definition_schema_version: str
    checksum: str = Field(min_length=64, max_length=64)


class DefinitionMigrationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    migration_id: str = Field(min_length=1)
    from_record_id: str = Field(min_length=1)
    from_checksum: str = Field(min_length=64, max_length=64)
    to_record_id: str = Field(min_length=1)
    to_checksum: str = Field(min_length=64, max_length=64)
    reason: str = Field(min_length=1, max_length=4000)
    actor_id: str = Field(min_length=1)
    recorded_at: float = Field(default_factory=time.time)


class UpgradePreflight(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    satisfied: bool
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    state_schema_version: int = Field(ge=0)
    recovery_qualified: bool
    active_action_intents: int = Field(ge=0)
    active_worker_assignments: int = Field(ge=0)
    incompatible_worker_ids: tuple[str, ...] = ()
    incompatible_extension_ids: tuple[str, ...] = ()
    incompatible_definition_record_ids: tuple[str, ...] = ()
    definition_baseline: tuple[UpgradeDefinitionBaseline, ...] = ()
    evaluated_at: float = Field(default_factory=time.time)


class UpgradePlanCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    project_id: str | None = None
    release_id: str = Field(min_length=1)
    current_app_version: str = Field(min_length=1)
    target_app_version: str = Field(min_length=1)
    compatibility: UpgradeCompatibilityProfile
    observed_control_plane_versions: tuple[str, ...]
    steps: tuple[UpgradeStepCreate, ...]
    require_recovery_qualification: bool = True
    require_drain: bool = True

    @model_validator(mode="after")
    def validate_profile(self) -> "UpgradePlanCreate":
        if self.current_app_version != self.compatibility.source_app_version:
            raise ValueError("current application version does not match compatibility profile")
        if self.target_app_version != self.compatibility.target_app_version:
            raise ValueError("target application version does not match compatibility profile")
        if len({step.id for step in self.steps}) != len(self.steps):
            raise ValueError("upgrade step ids must be unique")
        phase_order = {
            UpgradePhase.EXPAND: 0,
            UpgradePhase.ROLLOUT: 1,
            UpgradePhase.VERIFY: 2,
            UpgradePhase.CONTRACT: 3,
        }
        values = [phase_order[step.phase] for step in self.steps]
        if values != sorted(values):
            raise ValueError("upgrade steps must be ordered expand -> rollout -> verify -> contract")
        return self


class UpgradePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"upgrade-{uuid.uuid4().hex}")
    organization_id: str
    workspace_id: str
    project_id: str | None = None
    release_id: str
    current_app_version: str
    target_app_version: str
    compatibility: UpgradeCompatibilityProfile
    observed_control_plane_versions: tuple[str, ...]
    steps: tuple[UpgradeStep, ...]
    require_recovery_qualification: bool = True
    require_drain: bool = True
    status: UpgradeStatus = UpgradeStatus.DRAFT
    preflight: UpgradePreflight | None = None
    maintenance_mode: bool = False
    drain_started_at: float | None = None
    irreversible_boundary_crossed: bool = False
    rollback_available: bool = True
    definition_migrations: tuple[DefinitionMigrationRecord, ...] = ()
    pre_upgrade_backup_id: str | None = None
    post_upgrade_evidence_id: str | None = None
    rollback_evidence_id: str | None = None
    created_by: str
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    completed_at: float | None = None


class UpgradeStepExecute(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    idempotency_key: str = Field(min_length=1, max_length=500)


class UpgradeDefinitionMigrationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    migration_id: str = Field(min_length=1)
    from_record_id: str = Field(min_length=1)
    to_record_id: str = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=4000)


class UpgradeState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    plans: dict[str, UpgradePlan] = Field(default_factory=dict)
