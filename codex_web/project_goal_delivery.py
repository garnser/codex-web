"""Original Project Goal input, retained by the existing Goal binding owner."""
from typing import Literal
import hashlib
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProjectGoalDelivery(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    schema_version: Literal['1.0'] = '1.0'
    id: str = Field(min_length=1)
    goal_revision: int = Field(ge=1, strict=True)
    work_item_refs: tuple[str, ...]
    requested_by: str = Field(min_length=1)
    continuation_owner_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    assignment_id: str = Field(min_length=1)
    assignment_fingerprint: str = Field(pattern=r'^[0-9a-f]{64}$')
    message: str = Field(min_length=1, max_length=16000)
    message_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    phase: Literal['reserved', 'queued', 'started', 'terminal_pending_usage', 'settled', 'unknown'] = 'reserved'
    queued_id: str | None = Field(default=None, min_length=1)
    admission_attempt_id: str | None = Field(default=None, min_length=1)
    native_turn_id: str | None = Field(default=None, min_length=1)
    completion_succeeded: bool | None = Field(default=None, strict=True)
    usage_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def coherent_original(self):
        if hashlib.sha256(self.message.encode()).hexdigest() != self.message_sha256:
            raise ValueError('Original Project Goal message checksum differs')
        if (self.queued_id is None) != (self.admission_attempt_id is None):
            raise ValueError('Original Project Goal queue attribution is incomplete')
        if self.phase in {'queued', 'started', 'terminal_pending_usage', 'settled'} and self.queued_id is None:
            raise ValueError('Original Project Goal queue attribution is required')
        if self.phase in {'started', 'terminal_pending_usage', 'settled'} and self.native_turn_id is None:
            raise ValueError('Original Project Goal START attribution is required')
        if self.phase in {'terminal_pending_usage', 'settled'} and self.completion_succeeded is None:
            raise ValueError('Original Project Goal terminal outcome is required')
        if self.phase == 'settled' and self.usage_id is None:
            raise ValueError('Original Project Goal usage attribution is required')
        return self


class ProjectGoalProgress(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    reserved_calls: int = Field(default=0, ge=0)
    model_calls: int | None = Field(default=0, ge=0)
    input_tokens: int | None = Field(default=0, ge=0)
    output_tokens: int | None = Field(default=0, ge=0)
    cost_usd: float | None = Field(default=0., ge=0.)
    delivery: ProjectGoalDelivery | None = None
