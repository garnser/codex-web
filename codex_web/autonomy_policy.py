from __future__ import annotations

import hashlib
import json
import time
import uuid
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.action_providers import ActionRiskClass
from codex_web.identity import AuthenticationAssurance


class AutonomyLevel(StrEnum):
    OBSERVE = "observe"
    RECOMMEND = "recommend"
    PREPARE = "prepare"
    EXECUTE_LOW_RISK = "execute_low_risk"
    EXECUTE_BOUNDED = "execute_bounded"
    EXECUTE_BROAD = "execute_broad"


AUTONOMY_LEVEL_RANK: dict[AutonomyLevel, int] = {
    AutonomyLevel.OBSERVE: 0,
    AutonomyLevel.RECOMMEND: 1,
    AutonomyLevel.PREPARE: 2,
    AutonomyLevel.EXECUTE_LOW_RISK: 3,
    AutonomyLevel.EXECUTE_BOUNDED: 4,
    AutonomyLevel.EXECUTE_BROAD: 5,
}

ACTION_RISK_RANK: dict[ActionRiskClass, int] = {
    ActionRiskClass.LOW: 0,
    ActionRiskClass.MEDIUM: 1,
    ActionRiskClass.HIGH: 2,
    ActionRiskClass.CRITICAL: 3,
}


class AutonomyQualificationGate(StrEnum):
    EVALUATION = "evaluation"
    OBSERVABILITY = "observability"
    RELEASE = "release"
    INCIDENT_READINESS = "incident_readiness"
    RECOVERY = "recovery"
    CAPACITY = "capacity"
    UPGRADE = "upgrade"
    AUDIT_INTEGRITY = "audit_integrity"
    RELIABILITY = "reliability"
    WORKER_PLANE = "worker_plane"
    REPLICATED_OWNERSHIP = "replicated_ownership"


DEFAULT_PRODUCTION_GATES: tuple[AutonomyQualificationGate, ...] = (
    AutonomyQualificationGate.EVALUATION,
    AutonomyQualificationGate.OBSERVABILITY,
    AutonomyQualificationGate.RELEASE,
    AutonomyQualificationGate.INCIDENT_READINESS,
    AutonomyQualificationGate.RECOVERY,
    AutonomyQualificationGate.CAPACITY,
    AutonomyQualificationGate.UPGRADE,
    AutonomyQualificationGate.AUDIT_INTEGRITY,
    AutonomyQualificationGate.RELIABILITY,
    AutonomyQualificationGate.WORKER_PLANE,
)


class AutonomyBudgetLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_actions_per_cycle: int = Field(default=4, ge=0, le=1000)
    max_model_tokens_per_cycle: int = Field(default=32_000, ge=0, le=10_000_000)
    max_model_cost_usd_per_cycle: float = Field(default=10.0, ge=0.0)
    max_monetary_impact_usd_per_cycle: float = Field(default=10_000.0, ge=0.0)
    max_cloud_spend_usd_per_cycle: float = Field(default=1_000.0, ge=0.0)
    max_production_changes_per_cycle: int = Field(default=1, ge=0, le=1000)


class AutonomyBudgetPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_actions_per_cycle: int | None = Field(default=None, ge=0, le=1000)
    max_model_tokens_per_cycle: int | None = Field(default=None, ge=0, le=10_000_000)
    max_model_cost_usd_per_cycle: float | None = Field(default=None, ge=0.0)
    max_monetary_impact_usd_per_cycle: float | None = Field(default=None, ge=0.0)
    max_cloud_spend_usd_per_cycle: float | None = Field(default=None, ge=0.0)
    max_production_changes_per_cycle: int | None = Field(default=None, ge=0, le=1000)


class AutonomyActionCharge(BaseModel):
    """Deterministic policy-owned budget charge for one action.

    These values are configured by operators/policy, never supplied by model output.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    monetary_impact_usd: float = Field(default=0.0, ge=0.0)
    cloud_spend_usd: float = Field(default=0.0, ge=0.0)
    production_change: bool = False


class AutonomyMaintenanceWindow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    timezone: str = Field(min_length=1, max_length=200)
    weekdays: tuple[int, ...] = Field(default=(0, 1, 2, 3, 4), min_length=1)
    start_local: str = Field(default="00:00", min_length=4, max_length=8)
    end_local: str = Field(default="23:59:59", min_length=4, max_length=8)

    @model_validator(mode="after")
    def normalize(self) -> "AutonomyMaintenanceWindow":
        from datetime import time as wall_time
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("maintenance window timezone must be a valid IANA timezone") from exc
        try:
            wall_time.fromisoformat(self.start_local)
            wall_time.fromisoformat(self.end_local)
        except ValueError as exc:
            raise ValueError("maintenance window times must be ISO HH:MM[:SS]") from exc
        normalized = tuple(sorted(set(self.weekdays)))
        if any(value < 0 or value > 6 for value in normalized):
            raise ValueError("maintenance window weekdays must be in range 0..6")
        object.__setattr__(self, "weekdays", normalized)
        return self


class AutonomyQualificationEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    gate: AutonomyQualificationGate
    evidence_ids: tuple[str, ...] = Field(min_length=1, max_length=100)
    max_age_seconds: float | None = Field(default=None, gt=0.0)
    require_independent_verification: bool = False

    @model_validator(mode="after")
    def normalize(self) -> "AutonomyQualificationEvidence":
        object.__setattr__(
            self,
            "evidence_ids",
            tuple(dict.fromkeys(item.strip() for item in self.evidence_ids if item.strip())),
        )
        if not self.evidence_ids:
            raise ValueError("qualification evidence requires at least one evidence id")
        return self


class AutonomyBreakGlassPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    quorum: int = Field(default=2, ge=2, le=20)
    required_assurance: AuthenticationAssurance = AuthenticationAssurance.MFA
    max_duration_seconds: float = Field(default=900.0, gt=0.0, le=86_400.0)


class AutonomyScopeOverride(BaseModel):
    """A deterministic policy patch selected by canonical project/role/action scope."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    id: str = Field(default_factory=lambda: f"autonomy-scope-{uuid.uuid4().hex}")
    project_id: str | None = Field(default=None, max_length=500)
    role_id: str | None = Field(default=None, max_length=500)
    action_id: str | None = Field(default=None, max_length=500)
    level: AutonomyLevel | None = None
    budget: AutonomyBudgetPatch = Field(default_factory=AutonomyBudgetPatch)
    action_charge: AutonomyActionCharge | None = None
    production_change: bool | None = None
    approval_required_risks: tuple[ActionRiskClass, ...] | None = None
    approval_action_ids: tuple[str, ...] | None = None
    approval_quorum: int | None = Field(default=None, ge=1, le=20)
    distinct_humans: bool | None = None
    allow_self_approval: bool | None = None
    required_assurance: AuthenticationAssurance | None = None
    rollback_required_risks: tuple[ActionRiskClass, ...] | None = None
    verification_required_risks: tuple[ActionRiskClass, ...] | None = None
    maintenance_windows: tuple[AutonomyMaintenanceWindow, ...] | None = None
    required_qualification_gates: tuple[AutonomyQualificationGate, ...] | None = None
    qualifications: tuple[AutonomyQualificationEvidence, ...] | None = None
    multi_instance: bool | None = None

    @model_validator(mode="after")
    def validate_selector(self) -> "AutonomyScopeOverride":
        if not any((self.project_id, self.role_id, self.action_id)):
            raise ValueError("autonomy scope override requires project_id, role_id, or action_id")
        for name in (
            "approval_required_risks",
            "approval_action_ids",
            "rollback_required_risks",
            "verification_required_risks",
            "required_qualification_gates",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, tuple(dict.fromkeys(value)))
        if self.qualifications is not None:
            gates = [item.gate for item in self.qualifications]
            if len(gates) != len(set(gates)):
                raise ValueError("autonomy scope qualification gates must be unique")
        return self

    @property
    def specificity(self) -> int:
        return sum(value is not None for value in (self.project_id, self.role_id, self.action_id))


class AutonomyExclusiveGoalScope(BaseModel):
    """Fail-closed allowlist for autonomous continuation of one canonical Goal."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    goal_id: str = Field(min_length=1, max_length=500)
    goal_revision: int = Field(ge=1)
    project_ids: tuple[str, ...] = Field(min_length=1)
    work_item_refs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def normalize(self) -> "AutonomyExclusiveGoalScope":
        projects = tuple(dict.fromkeys(item for item in self.project_ids if item))
        work_items = tuple(dict.fromkeys(item for item in self.work_item_refs if item))
        if not projects:
            raise ValueError("exclusive Goal scope requires at least one project")
        object.__setattr__(self, "project_ids", projects)
        object.__setattr__(self, "work_item_refs", work_items)
        return self


class AutonomyPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    level: AutonomyLevel = AutonomyLevel.EXECUTE_BOUNDED
    budget: AutonomyBudgetLimits = Field(default_factory=AutonomyBudgetLimits)
    default_action_charge: AutonomyActionCharge = Field(default_factory=AutonomyActionCharge)
    approval_required_risks: tuple[ActionRiskClass, ...] = (
        ActionRiskClass.HIGH,
        ActionRiskClass.CRITICAL,
    )
    approval_action_ids: tuple[str, ...] = ()
    high_risk_approval_quorum: int = Field(default=1, ge=1, le=20)
    critical_risk_approval_quorum: int = Field(default=2, ge=2, le=20)
    distinct_humans: bool = True
    allow_self_approval: bool = False
    required_assurance: AuthenticationAssurance = AuthenticationAssurance.MFA
    approval_expiry_seconds: float = Field(default=3600.0, gt=0.0, le=604_800.0)
    rollback_required_risks: tuple[ActionRiskClass, ...] = (ActionRiskClass.CRITICAL,)
    verification_required_risks: tuple[ActionRiskClass, ...] = (
        ActionRiskClass.HIGH,
        ActionRiskClass.CRITICAL,
    )
    require_preflight: bool = True
    maintenance_windows: tuple[AutonomyMaintenanceWindow, ...] = ()
    required_qualification_gates: tuple[AutonomyQualificationGate, ...] = DEFAULT_PRODUCTION_GATES
    qualifications: tuple[AutonomyQualificationEvidence, ...] = ()
    multi_instance: bool = False
    break_glass: AutonomyBreakGlassPolicy = Field(default_factory=AutonomyBreakGlassPolicy)
    exclusive_goal_scope: AutonomyExclusiveGoalScope | None = None
    overrides: tuple[AutonomyScopeOverride, ...] = ()

    @model_validator(mode="after")
    def normalize(self) -> "AutonomyPolicy":
        for name in (
            "approval_required_risks",
            "approval_action_ids",
            "rollback_required_risks",
            "verification_required_risks",
            "required_qualification_gates",
        ):
            object.__setattr__(self, name, tuple(dict.fromkeys(getattr(self, name))))
        gates = [item.gate for item in self.qualifications]
        if len(gates) != len(set(gates)):
            raise ValueError("autonomy qualification gates must be unique")
        ids = [item.id for item in self.overrides]
        if len(ids) != len(set(ids)):
            raise ValueError("autonomy scope override ids must be unique")
        return self

    def fingerprint(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


class EffectiveAutonomyPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_fingerprint: str
    level: AutonomyLevel
    budget: AutonomyBudgetLimits
    action_charge: AutonomyActionCharge
    production_change: bool
    approval_required_risks: tuple[ActionRiskClass, ...]
    approval_action_ids: tuple[str, ...]
    approval_quorum: int
    distinct_humans: bool
    allow_self_approval: bool
    required_assurance: AuthenticationAssurance
    approval_expiry_seconds: float
    rollback_required_risks: tuple[ActionRiskClass, ...]
    verification_required_risks: tuple[ActionRiskClass, ...]
    require_preflight: bool
    maintenance_windows: tuple[AutonomyMaintenanceWindow, ...]
    required_qualification_gates: tuple[AutonomyQualificationGate, ...]
    qualifications: tuple[AutonomyQualificationEvidence, ...]
    multi_instance: bool
    exclusive_goal_scope: AutonomyExclusiveGoalScope | None = None
    matched_override_ids: tuple[str, ...] = ()


class AutonomyQualificationOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    gate: AutonomyQualificationGate
    satisfied: bool
    evidence_ids: tuple[str, ...] = ()
    verification_ids: tuple[str, ...] = ()
    reason: str | None = None


class AutonomyActionDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: bool
    effective_level: AutonomyLevel
    effective_risk: ActionRiskClass
    production_change: bool
    policy_fingerprint: str
    matched_override_ids: tuple[str, ...] = ()
    role_ids: tuple[str, ...] = ()
    approval_required: bool = False
    approval_quorum: int = 0
    approval_expiry_seconds: float = 0.0
    required_assurance: AuthenticationAssurance = AuthenticationAssurance.MFA
    distinct_humans: bool = True
    allow_self_approval: bool = False
    rollback_required: bool = False
    verification_required: bool = False
    preflight_required: bool = False
    budget: AutonomyBudgetLimits
    action_charge: AutonomyActionCharge
    qualification_outcomes: tuple[AutonomyQualificationOutcome, ...] = ()
    break_glass_grant_id: str | None = None
    reasons: tuple[str, ...] = ()


class AutonomyCycleBudgetUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    actions: int = Field(default=0, ge=0)
    model_tokens: int = Field(default=0, ge=0)
    model_cost_usd: float = Field(default=0.0, ge=0.0)
    monetary_impact_usd: float = Field(default=0.0, ge=0.0)
    cloud_spend_usd: float = Field(default=0.0, ge=0.0)
    production_changes: int = Field(default=0, ge=0)


class AutonomyBreakGlassRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    project_id: str | None = Field(default=None, max_length=500)
    reason: str = Field(min_length=1, max_length=4000)


class AutonomyBreakGlassActivate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    approval_request_id: str = Field(min_length=1, max_length=500)


class AutonomyBreakGlassGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: f"break-glass-{uuid.uuid4().hex}")
    organization_id: str
    workspace_id: str
    project_id: str | None = None
    approval_request_id: str
    policy_fingerprint: str
    activated_by_identity_id: str
    activated_at: float = Field(default_factory=time.time)
    expires_at: float
