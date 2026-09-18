from __future__ import annotations

import time
from typing import Any

from codex_web.models import WorkItemState


class WorkItemWatchdogCandidatePolicy:
    """Own work-item watchdog classification and candidate ordering."""

    def __init__(self, host: Any) -> None:
        self.host = host

    def orchestrator_reason(
        self,
        state: WorkItemState,
        *,
        now: float,
    ) -> tuple[str, float] | None:
        h = self.host
        if state.current_stage == "closed" or state.closed_at:
            return None
        if h._work_item_split_brain_findings(state):
            return ("split_brain", now - state.updated_at)
        if state.handoff and state.handoff.status == "pending":
            pending_age = now - state.handoff.requested_at
            if pending_age >= min(h._work_item_handoff_timeout_seconds(), 300.0):
                return ("pending_handoff", pending_age)
        owner = h._coerce_owner(state.current_owner or state.next_owner)
        if not owner:
            return ("unowned", now - state.updated_at)
        age = now - state.last_meaningful_update_at
        if state.current_stage == "failed_with_action_owner":
            return ("blocked_lane", age)
        if age >= h._work_item_sla_threshold_seconds(state):
            return ("stale_lane", age)
        return None

    def orchestrator_candidates(
        self,
        project_id: str,
    ) -> list[tuple[str, WorkItemState, float]]:
        now = time.time()
        candidates: list[tuple[str, WorkItemState, float]] = []
        for state in self.host._load_work_item_states().values():
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
        for state in self.host._load_work_item_states().values():
            if state.project_id != project_id or state.current_stage == "closed" or state.closed_at:
                continue
            findings = self.host._work_item_split_brain_findings(state)
            if findings:
                candidates.append((state, findings))
        candidates.sort(key=lambda item: (0 if item[0].release_gate else 1, item[0].ref))
        return candidates


def install_work_item_watchdog_candidate_policy(
    app: Any,
    host: Any,
) -> WorkItemWatchdogCandidatePolicy:
    existing = getattr(app.state, "work_item_watchdog_candidate_policy", None)
    if isinstance(existing, WorkItemWatchdogCandidatePolicy) and existing.host is host:
        policy = existing
    else:
        policy = WorkItemWatchdogCandidatePolicy(host)
        app.state.work_item_watchdog_candidate_policy = policy

    host._orchestrator_watch_reason = policy.orchestrator_reason
    host._orchestrator_watchdog_candidates = policy.orchestrator_candidates
    host._split_brain_watchdog_candidates = policy.split_brain_candidates
    return policy
