from __future__ import annotations

import time
import uuid
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class GoalExecutionBindingStatus(StrEnum):
    REQUESTED = "requested"
    ACTIVE = "active"
    IDLE = "idle"
    BLOCKED = "blocked"
    FAILED = "failed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class GoalExecutionBindingCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    project_id: str = Field(min_length=1)
    goal_revision: int | None = Field(default=None, ge=1)
    work_item_refs: tuple[str, ...] = ()
    provider_id: str = Field(min_length=1)
    runtime_id: str = Field(min_length=1)
    agent_session_id: str = Field(min_length=1)
    thread_id: str | None = None
    execution_owner_id: str = Field(min_length=1)
    provider_native_objective_id: str | None = None
    native_objective_supported: bool = False
    capability_snapshot: tuple[str, ...] = ()
    cursor_ref: str | None = None
    checkpoint_ref: str | None = None
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def normalize(self) -> "GoalExecutionBindingCreate":
        self.work_item_refs = tuple(dict.fromkeys(self.work_item_refs))
        self.capability_snapshot = tuple(dict.fromkeys(self.capability_snapshot))
        if self.provider_native_objective_id and not self.native_objective_supported:
            raise ValueError("provider_native_objective_id requires native objective support")
        return self


class GoalExecutionBindingUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    status: GoalExecutionBindingStatus | None = None
    cursor_ref: str | None = None
    checkpoint_ref: str | None = None
    last_turn_id: str | None = None
    last_execution_id: str | None = None
    stop_reason: str | None = None
    lease_expires_at: float | None = None
    heartbeat_at: float | None = None
    recovery_attempts: int | None = Field(default=None, ge=0)
    reason: str = Field(min_length=1)


class GoalExecutionBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(default_factory=lambda: f"goal-binding-{uuid.uuid4().hex}")
    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    goal_id: str = Field(min_length=1)
    goal_revision: int = Field(ge=1)
    project_id: str = Field(min_length=1)
    work_item_refs: tuple[str, ...] = ()
    provider_id: str = Field(min_length=1)
    runtime_id: str = Field(min_length=1)
    agent_session_id: str = Field(min_length=1)
    thread_id: str | None = None
    execution_owner_id: str = Field(min_length=1)
    provider_native_objective_id: str | None = None
    native_objective_supported: bool = False
    capability_snapshot: tuple[str, ...] = ()
    status: GoalExecutionBindingStatus = GoalExecutionBindingStatus.REQUESTED
    cursor_ref: str | None = None
    checkpoint_ref: str | None = None
    last_turn_id: str | None = None
    last_execution_id: str | None = None
    stop_reason: str | None = None
    lease_owner_id: str | None = None
    lease_expires_at: float | None = None
    heartbeat_at: float | None = None
    retry_not_before_at: float | None = None
    recovery_attempts: int = Field(default=0, ge=0)
    created_by: str = Field(min_length=1)
    updated_by: str = Field(min_length=1)
    change_reason: str = Field(min_length=1)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


class GoalExecutionBindingEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: f"goal-binding-event-{uuid.uuid4().hex}")
    binding_id: str
    goal_id: str
    event_type: str
    actor_id: str
    reason: str
    occurred_at: float = Field(default_factory=time.time)


class GoalExecutionBindingState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    bindings: list[GoalExecutionBinding] = Field(default_factory=list)
    events: list[GoalExecutionBindingEvent] = Field(default_factory=list)
