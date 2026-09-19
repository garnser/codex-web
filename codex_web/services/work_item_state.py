from __future__ import annotations

import json
import time
from typing import Any

from fastapi import HTTPException

from codex_web.models import (
    WorkItemAckCreate,
    WorkItemEvent,
    WorkItemHandoff,
    WorkItemHandoffCreate,
    WorkItemProgressUpdate,
    WorkItemState,
)
from codex_web.services.work_item_transitions import WorkItemTransitionService


class WorkItemStateMachine:
    """Canonical work-item transitions, handoffs, validation, and audit state."""

    def __init__(self, host: Any, _legacy_provider: Any | None = None) -> None:
        # The optional second argument is retained temporarily for constructor
        # compatibility. Canonical state no longer owns provider transports.
        self.host = host
        self.transitions = WorkItemTransitionService()

    def _append_work_item_event(self, event: WorkItemEvent) -> None:
        self.host.DATA_DIR.mkdir(exist_ok=True)
        with self.host.WORK_ITEM_EVENTS_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.model_dump(), separators=(",", ":")) + "\n")

    def _work_item_event(
        self,
        ref: str,
        event_type: str,
        *,
        actor: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> WorkItemEvent:
        return WorkItemEvent(
            ref=ref,
            event_type=event_type,
            created_at=time.time(),
            actor=actor,
            payload=payload or {},
        )

    def _work_item_state_public(self, state: WorkItemState) -> dict[str, Any]:
        state = self._ensure_work_item_lane_defaults(state)
        payload = state.model_dump()
        payload["isConfirmationPending"] = bool(
            state.handoff and state.handoff.status == "pending"
        )
        payload["isReleaseStage"] = state.current_stage in {
            "ready_for_validation",
            "validation_running",
            "failed_with_action_owner",
            "ready_to_close",
        }
        routing_errors = self._work_item_split_brain_findings(state)
        payload["routingErrors"] = routing_errors
        payload["routingError"] = routing_errors[0] if routing_errors else None
        return payload

    def _normalize_work_item_stage(
        self,
        stage: str | None,
        *,
        fallback: str = "implementation_active",
    ) -> str:
        normalized = (stage or "").strip().lower()
        allowed = {
            "implementation_active",
            "ready_for_validation",
            "validation_running",
            "failed_with_action_owner",
            "ready_to_close",
            "closed",
        }
        return normalized if normalized in allowed else fallback

    def _transition_work_item_stage(
        self,
        state: WorkItemState,
        target_stage: str,
        *,
        source: str,
        external_projection: bool = False,
    ) -> WorkItemState:
        normalized = self._normalize_work_item_stage(
            target_stage,
            fallback=state.current_stage,
        )
        return self.transitions.transition(
            state,
            normalized,
            source=source,
            external_projection=external_projection,
        )

    def _normalize_artifact_state(
        self,
        artifact_state: str | None,
        *,
        fallback: str = "branch",
    ) -> str:
        normalized = (artifact_state or "").strip().lower()
        allowed = {"branch", "merge_request", "merged_main", "tag_pipeline"}
        return normalized if normalized in allowed else fallback

    def _normalize_blocking_findings(self, findings: list[str] | None) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in findings or []:
            text = " ".join(str(raw or "").split()).strip()
            if not text or text in seen:
                continue
            seen.add(text)
            normalized.append(text)
        return normalized[:20]

    def _ensure_work_item_lane_defaults(self, state: WorkItemState) -> WorkItemState:
        state.validation_owner = (
            self._coerce_owner(state.validation_owner)
            or self.host.DEFAULT_VALIDATION_OWNER
        )
        state.release_owner = (
            self._coerce_owner(state.release_owner)
            or self.host.DEFAULT_RELEASE_OWNER
        )
        if not self._coerce_owner(state.implementation_owner):
            candidates = (
                state.current_owner,
                state.next_owner,
                state.handoff.from_agent if state.handoff else None,
            )
            for candidate in candidates:
                owner = self._coerce_owner(candidate)
                if (
                    owner
                    and owner not in self.host.NON_IMPLEMENTATION_OWNERS
                    and owner
                    not in {
                        self._coerce_owner(state.validation_owner),
                        self._coerce_owner(state.release_owner),
                    }
                ):
                    state.implementation_owner = owner
                    break
        state.artifact_state = self._normalize_artifact_state(
            state.artifact_state,
            fallback="branch",
        )
        state.blocking_findings = self._normalize_blocking_findings(
            state.blocking_findings
        )
        return state

    def _infer_artifact_state_from_state(self, state: WorkItemState) -> str:
        state = self._ensure_work_item_lane_defaults(state)
        current_owner = self._coerce_owner(state.current_owner)
        current_artifact = self._normalize_artifact_state(
            state.artifact_state,
            fallback="branch",
        )
        if current_owner == self._coerce_owner(state.release_owner):
            if current_artifact == "tag_pipeline":
                return "tag_pipeline"
            return "merged_main"
        if current_owner == self._coerce_owner(state.validation_owner):
            if current_artifact in {"merged_main", "tag_pipeline"}:
                return current_artifact
            return "merge_request" if state.mr_refs else "branch"
        if (
            current_artifact in {"merged_main", "tag_pipeline"}
            and state.current_stage == "closed"
        ):
            return current_artifact
        return "branch"

    def _routing_error_detail(
        self,
        *,
        code: str,
        message: str,
        from_agent: str | None,
        to_agent: str | None,
        artifact_state: str | None,
    ) -> dict[str, Any]:
        return {
            "code": code,
            "message": message,
            "from_agent": self._coerce_owner(from_agent) or from_agent,
            "to_agent": self._coerce_owner(to_agent) or to_agent,
            "artifact_state": self._normalize_artifact_state(
                artifact_state,
                fallback="branch",
            ),
        }

    def _validate_handoff_edge(
        self,
        state: WorkItemState,
        *,
        from_agent: str | None,
        to_agent: str | None,
        artifact_state: str | None,
    ) -> dict[str, Any] | None:
        state = self._ensure_work_item_lane_defaults(state)
        sender = self._coerce_owner(from_agent)
        recipient = self._coerce_owner(to_agent)
        artifact = self._normalize_artifact_state(
            artifact_state or state.artifact_state,
            fallback=self._infer_artifact_state_from_state(state),
        )
        implementation_owner = self._coerce_owner(state.implementation_owner)
        validation_owner = self._coerce_owner(state.validation_owner)
        release_owner = self._coerce_owner(state.release_owner)
        orchestrator_override = sender == "orchestrator"
        if recipient == release_owner and sender == implementation_owner and not orchestrator_override:
            if artifact == "branch":
                return self._routing_error_detail(
                    code="branch_only_artifact",
                    message=(
                        "Direct implementation->release handoff is blocked while the artifact "
                        "is only on a branch. Hand off to the validation owner first."
                    ),
                    from_agent=sender,
                    to_agent=recipient,
                    artifact_state=artifact,
                )
            if artifact == "merge_request":
                return self._routing_error_detail(
                    code="missing_merge",
                    message=(
                        "Direct implementation->release handoff is blocked while the artifact "
                        "is only on an open merge request. Merge to main first or reroute "
                        "explicitly via Orchestrator."
                    ),
                    from_agent=sender,
                    to_agent=recipient,
                    artifact_state=artifact,
                )
            return self._routing_error_detail(
                code="wrong_lane",
                message=(
                    "Direct implementation->release handoff requires explicit Orchestrator "
                    "reassignment."
                ),
                from_agent=sender,
                to_agent=recipient,
                artifact_state=artifact,
            )
        if (
            recipient == validation_owner
            and artifact not in {"branch", "merge_request"}
            and not (
                orchestrator_override
                and artifact in {"merged_main", "tag_pipeline"}
            )
        ):
            return self._routing_error_detail(
                code="wrong_lane",
                message=(
                    "Validation ownership requires an MR-ready artifact on a branch or open "
                    "merge request, unless Orchestrator explicitly reassigns a merged-main "
                    "or tag-pipeline validation lane."
                ),
                from_agent=sender,
                to_agent=recipient,
                artifact_state=artifact,
            )
        if (
            sender == validation_owner
            and recipient == release_owner
            and artifact not in {"merged_main", "tag_pipeline"}
        ):
            return self._routing_error_detail(
                code="missing_merge" if artifact == "merge_request" else "wrong_lane",
                message=(
                    "Release ownership requires a merged mainline or tag pipeline artifact "
                    "after validation/merge."
                ),
                from_agent=sender,
                to_agent=recipient,
                artifact_state=artifact,
            )
        return None

    def _record_handoff_history(
        self,
        state: WorkItemState,
        handoff: WorkItemHandoff,
        *,
        status: str | None = None,
        acknowledged_at: float | None = None,
        reason_code: str | None = None,
    ) -> WorkItemState:
        entry = handoff.model_copy(deep=True)
        if status is not None:
            entry.status = status
        if acknowledged_at is not None:
            entry.acknowledged_at = acknowledged_at
        if reason_code is not None:
            entry.reason_code = reason_code
        state.handoff_history = (state.handoff_history + [entry])[-100:]
        return state

    def _archive_active_handoff(
        self,
        state: WorkItemState,
        *,
        now: float,
        status: str,
        reason_code: str | None = None,
    ) -> WorkItemState:
        if state.handoff:
            state = self._record_handoff_history(
                state,
                state.handoff,
                status=status,
                acknowledged_at=now,
                reason_code=reason_code,
            )
            state.handoff = None
        return state

    def _derived_status_label_for_work_item(self, state: WorkItemState) -> str | None:
        if state.current_stage == "closed":
            return None
        if state.handoff and state.handoff.status == "pending":
            return "status::awaiting confirmation"
        if state.current_stage == "failed_with_action_owner":
            return "status::blocked"
        if state.current_stage in {
            "implementation_active",
            "ready_for_validation",
            "validation_running",
            "ready_to_close",
        }:
            return "status::in progress"
        return state.status_label

    def _work_item_split_brain_findings(self, state: WorkItemState) -> list[str]:
        state = self._ensure_work_item_lane_defaults(state)
        findings: list[str] = []
        canonical_status = self._derived_status_label_for_work_item(state)
        current_owner = self._coerce_owner(state.current_owner)
        next_owner = self._coerce_owner(state.next_owner)
        action_owner = self.host._leading_owner_cue_in_action(state.next_action)
        if canonical_status != state.status_label:
            findings.append(
                f"status drift: canonical={canonical_status or 'none'} "
                f"stored={state.status_label or 'none'}"
            )
        if state.handoff and state.handoff.status == "pending":
            handoff_to = self._coerce_owner(state.handoff.to_agent)
            handoff_from = self._coerce_owner(state.handoff.from_agent)
            if state.status_label != "status::awaiting confirmation":
                findings.append("pending handoff without awaiting-confirmation status")
            if next_owner != handoff_to:
                findings.append(
                    f"pending handoff next-owner drift: expected={handoff_to or 'none'} "
                    f"stored={next_owner or 'none'}"
                )
            if current_owner != handoff_from:
                findings.append(
                    f"pending handoff current-owner drift: expected={handoff_from or 'none'} "
                    f"stored={current_owner or 'none'}"
                )
        elif state.handoff and state.handoff.status == "accepted":
            handoff_to = self._coerce_owner(state.handoff.to_agent)
            if state.status_label == "status::awaiting confirmation":
                findings.append("accepted handoff still marked as awaiting confirmation")
            if current_owner != handoff_to:
                findings.append(
                    f"accepted handoff owner drift: expected={handoff_to or 'none'} "
                    f"stored={current_owner or 'none'}"
                )
            if next_owner and next_owner != handoff_to:
                findings.append(
                    f"accepted handoff next-owner drift: expected={handoff_to or 'none'} "
                    f"stored={next_owner or 'none'}"
                )
        elif state.status_label == "status::awaiting confirmation":
            findings.append("awaiting-confirmation status without pending handoff")
        if state.current_stage == "failed_with_action_owner":
            if not (state.blocker or "").strip():
                findings.append("blocked lane missing blocker text")
            if not (next_owner or current_owner):
                findings.append("blocked lane missing actionable owner")
        if state.handoff:
            handoff_error = self._validate_handoff_edge(
                state,
                from_agent=state.handoff.from_agent,
                to_agent=state.handoff.to_agent,
                artifact_state=state.handoff.artifact_state or state.artifact_state,
            )
            if handoff_error:
                findings.append(
                    "routing error: "
                    + str(handoff_error.get("code") or "wrong_lane")
                    + " - "
                    + str(handoff_error.get("message") or "invalid handoff edge")
                )
        if (
            current_owner == self._coerce_owner(state.release_owner)
            and state.artifact_state in {"branch", "merge_request"}
        ):
            findings.append(
                f"routing error: release owner on pre-merge artifact_state={state.artifact_state}"
            )
        expected_action_owner = next_owner or current_owner
        if action_owner and action_owner != expected_action_owner:
            findings.append(
                "next-action owner cue drift: "
                f"action={action_owner} current={current_owner or 'none'} "
                f"next={next_owner or 'none'}"
            )
        return findings

    def _preserve_accepted_handoff_recipient(
        self,
        state: WorkItemState,
        *,
        incoming_owner: str | None,
        incoming_stage: str | None,
        incoming_status_label: str | None,
    ) -> bool:
        state = self._ensure_work_item_lane_defaults(state)
        if not state.handoff or state.handoff.status != "accepted":
            return False
        recipient = self._coerce_owner(state.handoff.to_agent)
        current_owner = self._coerce_owner(state.current_owner)
        candidate_owner = self._coerce_owner(incoming_owner)
        candidate_stage = self._normalize_work_item_stage(
            incoming_stage,
            fallback=state.current_stage,
        )
        if not recipient or current_owner != recipient:
            return False
        if not candidate_owner or candidate_owner == recipient:
            return False
        if candidate_stage in {
            "implementation_active",
            "failed_with_action_owner",
            "closed",
        }:
            return False
        if incoming_status_label == "status::awaiting confirmation":
            return True
        return candidate_stage in {
            "ready_for_validation",
            "validation_running",
            "ready_to_close",
        }

    def _reconcile_blocked_work_item_state(
        self,
        state: WorkItemState,
        *,
        projected_owner: str | None,
        previous_owner: str | None,
        now: float,
    ) -> WorkItemState:
        if state.handoff and state.handoff.status == "pending":
            state = self._archive_active_handoff(
                state,
                now=now,
                status="superseded",
                reason_code="blocked_reconciled",
            )
        owner = (
            self._coerce_owner(projected_owner)
            or self._coerce_owner(state.current_owner)
            or self._coerce_owner(previous_owner)
        )
        if owner:
            state.current_owner = owner
            state.next_owner = owner
        if not (state.blocker or "").strip():
            state.blocker = (
                "Blocked-item reconciliation required from incoming task-source projection."
            )
        if not (state.next_action or "").strip():
            state.next_action = (
                "Reconcile the newly blocked item and either continue work or emit one exact blocker."
            )
        return state

    def _sync_work_item_status_label(self, state: WorkItemState) -> WorkItemState:
        state.status_label = self._derived_status_label_for_work_item(state)
        return state

    def _coerce_owner(self, value: str | None) -> str | None:
        normalized = (value or "").strip().lower()
        if normalized in {"", "none", "null"}:
            return None
        return normalized

    def _normalize_closed_work_item_state(
        self,
        state: WorkItemState,
        *,
        now: float,
        reason_code: str,
        closed_at: float | None = None,
    ) -> WorkItemState:
        if state.handoff:
            state = self._archive_active_handoff(
                state,
                now=now,
                status="superseded",
                reason_code=reason_code,
            )
        state.current_owner = None
        state.next_owner = None
        state.blocker = None
        state.blocking_findings = []
        state.status_label = None
        state.closed_at = closed_at or state.closed_at or now
        return state

    def _touch_work_item_progress(
        self,
        state: WorkItemState,
        *,
        actor: str | None = None,
        current_owner: str | None = None,
        current_stage: str | None = None,
        next_action: str | None = None,
        next_owner: str | None = None,
        next_owner_present: bool = False,
        blocker: str | None = None,
        blocker_present: bool = False,
        blocking_findings: list[str] | None = None,
        blocking_findings_present: bool = False,
        release_gate: bool | None = None,
        status_label: str | None = None,
        artifact_state: str | None = None,
        event_type: str = "progress_updated",
        note: str | None = None,
    ) -> WorkItemState:
        state = self._ensure_work_item_lane_defaults(state)
        now = time.time()
        previous_owner = self._coerce_owner(state.current_owner)
        previous_stage = state.current_stage
        incoming_stage = (
            self._normalize_work_item_stage(current_stage, fallback=state.current_stage)
            if current_stage is not None
            else None
        )
        preserve_accepted_recipient = self._preserve_accepted_handoff_recipient(
            state,
            incoming_owner=current_owner,
            incoming_stage=incoming_stage,
            incoming_status_label=status_label,
        )
        accepted_recipient = (
            self._coerce_owner(state.handoff.to_agent)
            if state.handoff and state.handoff.status == "accepted"
            else None
        )
        if current_stage is not None:
            state = self._transition_work_item_stage(
                state,
                incoming_stage,
                source=event_type,
            )
        if current_owner is not None and not preserve_accepted_recipient:
            state.current_owner = self._coerce_owner(current_owner)
        if next_action is not None:
            state.next_action = next_action or None
        if next_owner_present:
            incoming_next_owner = self._coerce_owner(next_owner)
            if not (
                preserve_accepted_recipient
                and accepted_recipient
                and incoming_next_owner
                and incoming_next_owner != accepted_recipient
            ):
                state.next_owner = incoming_next_owner
        if blocker_present:
            state.blocker = blocker or None
        if blocking_findings_present:
            state.blocking_findings = self._normalize_blocking_findings(
                blocking_findings
            )
        if release_gate is not None:
            state.release_gate = release_gate
        if status_label is not None:
            state.status_label = status_label or None
        if artifact_state is not None:
            state.artifact_state = self._normalize_artifact_state(
                artifact_state,
                fallback=state.artifact_state,
            )
        if state.current_stage == "closed":
            state = self._normalize_closed_work_item_state(
                state,
                now=now,
                reason_code="closed_lane",
            )
        elif state.handoff:
            current_owner_normalized = self._coerce_owner(state.current_owner)
            handoff_from = self._coerce_owner(state.handoff.from_agent)
            handoff_to = self._coerce_owner(state.handoff.to_agent)
            archive_resolved_handoff = False
            if state.handoff.status == "pending":
                archive_resolved_handoff = incoming_stage in {
                    "implementation_active",
                    "failed_with_action_owner",
                } or (
                    status_label is not None
                    and status_label != "status::awaiting confirmation"
                )
                if (
                    current_owner_normalized
                    and current_owner_normalized not in {handoff_from, handoff_to}
                ):
                    archive_resolved_handoff = True
            elif state.handoff.status == "accepted":
                archive_resolved_handoff = bool(
                    current_owner_normalized
                    and current_owner_normalized != handoff_to
                )
                if (
                    not archive_resolved_handoff
                    and incoming_stage
                    in {"implementation_active", "failed_with_action_owner"}
                    and current_owner_normalized == handoff_from
                ):
                    archive_resolved_handoff = True
            elif state.handoff.status == "rejected":
                archive_resolved_handoff = True
            if archive_resolved_handoff:
                state = self._archive_active_handoff(
                    state,
                    now=now,
                    status="superseded",
                    reason_code="new_canonical_progress",
                )
                if (
                    status_label is None
                    and state.current_stage
                    in {"implementation_active", "failed_with_action_owner"}
                ):
                    state.status_label = None
        if (
            state.current_stage == "implementation_active"
            and not (state.handoff and state.handoff.status == "pending")
        ):
            state.blocker = None
            state.blocking_findings = []
            if self._coerce_owner(state.next_owner) != self._coerce_owner(
                state.current_owner
            ):
                state.next_owner = None
        if artifact_state is None:
            state.artifact_state = self._infer_artifact_state_from_state(state)
        state = self._ensure_work_item_lane_defaults(state)
        current_owner_normalized = self._coerce_owner(state.current_owner)
        actor_normalized = self._coerce_owner(actor)
        refresh_owner_activity = (
            state.current_stage == "closed"
            or previous_owner != current_owner_normalized
            or previous_stage != state.current_stage
            or (
                actor_normalized is not None
                and actor_normalized == current_owner_normalized
            )
            or state.last_owner_activity_at is None
        )
        state.updated_at = now
        state.last_meaningful_update_at = now
        if refresh_owner_activity:
            state.last_owner_activity_at = now
        if note:
            state.notes = (state.notes + [note])[-20:]
        self._append_work_item_event(
            self._work_item_event(
                state.ref,
                event_type,
                actor=actor,
                payload={
                    "current_owner": state.current_owner,
                    "current_stage": state.current_stage,
                    "next_action": state.next_action,
                    "next_owner": state.next_owner,
                    "blocker": state.blocker,
                    "blocking_findings": state.blocking_findings,
                    "artifact_state": state.artifact_state,
                    "release_gate": state.release_gate,
                    "status_label": state.status_label,
                },
            )
        )
        return state

    def _work_item_state(self, ref: str) -> WorkItemState:
        states = self.host._load_work_item_states()
        state = states.get(ref)
        if state is None:
            raise HTTPException(status_code=404, detail="Work item state not found")
        return state

    def _save_work_item_state(self, state: WorkItemState) -> WorkItemState:
        states = self.host._load_work_item_states()
        states[state.ref] = state
        self.host._save_work_item_states(states)
        return state

    def bind_provenance(
        self,
        ref: str,
        *,
        goal_id: str | None,
        decision_id: str,
        action_intent_id: str,
        actor_id: str,
    ) -> WorkItemState:
        state = self._work_item_state(ref)
        if state.decision_id not in {None, decision_id}:
            raise HTTPException(
                status_code=409,
                detail="work item is already attributed to a different Decision",
            )
        if state.goal_id not in {None, goal_id}:
            raise HTTPException(
                status_code=409,
                detail="work item is already attributed to a different Goal",
            )
        if (
            state.originating_action_intent_id is not None
            and state.originating_action_intent_id != action_intent_id
        ):
            raise HTTPException(
                status_code=409,
                detail="work item is already attributed to a different ActionIntent",
            )
        now = time.time()
        state = state.model_copy(
            update={
                "goal_id": goal_id,
                "decision_id": decision_id,
                "originating_action_intent_id": action_intent_id,
                "updated_at": now,
                "last_meaningful_update_at": now,
            }
        )
        self._append_work_item_event(
            self._work_item_event(
                ref,
                "work_item_provenance_bound",
                actor=actor_id,
                payload={
                    "goal_id": goal_id,
                    "decision_id": decision_id,
                    "action_intent_id": action_intent_id,
                },
            )
        )
        return self._save_work_item_state(state)

    def _structured_handoff(
        self,
        ref: str,
        payload: WorkItemHandoffCreate,
    ) -> WorkItemState:
        state = self._work_item_state(ref)
        state = self._ensure_work_item_lane_defaults(state)
        now = time.time()
        from_agent = self._coerce_owner(payload.from_agent) or payload.from_agent
        to_agent = self._coerce_owner(payload.to_agent) or payload.to_agent
        artifact_state = self._normalize_artifact_state(
            payload.artifact_state,
            fallback=self._infer_artifact_state_from_state(state),
        )
        handoff_error = self._validate_handoff_edge(
            state,
            from_agent=from_agent,
            to_agent=to_agent,
            artifact_state=artifact_state,
        )
        if handoff_error:
            raise HTTPException(status_code=409, detail=handoff_error)
        previous_stage = state.current_stage
        target_stage = self._normalize_work_item_stage(
            payload.current_stage,
            fallback=(
                "ready_for_validation"
                if self._coerce_owner(payload.to_agent)
                == self._coerce_owner(state.validation_owner)
                else state.current_stage
            ),
        )
        state = self._transition_work_item_stage(
            state,
            target_stage,
            source="work-item-handoff",
        )
        if state.handoff:
            archive_status = (
                "superseded" if state.handoff.status == "pending" else state.handoff.status
            )
            state = self._archive_active_handoff(
                state,
                now=now,
                status=archive_status,
                reason_code="new_canonical_handoff",
            )
        state.handoff = WorkItemHandoff(
            from_agent=from_agent,
            to_agent=to_agent,
            reason=payload.reason,
            expected_action=payload.expected_action,
            requested_at=now,
            status="pending",
            artifact_state=artifact_state,
            stage=previous_stage,
        )
        state.current_owner = self._coerce_owner(
            payload.current_owner
        ) or self._coerce_owner(payload.from_agent)
        state.next_owner = self._coerce_owner(payload.to_agent)
        state.next_action = payload.next_action or payload.expected_action or state.next_action
        state.blocker = payload.blocker or None
        if payload.blocking_findings is not None:
            state.blocking_findings = self._normalize_blocking_findings(
                payload.blocking_findings
            )
        state.artifact_state = artifact_state
        state.status_label = "status::awaiting confirmation"
        state.updated_at = now
        state.last_meaningful_update_at = now
        state.last_owner_activity_at = now
        state = self._record_handoff_history(state, state.handoff)
        self._append_work_item_event(
            self._work_item_event(
                ref,
                "handoff_requested",
                actor=payload.from_agent,
                payload={
                    "from_agent": payload.from_agent,
                    "to_agent": payload.to_agent,
                    "expected_action": payload.expected_action,
                    "current_stage": state.current_stage,
                    "next_action": state.next_action,
                    "blocker": state.blocker,
                    "blocking_findings": state.blocking_findings,
                    "artifact_state": state.artifact_state,
                },
            )
        )
        state = self._sync_work_item_status_label(state)
        return self._save_work_item_state(state)

    def _structured_ack(
        self,
        ref: str,
        payload: WorkItemAckCreate,
    ) -> WorkItemState:
        state = self._work_item_state(ref)
        state = self._ensure_work_item_lane_defaults(state)
        actor = self._coerce_owner(payload.actor) or payload.actor
        now = time.time()
        handoff = state.handoff
        if not handoff:
            current_owner = self._coerce_owner(state.current_owner)
            next_owner = self._coerce_owner(state.next_owner)
            if (
                payload.accepted
                and current_owner
                and current_owner != actor
                and next_owner == actor
                and state.current_stage
                in {"ready_for_validation", "validation_running", "ready_to_close"}
            ):
                handoff = WorkItemHandoff(
                    from_agent=current_owner,
                    to_agent=actor,
                    reason="Inferred from canonical validation/release lane state.",
                    expected_action=state.next_action,
                    requested_at=state.last_meaningful_update_at or now,
                    acknowledged_at=now,
                    status="accepted",
                    artifact_state=state.artifact_state,
                    stage=state.current_stage,
                )
            else:
                raise HTTPException(
                    status_code=409,
                    detail="No pending handoff for work item",
                )
        if actor != handoff.to_agent:
            raise HTTPException(
                status_code=409,
                detail="Ack actor does not match handoff recipient",
            )
        artifact_state = self._normalize_artifact_state(
            payload.artifact_state or handoff.artifact_state or state.artifact_state,
            fallback=self._infer_artifact_state_from_state(state),
        )
        if payload.accepted:
            handoff_error = self._validate_handoff_edge(
                state,
                from_agent=handoff.from_agent,
                to_agent=actor,
                artifact_state=artifact_state,
            )
            if handoff_error:
                raise HTTPException(status_code=409, detail=handoff_error)
        target_stage = self._normalize_work_item_stage(
            payload.current_stage,
            fallback=(
                "validation_running"
                if payload.accepted and state.current_stage == "ready_for_validation"
                else (
                    "failed_with_action_owner"
                    if not payload.accepted and payload.blocker
                    else (
                        "implementation_active"
                        if not payload.accepted
                        else state.current_stage
                    )
                )
            ),
        )
        state = self._transition_work_item_stage(
            state,
            target_stage,
            source="work-item-ack",
        )
        state.handoff = handoff
        state.handoff.acknowledged_at = now
        state.handoff.status = "accepted" if payload.accepted else "rejected"
        state.handoff.artifact_state = artifact_state
        state.updated_at = now
        state.last_meaningful_update_at = now
        state.last_owner_activity_at = now
        state.blocker = payload.blocker or None
        if payload.blocking_findings is not None:
            state.blocking_findings = self._normalize_blocking_findings(
                payload.blocking_findings
            )
        state.artifact_state = artifact_state
        if payload.accepted:
            state.current_owner = actor
            state.next_owner = actor
            state.next_action = (
                payload.next_action or state.handoff.expected_action or state.next_action
            )
        else:
            state.current_owner = state.handoff.from_agent
            state.next_owner = state.handoff.from_agent
            state.next_action = payload.next_action or state.next_action
        if payload.next_owner is not None:
            state.next_owner = self._coerce_owner(payload.next_owner)
        state = self._record_handoff_history(
            state,
            state.handoff,
            status=state.handoff.status,
            acknowledged_at=now,
        )
        self._append_work_item_event(
            self._work_item_event(
                ref,
                "handoff_acknowledged" if payload.accepted else "handoff_rejected",
                actor=payload.actor,
                payload={
                    "current_owner": state.current_owner,
                    "current_stage": state.current_stage,
                    "next_action": state.next_action,
                    "blocker": state.blocker,
                    "blocking_findings": state.blocking_findings,
                    "inferred_handoff": bool(
                        state.handoff
                        and state.handoff.reason
                        == "Inferred from canonical validation/release lane state."
                    ),
                },
            )
        )
        state = self._sync_work_item_status_label(state)
        return self._save_work_item_state(state)

    def _structured_progress(
        self,
        ref: str,
        payload: WorkItemProgressUpdate,
    ) -> WorkItemState:
        state = self._work_item_state(ref)
        fields_set = payload.model_fields_set
        state = self._touch_work_item_progress(
            state,
            actor=payload.actor,
            current_owner=payload.current_owner,
            current_stage=payload.current_stage,
            next_action=payload.next_action,
            next_owner=payload.next_owner,
            next_owner_present="next_owner" in fields_set,
            blocker=payload.blocker,
            blocker_present="blocker" in fields_set,
            blocking_findings=payload.blocking_findings,
            blocking_findings_present="blocking_findings" in fields_set,
            release_gate=payload.release_gate,
            status_label=payload.status_label,
            artifact_state=payload.artifact_state,
            note=payload.note,
        )
        state = self._sync_work_item_status_label(state)
        return self._save_work_item_state(state)


def install_work_item_state_machine(
    app: Any,
    host: Any,
    _legacy_provider: Any | None = None,
) -> WorkItemStateMachine:
    existing = getattr(app.state, "work_item_state_machine", None)
    if existing is not None:
        return existing
    machine = WorkItemStateMachine(host)
    app.state.work_item_state_machine = machine
    host._append_work_item_event = machine._append_work_item_event
    host._work_item_event = machine._work_item_event
    host._work_item_state_public = machine._work_item_state_public
    host._normalize_work_item_stage = machine._normalize_work_item_stage
    host._transition_work_item_stage = machine._transition_work_item_stage
    host._normalize_artifact_state = machine._normalize_artifact_state
    host._normalize_blocking_findings = machine._normalize_blocking_findings
    host._ensure_work_item_lane_defaults = machine._ensure_work_item_lane_defaults
    host._infer_artifact_state_from_state = machine._infer_artifact_state_from_state
    host._routing_error_detail = machine._routing_error_detail
    host._validate_handoff_edge = machine._validate_handoff_edge
    host._record_handoff_history = machine._record_handoff_history
    host._archive_active_handoff = machine._archive_active_handoff
    host._derived_status_label_for_work_item = machine._derived_status_label_for_work_item
    host._work_item_split_brain_findings = machine._work_item_split_brain_findings
    host._preserve_accepted_handoff_recipient = machine._preserve_accepted_handoff_recipient
    host._reconcile_blocked_work_item_state = machine._reconcile_blocked_work_item_state
    host._sync_work_item_status_label = machine._sync_work_item_status_label
    host._coerce_owner = machine._coerce_owner
    host._normalize_closed_work_item_state = machine._normalize_closed_work_item_state
    host._touch_work_item_progress = machine._touch_work_item_progress
    host._work_item_state = machine._work_item_state
    host._save_work_item_state = machine._save_work_item_state
    host._structured_handoff = machine._structured_handoff
    host._structured_ack = machine._structured_ack
    host._structured_progress = machine._structured_progress
    return machine
