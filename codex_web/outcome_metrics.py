"""Typed human observations; persisted as canonical review Evidence."""
from __future__ import annotations

from enum import StrEnum
from pydantic import BaseModel, ConfigDict, Field


class UserOutcome(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    PENDING = "pending"
    UNKNOWN = "unknown"
    REOPENED = "reopened"


class OutcomeFeedbackCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    runtime_usage_id: str = Field(min_length=1, max_length=128)
    outcome: UserOutcome
    correction_required: bool | None = None
    review_count: int | None = Field(default=None, ge=0, le=10000, strict=True)
    intervention_count: int | None = Field(default=None, ge=0, le=10000, strict=True)
    review_seconds: int | None = Field(default=None, ge=0, le=604800, strict=True)
    workload: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    template_artifact_id: str | None = Field(default=None, min_length=1, max_length=128)
    supersedes_evidence_id: str | None = Field(default=None, min_length=1, max_length=128)
    supersedes_evidence_ids: tuple[str, ...] = Field(default=(), max_length=16)
    idempotency_key: str = Field(min_length=1, max_length=128)


class OutcomeWindow(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    start_at: float = Field(ge=0)
    end_at: float = Field(ge=0)
    as_of: float | None = Field(default=None, ge=0)
