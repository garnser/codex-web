from __future__ import annotations

import time
import uuid
from datetime import time as wall_time
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.compatibility import ContractSpec


SCHEDULER_STATE_CONTRACT = ContractSpec("scheduler-state", "1.0", ("1.0",))


class ScheduleKind(StrEnum):
    ONE_SHOT = "one_shot"
    RECURRING = "recurring"


class RecurrenceKind(StrEnum):
    INTERVAL = "interval"
    DAILY = "daily"


class MisfirePolicy(StrEnum):
    SKIP = "skip"
    FIRE_ONCE = "fire_once"
    BOUNDED_CATCH_UP = "bounded_catch_up"


class ScheduleStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class ScheduleRecurrence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    kind: RecurrenceKind
    interval_seconds: float | None = Field(default=None, ge=1.0, le=31_536_000.0)
    local_time: str | None = None
    timezone: str | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> "ScheduleRecurrence":
        if self.kind == RecurrenceKind.INTERVAL:
            if self.interval_seconds is None:
                raise ValueError("interval recurrence requires interval_seconds")
            if self.local_time is not None or self.timezone is not None:
                raise ValueError("interval recurrence does not accept local_time/timezone")
            return self

        if not self.local_time or not self.timezone:
            raise ValueError("daily recurrence requires local_time and timezone")
        try:
            wall_time.fromisoformat(self.local_time)
        except ValueError as exc:
            raise ValueError("daily recurrence local_time must be ISO HH:MM[:SS]") from exc
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("daily recurrence timezone must be a valid IANA timezone") from exc
        if self.interval_seconds is not None:
            raise ValueError("daily recurrence does not accept interval_seconds")
        return self


class ScheduleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=200)
    tenant_id: str = Field(min_length=1, max_length=200)
    workspace_id: str | None = Field(default=None, max_length=200)
    trigger_type: str = Field(min_length=1, max_length=200)
    payload: dict[str, Any] = Field(default_factory=dict)
    due_at: float
    recurrence: ScheduleRecurrence | None = None
    misfire_policy: MisfirePolicy = MisfirePolicy.FIRE_ONCE
    misfire_grace_seconds: float = Field(default=60.0, ge=0.0, le=86_400.0)
    catch_up_limit: int = Field(default=10, ge=1, le=100)

    @model_validator(mode="after")
    def validate_kind(self) -> "ScheduleCreate":
        if not self.tenant_id.strip():
            raise ValueError("tenant_id must not be empty")
        if self.workspace_id is not None and not self.workspace_id.strip():
            raise ValueError("workspace_id must not be empty when provided")
        return self


class ScheduleRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"schedule-{uuid.uuid4().hex}")
    name: str
    tenant_id: str
    workspace_id: str | None = None
    trigger_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    kind: ScheduleKind
    due_at: float
    recurrence: ScheduleRecurrence | None = None
    misfire_policy: MisfirePolicy
    misfire_grace_seconds: float
    catch_up_limit: int
    status: ScheduleStatus = ScheduleStatus.ACTIVE
    next_run_at: float | None = None
    last_fired_at: float | None = None
    last_scheduled_for: float | None = None
    firing_count: int = Field(default=0, ge=0)
    lease_owner: str | None = None
    lease_expires_at: float | None = None
    revision: int = Field(default=1, ge=1)
    created_at: float = Field(default_factory=time.time)
    created_by: str = "system"
    updated_at: float = Field(default_factory=time.time)
    updated_by: str = "system"

    @classmethod
    def from_create(
        cls,
        payload: ScheduleCreate,
        *,
        actor_id: str,
        now: float | None = None,
    ) -> "ScheduleRecord":
        timestamp = time.time() if now is None else float(now)
        return cls(
            name=payload.name,
            tenant_id=payload.tenant_id,
            workspace_id=payload.workspace_id,
            trigger_type=payload.trigger_type,
            payload=dict(payload.payload),
            kind=(
                ScheduleKind.RECURRING
                if payload.recurrence is not None
                else ScheduleKind.ONE_SHOT
            ),
            due_at=float(payload.due_at),
            recurrence=payload.recurrence,
            misfire_policy=payload.misfire_policy,
            misfire_grace_seconds=payload.misfire_grace_seconds,
            catch_up_limit=payload.catch_up_limit,
            next_run_at=float(payload.due_at),
            created_at=timestamp,
            created_by=actor_id,
            updated_at=timestamp,
            updated_by=actor_id,
        )


class SchedulerState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEDULER_STATE_CONTRACT.current
    schedules: dict[str, ScheduleRecord] = Field(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        SCHEDULER_STATE_CONTRACT.require(self.schema_version)
