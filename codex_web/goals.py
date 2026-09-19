from __future__ import annotations

import time
import uuid
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.compatibility import ContractSpec


GOAL_CONTRACT = ContractSpec("goal-state", "1.2", ("1.0", "1.1", "1.2"), deprecated=("1.0",))


class GoalStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class GoalPriority(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class GoalHealth(StrEnum):
    UNKNOWN = "unknown"
    ON_TRACK = "on_track"
    AT_RISK = "at_risk"
    BLOCKED = "blocked"


class GoalRiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class GoalCriterionKind(StrEnum):
    MANUAL = "manual"
    METRIC = "metric"


class GoalCriterionOperator(StrEnum):
    EQ = "eq"
    GTE = "gte"
    LTE = "lte"


class GoalConstraintKind(StrEnum):
    SCOPE = "scope"
    POLICY = "policy"
    TIME = "time"
    RESOURCE = "resource"
    OTHER = "other"


class GoalApprovalTrigger(StrEnum):
    DECOMPOSITION = "decomposition"
    SCOPE_CHANGE = "scope_change"
    BUDGET_CHANGE = "budget_change"
    COMPLETION = "completion"
    OTHER = "other"


class GoalBudget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_input_tokens: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    max_model_calls: int | None = Field(default=None, ge=1)
    max_cost_usd: float | None = Field(default=None, ge=0.0)
    max_retries: int | None = Field(default=None, ge=0)
    max_handoffs: int | None = Field(default=None, ge=0)


class GoalSuccessCriterion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    id: str = Field(default_factory=lambda: f"goal-criterion-{uuid.uuid4().hex}")
    description: str = Field(min_length=1)
    kind: GoalCriterionKind = GoalCriterionKind.MANUAL
    metric_key: str | None = None
    metric_id: str | None = None
    metric_snapshot_id: str | None = None
    metric_window_seconds: int | None = Field(default=None, ge=1)
    operator: GoalCriterionOperator | None = None
    target_value: str | int | float | bool | None = None
    unit: str | None = None
    required: bool = True

    @model_validator(mode="after")
    def validate_metric(self) -> "GoalSuccessCriterion":
        if self.kind == GoalCriterionKind.METRIC:
            if not self.metric_id and not self.metric_key:
                raise ValueError("metric success criterion requires metric_id or legacy metric_key")
            if self.metric_snapshot_id and not self.metric_id:
                raise ValueError("metric_snapshot_id requires canonical metric_id")
            if self.operator is None:
                raise ValueError("metric success criterion requires operator")
            if self.target_value is None:
                raise ValueError("metric success criterion requires target_value")
        return self


class GoalConstraint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    id: str = Field(default_factory=lambda: f"goal-constraint-{uuid.uuid4().hex}")
    kind: GoalConstraintKind = GoalConstraintKind.OTHER
    description: str = Field(min_length=1)
    reference: str | None = None


class GoalRisk(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    id: str = Field(default_factory=lambda: f"goal-risk-{uuid.uuid4().hex}")
    level: GoalRiskLevel
    category: str = Field(min_length=1)
    description: str = Field(min_length=1)
    mitigation: str | None = None


class GoalApprovalRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    id: str = Field(default_factory=lambda: f"goal-approval-{uuid.uuid4().hex}")
    trigger: GoalApprovalTrigger
    description: str = Field(min_length=1)
    required_role: str | None = None


class GoalWorkGraphBinding(BaseModel):
    """Bind one goal to a project graph or explicit parent-rooted subgraphs."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    project_id: str = Field(min_length=1)
    root_work_item_refs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def normalize(self) -> "GoalWorkGraphBinding":
        object.__setattr__(
            self,
            "root_work_item_refs",
            tuple(dict.fromkeys(self.root_work_item_refs)),
        )
        return self


class GoalCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    owner_identity_id: str = Field(min_length=1)
    priority: GoalPriority = GoalPriority.MEDIUM
    target_date: float | None = None
    success_criteria: tuple[GoalSuccessCriterion, ...] = ()
    constraints: tuple[GoalConstraint, ...] = ()
    risks: tuple[GoalRisk, ...] = ()
    budget: GoalBudget = Field(default_factory=GoalBudget)
    approval_requirements: tuple[GoalApprovalRequirement, ...] = ()
    work_graph_bindings: tuple[GoalWorkGraphBinding, ...] = ()


class GoalUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str | None = Field(default=None, min_length=1)
    description: str | None = Field(default=None, min_length=1)
    owner_identity_id: str | None = Field(default=None, min_length=1)
    priority: GoalPriority | None = None
    target_date: float | None = None
    success_criteria: tuple[GoalSuccessCriterion, ...] | None = None
    constraints: tuple[GoalConstraint, ...] | None = None
    risks: tuple[GoalRisk, ...] | None = None
    budget: GoalBudget | None = None
    approval_requirements: tuple[GoalApprovalRequirement, ...] | None = None
    work_graph_bindings: tuple[GoalWorkGraphBinding, ...] | None = None
    reason: str = Field(min_length=1)


class GoalCriterionObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    criterion_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    reference: str | None = None
    observed_value: str | int | float | bool | None = None
    verified: bool | None = None
    note: str | None = None
    observed_at: float = Field(default_factory=time.time)


class GoalCompletionEvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    observations: tuple[GoalCriterionObservation, ...] = ()
    reason: str = Field(min_length=1)


class GoalCriterionEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    criterion_id: str
    kind: GoalCriterionKind
    required: bool
    passed: bool
    description: str
    metric_key: str | None = None
    operator: GoalCriterionOperator | None = None
    target_value: str | int | float | bool | None = None
    observed_value: str | int | float | bool | None = None
    source: str | None = None
    reference: str | None = None
    observed_at: float | None = None
    findings: tuple[str, ...] = ()


class GoalWorkItemCompletionEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    work_item_ref: str
    terminal_outcome: str | None = None
    passed: bool
    findings: tuple[str, ...] = ()


class GoalCompletionEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: f"goal-completion-{uuid.uuid4().hex}")
    organization_id: str
    workspace_id: str
    goal_id: str
    goal_revision: int = Field(ge=1)
    eligible: bool
    work_items: tuple[GoalWorkItemCompletionEvaluation, ...] = ()
    criteria: tuple[GoalCriterionEvaluation, ...] = ()
    blockers: tuple[str, ...] = ()
    evaluated_by: str
    reason: str
    evaluated_at: float = Field(default_factory=time.time)


class GoalTransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    status: GoalStatus
    reason: str = Field(min_length=1)
    completion_evaluation_id: str | None = None


class GoalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(default_factory=lambda: f"goal-{uuid.uuid4().hex}")
    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    owner_identity_id: str = Field(min_length=1)
    status: GoalStatus = GoalStatus.DRAFT
    priority: GoalPriority = GoalPriority.MEDIUM
    target_date: float | None = None
    success_criteria: tuple[GoalSuccessCriterion, ...] = ()
    constraints: tuple[GoalConstraint, ...] = ()
    risks: tuple[GoalRisk, ...] = ()
    budget: GoalBudget = Field(default_factory=GoalBudget)
    approval_requirements: tuple[GoalApprovalRequirement, ...] = ()
    work_graph_bindings: tuple[GoalWorkGraphBinding, ...] = ()
    completion_evaluation_id: str | None = None
    completed_at: float | None = None
    revision: int = Field(default=1, ge=1)
    created_by: str = Field(min_length=1)
    updated_by: str = Field(min_length=1)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


class GoalRevision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    goal_id: str
    revision: int = Field(ge=1)
    snapshot: GoalRecord
    reason: str
    revised_by: str
    revised_at: float = Field(default_factory=time.time)


class GoalEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: f"goal-event-{uuid.uuid4().hex}")
    goal_id: str
    event_type: str
    revision: int = Field(ge=1)
    actor_id: str
    reason: str
    occurred_at: float = Field(default_factory=time.time)


class GoalProgress(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_count: int = Field(ge=0)
    work_item_count: int = Field(ge=0)
    completed: int = Field(ge=0)
    failed: int = Field(ge=0)
    cancelled: int = Field(ge=0)
    active: int = Field(ge=0)
    runnable: int = Field(ge=0)
    blocked: int = Field(ge=0)
    completion_fraction: float = Field(ge=0.0, le=1.0)


class GoalHealthSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    health: GoalHealth
    reasons: tuple[str, ...] = ()


class GoalSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    goal: GoalRecord
    progress: GoalProgress
    health: GoalHealthSnapshot


class GoalState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = GOAL_CONTRACT.current
    goals: list[GoalRecord] = Field(default_factory=list)
    revisions: list[GoalRevision] = Field(default_factory=list)
    events: list[GoalEvent] = Field(default_factory=list)
    completion_evaluations: list[GoalCompletionEvaluation] = Field(default_factory=list)
