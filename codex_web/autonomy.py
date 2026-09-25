from __future__ import annotations

import time
import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.action_intents import ActionIntentCreate
from codex_web.autonomy_policy import (
    AutonomyBreakGlassGrant,
    AutonomyCycleBudgetUsage,
    AutonomyLevel,
    AutonomyPolicy,
)
from codex_web.compatibility import ContractSpec


AUTONOMY_STATE_CONTRACT = ContractSpec("autonomy-state", "2.0", ("1.0", "2.0"))


class AutonomyMode(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    KILLED = "killed"


class AutonomyPauseScope(StrEnum):
    IDENTITY = "identity"
    PROJECT = "project"
    RESOURCE = "resource"


class AutonomyScopedPause(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    id: str = Field(default_factory=lambda: f"autonomy-pause-{uuid.uuid4().hex}")
    scope: AutonomyPauseScope
    scope_id: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=4000)
    created_by: str = Field(min_length=1, max_length=500)
    created_at: float = Field(default_factory=time.time)
    expires_at: float | None = None

    def active(self, now: float | None = None) -> bool:
        current = time.time() if now is None else float(now)
        return self.expires_at is None or self.expires_at > current


class AutonomyScopedPauseCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    scope: AutonomyPauseScope
    scope_id: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=4000)
    expires_at: float | None = None


class AutonomyExclusiveGoalScope(BaseModel):
    """Exclusive autonomous continuation allowlist for one canonical Goal scope."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    goal_id: str = Field(min_length=1, max_length=500)
    project_id: str = Field(min_length=1, max_length=500)
    root_work_item_refs: tuple[str, ...] = ()
    reason: str = Field(min_length=1, max_length=4000)

    @model_validator(mode="after")
    def normalize(self) -> "AutonomyExclusiveGoalScope":
        object.__setattr__(
            self,
            "root_work_item_refs",
            tuple(
                dict.fromkeys(
                    item.strip()
                    for item in self.root_work_item_refs
                    if item.strip()
                )
            ),
        )
        return self


class AutonomyCycleOutcome(StrEnum):
    DETERMINISTIC = "deterministic"
    SKIPPED = "skipped"
    RECOMMENDED = "recommended"
    PREPARED = "prepared"
    DRY_RUN = "dry_run"
    SIMULATED = "simulated"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"


class AutonomyControl(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: AutonomyMode = AutonomyMode.ACTIVE
    dry_run: bool = False
    simulation: bool = False
    reasoning_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    cooldown_seconds: float = Field(default=30.0, ge=0.0, le=86400.0)
    max_reasoning_attempts: int = Field(default=2, ge=1, le=5)
    reasoning_backoff_seconds: float = Field(default=1.0, ge=0.0, le=60.0)
    max_recursion_depth: int = Field(default=4, ge=0, le=16)
    max_actions_per_cycle: int = Field(default=4, ge=0, le=32)
    trigger_event_types: tuple[str, ...] = ()
    scoped_pauses: tuple[AutonomyScopedPause, ...] = ()
    exclusive_goal_scope: AutonomyExclusiveGoalScope | None = None
    policy: AutonomyPolicy = Field(default_factory=AutonomyPolicy)

    @model_validator(mode="after")
    def normalize(self) -> "AutonomyControl":
        object.__setattr__(
            self,
            "trigger_event_types",
            tuple(dict.fromkeys(item.strip() for item in self.trigger_event_types if item.strip())),
        )
        seen = set()
        pauses = []
        for item in self.scoped_pauses:
            key = (item.scope.value, item.scope_id, item.id)
            if key in seen:
                continue
            seen.add(key)
            pauses.append(item)
        object.__setattr__(self, "scoped_pauses", tuple(pauses))
        return self


class AutonomyControlUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: AutonomyMode | None = None
    dry_run: bool | None = None
    simulation: bool | None = None
    reasoning_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    cooldown_seconds: float | None = Field(default=None, ge=0.0, le=86400.0)
    max_reasoning_attempts: int | None = Field(default=None, ge=1, le=5)
    reasoning_backoff_seconds: float | None = Field(default=None, ge=0.0, le=60.0)
    max_recursion_depth: int | None = Field(default=None, ge=0, le=16)
    max_actions_per_cycle: int | None = Field(default=None, ge=0, le=32)
    trigger_event_types: tuple[str, ...] | None = None
    policy: AutonomyPolicy | None = None


class AutonomyObservation(BaseModel):
    """Deterministic result produced before any reasoning is permitted."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    deterministic_resolved: bool
    reasoning_score: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str = Field(min_length=1)


class AutonomyReasoningResult(BaseModel):
    """Bounded reasoning output. External effects remain ActionIntents."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: str = ""
    actions: tuple[ActionIntentCreate, ...] = ()
    model_input_tokens: int = Field(default=0, ge=0)
    model_output_tokens: int = Field(default=0, ge=0)
    model_cost_usd: float = Field(default=0.0, ge=0.0)
    model_provider_id: str | None = None
    model_id: str | None = None
    model_revision: str | None = None
    prompt_template_id: str | None = None
    model_routing_reason: str | None = None
    model_invocation_ids: tuple[str, ...] = ()

    @property
    def model_tokens(self) -> int:
        return self.model_input_tokens + self.model_output_tokens


class AutonomyCycleRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"autonomy-cycle-{uuid.uuid4().hex}")
    cycle_key: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    source: str = Field(min_length=1)
    correlation_id: str | None = None
    causation_id: str | None = None
    organization_id: str | None = None
    workspace_id: str | None = None
    recursion_depth: int = Field(ge=0)
    reasoning_score: float = Field(ge=0.0, le=1.0)
    reasoning_invoked: bool = False
    reasoning_attempts: int = Field(default=0, ge=0)
    action_count: int = Field(default=0, ge=0)
    action_intent_ids: tuple[str, ...] = ()
    approval_request_ids: tuple[str, ...] = ()
    autonomy_level: AutonomyLevel | None = None
    policy_fingerprint: str | None = None
    break_glass_grant_id: str | None = None
    budget_usage: AutonomyCycleBudgetUsage = Field(default_factory=AutonomyCycleBudgetUsage)
    model_provider_id: str | None = None
    model_id: str | None = None
    model_revision: str | None = None
    prompt_template_id: str | None = None
    model_routing_reason: str | None = None
    model_invocation_ids: tuple[str, ...] = ()
    outcome: AutonomyCycleOutcome
    reason: str
    started_at: float = Field(default_factory=time.time)
    completed_at: float = Field(default_factory=time.time)
    last_error: str | None = None


class AutonomyDeadLetter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"autonomy-dead-{uuid.uuid4().hex}")
    cycle_id: str
    cycle_key: str
    event_id: str
    event_type: str
    reason: str
    attempts: int = Field(ge=0)
    error: str | None = None
    created_at: float = Field(default_factory=time.time)


class AutonomyState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = AUTONOMY_STATE_CONTRACT.current
    control: AutonomyControl = Field(default_factory=AutonomyControl)
    cycles: list[AutonomyCycleRecord] = Field(default_factory=list)
    dead_letters: list[AutonomyDeadLetter] = Field(default_factory=list)
    break_glass_grants: list[AutonomyBreakGlassGrant] = Field(default_factory=list)
    updated_at: float = Field(default_factory=time.time)
    updated_by: str = "system"

    def model_post_init(self, __context: Any) -> None:
        AUTONOMY_STATE_CONTRACT.require(self.schema_version)
