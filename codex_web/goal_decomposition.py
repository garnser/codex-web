from __future__ import annotations

import time
import uuid
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.compatibility import ContractSpec
from codex_web.goals import GoalBudget


GOAL_DECOMPOSITION_CONTRACT = ContractSpec(
    "goal-decomposition-state",
    "1.1",
    ("1.0", "1.1"),
)

MAX_GOAL_DECOMPOSITION_DEPTH = 8
MAX_GOAL_DECOMPOSITION_ITEMS = 100
MAX_GOAL_DECOMPOSITION_PROJECTS = 8
MAX_GOAL_DECOMPOSITION_CONTEXT_ITEMS = 30
MAX_GOAL_DECOMPOSITION_OUTPUT_TOKENS = 4096


class GoalDecompositionStatus(StrEnum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    COMMITTING = "committing"
    COMMITTED = "committed"


class GoalDecompositionCommitState(StrEnum):
    PLANNED = "planned"
    QUEUED = "queued"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNCERTAIN = "uncertain"
    REQUIRES_RECONCILIATION = "requires_reconciliation"
    ROLLED_BACK = "rolled_back"


class GoalDecompositionReviewDecision(StrEnum):
    ACCEPT = "accept"
    REJECT = "reject"


class GoalDecompositionLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_depth: int = Field(default=3, ge=1, le=MAX_GOAL_DECOMPOSITION_DEPTH)
    max_items: int = Field(default=20, ge=1, le=MAX_GOAL_DECOMPOSITION_ITEMS)


class GoalProposedWorkItem(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    id: str = Field(default_factory=lambda: f"goal-work-{uuid.uuid4().hex}")
    project_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    owner_identity_id: str | None = Field(default=None, min_length=1)
    labels: tuple[str, ...] = ()
    parent_item_id: str | None = None
    blocked_by_item_ids: tuple[str, ...] = ()
    expected_result: str | None = None

    @model_validator(mode="after")
    def normalize(self) -> "GoalProposedWorkItem":
        object.__setattr__(
            self,
            "labels",
            tuple(dict.fromkeys(value for value in self.labels if value)),
        )
        object.__setattr__(
            self,
            "blocked_by_item_ids",
            tuple(dict.fromkeys(self.blocked_by_item_ids)),
        )
        if self.parent_item_id == self.id:
            raise ValueError("proposed work item cannot parent itself")
        if self.id in self.blocked_by_item_ids:
            raise ValueError("proposed work item cannot block itself")
        return self


class GoalDecompositionProposalCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    items: tuple[GoalProposedWorkItem, ...]
    limits: GoalDecompositionLimits = Field(default_factory=GoalDecompositionLimits)
    reason: str = Field(min_length=1)
    model_invocation_id: str | None = None
    expected_goal_revision: int | None = Field(default=None, ge=1)


class GoalDecompositionGenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    project_ids: tuple[str, ...] = ()
    limits: GoalDecompositionLimits = Field(default_factory=GoalDecompositionLimits)
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def normalize_projects(self) -> "GoalDecompositionGenerationRequest":
        object.__setattr__(
            self,
            "project_ids",
            tuple(dict.fromkeys(value for value in self.project_ids if value)),
        )
        if len(self.project_ids) > MAX_GOAL_DECOMPOSITION_PROJECTS:
            raise ValueError(
                "goal decomposition project selection exceeds "
                f"{MAX_GOAL_DECOMPOSITION_PROJECTS}"
            )
        return self


class GoalDecompositionModelOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: tuple[GoalProposedWorkItem, ...]


class GoalDecompositionProposalRevise(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    items: tuple[GoalProposedWorkItem, ...]
    limits: GoalDecompositionLimits | None = None
    reason: str = Field(min_length=1)
    model_invocation_id: str | None = None


class GoalDecompositionReview(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    decision: GoalDecompositionReviewDecision
    reason: str = Field(min_length=1)


class GoalDecompositionCommitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: str = Field(min_length=1)


class GoalDecompositionCommitItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    proposal_item_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    binding_id: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)
    intent_id: str | None = None
    work_item_ref: str | None = None
    state: GoalDecompositionCommitState = GoalDecompositionCommitState.PLANNED
    last_error: str | None = None
    updated_at: float = Field(default_factory=time.time)


class GoalDecompositionProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"goal-decomposition-{uuid.uuid4().hex}")
    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    goal_id: str = Field(min_length=1)
    goal_revision: int = Field(ge=1)
    revision: int = Field(default=1, ge=1)
    status: GoalDecompositionStatus = GoalDecompositionStatus.PROPOSED
    items: tuple[GoalProposedWorkItem, ...]
    limits: GoalDecompositionLimits
    reasoning_budget: GoalBudget
    model_invocation_id: str | None = None
    created_by: str = Field(min_length=1)
    updated_by: str = Field(min_length=1)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    reviewed_by: str | None = None
    reviewed_at: float | None = None
    review_reason: str | None = None
    commit_items: tuple[GoalDecompositionCommitItem, ...] = ()
    commit_started_by: str | None = None
    commit_started_at: float | None = None
    commit_error: str | None = None
    committed_at: float | None = None
    committed_work_item_refs: tuple[str, ...] = ()


class GoalDecompositionRevision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    proposal_id: str
    revision: int = Field(ge=1)
    snapshot: GoalDecompositionProposal
    reason: str
    revised_by: str
    revised_at: float = Field(default_factory=time.time)


class GoalDecompositionEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: f"goal-decomposition-event-{uuid.uuid4().hex}")
    proposal_id: str
    goal_id: str
    event_type: str
    revision: int = Field(ge=1)
    actor_id: str
    reason: str
    occurred_at: float = Field(default_factory=time.time)


class GoalDecompositionState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = GOAL_DECOMPOSITION_CONTRACT.current
    proposals: list[GoalDecompositionProposal] = Field(default_factory=list)
    revisions: list[GoalDecompositionRevision] = Field(default_factory=list)
    events: list[GoalDecompositionEvent] = Field(default_factory=list)
