from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from codex_web.integrations.gitlab_client import GitLabClient
from codex_web.models import (
    GitLabProjectRoutingSettings,
    WorkItemAckCreate,
    WorkItemEvent,
    WorkItemHandoff,
    WorkItemHandoffCreate,
    WorkItemProgressUpdate,
    WorkItemState,
)


class WorkItemStateMachine:
    """Canonical work-item transition, validation, and GitLab projection engine."""

    def __init__(self, host: Any, gitlab: GitLabClient | None = None) -> None:
        self.host = host
        self.gitlab = gitlab or GitLabClient()

    def _append_work_item_event(self, event: WorkItemEvent) -> None:
        self.host.DATA_DIR.mkdir(exist_ok=True)
        with self.host.WORK_ITEM_EVENTS_FILE.open('a') as handle:
            handle.write(json.dumps(event.model_dump(), separators=(',', ':')) + '\n')

    def _work_item_event(self, ref: str, event_type: str, *, actor: str | None=None, payload: dict[str, Any] | None=None) -> WorkItemEvent:
        safe_payload: dict[str, Any] = payload or {}
        return WorkItemEvent(ref=ref, event_type=event_type, created_at=time.time(), actor=actor, payload=safe_payload)

    def _work_item_state_public(self, state: WorkItemState) -> dict[str, Any]:
        state = self._ensure_work_item_lane_defaults(state)
        payload = state.model_dump()
        payload['isConfirmationPending'] = bool(state.handoff and state.handoff.status == 'pending')
        payload['isReleaseStage'] = state.current_stage in {'ready_for_validation', 'validation_running', 'failed_with_action_owner', 'ready_to_close'}
        routing_errors = self._work_item_split_brain_findings(state)
        payload['routingErrors'] = routing_errors
        payload['routingError'] = routing_errors[0] if routing_errors else None
        return payload

    def _normalize_work_item_stage(self, stage: str | None, *, fallback: str='implementation_active') -> str:
        normalized = (stage or '').strip().lower()
        allowed = {'implementation_active', 'ready_for_validation', 'validation_running', 'failed_with_action_owner', 'ready_to_close', 'closed'}
        return normalized if normalized in allowed else fallback

    def _normalize_artifact_state(self, artifact_state: str | None, *, fallback: str='branch') -> str:
        normalized = (artifact_state or '').strip().lower()
        allowed = {'branch', 'merge_request', 'merged_main', 'tag_pipeline'}
        return normalized if normalized in allowed else fallback

    def _normalize_blocking_findings(self, findings: list[str] | None) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in findings or []:
            text = ' '.join(str(raw or '').split()).strip()
            if not text or text in seen:
                continue
            seen.add(text)
            normalized.append(text)
        return normalized[:20]

    def _ensure_work_item_lane_defaults(self, state: WorkItemState) -> WorkItemState:
        state.validation_owner = self._coerce_owner(state.validation_owner) or self.host.DEFAULT_VALIDATION_OWNER
        state.release_owner = self._coerce_owner(state.release_owner) or self.host.DEFAULT_RELEASE_OWNER
        if not self._coerce_owner(state.implementation_owner):
            for candidate in (state.current_owner, state.next_owner, state.handoff.from_agent if state.handoff else None):
                owner = self._coerce_owner(candidate)
                if owner and owner not in self.host.NON_IMPLEMENTATION_OWNERS and (owner not in {self._coerce_owner(state.validation_owner), self._coerce_owner(state.release_owner)}):
                    state.implementation_owner = owner
                    break
        state.artifact_state = self._normalize_artifact_state(state.artifact_state, fallback='branch')
        state.blocking_findings = self._normalize_blocking_findings(state.blocking_findings)
        return state

    def _infer_artifact_state_from_state(self, state: WorkItemState) -> str:
        state = self._ensure_work_item_lane_defaults(state)
        current_owner = self._coerce_owner(state.current_owner)
        current_artifact = self._normalize_artifact_state(state.artifact_state, fallback='branch')
        if current_owner == self._coerce_owner(state.release_owner):
            if current_artifact == 'tag_pipeline':
                return 'tag_pipeline'
            return 'merged_main'
        if current_owner == self._coerce_owner(state.validation_owner):
            if current_artifact in {'merged_main', 'tag_pipeline'}:
                return current_artifact
            return 'merge_request' if state.mr_refs else 'branch'
        if current_artifact in {'merged_main', 'tag_pipeline'} and state.current_stage == 'closed':
            return current_artifact
        return 'branch'

    def _infer_artifact_state_from_gitlab_payload(self, payload: dict[str, Any], *, current_state: WorkItemState | None) -> str:
        attrs = payload.get('object_attributes') or {}
        kind = str(payload.get('object_kind') or payload.get('event_name') or '').strip().lower()
        if kind == 'merge_request':
            mr_state = str(attrs.get('state') or '').strip().lower()
            action = str(attrs.get('action') or '').strip().lower()
            if mr_state == 'merged' or action == 'merge':
                return 'merged_main'
            return 'merge_request'
        if kind == 'pipeline':
            ref_name = str(attrs.get('ref') or '').strip()
            if ref_name == 'main':
                return 'merged_main'
            if ref_name.startswith('v'):
                return 'tag_pipeline'
            return 'branch'
        if current_state is not None:
            return self._infer_artifact_state_from_state(current_state)
        projected = WorkItemState(ref='projection', current_stage='implementation_active', last_meaningful_update_at=time.time(), updated_at=time.time(), created_at=time.time(), artifact_state='branch', mr_refs=[])
        return self._infer_artifact_state_from_state(projected)

    def _routing_error_detail(self, *, code: str, message: str, from_agent: str | None, to_agent: str | None, artifact_state: str | None) -> dict[str, Any]:
        return {'code': code, 'message': message, 'from_agent': self._coerce_owner(from_agent) or from_agent, 'to_agent': self._coerce_owner(to_agent) or to_agent, 'artifact_state': self._normalize_artifact_state(artifact_state, fallback='branch')}

    def _validate_handoff_edge(self, state: WorkItemState, *, from_agent: str | None, to_agent: str | None, artifact_state: str | None) -> dict[str, Any] | None:
        state = self._ensure_work_item_lane_defaults(state)
        sender = self._coerce_owner(from_agent)
        recipient = self._coerce_owner(to_agent)
        artifact = self._normalize_artifact_state(artifact_state or state.artifact_state, fallback=self._infer_artifact_state_from_state(state))
        implementation_owner = self._coerce_owner(state.implementation_owner)
        validation_owner = self._coerce_owner(state.validation_owner)
        release_owner = self._coerce_owner(state.release_owner)
        orchestrator_override = sender == 'orchestrator'
        if recipient == release_owner and sender == implementation_owner and (not orchestrator_override):
            if artifact == 'branch':
                return self._routing_error_detail(code='branch_only_artifact', message='Direct implementation->release handoff is blocked while the artifact is only on a branch. Hand off to the validation owner first.', from_agent=sender, to_agent=recipient, artifact_state=artifact)
            if artifact == 'merge_request':
                return self._routing_error_detail(code='missing_merge', message='Direct implementation->release handoff is blocked while the artifact is only on an open merge request. Merge to main first or reroute explicitly via Orchestrator.', from_agent=sender, to_agent=recipient, artifact_state=artifact)
            return self._routing_error_detail(code='wrong_lane', message='Direct implementation->release handoff requires explicit Orchestrator reassignment.', from_agent=sender, to_agent=recipient, artifact_state=artifact)
        if recipient == validation_owner and artifact not in {'branch', 'merge_request'} and (not (orchestrator_override and artifact in {'merged_main', 'tag_pipeline'})):
            return self._routing_error_detail(code='wrong_lane', message='Validation ownership requires an MR-ready artifact on a branch or open merge request, unless Orchestrator explicitly reassigns a merged-main or tag-pipeline validation lane.', from_agent=sender, to_agent=recipient, artifact_state=artifact)
        if sender == validation_owner and recipient == release_owner and (artifact not in {'merged_main', 'tag_pipeline'}):
            return self._routing_error_detail(code='missing_merge' if artifact == 'merge_request' else 'wrong_lane', message='Release ownership requires a merged mainline or tag pipeline artifact after validation/merge.', from_agent=sender, to_agent=recipient, artifact_state=artifact)
        return None

    def _record_handoff_history(self, state: WorkItemState, handoff: WorkItemHandoff, *, status: str | None=None, acknowledged_at: float | None=None, reason_code: str | None=None) -> WorkItemState:
        entry = handoff.model_copy(deep=True)
        if status is not None:
            entry.status = status
        if acknowledged_at is not None:
            entry.acknowledged_at = acknowledged_at
        if reason_code is not None:
            entry.reason_code = reason_code
        state.handoff_history = (state.handoff_history + [entry])[-100:]
        return state

    def _archive_active_handoff(self, state: WorkItemState, *, now: float, status: str, reason_code: str | None=None) -> WorkItemState:
        if state.handoff:
            state = self._record_handoff_history(state, state.handoff, status=status, acknowledged_at=now, reason_code=reason_code)
            state.handoff = None
        return state

    def _current_status_label(self, labels: list[str]) -> str | None:
        for label in labels:
            if label.lower().startswith('status::'):
                return label
        return None

    def _has_routing_labels(self, labels: list[str]) -> bool:
        return any((label.lower().startswith(('owner::', 'status::')) for label in labels))

    def _priority_from_labels(self, labels: list[str]) -> str | None:
        for label in labels:
            if label.lower().startswith('priority::'):
                return label
        return None

    def _parse_gitlab_timestamp(self, value: Any) -> float | None:
        if isinstance(value, (int, float)):
            return float(value)
        if not isinstance(value, str):
            return None
        text = value.strip()
        if not text:
            return None
        if text.endswith('Z'):
            text = f'{text[:-1]}+00:00'
        with contextlib.suppress(ValueError):
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        return None

    def _latest_gitlab_timestamp(self, *values: Any) -> float | None:
        parsed = [stamp for stamp in (self._parse_gitlab_timestamp(value) for value in values) if stamp is not None]
        return max(parsed) if parsed else None

    def _gitlab_issue_timestamp(self, issue: dict[str, Any]) -> float | None:
        return self._latest_gitlab_timestamp(issue.get('updated_at'), issue.get('closed_at'), issue.get('created_at'))

    def _gitlab_payload_timestamp(self, payload: dict[str, Any]) -> float | None:
        attrs = payload.get('object_attributes') or {}
        return self._latest_gitlab_timestamp(attrs.get('updated_at'), attrs.get('closed_at'), attrs.get('last_edited_at'), attrs.get('created_at'))

    def _gitlab_stage_from_projection(self, *, state_name: str, status_label: str | None, existing_stage: str | None=None) -> str:
        normalized_state = (state_name or '').strip().lower()
        if normalized_state in {'closed', 'merged'}:
            return 'closed'
        if status_label == 'status::awaiting confirmation':
            return 'ready_for_validation'
        if status_label == 'status::blocked':
            return 'failed_with_action_owner'
        if status_label == 'status::in progress':
            if existing_stage in {'implementation_active', 'validation_running', 'ready_to_close'}:
                return existing_stage
            return 'implementation_active'
        return existing_stage or 'implementation_active'

    def _gitlab_projection_semantics(self, *, owner: str | None, stage: str, status_label: str | None) -> tuple[str, tuple[str, ...]]:
        state_name = 'closed' if stage == 'closed' else 'opened'
        if stage == 'closed':
            return (state_name, ())
        active_labels: list[str] = []
        if owner:
            active_labels.append(f'owner::{owner}')
        if status_label:
            active_labels.append(status_label)
        return (state_name, tuple(sorted(dict.fromkeys(active_labels))))

    def _gitlab_projection_is_stale(self, state: WorkItemState, *, projected_owner: str | None, projected_stage: str, projected_status_label: str | None, event_timestamp: float | None) -> bool:
        if event_timestamp is None:
            return False
        if state.last_gitlab_event_at is not None and event_timestamp < state.last_gitlab_event_at:
            return True
        if event_timestamp >= state.last_meaningful_update_at:
            return False
        incoming = self._gitlab_projection_semantics(owner=projected_owner, stage=projected_stage, status_label=projected_status_label)
        current = self._gitlab_projection_semantics(owner=self._coerce_owner(state.current_owner), stage=state.current_stage, status_label=self._derived_status_label_for_work_item(state))
        return incoming != current

    def _work_item_issue_ref_parts(self, ref: str) -> tuple[str, int] | None:
        project_path, sep, iid_text = str(ref or '').partition('#')
        if not sep or not project_path.strip() or (not iid_text.strip().isdigit()):
            return None
        return (project_path.strip(), int(iid_text.strip()))

    def _derived_status_label_for_work_item(self, state: WorkItemState) -> str | None:
        if state.current_stage == 'closed':
            return None
        if state.handoff and state.handoff.status == 'pending':
            return 'status::awaiting confirmation'
        if state.current_stage == 'failed_with_action_owner':
            return 'status::blocked'
        if state.current_stage in {'implementation_active', 'ready_for_validation', 'validation_running', 'ready_to_close'}:
            return 'status::in progress'
        return state.status_label

    def _owner_label_from_labels(self, labels: list[str]) -> str | None:
        for label in labels:
            match = re.match('owner::(.+)', label.strip(), re.IGNORECASE)
            if match:
                return self._coerce_owner(match.group(1))
        return None

    def _work_item_split_brain_findings(self, state: WorkItemState) -> list[str]:
        state = self._ensure_work_item_lane_defaults(state)
        findings: list[str] = []
        canonical_status = self._derived_status_label_for_work_item(state)
        label_owner = self._owner_label_from_labels(state.labels)
        current_owner = self._coerce_owner(state.current_owner)
        next_owner = self._coerce_owner(state.next_owner)
        action_owner = self.host._leading_owner_cue_in_action(state.next_action)
        if canonical_status != state.status_label:
            findings.append(f'status drift: canonical={canonical_status or 'none'} stored={state.status_label or 'none'}')
        if label_owner != current_owner:
            findings.append(f'owner drift: gitlab={label_owner or 'none'} codex-web={current_owner or 'none'}')
        if state.handoff and state.handoff.status == 'pending':
            handoff_to = self._coerce_owner(state.handoff.to_agent)
            handoff_from = self._coerce_owner(state.handoff.from_agent)
            if state.status_label != 'status::awaiting confirmation':
                findings.append('pending handoff without awaiting-confirmation status')
            if next_owner != handoff_to:
                findings.append(f'pending handoff next-owner drift: expected={handoff_to or 'none'} stored={next_owner or 'none'}')
            if current_owner != handoff_from:
                findings.append(f'pending handoff current-owner drift: expected={handoff_from or 'none'} stored={current_owner or 'none'}')
        elif state.handoff and state.handoff.status == 'accepted':
            handoff_to = self._coerce_owner(state.handoff.to_agent)
            if state.status_label == 'status::awaiting confirmation':
                findings.append('accepted handoff still marked as awaiting confirmation')
            if current_owner != handoff_to:
                findings.append(f'accepted handoff owner drift: expected={handoff_to or 'none'} stored={current_owner or 'none'}')
            if next_owner and next_owner != handoff_to:
                findings.append(f'accepted handoff next-owner drift: expected={handoff_to or 'none'} stored={next_owner or 'none'}')
        elif state.status_label == 'status::awaiting confirmation':
            findings.append('awaiting-confirmation status without pending handoff')
        if state.current_stage == 'failed_with_action_owner':
            if not (state.blocker or '').strip():
                findings.append('blocked lane missing blocker text')
            if not (next_owner or current_owner):
                findings.append('blocked lane missing actionable owner')
        active_handoff = state.handoff
        if active_handoff:
            handoff_error = self._validate_handoff_edge(state, from_agent=active_handoff.from_agent, to_agent=active_handoff.to_agent, artifact_state=active_handoff.artifact_state or state.artifact_state)
            if handoff_error:
                findings.append('routing error: ' + str(handoff_error.get('code') or 'wrong_lane') + ' - ' + str(handoff_error.get('message') or 'invalid handoff edge'))
        if current_owner == self._coerce_owner(state.release_owner) and state.artifact_state in {'branch', 'merge_request'}:
            findings.append(f'routing error: release owner on pre-merge artifact_state={state.artifact_state}')
        expected_action_owner = next_owner or current_owner
        if action_owner and action_owner != expected_action_owner:
            findings.append('next-action owner cue drift: ' + f'action={action_owner} current={current_owner or 'none'} next={next_owner or 'none'}')
        return findings

    def _preserve_accepted_handoff_recipient(self, state: WorkItemState, *, incoming_owner: str | None, incoming_stage: str | None, incoming_status_label: str | None) -> bool:
        state = self._ensure_work_item_lane_defaults(state)
        if not state.handoff or state.handoff.status != 'accepted':
            return False
        recipient = self._coerce_owner(state.handoff.to_agent)
        current_owner = self._coerce_owner(state.current_owner)
        candidate_owner = self._coerce_owner(incoming_owner)
        candidate_stage = self._normalize_work_item_stage(incoming_stage, fallback=state.current_stage)
        if not recipient or current_owner != recipient:
            return False
        if not candidate_owner or candidate_owner == recipient:
            return False
        if candidate_stage in {'implementation_active', 'failed_with_action_owner', 'closed'}:
            return False
        if incoming_status_label == 'status::awaiting confirmation':
            return True
        return candidate_stage in {'ready_for_validation', 'validation_running', 'ready_to_close'}

    def _reconcile_blocked_work_item_state(self, state: WorkItemState, *, projected_owner: str | None, previous_owner: str | None, now: float) -> WorkItemState:
        if state.handoff and state.handoff.status == 'pending':
            state = self._archive_active_handoff(state, now=now, status='superseded', reason_code='blocked_reconciled')
        owner = self._coerce_owner(projected_owner) or self._coerce_owner(state.current_owner) or self._coerce_owner(previous_owner)
        if owner:
            state.current_owner = owner
            state.next_owner = owner
        if not (state.blocker or '').strip():
            state.blocker = 'Blocked-item reconciliation required from incoming GitLab event.'
        if not (state.next_action or '').strip():
            state.next_action = 'Reconcile the newly blocked item and either continue work or emit one exact blocker.'
        return state

    def _sync_work_item_status_label(self, state: WorkItemState) -> WorkItemState:
        state.status_label = self._derived_status_label_for_work_item(state)
        return state

    def _coerce_owner(self, value: str | None) -> str | None:
        normalized = (value or '').strip().lower()
        if normalized in {'', 'none', 'null'}:
            return None
        return normalized

    def _normalize_closed_work_item_state(self, state: WorkItemState, *, now: float, reason_code: str, closed_at: float | None=None) -> WorkItemState:
        if state.handoff:
            state = self._archive_active_handoff(state, now=now, status='superseded', reason_code=reason_code)
        state.current_owner = None
        state.next_owner = None
        state.blocker = None
        state.blocking_findings = []
        state.status_label = None
        state.closed_at = closed_at or state.closed_at or now
        return state

    def _touch_work_item_progress(self, state: WorkItemState, *, actor: str | None=None, current_owner: str | None=None, current_stage: str | None=None, next_action: str | None=None, next_owner: str | None=None, next_owner_present: bool=False, blocker: str | None=None, blocker_present: bool=False, blocking_findings: list[str] | None=None, blocking_findings_present: bool=False, release_gate: bool | None=None, status_label: str | None=None, artifact_state: str | None=None, event_type: str='progress_updated', note: str | None=None) -> WorkItemState:
        state = self._ensure_work_item_lane_defaults(state)
        now = time.time()
        previous_owner = self._coerce_owner(state.current_owner)
        previous_stage = state.current_stage
        incoming_stage = self._normalize_work_item_stage(current_stage, fallback=state.current_stage) if current_stage is not None else None
        preserve_accepted_recipient = self._preserve_accepted_handoff_recipient(state, incoming_owner=current_owner, incoming_stage=incoming_stage, incoming_status_label=status_label)
        accepted_recipient = self._coerce_owner(state.handoff.to_agent) if state.handoff and state.handoff.status == 'accepted' else None
        if current_owner is not None and (not preserve_accepted_recipient):
            state.current_owner = self._coerce_owner(current_owner)
        if current_stage is not None:
            state.current_stage = incoming_stage
        if next_action is not None:
            state.next_action = next_action or None
        if next_owner_present:
            incoming_next_owner = self._coerce_owner(next_owner)
            if not (preserve_accepted_recipient and accepted_recipient and incoming_next_owner and (incoming_next_owner != accepted_recipient)):
                state.next_owner = incoming_next_owner
        if blocker_present:
            state.blocker = blocker or None
        if blocking_findings_present:
            state.blocking_findings = self._normalize_blocking_findings(blocking_findings)
        if release_gate is not None:
            state.release_gate = release_gate
        if status_label is not None:
            state.status_label = status_label or None
        if artifact_state is not None:
            state.artifact_state = self._normalize_artifact_state(artifact_state, fallback=state.artifact_state)
        if state.current_stage == 'closed':
            state = self._normalize_closed_work_item_state(state, now=now, reason_code='closed_lane')
        elif state.handoff:
            current_owner_normalized = self._coerce_owner(state.current_owner)
            handoff_from = self._coerce_owner(state.handoff.from_agent)
            handoff_to = self._coerce_owner(state.handoff.to_agent)
            archive_resolved_handoff = False
            if state.handoff.status == 'pending':
                archive_resolved_handoff = incoming_stage in {'implementation_active', 'failed_with_action_owner'} or (status_label is not None and status_label != 'status::awaiting confirmation')
                if current_owner_normalized and current_owner_normalized not in {handoff_from, handoff_to}:
                    archive_resolved_handoff = True
            elif state.handoff.status == 'accepted':
                archive_resolved_handoff = bool(current_owner_normalized and current_owner_normalized != handoff_to)
                if not archive_resolved_handoff and incoming_stage in {'implementation_active', 'failed_with_action_owner'} and (current_owner_normalized == handoff_from):
                    archive_resolved_handoff = True
            elif state.handoff.status == 'rejected':
                archive_resolved_handoff = True
            if archive_resolved_handoff:
                state = self._archive_active_handoff(state, now=now, status='superseded', reason_code='new_canonical_progress')
                if status_label is None and state.current_stage in {'implementation_active', 'failed_with_action_owner'}:
                    state.status_label = None
        if state.current_stage == 'implementation_active' and (not (state.handoff and state.handoff.status == 'pending')):
            state.blocker = None
            state.blocking_findings = []
            if self._coerce_owner(state.next_owner) != self._coerce_owner(state.current_owner):
                state.next_owner = None
        if artifact_state is None:
            state.artifact_state = self._infer_artifact_state_from_state(state)
        state = self._ensure_work_item_lane_defaults(state)
        current_owner_normalized = self._coerce_owner(state.current_owner)
        actor_normalized = self._coerce_owner(actor)
        refresh_owner_activity = state.current_stage == 'closed' or previous_owner != current_owner_normalized or previous_stage != state.current_stage or (actor_normalized is not None and actor_normalized == current_owner_normalized) or (state.last_owner_activity_at is None)
        state.updated_at = now
        state.last_meaningful_update_at = now
        if refresh_owner_activity:
            state.last_owner_activity_at = now
        if note:
            state.notes = (state.notes + [note])[-20:]
        self._append_work_item_event(self._work_item_event(state.ref, event_type, actor=actor, payload={'current_owner': state.current_owner, 'current_stage': state.current_stage, 'next_action': state.next_action, 'next_owner': state.next_owner, 'blocker': state.blocker, 'blocking_findings': state.blocking_findings, 'artifact_state': state.artifact_state, 'release_gate': state.release_gate, 'status_label': state.status_label}))
        return state

    def _upsert_work_item_state_from_gitlab_issue(self, issue: dict[str, Any], *, project_id: str) -> WorkItemState | None:
        ref = str((issue.get('references') or {}).get('full') or '').strip()
        if not ref or '#' not in ref:
            return None
        project_path = ref.split('#', 1)[0]
        labels = sorted({str(label).strip() for label in issue.get('labels', []) if str(label).strip()})
        owners = sorted({match.group(1).strip().lower() for label in labels for match in [re.match('owner::(.+)', label, re.IGNORECASE)] if match})
        status_label = self._current_status_label(labels)
        priority = self._priority_from_labels(labels)
        state_name = str(issue.get('state') or '').strip().lower()
        now = time.time()
        event_timestamp = self._gitlab_issue_timestamp(issue)
        states = self.host._load_work_item_states()
        state = states.get(ref)
        projected_stage = self._gitlab_stage_from_projection(state_name=state_name, status_label=status_label, existing_stage=state.current_stage if state else None)
        projected_owner = None if projected_stage == 'closed' else owners[0] if owners else None
        projected_status_label = None if projected_stage == 'closed' else status_label
        if state is None:
            state = WorkItemState(ref=ref, project_id=project_id, project_path=project_path, title=str(issue.get('title') or '') or None, url=str(issue.get('web_url') or '') or None, kind='issue', priority=priority, current_owner=projected_owner, current_stage=projected_stage, implementation_owner=projected_owner if projected_owner and projected_owner not in self.host.NON_IMPLEMENTATION_OWNERS else None, validation_owner=self.host.DEFAULT_VALIDATION_OWNER, release_owner=self.host.DEFAULT_RELEASE_OWNER, artifact_state='branch', last_meaningful_update_at=now, last_owner_activity_at=now, last_gitlab_event_at=event_timestamp or now, blocker=None, next_action=None, next_owner=None, release_gate=priority == 'priority::P1', status_label=projected_status_label, labels=labels, mr_refs=[], created_at=now, updated_at=now, closed_at=event_timestamp or now if projected_stage == 'closed' else None)
            self._append_work_item_event(self._work_item_event(ref, 'gitlab_issue_backfilled', payload={'project_id': project_id, 'current_owner': state.current_owner, 'current_stage': state.current_stage, 'priority': state.priority}))
            if state.current_stage == 'failed_with_action_owner':
                state = self._reconcile_blocked_work_item_state(state, projected_owner=projected_owner, previous_owner=None, now=now)
            state = self._ensure_work_item_lane_defaults(state)
            state.artifact_state = self._infer_artifact_state_from_state(state)
        else:
            previous_stage = state.current_stage
            previous_owner = state.current_owner
            previous_status_label = state.status_label
            previous_priority = state.priority
            was_closed = bool(state.closed_at)
            if self._gitlab_projection_is_stale(state, projected_owner=projected_owner, projected_stage=projected_stage, projected_status_label=projected_status_label, event_timestamp=event_timestamp):
                self._append_work_item_event(self._work_item_event(ref, 'gitlab_issue_stale_ignored', payload={'projected_owner': projected_owner, 'projected_stage': projected_stage, 'projected_status_label': projected_status_label, 'event_timestamp': event_timestamp}))
                return state
            if self._preserve_accepted_handoff_recipient(state, incoming_owner=projected_owner, incoming_stage=projected_stage, incoming_status_label=projected_status_label):
                self._append_work_item_event(self._work_item_event(ref, 'gitlab_issue_accepted_handoff_projection_ignored', payload={'projected_owner': projected_owner, 'projected_stage': projected_stage, 'projected_status_label': projected_status_label, 'event_timestamp': event_timestamp}))
                return state
            state.project_id = project_id
            state.project_path = project_path
            state.title = str(issue.get('title') or '') or state.title
            state.url = str(issue.get('web_url') or '') or state.url
            state.priority = priority or state.priority
            state.labels = labels
            state.status_label = projected_status_label
            state.release_gate = priority == 'priority::P1' or state.release_gate
            state.current_stage = projected_stage
            if projected_owner and (not state.handoff or state.handoff.status != 'pending'):
                state.current_owner = projected_owner
            state.updated_at = now
            state.last_gitlab_event_at = event_timestamp or now
            if projected_stage == 'closed':
                state = self._normalize_closed_work_item_state(state, now=now, reason_code='gitlab_closed', closed_at=event_timestamp or now)
            else:
                state.closed_at = None
                if projected_stage == 'implementation_active' and (not (state.handoff and state.handoff.status == 'pending')):
                    state.blocker = None
                    if self._coerce_owner(state.next_owner) != self._coerce_owner(state.current_owner):
                        state.next_owner = None
            if previous_stage != state.current_stage or previous_owner != state.current_owner or previous_status_label != state.status_label or (previous_priority != state.priority) or (was_closed != bool(state.closed_at)):
                state.last_meaningful_update_at = now
                state.last_owner_activity_at = now
            if state.current_stage == 'failed_with_action_owner':
                state = self._reconcile_blocked_work_item_state(state, projected_owner=projected_owner, previous_owner=previous_owner, now=now)
            state = self._ensure_work_item_lane_defaults(state)
            state.artifact_state = self._infer_artifact_state_from_state(state)
        states[ref] = state
        self.host._save_work_item_states(states)
        return state

    def _upsert_work_item_state_from_gitlab_event(self, payload: dict[str, Any], *, project_id: str) -> WorkItemState | None:
        ref = self.host._project_issue_ref(payload)
        if not ref:
            return None
        attrs = payload.get('object_attributes') or {}
        project = payload.get('project') or {}
        kind = str(payload.get('object_kind') or payload.get('event_name') or '').strip().lower()
        labels = self.host._gitlab_label_names(payload)
        owners = self.host._gitlab_owner_agents(payload, self.host._load_gitlab_routing_settings().projects.get(project_id, GitLabProjectRoutingSettings()))
        status_label = self._current_status_label(labels)
        priority = self._priority_from_labels(labels)
        now = time.time()
        event_timestamp = self._gitlab_payload_timestamp(payload)
        states = self.host._load_work_item_states()
        state = states.get(ref)
        attrs = payload.get('object_attributes') or {}
        projected_stage = self._gitlab_stage_from_projection(state_name=str(attrs.get('state') or attrs.get('status') or '').strip().lower(), status_label=status_label, existing_stage=state.current_stage if state else None)
        if kind == 'pipeline':
            projected_stage = 'closed'
        projected_owner = None if projected_stage == 'closed' else owners[0] if owners else None
        projected_status_label = None if projected_stage == 'closed' else status_label
        if state is None:
            state = WorkItemState(ref=ref, project_id=project_id, project_path=str(project.get('path_with_namespace') or '') or None, title=str(attrs.get('title') or attrs.get('name') or '') or None, url=self.host._gitlab_url(payload), kind=str(payload.get('object_kind') or payload.get('event_name') or '') or None, priority=priority, current_owner=projected_owner, current_stage=projected_stage, implementation_owner=projected_owner if projected_owner and projected_owner not in self.host.NON_IMPLEMENTATION_OWNERS else None, validation_owner=self.host.DEFAULT_VALIDATION_OWNER, release_owner=self.host.DEFAULT_RELEASE_OWNER, artifact_state='branch', last_meaningful_update_at=now, last_owner_activity_at=now, last_gitlab_event_at=event_timestamp or now, blocker=None, next_action=None, next_owner=None, release_gate=priority == 'priority::P1', status_label=projected_status_label, labels=labels, mr_refs=self.host._mr_refs_from_payload(payload), created_at=now, updated_at=now)
            if state.current_stage == 'closed':
                state = self._normalize_closed_work_item_state(state, now=now, reason_code='gitlab_closed', closed_at=event_timestamp or now)
            self._append_work_item_event(self._work_item_event(ref, 'gitlab_work_item_created', payload={'project_id': project_id, 'current_owner': state.current_owner, 'current_stage': state.current_stage, 'priority': priority}))
            if state.current_stage == 'failed_with_action_owner':
                state = self._reconcile_blocked_work_item_state(state, projected_owner=projected_owner, previous_owner=None, now=now)
            state = self._ensure_work_item_lane_defaults(state)
            state.artifact_state = self._infer_artifact_state_from_gitlab_payload(payload, current_state=state)
            if state.current_stage == 'closed' and self._has_routing_labels(labels):
                state = self.host._sync_gitlab_issue_labels_from_work_item(state)
        else:
            previous_stage = state.current_stage
            previous_owner = state.current_owner
            previous_status_label = state.status_label
            previous_priority = state.priority
            previous_mr_refs = list(state.mr_refs)
            was_closed = bool(state.closed_at)
            if self._gitlab_projection_is_stale(state, projected_owner=projected_owner, projected_stage=projected_stage, projected_status_label=projected_status_label, event_timestamp=event_timestamp):
                self._append_work_item_event(self._work_item_event(ref, 'gitlab_event_stale_ignored', payload={'projected_owner': projected_owner, 'projected_stage': projected_stage, 'projected_status_label': projected_status_label, 'event_timestamp': event_timestamp}))
                return state
            if self._preserve_accepted_handoff_recipient(state, incoming_owner=projected_owner, incoming_stage=projected_stage, incoming_status_label=projected_status_label):
                self._append_work_item_event(self._work_item_event(ref, 'gitlab_event_accepted_handoff_projection_ignored', payload={'projected_owner': projected_owner, 'projected_stage': projected_stage, 'projected_status_label': projected_status_label, 'event_timestamp': event_timestamp}))
                return state
            state.project_id = project_id
            state.project_path = str(project.get('path_with_namespace') or '') or state.project_path
            state.title = str(attrs.get('title') or attrs.get('name') or '') or state.title
            state.url = self.host._gitlab_url(payload) or state.url
            state.kind = str(payload.get('object_kind') or payload.get('event_name') or '') or state.kind
            state.priority = priority or state.priority
            state.labels = labels
            state.status_label = projected_status_label
            state.release_gate = priority == 'priority::P1' or state.release_gate
            state.current_stage = projected_stage
            if projected_owner:
                if not state.handoff or state.handoff.status != 'pending':
                    state.current_owner = projected_owner
            state.updated_at = now
            state.last_gitlab_event_at = event_timestamp or now
            state.mr_refs = sorted(set(state.mr_refs + self.host._mr_refs_from_payload(payload)))
            if projected_stage == 'closed':
                state = self._normalize_closed_work_item_state(state, now=now, reason_code='gitlab_closed', closed_at=event_timestamp or now)
            else:
                state.closed_at = None
                if projected_stage == 'implementation_active' and (not (state.handoff and state.handoff.status == 'pending')):
                    state.blocker = None
                    if self._coerce_owner(state.next_owner) != self._coerce_owner(state.current_owner):
                        state.next_owner = None
            if previous_stage != state.current_stage or previous_owner != state.current_owner or previous_status_label != state.status_label or (previous_priority != state.priority) or (previous_mr_refs != state.mr_refs) or (was_closed != bool(state.closed_at)):
                state.last_meaningful_update_at = now
                state.last_owner_activity_at = now
            if state.current_stage == 'failed_with_action_owner':
                state = self._reconcile_blocked_work_item_state(state, projected_owner=projected_owner, previous_owner=previous_owner, now=now)
            state = self._ensure_work_item_lane_defaults(state)
            state.artifact_state = self._infer_artifact_state_from_gitlab_payload(payload, current_state=state)
            if state.current_stage == 'closed' and self._has_routing_labels(labels):
                state = self.host._sync_gitlab_issue_labels_from_work_item(state)
        if state.current_stage == 'ready_for_validation' and (not state.handoff):
            state.next_owner = state.current_owner
        states[ref] = state
        self.host._save_work_item_states(states)
        self._append_work_item_event(self._work_item_event(ref, 'gitlab_event_projected', payload={'current_owner': state.current_owner, 'current_stage': state.current_stage, 'status_label': state.status_label, 'priority': state.priority, 'release_gate': state.release_gate}))
        return state

    def _work_item_state(self, ref: str) -> WorkItemState:
        states = self.host._load_work_item_states()
        state = states.get(ref)
        if state is None:
            raise HTTPException(status_code=404, detail='Work item state not found')
        return state

    def _save_work_item_state(self, state: WorkItemState) -> WorkItemState:
        states = self.host._load_work_item_states()
        states[state.ref] = state
        self.host._save_work_item_states(states)
        return state

    def _structured_handoff(self, ref: str, payload: WorkItemHandoffCreate) -> WorkItemState:
        state = self._work_item_state(ref)
        state = self._ensure_work_item_lane_defaults(state)
        now = time.time()
        from_agent = self._coerce_owner(payload.from_agent) or payload.from_agent
        to_agent = self._coerce_owner(payload.to_agent) or payload.to_agent
        artifact_state = self._normalize_artifact_state(payload.artifact_state, fallback=self._infer_artifact_state_from_state(state))
        handoff_error = self._validate_handoff_edge(state, from_agent=from_agent, to_agent=to_agent, artifact_state=artifact_state)
        if handoff_error:
            raise HTTPException(status_code=409, detail=handoff_error)
        if state.handoff:
            archive_status = 'superseded' if state.handoff.status == 'pending' else state.handoff.status
            state = self._archive_active_handoff(state, now=now, status=archive_status, reason_code='new_canonical_handoff')
        state.handoff = WorkItemHandoff(from_agent=from_agent, to_agent=to_agent, reason=payload.reason, expected_action=payload.expected_action, requested_at=now, status='pending', artifact_state=artifact_state, stage=state.current_stage)
        state.current_owner = self._coerce_owner(payload.current_owner) or self._coerce_owner(payload.from_agent)
        state.current_stage = self._normalize_work_item_stage(payload.current_stage, fallback='ready_for_validation' if self._coerce_owner(payload.to_agent) == self._coerce_owner(state.validation_owner) else state.current_stage)
        state.next_owner = self._coerce_owner(payload.to_agent)
        state.next_action = payload.next_action or payload.expected_action or state.next_action
        state.blocker = payload.blocker or None
        if payload.blocking_findings is not None:
            state.blocking_findings = self._normalize_blocking_findings(payload.blocking_findings)
        state.artifact_state = artifact_state
        state.status_label = 'status::awaiting confirmation'
        state.updated_at = now
        state.last_meaningful_update_at = now
        state.last_owner_activity_at = now
        state = self._record_handoff_history(state, state.handoff)
        self._append_work_item_event(self._work_item_event(ref, 'handoff_requested', actor=payload.from_agent, payload={'from_agent': payload.from_agent, 'to_agent': payload.to_agent, 'expected_action': payload.expected_action, 'current_stage': state.current_stage, 'next_action': state.next_action, 'blocker': state.blocker, 'blocking_findings': state.blocking_findings, 'artifact_state': state.artifact_state}))
        state = self._sync_work_item_status_label(state)
        state = state
        return self._save_work_item_state(state)

    def _structured_ack(self, ref: str, payload: WorkItemAckCreate) -> WorkItemState:
        state = self._work_item_state(ref)
        state = self._ensure_work_item_lane_defaults(state)
        actor = self._coerce_owner(payload.actor) or payload.actor
        now = time.time()
        if not state.handoff:
            current_owner = self._coerce_owner(state.current_owner)
            next_owner = self._coerce_owner(state.next_owner)
            if payload.accepted and current_owner and (current_owner != actor) and (next_owner == actor) and (state.current_stage in {'ready_for_validation', 'validation_running', 'ready_to_close'}):
                state.handoff = WorkItemHandoff(from_agent=current_owner, to_agent=actor, reason='Inferred from canonical validation/release lane state.', expected_action=state.next_action, requested_at=state.last_meaningful_update_at or now, acknowledged_at=now, status='accepted', artifact_state=state.artifact_state, stage=state.current_stage)
            else:
                raise HTTPException(status_code=409, detail='No pending handoff for work item')
        if actor != state.handoff.to_agent:
            raise HTTPException(status_code=409, detail='Ack actor does not match handoff recipient')
        artifact_state = self._normalize_artifact_state(payload.artifact_state or state.handoff.artifact_state or state.artifact_state, fallback=self._infer_artifact_state_from_state(state))
        if payload.accepted:
            handoff_error = self._validate_handoff_edge(state, from_agent=state.handoff.from_agent, to_agent=actor, artifact_state=artifact_state)
            if handoff_error:
                raise HTTPException(status_code=409, detail=handoff_error)
        state.handoff.acknowledged_at = now
        state.handoff.status = 'accepted' if payload.accepted else 'rejected'
        state.handoff.artifact_state = artifact_state
        state.updated_at = now
        state.last_meaningful_update_at = now
        state.last_owner_activity_at = now
        state.blocker = payload.blocker or None
        if payload.blocking_findings is not None:
            state.blocking_findings = self._normalize_blocking_findings(payload.blocking_findings)
        state.artifact_state = artifact_state
        if payload.accepted:
            state.current_owner = actor
            state.next_owner = actor
            state.next_action = payload.next_action or state.handoff.expected_action or state.next_action
            state.current_stage = self._normalize_work_item_stage(payload.current_stage, fallback='validation_running' if state.current_stage == 'ready_for_validation' else state.current_stage)
        else:
            state.current_owner = state.handoff.from_agent
            state.next_owner = state.handoff.from_agent
            state.next_action = payload.next_action or state.next_action
            state.current_stage = self._normalize_work_item_stage(payload.current_stage, fallback='failed_with_action_owner' if state.blocker else 'implementation_active')
        if payload.next_owner is not None:
            state.next_owner = self._coerce_owner(payload.next_owner)
        state = self._record_handoff_history(state, state.handoff, status=state.handoff.status, acknowledged_at=now)
        self._append_work_item_event(self._work_item_event(ref, 'handoff_acknowledged' if payload.accepted else 'handoff_rejected', actor=payload.actor, payload={'current_owner': state.current_owner, 'current_stage': state.current_stage, 'next_action': state.next_action, 'blocker': state.blocker, 'blocking_findings': state.blocking_findings, 'inferred_handoff': bool(state.handoff and state.handoff.reason == 'Inferred from canonical validation/release lane state.')}))
        state = self._sync_work_item_status_label(state)
        state = state
        return self._save_work_item_state(state)

    def _structured_progress(self, ref: str, payload: WorkItemProgressUpdate) -> WorkItemState:
        state = self._work_item_state(ref)
        fields_set = payload.model_fields_set
        state = self._touch_work_item_progress(state, actor=payload.actor, current_owner=payload.current_owner, current_stage=payload.current_stage, next_action=payload.next_action, next_owner=payload.next_owner, next_owner_present='next_owner' in fields_set, blocker=payload.blocker, blocker_present='blocker' in fields_set, blocking_findings=payload.blocking_findings, blocking_findings_present='blocking_findings' in fields_set, release_gate=payload.release_gate, status_label=payload.status_label, artifact_state=payload.artifact_state, note=payload.note)
        state = self._sync_work_item_status_label(state)
        state = state
        return self._save_work_item_state(state)

    def schedule_gitlab_issue_label_sync(self, state: WorkItemState) -> WorkItemState:
        """Schedule GitLab label projection without blocking synchronous state consumers."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return state

        snapshot = state.model_copy(deep=True)

        async def run() -> None:
            try:
                await self.sync_gitlab_issue_labels(snapshot)
            except Exception as exc:
                self.host._append_bot_event(
                    {
                        "type": "work_item_gitlab_label_sync_failed",
                        "ref": snapshot.ref,
                        "error": str(exc)[:500],
                    }
                )

        loop.create_task(run())
        return state

    async def sync_gitlab_issue_labels(self, state: WorkItemState) -> WorkItemState:
        """Project canonical ownership/status labels through the async GitLab client."""
        if state.kind not in {"issue", "merge_request"}:
            return state
        ref_parts = self._work_item_issue_ref_parts(state.ref)
        if ref_parts is None or not state.project_id:
            return state
        token = self.host._gitlab_token_for_project(state.project_id)
        if not token:
            return state
        project_path, iid = ref_parts
        issue = await self.gitlab.project_issue(
            self.host.GITLAB_API_BASE,
            project_path,
            iid,
            token=token,
        )
        current_labels = [str(label).strip() for label in issue.get("labels", []) if str(label).strip()]
        next_labels = [label for label in current_labels if not label.startswith(("owner::", "status::"))]
        if state.current_stage != "closed" and state.current_owner:
            next_labels.append(f"owner::{state.current_owner}")
        status_label = self._derived_status_label_for_work_item(state)
        if state.current_stage != "closed" and status_label:
            next_labels.append(status_label)
        deduped_labels = list(dict.fromkeys(next_labels))
        if deduped_labels != current_labels:
            issue = await self.gitlab.update_project_issue(
                self.host.GITLAB_API_BASE,
                project_path,
                iid,
                token=token,
                payload={"labels": ",".join(deduped_labels)},
            )
        state.labels = [str(label).strip() for label in issue.get("labels", []) if str(label).strip()]
        state.status_label = self._current_status_label(state.labels) or status_label
        self.host._remember_gitlab_semantic_issue_state(
            state.ref,
            labels=state.labels,
            state=str(issue.get("state") or "opened"),
            reason="codex-web-label-sync",
        )
        return self._save_work_item_state(state)



def install_work_item_state_machine(
    app: Any,
    host: Any,
    gitlab: GitLabClient | None = None,
) -> WorkItemStateMachine:
    existing = getattr(app.state, "work_item_state_machine", None)
    if existing is not None:
        return existing
    machine = WorkItemStateMachine(host, gitlab)
    app.state.work_item_state_machine = machine
    host._sync_gitlab_issue_labels_from_work_item = machine.schedule_gitlab_issue_label_sync
    host._append_work_item_event = machine._append_work_item_event
    host._work_item_event = machine._work_item_event
    host._work_item_state_public = machine._work_item_state_public
    host._normalize_work_item_stage = machine._normalize_work_item_stage
    host._normalize_artifact_state = machine._normalize_artifact_state
    host._normalize_blocking_findings = machine._normalize_blocking_findings
    host._ensure_work_item_lane_defaults = machine._ensure_work_item_lane_defaults
    host._infer_artifact_state_from_state = machine._infer_artifact_state_from_state
    host._infer_artifact_state_from_gitlab_payload = machine._infer_artifact_state_from_gitlab_payload
    host._routing_error_detail = machine._routing_error_detail
    host._validate_handoff_edge = machine._validate_handoff_edge
    host._record_handoff_history = machine._record_handoff_history
    host._archive_active_handoff = machine._archive_active_handoff
    host._current_status_label = machine._current_status_label
    host._has_routing_labels = machine._has_routing_labels
    host._priority_from_labels = machine._priority_from_labels
    host._parse_gitlab_timestamp = machine._parse_gitlab_timestamp
    host._latest_gitlab_timestamp = machine._latest_gitlab_timestamp
    host._gitlab_issue_timestamp = machine._gitlab_issue_timestamp
    host._gitlab_payload_timestamp = machine._gitlab_payload_timestamp
    host._gitlab_stage_from_projection = machine._gitlab_stage_from_projection
    host._gitlab_projection_semantics = machine._gitlab_projection_semantics
    host._gitlab_projection_is_stale = machine._gitlab_projection_is_stale
    host._work_item_issue_ref_parts = machine._work_item_issue_ref_parts
    host._derived_status_label_for_work_item = machine._derived_status_label_for_work_item
    host._owner_label_from_labels = machine._owner_label_from_labels
    host._work_item_split_brain_findings = machine._work_item_split_brain_findings
    host._preserve_accepted_handoff_recipient = machine._preserve_accepted_handoff_recipient
    host._reconcile_blocked_work_item_state = machine._reconcile_blocked_work_item_state
    host._sync_work_item_status_label = machine._sync_work_item_status_label
    host._coerce_owner = machine._coerce_owner
    host._normalize_closed_work_item_state = machine._normalize_closed_work_item_state
    host._touch_work_item_progress = machine._touch_work_item_progress
    host._upsert_work_item_state_from_gitlab_issue = machine._upsert_work_item_state_from_gitlab_issue
    host._upsert_work_item_state_from_gitlab_event = machine._upsert_work_item_state_from_gitlab_event
    host._work_item_state = machine._work_item_state
    host._save_work_item_state = machine._save_work_item_state
    host._structured_handoff = machine._structured_handoff
    host._structured_ack = machine._structured_ack
    host._structured_progress = machine._structured_progress
    return machine
