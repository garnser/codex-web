from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from codex_web.models import WorkItemState
from codex_web.services.task_source_conformance import TaskSourceConformanceSuite
from codex_web.services.task_source_reconciliation import (
    TaskSourceReconciliationDecision,
    TaskSourceReconciliationOutcome,
    TaskSourceReconciliationPolicy,
)
from codex_web.services.task_source_work_items import TaskSourceWorkItemProjector
from codex_web.services.task_sources import TaskSource, TaskSourceEvent


@dataclass(frozen=True, slots=True)
class TaskSourceEventReconciliationResult:
    state: WorkItemState | None
    decision: TaskSourceReconciliationDecision


class TaskSourceWorkItemEventReconciler:
    """Apply normalized source events to canonical work-item state.

    Provider webhook payloads must already have been normalized by a TaskSource
    adapter. This boundary decides authority/staleness deterministically and then
    delegates state projection to the same local projector used by discovery.
    """

    def __init__(
        self,
        host: Any,
        projector: TaskSourceWorkItemProjector,
        *,
        policy: TaskSourceReconciliationPolicy | None = None,
    ) -> None:
        self.host = host
        self.projector = projector
        self.policy = policy or TaskSourceReconciliationPolicy()
        self.conformance = TaskSourceConformanceSuite()

    def reconcile(
        self,
        source: TaskSource,
        event: TaskSourceEvent,
        *,
        project_id: str,
        event_cursor: str | None = None,
    ) -> TaskSourceEventReconciliationResult:
        self.conformance.validate_event(source, event)
        if event_cursor:
            identity = event.identity.model_copy(update={"event_cursor": event_cursor})
            snapshot = (
                replace(event.snapshot, identity=identity)
                if event.snapshot is not None
                else None
            )
            event = replace(event, identity=identity, snapshot=snapshot)
            self.conformance.validate_event(source, event)

        states = self.host._load_work_item_states()
        state = None
        if event.snapshot is not None:
            state = self.projector._find_existing_state(states, event.snapshot)
        else:
            direct = states.get(event.identity.external_id.strip())
            if direct is not None:
                state = direct

        decision = self.policy.evaluate(
            event,
            current_identity=state.source_identity if state is not None else None,
            last_event_at=state.last_gitlab_event_at if state is not None else None,
        )
        if decision.outcome is not TaskSourceReconciliationOutcome.APPLY:
            if state is not None:
                self.projector.state_machine._append_work_item_event(
                    self.projector.state_machine._work_item_event(
                        state.ref,
                        f"task_source_event_{decision.outcome.value}",
                        payload={
                            "source_type": event.identity.source_type,
                            "event_type": event.event_type,
                            "event_key": decision.event_key,
                            "reason": decision.reason,
                        },
                    )
                )
            return TaskSourceEventReconciliationResult(state=state, decision=decision)

        if event.snapshot is None:
            return TaskSourceEventReconciliationResult(state=state, decision=decision)

        state = self.projector.upsert(
            source,
            event.snapshot,
            project_id=project_id,
        )
        self.projector.state_machine._append_work_item_event(
            self.projector.state_machine._work_item_event(
                state.ref,
                "task_source_event_applied",
                payload={
                    "source_type": event.identity.source_type,
                    "event_type": event.event_type,
                    "event_key": decision.event_key,
                },
            )
        )
        return TaskSourceEventReconciliationResult(state=state, decision=decision)
