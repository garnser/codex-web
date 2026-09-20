from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from codex_web.models import WorkItemState


class WorkItemWatchdogCandidatePolicy:
    """Own work-item watchdog classification and candidate ordering."""

    def __init__(
        self,
        host: Any | None = None,
        *,
        load_states: Callable[[], dict[str, WorkItemState]] | None = None,
        split_brain_findings: Callable[[WorkItemState], list[str]] | None = None,
        handoff_timeout_seconds: Callable[[], float] | None = None,
        coerce_owner: Callable[[str | None], str | None] | None = None,
        sla_threshold_seconds: Callable[[WorkItemState], float] | None = None,
    ) -> None:
        if host is not None:
            load_states = load_states or host._load_work_item_states
            split_brain_findings = (
                split_brain_findings
                or host._work_item_split_brain_findings
            )
            handoff_timeout_seconds = (
                handoff_timeout_seconds
                or host._work_item_handoff_timeout_seconds
            )
            coerce_owner = coerce_owner or host._coerce_owner
            sla_threshold_seconds = (
                sla_threshold_seconds
                or host._work_item_sla_threshold_seconds
            )
        if not all(
            (
                load_states,
                split_brain_findings,
                handoff_timeout_seconds,
                coerce_owner,
                sla_threshold_seconds,
            )
        ):
            raise TypeError(
                "WorkItemWatchdogCandidatePolicy requires explicit dependencies"
            )
        self.load_states = load_states
        self.split_brain_findings = split_brain_findings
        self.handoff_timeout_seconds = handoff_timeout_seconds
        self.coerce_owner = coerce_owner
        self.sla_threshold_seconds = sla_threshold_seconds

    def orchestrator_reason(
        self,
        state: WorkItemState,
        *,
        now: float,
    ) -> tuple[str, float] | None:
                if state.current_stage == "closed" or state.closed_at:
            return None
        if self.split_brain_findings(state):
            return ("split_brain", now - state.updated_at)
        if state.handoff and state.handoff.status == "pending":
            pending_age = now - state.handoff.requested_at
            if pending_age >= min(self.handoff_timeout_seconds(), 300.0):
                return ("pending_handoff", pending_age)
        owner = self.coerce_owner(state.current_owner or state.next_owner)
        if not owner:
            return ("unowned", now - state.updated_at)
        age = now - state.last_meaningful_update_at
        if state.current_stage == "failed_with_action_owner":
            return ("blocked_lane", age)
        if age >= self.sla_threshold_seconds(state):
            return ("stale_lane", age)
        return None

    def orchestrator_candidates(
        self,
        project_id: str,
    ) -> list[tuple[str, WorkItemState, float]]:
        now = time.time()
        candidates: list[tuple[str, WorkItemState, float]] = []
        for state in self.load_states().values():
            if state.project_id != project_id:
                continue
            decision = self.orchestrator_reason(state, now=now)
            if not decision:
                continue
            reason, age = decision
            candidates.append((reason, state, age))
        candidates.sort(
            key=lambda item: (
                0 if item[1].release_gate else 1,
                0 if item[0] == "unowned" else 1,
                -item[2],
                item[1].ref,
            )
        )
        return candidates

    def split_brain_candidates(
        self,
        project_id: str,
    ) -> list[tuple[WorkItemState, list[str]]]:
        candidates: list[tuple[WorkItemState, list[str]]] = []
        for state in self.load_states().values():
            if state.project_id != project_id or state.current_stage == "closed" or state.closed_at:
                continue
            findings = self.split_brain_findings(state)
            if findings:
                candidates.append((state, findings))
        candidates.sort(key=lambda item: (0 if item[0].release_gate else 1, item[0].ref))
        return candidates


def install_work_item_watchdog_candidate_policy(
    app: Any,
    host: Any,
    *,
    load_states: Callable[[], dict[str, WorkItemState]] | None = None,
    split_brain_findings: Callable[[WorkItemState], list[str]] | None = None,
    handoff_timeout_seconds: Callable[[], float] | None = None,
    coerce_owner: Callable[[str | None], str | None] | None = None,
    sla_threshold_seconds: Callable[[WorkItemState], float] | None = None,
) -> WorkItemWatchdogCandidatePolicy:
    existing = getattr(app.state, "work_item_watchdog_candidate_policy", None)
    if isinstance(existing, WorkItemWatchdogCandidatePolicy):
        policy = existing
    else:
        policy = WorkItemWatchdogCandidatePolicy(
            host,
            load_states=load_states,
            split_brain_findings=split_brain_findings,
            handoff_timeout_seconds=handoff_timeout_seconds,
            coerce_owner=coerce_owner,
            sla_threshold_seconds=sla_threshold_seconds,
        )
        app.state.work_item_watchdog_candidate_policy = policy

    host._orchestrator_watch_reason = policy.orchestrator_reason
    host._orchestrator_watchdog_candidates = policy.orchestrator_candidates
    host._split_brain_watchdog_candidates = policy.split_brain_candidates
    return policy
