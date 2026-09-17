from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum

from codex_web.models import TaskSourceIdentity, WorkItemStage
from codex_web.services.task_sources import TaskSourceCanonicalProjection, TaskSourceEvent


class TaskSourceReconciliationOutcome(StrEnum):
    """Deterministic result of evaluating one normalized source event."""

    APPLY = "apply"
    DUPLICATE = "duplicate"
    STALE = "stale"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class TaskSourceReconciliationDecision:
    outcome: TaskSourceReconciliationOutcome
    event_key: str
    reason: str

    @property
    def should_apply(self) -> bool:
        return self.outcome is TaskSourceReconciliationOutcome.APPLY


@dataclass(frozen=True, slots=True)
class TaskSourceDriftFinding:
    code: str
    canonical_value: str | None
    projected_value: str | None


def _authority_key(identity: TaskSourceIdentity) -> tuple[str, str, str]:
    return (
        identity.source_type.strip().casefold(),
        identity.source_instance.strip().rstrip("/"),
        identity.external_id.strip(),
    )


def same_task_source_identity(
    current: TaskSourceIdentity,
    incoming: TaskSourceIdentity,
) -> bool:
    """Return whether two identities refer to the same authoritative item."""

    return _authority_key(current) == _authority_key(incoming)


def task_source_event_key(event: TaskSourceEvent) -> str:
    """Create a stable provider-neutral idempotency key for one normalized event.

    A provider cursor is preferred when available. Otherwise the key is derived
    from normalized identity/event/snapshot facts rather than provider payload
    bytes, so adapters do not leak source-specific schemas into core logic.
    """

    identity = event.identity
    prefix = "|".join(_authority_key(identity))
    if identity.event_cursor:
        return f"{prefix}|cursor:{identity.event_cursor}"

    snapshot = event.snapshot
    payload = {
        "identity": {
            "source_type": identity.source_type,
            "source_instance": identity.source_instance.rstrip("/"),
            "external_id": identity.external_id,
            "revision": identity.revision,
        },
        "event_type": event.event_type,
        "occurred_at": event.occurred_at,
        "snapshot": None
        if snapshot is None
        else {
            "title": snapshot.title,
            "source_state": snapshot.source_state,
            "owners": list(snapshot.owners),
            "labels": list(snapshot.labels),
            "artifact_links": list(snapshot.artifact_links),
        },
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"{prefix}|sha256:{digest}"


class TaskSourceReconciliationPolicy:
    """Provider-neutral stale/idempotency/authority decision boundary."""

    def evaluate(
        self,
        event: TaskSourceEvent,
        *,
        current_identity: TaskSourceIdentity | None,
        last_event_at: float | None = None,
        last_event_key: str | None = None,
    ) -> TaskSourceReconciliationDecision:
        event_key = task_source_event_key(event)

        if current_identity is not None and not same_task_source_identity(
            current_identity,
            event.identity,
        ):
            return TaskSourceReconciliationDecision(
                outcome=TaskSourceReconciliationOutcome.CONFLICT,
                event_key=event_key,
                reason="incoming event belongs to a different authoritative task source item",
            )

        if last_event_key is not None and event_key == last_event_key:
            return TaskSourceReconciliationDecision(
                outcome=TaskSourceReconciliationOutcome.DUPLICATE,
                event_key=event_key,
                reason="normalized event idempotency key has already been applied",
            )

        if (
            last_event_at is not None
            and event.occurred_at is not None
            and event.occurred_at < last_event_at
        ):
            return TaskSourceReconciliationDecision(
                outcome=TaskSourceReconciliationOutcome.STALE,
                event_key=event_key,
                reason="incoming event predates the last applied authoritative-source event",
            )

        return TaskSourceReconciliationDecision(
            outcome=TaskSourceReconciliationOutcome.APPLY,
            event_key=event_key,
            reason="incoming event is authoritative, new, and not stale",
        )


def task_source_projection_drift(
    *,
    canonical_stage: WorkItemStage,
    canonical_owner: str | None,
    projection: TaskSourceCanonicalProjection,
) -> tuple[TaskSourceDriftFinding, ...]:
    """Return provider-neutral split-brain diagnostics without deciding policy.

    Whether drift should be applied, preserved, or escalated depends on the
    canonical work-item state (for example a pending handoff). This function
    only reports deterministic differences after the provider adapter has
    mapped native source facts into canonical fields.
    """

    findings: list[TaskSourceDriftFinding] = []
    if projection.stage is not None and projection.stage != canonical_stage:
        findings.append(
            TaskSourceDriftFinding(
                code="source_stage_drift",
                canonical_value=canonical_stage,
                projected_value=projection.stage,
            )
        )

    if projection.owner_known:
        canonical = canonical_owner.strip().casefold() if canonical_owner else None
        projected = projection.owner.strip().casefold() if projection.owner else None
        if canonical != projected:
            findings.append(
                TaskSourceDriftFinding(
                    code="source_owner_drift",
                    canonical_value=canonical_owner,
                    projected_value=projection.owner,
                )
            )

    return tuple(findings)
