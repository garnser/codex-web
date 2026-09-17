from __future__ import annotations

import contextlib
import time
from datetime import datetime, timezone
from typing import Any

from codex_web.models import GitLabProjectRoutingSettings, TaskSourceIdentity, WorkItemState


class GitLabArtifactEventProjector:
    """Project GitLab MR/pipeline facts without leaking provider payloads into core.

    Issue lifecycle events are handled by the TaskSource adapter/reconciler. This
    compatibility projector owns the remaining GitLab-specific artifact-event
    correlation until artifact sources become a first-class provider contract.
    """

    def __init__(self, host: Any, state_machine: Any) -> None:
        self.host = host
        self.state_machine = state_machine

    @staticmethod
    def _parse_timestamp(value: Any) -> float | None:
        if isinstance(value, (int, float)):
            return float(value)
        if not isinstance(value, str) or not value.strip():
            return None
        text = value.strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        with contextlib.suppress(ValueError):
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        return None

    @classmethod
    def _payload_timestamp(cls, payload: dict[str, Any]) -> float | None:
        attrs = payload.get("object_attributes") or {}
        values = (
            attrs.get("updated_at"),
            attrs.get("closed_at"),
            attrs.get("last_edited_at"),
            attrs.get("created_at"),
        )
        parsed = [timestamp for value in values if (timestamp := cls._parse_timestamp(value)) is not None]
        return max(parsed) if parsed else None

    @staticmethod
    def _status_label(labels: list[str]) -> str | None:
        for label in labels:
            if label.casefold().startswith("status::"):
                return label
        return None

    @staticmethod
    def _priority(labels: list[str]) -> str | None:
        for label in labels:
            if label.casefold().startswith("priority::"):
                return label
        return None

    @staticmethod
    def _stage(*, state_name: str, status_label: str | None, existing_stage: str | None) -> str:
        normalized = (state_name or "").strip().casefold()
        if normalized in {"closed", "merged"}:
            return "closed"
        if status_label == "status::awaiting confirmation":
            return "ready_for_validation"
        if status_label == "status::blocked":
            return "failed_with_action_owner"
        if status_label == "status::in progress":
            if existing_stage in {"implementation_active", "validation_running", "ready_to_close"}:
                return existing_stage
            return "implementation_active"
        return existing_stage or "implementation_active"

    @staticmethod
    def _derived_status(state: WorkItemState) -> str | None:
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

    @classmethod
    def _semantics(
        cls,
        *,
        owner: str | None,
        stage: str,
        status_label: str | None,
    ) -> tuple[str, tuple[str, ...]]:
        state_name = "closed" if stage == "closed" else "opened"
        if stage == "closed":
            return state_name, ()
        labels: list[str] = []
        if owner:
            labels.append(f"owner::{owner}")
        if status_label:
            labels.append(status_label)
        return state_name, tuple(sorted(dict.fromkeys(labels)))

    def _is_stale(
        self,
        state: WorkItemState,
        *,
        projected_owner: str | None,
        projected_stage: str,
        projected_status_label: str | None,
        event_timestamp: float | None,
    ) -> bool:
        if event_timestamp is None:
            return False
        if state.last_gitlab_event_at is not None and event_timestamp < state.last_gitlab_event_at:
            return True
        if event_timestamp >= state.last_meaningful_update_at:
            return False
        incoming = self._semantics(
            owner=projected_owner,
            stage=projected_stage,
            status_label=projected_status_label,
        )
        current = self._semantics(
            owner=self.state_machine._coerce_owner(state.current_owner),
            stage=state.current_stage,
            status_label=self._derived_status(state),
        )
        return incoming != current

    def _artifact_state(self, payload: dict[str, Any], state: WorkItemState | None) -> str:
        attrs = payload.get("object_attributes") or {}
        kind = str(payload.get("object_kind") or payload.get("event_name") or "").strip().casefold()
        if kind == "merge_request":
            mr_state = str(attrs.get("state") or "").strip().casefold()
            action = str(attrs.get("action") or "").strip().casefold()
            return "merged_main" if mr_state == "merged" or action == "merge" else "merge_request"
        if kind == "pipeline":
            ref_name = str(attrs.get("ref") or "").strip()
            if ref_name == "main":
                return "merged_main"
            if ref_name.startswith("v"):
                return "tag_pipeline"
            return "branch"
        return self.state_machine._infer_artifact_state_from_state(state) if state is not None else "branch"

    def project(self, payload: dict[str, Any], *, project_id: str) -> WorkItemState | None:
        kind = str(payload.get("object_kind") or payload.get("event_name") or "").strip().casefold()
        if kind not in {"merge_request", "pipeline"}:
            return None
        ref = self.host._project_issue_ref(payload)
        if not ref:
            return None

        attrs = payload.get("object_attributes") or {}
        project = payload.get("project") or {}
        labels = self.host._gitlab_label_names(payload)
        settings = self.host._load_gitlab_routing_settings().projects.get(
            project_id,
            GitLabProjectRoutingSettings(),
        )
        owners = self.host._gitlab_owner_agents(payload, settings)
        status_label = self._status_label(labels)
        priority = self._priority(labels)
        now = time.time()
        event_timestamp = self._payload_timestamp(payload)
        states = self.host._load_work_item_states()
        state = states.get(ref)
        projected_stage = self._stage(
            state_name=str(attrs.get("state") or attrs.get("status") or ""),
            status_label=status_label,
            existing_stage=state.current_stage if state else None,
        )
        if kind == "pipeline":
            projected_stage = "closed"
        projected_owner = None if projected_stage == "closed" else (owners[0] if owners else None)
        projected_status_label = None if projected_stage == "closed" else status_label

        if state is None:
            project_path = str(project.get("path_with_namespace") or "").strip() or None
            state = WorkItemState(
                ref=ref,
                project_id=project_id,
                project_path=project_path,
                source_identity=TaskSourceIdentity(
                    source_type="gitlab",
                    source_instance=self.host.GITLAB_API_BASE.rstrip("/"),
                    external_id=ref,
                    external_url=self.host._gitlab_url(payload),
                    revision=str(attrs.get("updated_at") or "").strip() or None,
                ),
                title=str(attrs.get("title") or attrs.get("name") or "").strip() or None,
                url=self.host._gitlab_url(payload),
                kind=kind,
                priority=priority,
                current_owner=projected_owner,
                current_stage=projected_stage,
                implementation_owner=(
                    projected_owner
                    if projected_owner and projected_owner not in self.host.NON_IMPLEMENTATION_OWNERS
                    else None
                ),
                validation_owner=self.host.DEFAULT_VALIDATION_OWNER,
                release_owner=self.host.DEFAULT_RELEASE_OWNER,
                artifact_state="branch",
                last_meaningful_update_at=now,
                last_owner_activity_at=now,
                last_gitlab_event_at=event_timestamp or now,
                release_gate=priority == "priority::P1",
                status_label=projected_status_label,
                labels=labels,
                mr_refs=self.host._mr_refs_from_payload(payload),
                created_at=now,
                updated_at=now,
            )
            if state.current_stage == "closed":
                state = self.state_machine._normalize_closed_work_item_state(
                    state,
                    now=now,
                    reason_code="gitlab_closed",
                    closed_at=event_timestamp or now,
                )
            self.state_machine._append_work_item_event(
                self.state_machine._work_item_event(
                    ref,
                    "gitlab_work_item_created",
                    payload={
                        "project_id": project_id,
                        "current_owner": state.current_owner,
                        "current_stage": state.current_stage,
                        "priority": priority,
                    },
                )
            )
            if state.current_stage == "failed_with_action_owner":
                state = self.state_machine._reconcile_blocked_work_item_state(
                    state,
                    projected_owner=projected_owner,
                    previous_owner=None,
                    now=now,
                )
        else:
            previous_stage = state.current_stage
            previous_owner = state.current_owner
            previous_status_label = state.status_label
            previous_priority = state.priority
            previous_mr_refs = list(state.mr_refs)
            was_closed = bool(state.closed_at)
            if self._is_stale(
                state,
                projected_owner=projected_owner,
                projected_stage=projected_stage,
                projected_status_label=projected_status_label,
                event_timestamp=event_timestamp,
            ):
                self.state_machine._append_work_item_event(
                    self.state_machine._work_item_event(
                        ref,
                        "gitlab_event_stale_ignored",
                        payload={
                            "projected_owner": projected_owner,
                            "projected_stage": projected_stage,
                            "projected_status_label": projected_status_label,
                            "event_timestamp": event_timestamp,
                        },
                    )
                )
                return state
            if self.state_machine._preserve_accepted_handoff_recipient(
                state,
                incoming_owner=projected_owner,
                incoming_stage=projected_stage,
                incoming_status_label=projected_status_label,
            ):
                self.state_machine._append_work_item_event(
                    self.state_machine._work_item_event(
                        ref,
                        "gitlab_event_accepted_handoff_projection_ignored",
                        payload={
                            "projected_owner": projected_owner,
                            "projected_stage": projected_stage,
                            "projected_status_label": projected_status_label,
                            "event_timestamp": event_timestamp,
                        },
                    )
                )
                return state

            state.project_id = project_id
            state.project_path = str(project.get("path_with_namespace") or "").strip() or state.project_path
            state.title = str(attrs.get("title") or attrs.get("name") or "").strip() or state.title
            state.url = self.host._gitlab_url(payload) or state.url
            state.kind = kind or state.kind
            state.priority = priority or state.priority
            state.labels = labels
            state.status_label = projected_status_label
            state.release_gate = priority == "priority::P1" or state.release_gate
            state = self.state_machine._transition_work_item_stage(
                state,
                projected_stage,
                source="gitlab-artifact-event-projection",
                external_projection=True,
            )
            if projected_owner and not (state.handoff and state.handoff.status == "pending"):
                state.current_owner = projected_owner
            state.updated_at = now
            state.last_gitlab_event_at = event_timestamp or now
            state.mr_refs = sorted(set(state.mr_refs + self.host._mr_refs_from_payload(payload)))
            if projected_stage == "closed":
                state = self.state_machine._normalize_closed_work_item_state(
                    state,
                    now=now,
                    reason_code="gitlab_closed",
                    closed_at=event_timestamp or now,
                )
            else:
                state.closed_at = None
                if projected_stage == "implementation_active" and not (
                    state.handoff and state.handoff.status == "pending"
                ):
                    state.blocker = None
                    if self.state_machine._coerce_owner(state.next_owner) != self.state_machine._coerce_owner(
                        state.current_owner
                    ):
                        state.next_owner = None
            if (
                previous_stage != state.current_stage
                or previous_owner != state.current_owner
                or previous_status_label != state.status_label
                or previous_priority != state.priority
                or previous_mr_refs != state.mr_refs
                or was_closed != bool(state.closed_at)
            ):
                state.last_meaningful_update_at = now
                state.last_owner_activity_at = now
            if state.current_stage == "failed_with_action_owner":
                state = self.state_machine._reconcile_blocked_work_item_state(
                    state,
                    projected_owner=projected_owner,
                    previous_owner=previous_owner,
                    now=now,
                )

        state = self.state_machine._ensure_work_item_lane_defaults(state)
        state.artifact_state = self._artifact_state(payload, state)
        if state.current_stage == "ready_for_validation" and not state.handoff:
            state.next_owner = state.current_owner
        states[ref] = state
        self.host._save_work_item_states(states)
        self.state_machine._append_work_item_event(
            self.state_machine._work_item_event(
                ref,
                "gitlab_artifact_event_projected",
                payload={
                    "kind": kind,
                    "current_owner": state.current_owner,
                    "current_stage": state.current_stage,
                    "artifact_state": state.artifact_state,
                    "priority": state.priority,
                    "release_gate": state.release_gate,
                },
            )
        )
        return state
