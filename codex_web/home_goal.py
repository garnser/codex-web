"""Typed retained input/accounting proof inside the canonical Goal binding."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from codex_web.definitions import DefinitionReference

SOURCE = 'home-goal-continuation:'


class HomeGoalDelivery(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    schema_version: Literal['1.0'] = '1.0'
    id: str = Field(min_length=1, max_length=200)
    requested_execution_id: str = Field(min_length=1, max_length=300)
    goal_revision: int = Field(ge=1)
    requested_by: str = Field(min_length=1)
    home_revision: int = Field(ge=1)
    thread_id: str = Field(min_length=1)
    assignment_id: str = Field(min_length=1)
    execution_id: str = Field(min_length=1)
    execution_workspace_id: str = Field(min_length=1)
    worker_id: str = Field(min_length=1)
    fence: int = Field(ge=1)
    profile_id: str = Field(min_length=1)
    profile_revision: int = Field(ge=1)
    profile_record_id: str = Field(min_length=1)
    definition: DefinitionReference
    message: str = Field(min_length=1, max_length=16000)
    message_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    phase: Literal['reserved', 'queued', 'started', 'terminal_pending_usage', 'settled', 'unknown'] = 'reserved'
    queued_id: str | None = None
    admission_attempt_id: str | None = None
    native_turn_id: str | None = None
    usage_id: str | None = None
    completion_succeeded: bool | None = None


class HomeGoalProgress(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    # Reservation is a conservative call ceiling, never fabricated usage.
    reserved_calls: int = Field(default=0, ge=0)
    model_calls: int = Field(default=0, ge=0)
    cost_usd: float | None = Field(default=0., ge=0.)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    delivery: HomeGoalDelivery | None = None
