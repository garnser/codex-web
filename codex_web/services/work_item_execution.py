from __future__ import annotations

import json
import time
from typing import Any

from fastapi import HTTPException

from codex_web.failures import (
    create_failure,
    legacy_failure_reason,
)
from codex_web.models import WorkItemEvent, WorkItemState
from codex_web.services.work_item_dependencies import WorkItemRuntimeDependencies
from codex_web.work_item_execution_models import (
    WorkItemCheckpointCreate,
    WorkItemExecutionCheckpoint,
    WorkItemExecutionUpdate,
    WorkItemFailureReason,
    WorkItemUsageRecord,
)


class WorkItemExecutionLifecycleService:
    """Structured retry, deadline, checkpoint, history, and usage state."""

    MAX_CHECKPOINT_HISTORY = 20
    MAX_HISTORY_LIMIT = 500

    def __init__(
        self,
        host: Any | None,
        state_machine: Any,
        *,
        dependencies: WorkItemRuntimeDependencies | None = None,
    ) -> None:
        if dependencies is None:
            dependencies = getattr(state_machine, "dependencies", None)
        if dependencies is None:
            if host is None:
                raise TypeError(
                    "WorkItemExecutionLifecycleService requires "
                    "work-item dependencies"
                )
            dependencies = WorkItemRuntimeDependencies.from_host(host)
        self.dependencies = dependencies
        self.state_machine = state_machine

    def _state(self, ref: str) -> WorkItemState:
        return self.state_machine._work_item_state(ref)

    def _save(self, state: WorkItemState) -> WorkItemState:
        state.updated_at = time.time()
        state.last_meaningful_update_at = state.updated_at
        return self.state_machine._save_work_item_state(state)

    def _event(
        self,
        state: WorkItemState,
        event_type: str,
        *,
        actor: str | None,
        source: str | None,
        reason: str | None,
        payload: dict[str, Any],
    ) -> None:
        self.state_machine._append_work_item_event(
            WorkItemEvent(
                ref=state.ref,
                event_type=event_type,
                created_at=time.time(),
                actor=actor,
                source=source,
                reason=reason,
                payload=payload,
            )
        )

    def history(self, ref: str, *, limit: int = 100) -> dict[str, Any]:
        self._state(ref)
        limit = max(1, min(int(limit), self.MAX_HISTORY_LIMIT))
        events: list[WorkItemEvent] = []
        path = self.dependencies.events_file
        if path.exists():
            try:
                with path.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        try:
                            raw = json.loads(line)
                            event = WorkItemEvent.model_validate(raw)
                        except (json.JSONDecodeError, ValueError, TypeError):
                            continue
                        if event.ref == ref:
                            events.append(event)
            except OSError:
                events = []
        selected = events[-limit:]
        return {
            "ref": ref,
            "items": [event.model_dump(mode="json") for event in selected],
            "count": len(events),
            "returned": len(selected),
        }

    def execution(self, ref: str) -> dict[str, Any]:
        state = self._state(ref)
        return {
            "ref": ref,
            "execution": state.execution.model_dump(mode="json"),
        }

    def update(self, ref: str, payload: WorkItemExecutionUpdate) -> dict[str, Any]:
        state = self._state(ref)
        execution = state.execution
        fields = payload.model_fields_set
        now = time.time()

        if "retry_max_attempts" in fields and payload.retry_max_attempts is not None:
            execution.retry.policy.max_attempts = payload.retry_max_attempts
        if "retry_backoff_seconds" in fields and payload.retry_backoff_seconds is not None:
            execution.retry.policy.backoff_seconds = payload.retry_backoff_seconds
        if "retry_attempt" in fields and payload.retry_attempt is not None:
            if payload.retry_attempt > execution.retry.policy.max_attempts:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "retry_attempt_exceeds_policy",
                        "attempt": payload.retry_attempt,
                        "max_attempts": execution.retry.policy.max_attempts,
                    },
                )
            if payload.retry_attempt > execution.retry.attempt:
                execution.retry.last_retry_at = now
            execution.retry.attempt = payload.retry_attempt
        if execution.retry.attempt > execution.retry.policy.max_attempts:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "retry_policy_below_current_attempt",
                    "attempt": execution.retry.attempt,
                    "max_attempts": execution.retry.policy.max_attempts,
                },
            )

        if "timeout_seconds" in fields:
            execution.timeout_seconds = payload.timeout_seconds
        if "deadline_at" in fields:
            execution.deadline_at = payload.deadline_at

        failure_fields = {
            "failure_category",
            "failure_code",
            "failure_message",
            "failure_retryable",
        }
        supplied_failure_fields = fields & failure_fields
        if payload.clear_failure and supplied_failure_fields:
            raise HTTPException(
                status_code=409,
                detail={"code": "failure_clear_conflicts_with_failure_update"},
            )
        if payload.clear_failure:
            execution.failure_reason = None
        elif supplied_failure_fields:
            if not payload.failure_category or not payload.failure_message:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "failure_classification_incomplete",
                        "required": ["failure_category", "failure_message"],
                    },
                )
            stable_reason = legacy_failure_reason(
                payload.failure_category,
                payload.failure_code,
            )
            canonical = create_failure(
                stable_reason,
                source_subsystem=(
                    payload.source or "work_item_execution"
                ),
                details={
                    "legacy_category": payload.failure_category,
                    "legacy_code": payload.failure_code,
                },
                occurred_at=now,
            )
            execution.failure_reason = WorkItemFailureReason(
                category=payload.failure_category,
                code=payload.failure_code,
                message=payload.failure_message,
                retryable=(
                    payload.failure_retryable
                    if payload.failure_retryable is not None
                    else canonical.automatic_retry_allowed
                ),
                recorded_at=now,
                canonical=canonical,
            )

        state.execution = execution
        state = self._save(state)
        self._event(
            state,
            "execution_lifecycle_updated",
            actor=payload.actor,
            source=payload.source,
            reason=payload.reason,
            payload={
                "retry_attempt": execution.retry.attempt,
                "retry_max_attempts": execution.retry.policy.max_attempts,
                "retry_backoff_seconds": execution.retry.policy.backoff_seconds,
                "timeout_seconds": execution.timeout_seconds,
                "deadline_at": execution.deadline_at,
                "failure_category": (
                    execution.failure_reason.category if execution.failure_reason else None
                ),
                "failure_code": (
                    execution.failure_reason.code if execution.failure_reason else None
                ),
                "failure_reason_code": (
                    execution.failure_reason.canonical.reason_code.value
                    if execution.failure_reason
                    and execution.failure_reason.canonical
                    else None
                ),
                "failure_retryability": (
                    execution.failure_reason.canonical.retryability.value
                    if execution.failure_reason
                    and execution.failure_reason.canonical
                    else None
                ),
                "failure_remediation_key": (
                    execution.failure_reason.canonical.remediation_key
                    if execution.failure_reason
                    and execution.failure_reason.canonical
                    else None
                ),
            },
        )
        return self.execution(ref)

    def checkpoint(self, ref: str, payload: WorkItemCheckpointCreate) -> dict[str, Any]:
        state = self._state(ref)
        execution = state.execution
        sequence = (
            execution.latest_checkpoint.sequence + 1
            if execution.latest_checkpoint is not None
            else 1
        )
        checkpoint = WorkItemExecutionCheckpoint(
            id=f"checkpoint-{sequence}",
            sequence=sequence,
            created_at=time.time(),
            actor=payload.actor,
            source=payload.source,
            reason=payload.reason,
            summary=payload.summary,
            objective=payload.objective,
            current_state=payload.current_state,
            important_decisions=payload.important_decisions,
            blockers=payload.blockers,
            changed_files=payload.changed_files,
            next_actions=payload.next_actions,
        )
        execution.latest_checkpoint = checkpoint
        execution.checkpoint_history = (
            execution.checkpoint_history + [checkpoint]
        )[-self.MAX_CHECKPOINT_HISTORY :]
        state.execution = execution
        state = self._save(state)
        self._event(
            state,
            "execution_checkpoint_recorded",
            actor=payload.actor,
            source=payload.source,
            reason=payload.reason,
            payload={
                "checkpoint_id": checkpoint.id,
                "sequence": checkpoint.sequence,
                "summary": checkpoint.summary,
                "blockers": checkpoint.blockers,
                "changed_files": checkpoint.changed_files,
                "next_actions": checkpoint.next_actions,
            },
        )
        return {
            "ref": ref,
            "checkpoint": checkpoint.model_dump(mode="json"),
        }

    def record_usage(self, ref: str, payload: WorkItemUsageRecord) -> dict[str, Any]:
        state = self._state(ref)
        usage = state.execution.usage
        usage.calls += payload.calls
        usage.input_tokens += payload.input_tokens
        usage.output_tokens += payload.output_tokens
        usage.reasoning_tokens += payload.reasoning_tokens
        usage.estimated_cost_usd += payload.estimated_cost_usd
        if payload.goal_id is not None:
            usage.goal_id = payload.goal_id
        if payload.decision_id is not None:
            usage.decision_id = payload.decision_id
        state.execution.usage = usage
        state = self._save(state)
        self._event(
            state,
            "execution_usage_recorded",
            actor=payload.actor,
            source=payload.source,
            reason=payload.reason,
            payload={
                "provider": payload.provider,
                "model": payload.model,
                "role": payload.role,
                "calls": payload.calls,
                "input_tokens": payload.input_tokens,
                "output_tokens": payload.output_tokens,
                "reasoning_tokens": payload.reasoning_tokens,
                "estimated_cost_usd": payload.estimated_cost_usd,
                "goal_id": payload.goal_id,
                "decision_id": payload.decision_id,
            },
        )
        return {
            "ref": ref,
            "usage": usage.model_dump(mode="json"),
        }
