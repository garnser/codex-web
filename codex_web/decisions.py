from __future__ import annotations

import hashlib
import json
import time
import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.approval_requests import ApprovalRequirement
from codex_web.compatibility import ContractSpec
from codex_web.metrics import MetricFreshness


DECISION_CONTRACT = ContractSpec("decision-state", "1.0", ("1.0",))

MAX_DECISION_PARTICIPANTS = 8
MAX_DECISION_ROUNDS = 3
MAX_DECISION_OPTIONS = 20
MAX_DECISION_EVIDENCE = 100


class DecisionStatus(StrEnum):
    DRAFT = "draft"
    ANALYSIS = "analysis"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class DecisionImportance(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class DecisionEvidenceKind(StrEnum):
    EVIDENCE = "evidence"
    METRIC_SNAPSHOT = "metric_snapshot"


class DecisionReviewOutcome(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    UNKNOWN = "unknown"


class DecisionWorkState(StrEnum):
    PLANNED = "planned"
    QUEUED = "queued"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNCERTAIN = "uncertain"
    REQUIRES_RECONCILIATION = "requires_reconciliation"


class DecisionBudget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_input_tokens: int = Field(ge=1)
    max_output_tokens: int = Field(ge=1)
    max_model_calls: int = Field(ge=1)
    max_cost_usd: float = Field(gt=0.0)


class DecisionDeliberationLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_participants: int = Field(default=6, ge=1, le=MAX_DECISION_PARTICIPANTS)
    max_rounds: int = Field(default=1, ge=1, le=MAX_DECISION_ROUNDS)


class DecisionParticipant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    id: str = Field(default_factory=lambda: f"decision-participant-{uuid.uuid4().hex}")
    role: str = Field(min_length=1, max_length=200)
    perspective: str = Field(min_length=1, max_length=2000)
    identity_id: str | None = Field(default=None, min_length=1, max_length=500)


class DecisionEvidenceRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    id: str = Field(default_factory=lambda: f"decision-evidence-{uuid.uuid4().hex}")
    kind: DecisionEvidenceKind
    evidence_id: str | None = None
    metric_id: str | None = None
    metric_snapshot_id: str | None = None
    metric_revision: int | None = Field(default=None, ge=1)
    metric_freshness: MetricFreshness | None = None
    observed_value: float | int | bool | None = None
    unit: str | None = None
    observation_ids: tuple[str, ...] = ()
    window_start: float | None = None
    window_end: float | None = None
    summary: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def validate_reference(self) -> "DecisionEvidenceRef":
        if self.kind == DecisionEvidenceKind.EVIDENCE:
            if not self.evidence_id:
                raise ValueError("canonical evidence reference requires evidence_id")
            if self.metric_id or self.metric_snapshot_id:
                raise ValueError("canonical evidence reference cannot include metric identifiers")
        else:
            if not self.metric_id or not self.metric_snapshot_id:
                raise ValueError(
                    "metric snapshot evidence requires metric_id and metric_snapshot_id"
                )
            if self.evidence_id:
                raise ValueError("metric snapshot evidence cannot include evidence_id")
        if self.window_start is not None and self.window_end is not None:
            if self.window_end < self.window_start:
                raise ValueError("decision evidence window_end must be >= window_start")
        object.__setattr__(
            self,
            "observation_ids",
            tuple(dict.fromkeys(self.observation_ids)),
        )
        return self


class DecisionEvidenceInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    kind: DecisionEvidenceKind
    evidence_id: str | None = None
    metric_id: str | None = None
    metric_snapshot_id: str | None = None
    summary: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def validate_reference(self) -> "DecisionEvidenceInput":
        if self.kind == DecisionEvidenceKind.EVIDENCE:
            if not self.evidence_id:
                raise ValueError("canonical evidence reference requires evidence_id")
            if self.metric_id or self.metric_snapshot_id:
                raise ValueError("canonical evidence reference cannot include metric identifiers")
        else:
            if not self.metric_id or not self.metric_snapshot_id:
                raise ValueError(
                    "metric snapshot evidence requires metric_id and metric_snapshot_id"
                )
            if self.evidence_id:
                raise ValueError("metric snapshot evidence cannot include evidence_id")
        return self


class DecisionOption(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    id: str = Field(default_factory=lambda: f"decision-option-{uuid.uuid4().hex}")
    title: str = Field(min_length=1, max_length=500)
    description: str = Field(min_length=1, max_length=8000)
    pros: tuple[str, ...] = ()
    cons: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()

    @model_validator(mode="after")
    def normalize(self) -> "DecisionOption":
        object.__setattr__(self, "pros", tuple(dict.fromkeys(self.pros)))
        object.__setattr__(self, "cons", tuple(dict.fromkeys(self.cons)))
        object.__setattr__(self, "risks", tuple(dict.fromkeys(self.risks)))
        return self


class DecisionRecommendation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    option_id: str = Field(min_length=1)
    rationale: str = Field(min_length=1, max_length=12000)
    confidence: float = Field(ge=0.0, le=1.0)
    uncertainty: tuple[str, ...] = ()

    @model_validator(mode="after")
    def normalize(self) -> "DecisionRecommendation":
        object.__setattr__(
            self,
            "uncertainty",
            tuple(dict.fromkeys(self.uncertainty)),
        )
        return self


class DecisionDissent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    participant_id: str = Field(min_length=1)
    option_id: str | None = None
    rationale: str = Field(min_length=1, max_length=8000)


class DecisionParticipantAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    participant_id: str = Field(min_length=1)
    preferred_option_id: str | None = None
    analysis: str = Field(min_length=1, max_length=12000)
    pros: tuple[str, ...] = ()
    cons: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    uncertainty: tuple[str, ...] = ()
    model_invocation_id: str = Field(min_length=1)


class DecisionDeliberationRound(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: f"decision-round-{uuid.uuid4().hex}")
    round_number: int = Field(ge=1, le=MAX_DECISION_ROUNDS)
    analyses: tuple[DecisionParticipantAnalysis, ...]
    recommendation: DecisionRecommendation
    dissent: tuple[DecisionDissent, ...] = ()
    synthesis_model_invocation_id: str = Field(min_length=1)
    started_at: float
    completed_at: float = Field(default_factory=time.time)


class DecisionActionRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    work_item_ref: str | None = Field(default=None, min_length=1, max_length=1000)
    action_intent_id: str | None = Field(default=None, min_length=1, max_length=1000)

    @model_validator(mode="after")
    def require_canonical_path(self) -> "DecisionActionRef":
        if not self.work_item_ref and not self.action_intent_id:
            raise ValueError(
                "decision consequence must reference canonical Work or ActionIntent"
            )
        return self


class DecisionWorkItemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    id: str = Field(min_length=1, max_length=300)
    project_id: str = Field(min_length=1, max_length=500)
    title: str = Field(min_length=1, max_length=500)
    description: str = Field(min_length=1, max_length=8000)
    expected_result: str | None = Field(default=None, max_length=4000)
    owner_identity_id: str | None = Field(default=None, min_length=1, max_length=500)
    labels: tuple[str, ...] = ()
    parent_item_id: str | None = Field(default=None, min_length=1, max_length=300)
    blocked_by_item_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def normalize(self) -> "DecisionWorkItemRequest":
        object.__setattr__(self, "labels", tuple(dict.fromkeys(self.labels)))
        object.__setattr__(
            self,
            "blocked_by_item_ids",
            tuple(dict.fromkeys(self.blocked_by_item_ids)),
        )
        if self.id == self.parent_item_id or self.id in self.blocked_by_item_ids:
            raise ValueError("Decision work item cannot depend on itself")
        return self


class DecisionWorkCommitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    items: tuple[DecisionWorkItemRequest, ...]
    reason: str = Field(min_length=1, max_length=4000)

    @model_validator(mode="after")
    def validate_items(self) -> "DecisionWorkCommitRequest":
        if not self.items:
            raise ValueError("Decision work commit requires at least one item")
        ids = {item.id for item in self.items}
        if len(ids) != len(self.items):
            raise ValueError("Decision work item ids must be unique")
        for item in self.items:
            if item.parent_item_id is not None and item.parent_item_id not in ids:
                raise ValueError(
                    f"Decision work parent does not exist: {item.parent_item_id}"
                )
            unknown = sorted(set(item.blocked_by_item_ids) - ids)
            if unknown:
                raise ValueError(
                    "Decision work blockers do not exist: " + ", ".join(unknown)
                )
        return self


class DecisionWorkLink(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    item_id: str
    project_id: str
    title: str
    description: str
    expected_result: str | None = None
    owner_identity_id: str | None = None
    labels: tuple[str, ...] = ()
    correlation_id: str
    binding_id: str
    action_intent_id: str | None = None
    work_item_ref: str | None = None
    state: DecisionWorkState = DecisionWorkState.PLANNED
    parent_item_id: str | None = None
    blocked_by_item_ids: tuple[str, ...] = ()
    last_error: str | None = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


class DecisionPostExecutionReviewCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    outcome: DecisionReviewOutcome
    summary: str = Field(min_length=1, max_length=12000)
    evidence_refs: tuple[str, ...] = ()
    actions: tuple[DecisionActionRef, ...] = ()


class DecisionPostExecutionReview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: f"decision-review-{uuid.uuid4().hex}")
    outcome: DecisionReviewOutcome
    summary: str
    evidence_refs: tuple[str, ...] = ()
    actions: tuple[DecisionActionRef, ...] = ()
    reviewed_by: str
    reviewed_at: float = Field(default_factory=time.time)


class DecisionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    question: str = Field(min_length=1, max_length=12000)
    title: str = Field(min_length=1, max_length=500)
    project_id: str | None = Field(default=None, max_length=500)
    goal_id: str | None = Field(default=None, max_length=500)
    participants: tuple[DecisionParticipant, ...]
    evidence: tuple[DecisionEvidenceInput, ...] = ()
    assumptions: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    options: tuple[DecisionOption, ...]
    importance: DecisionImportance = DecisionImportance.MEDIUM
    budget: DecisionBudget | None = None
    limits: DecisionDeliberationLimits = Field(default_factory=DecisionDeliberationLimits)
    review_at: float | None = None
    expires_at: float | None = None

    @model_validator(mode="after")
    def validate_bounds(self) -> "DecisionCreate":
        if not self.participants:
            raise ValueError("decision requires at least one explicit participant")
        if len(self.participants) > self.limits.max_participants:
            raise ValueError("decision participants exceed configured participant limit")
        if len(self.options) < 2:
            raise ValueError("decision requires at least two options")
        if len(self.options) > MAX_DECISION_OPTIONS:
            raise ValueError(f"decision supports at most {MAX_DECISION_OPTIONS} options")
        if len(self.evidence) > MAX_DECISION_EVIDENCE:
            raise ValueError(f"decision supports at most {MAX_DECISION_EVIDENCE} evidence references")
        participant_ids = [item.id for item in self.participants]
        if len(set(participant_ids)) != len(participant_ids):
            raise ValueError("decision participant ids must be unique")
        option_ids = [item.id for item in self.options]
        if len(set(option_ids)) != len(option_ids):
            raise ValueError("decision option ids must be unique")
        if self.review_at is not None and self.review_at <= 0:
            raise ValueError("decision review_at must be a positive timestamp")
        if self.expires_at is not None and self.expires_at <= 0:
            raise ValueError("decision expires_at must be a positive timestamp")
        return self


class DecisionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str | None = Field(default=None, min_length=1, max_length=500)
    question: str | None = Field(default=None, min_length=1, max_length=12000)
    goal_id: str | None = Field(default=None, max_length=500)
    participants: tuple[DecisionParticipant, ...] | None = None
    evidence: tuple[DecisionEvidenceInput, ...] | None = None
    assumptions: tuple[str, ...] | None = None
    constraints: tuple[str, ...] | None = None
    options: tuple[DecisionOption, ...] | None = None
    importance: DecisionImportance | None = None
    budget: DecisionBudget | None = None
    limits: DecisionDeliberationLimits | None = None
    review_at: float | None = None
    expires_at: float | None = None
    reason: str = Field(min_length=1, max_length=4000)
    expected_revision: int | None = Field(default=None, ge=1)


class DecisionApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: str = Field(min_length=1, max_length=4000)
    requirement: ApprovalRequirement = Field(default_factory=ApprovalRequirement)
    expires_at: float | None = None


class DecisionSupersedeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    replacement_decision_id: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=4000)


class DecisionFinalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    option_id: str = Field(min_length=1)
    rationale: str = Field(min_length=1, max_length=12000)
    confidence: float = Field(ge=0.0, le=1.0)
    uncertainty: tuple[str, ...] = ()
    approval_request_id: str = Field(min_length=1)
    decided_at: float = Field(default_factory=time.time)


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"decision-{uuid.uuid4().hex}")
    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    project_id: str | None = None
    goal_id: str | None = None
    title: str = Field(min_length=1)
    question: str = Field(min_length=1)
    initiator_identity_id: str = Field(min_length=1)
    participants: tuple[DecisionParticipant, ...]
    evidence: tuple[DecisionEvidenceRef, ...] = ()
    assumptions: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    options: tuple[DecisionOption, ...]
    importance: DecisionImportance
    budget: DecisionBudget
    limits: DecisionDeliberationLimits
    status: DecisionStatus = DecisionStatus.DRAFT
    deliberation_rounds: tuple[DecisionDeliberationRound, ...] = ()
    recommendation: DecisionRecommendation | None = None
    dissent: tuple[DecisionDissent, ...] = ()
    approval_request_id: str | None = None
    approval_target_revision: int | None = Field(default=None, ge=1)
    final_decision: DecisionFinalDecision | None = None
    review_at: float | None = None
    expires_at: float | None = None
    superseded_by_decision_id: str | None = None
    post_execution_reviews: tuple[DecisionPostExecutionReview, ...] = ()
    work_links: tuple[DecisionWorkLink, ...] = ()
    originating_executive_activation_id: str | None = None
    originating_executive_proposal_id: str | None = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    revision: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def validate_refs(self) -> "Decision":
        participant_ids = {item.id for item in self.participants}
        option_ids = {item.id for item in self.options}
        if len(participant_ids) != len(self.participants):
            raise ValueError("decision participant ids must be unique")
        if len(option_ids) != len(self.options):
            raise ValueError("decision option ids must be unique")
        if len(self.participants) > self.limits.max_participants:
            raise ValueError("decision participants exceed configured participant limit")
        if self.recommendation is not None and self.recommendation.option_id not in option_ids:
            raise ValueError("decision recommendation references unknown option")
        if self.final_decision is not None and self.final_decision.option_id not in option_ids:
            raise ValueError("final decision references unknown option")
        for item in self.dissent:
            if item.participant_id not in participant_ids:
                raise ValueError("decision dissent references unknown participant")
            if item.option_id is not None and item.option_id not in option_ids:
                raise ValueError("decision dissent references unknown option")
        return self

    def approval_digest(self) -> str:
        payload = {
            "id": self.id,
            "revision": self.revision,
            "project_id": self.project_id,
            "goal_id": self.goal_id,
            "title": self.title,
            "question": self.question,
            "participants": [item.model_dump(mode="json") for item in self.participants],
            "evidence": [item.model_dump(mode="json") for item in self.evidence],
            "assumptions": self.assumptions,
            "constraints": self.constraints,
            "options": [item.model_dump(mode="json") for item in self.options],
            "importance": self.importance.value,
            "budget": self.budget.model_dump(mode="json"),
            "limits": self.limits.model_dump(mode="json"),
            "recommendation": (
                self.recommendation.model_dump(mode="json")
                if self.recommendation is not None
                else None
            ),
            "dissent": [item.model_dump(mode="json") for item in self.dissent],
            "review_at": self.review_at,
            "expires_at": self.expires_at,
        }
        if self.originating_executive_activation_id is not None:
            payload["originating_executive_activation_id"] = (
                self.originating_executive_activation_id
            )
        if self.originating_executive_proposal_id is not None:
            payload["originating_executive_proposal_id"] = (
                self.originating_executive_proposal_id
            )
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class DecisionRevision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_id: str
    revision: int = Field(ge=1)
    snapshot: Decision
    reason: str
    revised_by: str
    revised_at: float = Field(default_factory=time.time)


class DecisionEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: f"decision-event-{uuid.uuid4().hex}")
    decision_id: str
    event_type: str
    revision: int = Field(ge=1)
    actor_id: str
    reason: str
    occurred_at: float = Field(default_factory=time.time)


class DecisionState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = DECISION_CONTRACT.current
    decisions: list[Decision] = Field(default_factory=list)
    revisions: list[DecisionRevision] = Field(default_factory=list)
    events: list[DecisionEvent] = Field(default_factory=list)

    def model_post_init(self, __context: Any) -> None:
        DECISION_CONTRACT.require(self.schema_version)
