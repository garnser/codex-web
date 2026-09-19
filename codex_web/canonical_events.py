from __future__ import annotations

from enum import StrEnum


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


def task_source_event_type(provider_event_type: str) -> CanonicalEventType:
    normalized = str(provider_event_type or "").strip()
    if not normalized:
        raise ValueError("provider event type must not be empty")
    return CanonicalEventType.TASK_SOURCE
