from __future__ import annotations

import time
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class CanonicalEventType(StrEnum):
    """Stable code-owned taxonomy for cross-domain event classes."""

    WORK_TRANSITION = "work.transition"
    PULL_REQUEST = "code.pull_request"
    CI_PIPELINE = "ci.pipeline"
    DEPLOYMENT = "deployment.status"
    INCIDENT = "incident.status"
    FAILURE = "failure.observed"
    TASK_SOURCE = "task_source.event"
    SCHEDULE = "schedule.due"
    APPROVAL = "approval.transition"
    DECISION = "decision.transition"
    ATTENTION = "attention.transition"


def task_source_event_type(provider_event_type: str) -> CanonicalEventType:
    normalized = str(provider_event_type or "").strip()
    if not normalized:
        raise ValueError("provider event type must not be empty")
    return CanonicalEventType.TASK_SOURCE


class CanonicalEventOutboxStatus(StrEnum):
    PENDING = "pending"
    PUBLISHED = "published"
    DEAD_LETTER = "dead_letter"


class CanonicalEventOutboxRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    status: CanonicalEventOutboxStatus = CanonicalEventOutboxStatus.PENDING
    attempts: int = Field(default=0, ge=0)
    transport_backend_id: str | None = None
    transport_delivery_id: str | None = None
    not_before: float | None = None
    last_error_code: str | None = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    published_at: float | None = None


class CanonicalEventInboxReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    transport_backend_id: str
    transport_delivery_id: str
    consumer_id: str
    acknowledged_at: float = Field(default_factory=time.time)
