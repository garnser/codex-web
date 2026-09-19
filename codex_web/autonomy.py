from __future__ import annotations

import time
import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.action_intents import ActionIntentCreate
from codex_web.compatibility import ContractSpec


AUTONOMY_STATE_CONTRACT = ContractSpec("autonomy-state", "1.0", ("1.0",))


class AutonomyMode(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    KILLED = "killed"


class AutonomyCycleOutcome(StrEnum):
    DETERMINISTIC = "deterministic"
    SKIPPED = "skipped"
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

    @model_validator(mode="after")
    def normalize(self) -> "AutonomyControl":
        object.__setattr__(
            self,
            "trigger_event_types",
            tuple(dict.fromkeys(item.strip() for item in self.trigger_event_types if item.strip())),
        )
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
    updated_at: float = Field(default_factory=time.time)
    updated_by: str = "system"

    def model_post_init(self, __context: Any) -> None:
        AUTONOMY_STATE_CONTRACT.require(self.schema_version)
