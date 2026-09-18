from __future__ import annotations

import time
import uuid
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.compatibility import ContractSpec


WORK_GRAPH_CONTRACT = ContractSpec("work-graph-state", "1.0", ("1.0",))


class WorkGraphRelation(StrEnum):
    PARENT = "parent"
    BLOCKS = "blocks"


class DependencyFailureBehavior(StrEnum):
    PAUSE = "pause"
    FAIL = "fail"
    REPLAN = "replan"
    ESCALATE = "escalate"


class WorkGraphEdge(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    id: str = Field(default_factory=lambda: f"work-edge-{uuid.uuid4().hex}")
    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    relation: WorkGraphRelation
    source_ref: str = Field(min_length=1)
    target_ref: str = Field(min_length=1)
    failure_behavior: DependencyFailureBehavior = DependencyFailureBehavior.PAUSE
    created_by: str = Field(min_length=1)
    created_at: float = Field(default_factory=time.time)
    reason: str | None = None

    @model_validator(mode="after")
    def validate_edge(self) -> "WorkGraphEdge":
        if self.source_ref == self.target_ref:
            raise ValueError("work graph edge cannot reference the same item twice")
        if (
            self.relation == WorkGraphRelation.PARENT
            and self.failure_behavior != DependencyFailureBehavior.PAUSE
        ):
            raise ValueError("parent edges do not carry dependency failure behavior")
        return self


class WorkGraphEdgeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    relation: WorkGraphRelation
    source_ref: str = Field(min_length=1)
    target_ref: str = Field(min_length=1)
    failure_behavior: DependencyFailureBehavior = DependencyFailureBehavior.PAUSE
    reason: str | None = None


class WorkGraphEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: f"work-graph-event-{uuid.uuid4().hex}")
    event_type: str
    edge: WorkGraphEdge
    actor_id: str
    occurred_at: float = Field(default_factory=time.time)


class WorkGraphState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = WORK_GRAPH_CONTRACT.current
    edges: list[WorkGraphEdge] = Field(default_factory=list)
    events: list[WorkGraphEvent] = Field(default_factory=list)


class WorkReadinessStatus(StrEnum):
    RUNNABLE = "runnable"
    BLOCKED = "blocked"
    TERMINAL = "terminal"


class WorkDependencyImpact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    blocker_ref: str
    blocked_ref: str
    blocker_outcome: str
    behavior: DependencyFailureBehavior
    reason: str


class WorkReadiness(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ref: str
    status: WorkReadinessStatus
    reasons: tuple[str, ...] = ()
    blocking_refs: tuple[str, ...] = ()
    failure_impacts: tuple[WorkDependencyImpact, ...] = ()


class WorkGraphNode(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ref: str
    title: str | None = None
    project_id: str
    stage: str
    terminal_outcome: str | None = None
    readiness: WorkReadiness


class WorkGraphCriticalPath(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    refs: tuple[str, ...] = ()
    node_count: int = 0
    edge_count: int = 0


class WorkGraphProgress(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    total: int
    completed: int
    failed: int
    cancelled: int
    active: int
    runnable: int
    blocked: int
    completion_fraction: float


class WorkGraphSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    organization_id: str
    workspace_id: str
    project_id: str
    nodes: tuple[WorkGraphNode, ...]
    edges: tuple[WorkGraphEdge, ...]
    runnable_refs: tuple[str, ...]
    failure_impacts: tuple[WorkDependencyImpact, ...]
    critical_path: WorkGraphCriticalPath
    progress: WorkGraphProgress
