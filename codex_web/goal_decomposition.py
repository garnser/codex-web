from __future__ import annotations

import time
import uuid
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.compatibility import ContractSpec
from codex_web.goals import GoalBudget


GOAL_DECOMPOSITION_CONTRACT = ContractSpec(
    "goal-decomposition-state",
    "1.0",
    ("1.0",),
)

MAX_GOAL_DECOMPOSITION_DEPTH = 8
MAX_GOAL_DECOMPOSITION_ITEMS = 100


class GoalDecompositionStatus(StrEnum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    COMMITTED = "committed"


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
