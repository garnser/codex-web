"""Deterministic projection identity and bounded, credential-free failures."""
from __future__ import annotations

import hashlib
import json
from codex_web.models import TaskSourceProjectionState, WorkItemState


class TaskSourceProjectionStorageError(Exception):
    """Storage failures must never be classified as provider partial success."""


class TaskSourceProjectionError(RuntimeError):
    def __init__(self, reason: str, *, conflict: bool = False):
        allowed = {
            "github_assignee_mapping_missing", "provider_state_changed", "provider_revision_changed",
            "github_state_transition_requires_reconciliation", "provider_projection_not_verified", "provider_identity_mismatch", "provider_response_invalid",
            "projection_authority_changed", "provider_configuration_changed", "canonical_item_missing",
            "canonical_scope_changed", "canonical_revision_changed", "task_source_unavailable",
            "provider_timeout_outcome_unknown", "provider_http_rejected", "provider_unavailable_or_denied",
        }
        safe_reason = reason if reason in allowed else "provider_unavailable_or_denied"
        super().__init__(safe_reason)
        self.reason = safe_reason
        self.conflict = conflict
        self.status_code = None
        self.retry_after_seconds = None

    @property
    def retryable(self):
        return not self.conflict and (
            self.reason in {"provider_unavailable_or_denied", "provider_timeout_outcome_unknown"}
            or self.status_code == 429
            or (self.status_code is not None and 500 <= self.status_code < 600)
        )


def desired_revision(state: WorkItemState) -> str:
    identity = state.source_identity
    facts = [state.organization_id, state.workspace_id, state.project_id,
             identity.source_type if identity else None,
             identity.source_instance if identity else None,
             identity.external_id if identity else None,
             state.current_owner, state.current_stage, state.last_meaningful_update_at]
    return hashlib.sha256(json.dumps(facts, separators=(",", ":")).encode()).hexdigest()


def ensure_projection(state: WorkItemState) -> WorkItemState:
    if state.source_identity is None:
        state.source_projection = None
    elif state.source_projection is None or state.source_projection.desired_revision != desired_revision(state):
        state.source_projection = TaskSourceProjectionState(
            desired_revision=desired_revision(state), target=state.source_identity,
            desired_owner=state.current_owner, desired_stage=state.current_stage,
        )
    return state
