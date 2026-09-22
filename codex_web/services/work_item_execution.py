from __future__ import annotations

import hashlib
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

    def _events_for_ref(self, ref: str) -> list[WorkItemEvent]:
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
                return []
        return events

    @staticmethod
    def _stable_json(value: Any) -> str:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        )

    @classmethod
    def _hash_value(cls, value: Any) -> str:
        digest = hashlib.sha256(cls._stable_json(value).encode("utf-8")).hexdigest()
        return f"sha256:{digest}"

    @staticmethod
    def _work_item_context(state: WorkItemState) -> dict[str, Any]:
        """Bounded task context; volatile timestamps and execution bookkeeping are excluded."""
        return {
            "ref": state.ref,
            "project_id": state.project_id,
            "goal_id": state.goal_id,
            "decision_id": state.decision_id,
            "originating_action_intent_id": state.originating_action_intent_id,
            "resource_ids": sorted(set(state.resource_ids)),
            "project_path": state.project_path,
            "source_identity": (
                state.source_identity.model_dump(mode="json")
                if state.source_identity is not None
                else None
            ),
            "title": state.title,
            "url": state.url,
            "kind": state.kind,
            "priority": state.priority,
            "current_owner": state.current_owner,
            "current_stage": state.current_stage,
            "terminal_outcome": state.terminal_outcome,
            "implementation_owner": state.implementation_owner,
            "validation_owner": state.validation_owner,
            "release_owner": state.release_owner,
            "artifact_state": state.artifact_state,
            "handoff": (
                state.handoff.model_dump(mode="json")
                if state.handoff is not None
                else None
            ),
            "blocker": state.blocker,
            "blocking_findings": list(state.blocking_findings),
            "next_action": state.next_action,
            "next_owner": state.next_owner,
            "release_gate": state.release_gate,
            "labels": list(state.labels),
            "mr_refs": list(state.mr_refs),
            "notes": list(state.notes),
            "closed_at": state.closed_at,
        }

    @classmethod
    def _event_watermark(cls, events: list[WorkItemEvent]) -> str:
        payload = [event.model_dump(mode="json") for event in events]
        return f"wi-events-v1:{len(events)}:{cls._hash_value(payload).split(':', 1)[1]}"

    @classmethod
    def _parse_event_watermark(cls, value: str | None) -> tuple[int, str] | None:
        if not value:
            return None
        parts = value.split(":")
        if len(parts) != 3 or parts[0] != "wi-events-v1":
            return None
        try:
            count = int(parts[1])
        except ValueError:
            return None
        if count < 0 or len(parts[2]) != 64:
            return None
        return count, parts[2]

    def continuation_snapshot(self, ref: str) -> dict[str, Any]:
        state = self._state(ref)
        context = self._work_item_context(state)
        events = self._events_for_ref(ref)
        context_hash = self._hash_value(context)
        return {
            "ref": ref,
            "schema_version": "1.0",
            "work_item_context": context,
            "work_item_hash": context_hash,
            "work_item_revision": f"work-item-context-v1:{context_hash.split(':', 1)[1][:16]}",
            "event_watermark": self._event_watermark(events),
            "event_count": len(events),
        }

    def history(self, ref: str, *, limit: int = 100) -> dict[str, Any]:
        self._state(ref)
        limit = max(1, min(int(limit), self.MAX_HISTORY_LIMIT))
        events = self._events_for_ref(ref)
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
        if "writable_repository_resource_ids" in fields:
            execution.writable_repository_resource_ids = tuple(
                dict.fromkeys(
                    value.strip()
                    for value in payload.writable_repository_resource_ids
                    if value and value.strip()
                )
            )

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
                "writable_repository_resource_ids": list(
                    execution.writable_repository_resource_ids
                ),
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

    def continuation_anchor(self, ref: str) -> dict[str, Any]:
        """Select the newest safe checkpoint without trusting failed attempts."""
        execution = self._state(ref).execution
        history = list(execution.checkpoint_history)
        if not history and execution.latest_checkpoint is not None:
            history = [execution.latest_checkpoint]
        if not history:
            return {
                "trusted": False,
                "reason": "checkpoint_missing",
                "checkpoint": None,
            }

        def assess(
            checkpoint: WorkItemExecutionCheckpoint,
        ) -> tuple[bool, str, list[str]]:
            missing: list[str] = []
            if not checkpoint.delivery_proven:
                missing.append("delivery_not_proven")
            if not checkpoint.delivery_proof_ref:
                missing.append("delivery_proof_missing")
            if not checkpoint.execution_id:
                missing.append("execution_id_missing")
            if not checkpoint.work_item_revision:
                missing.append("work_item_revision_missing")
            if not checkpoint.work_item_hash:
                missing.append("work_item_hash_missing")
            if not checkpoint.event_watermark:
                missing.append("event_watermark_missing")
            if not checkpoint.delivered_context_hash:
                missing.append("delivered_context_hash_missing")
            if missing:
                return (
                    False,
                    "checkpoint_provenance_incomplete",
                    missing,
                )
            if (
                checkpoint.source == "turn-execution-runtime"
                and checkpoint.execution_outcome != "succeeded"
            ):
                outcome = checkpoint.execution_outcome or "unconfirmed"
                return (
                    False,
                    "checkpoint_execution_untrusted",
                    [f"execution_outcome_{outcome}"],
                )
            return True, "delivery_proven", []

        ignored: list[dict[str, Any]] = []
        for checkpoint in reversed(history):
            trusted, reason, blockers = assess(checkpoint)
            if trusted:
                return {
                    "trusted": True,
                    "reason": (
                        "delivery_proven"
                        if not ignored
                        else "prior_trusted_checkpoint"
                    ),
                    "blockers": [],
                    "checkpoint": checkpoint.model_dump(mode="json"),
                    "ignored_checkpoints": ignored,
                }
            ignored.append(
                {
                    "checkpoint_id": checkpoint.id,
                    "execution_id": checkpoint.execution_id,
                    "reason": reason,
                    "blockers": blockers,
                }
            )

        latest = history[-1]
        latest_assessment = ignored[0]
        return {
            "trusted": False,
            "reason": latest_assessment["reason"],
            "blockers": latest_assessment["blockers"],
            "checkpoint": latest.model_dump(mode="json"),
            "ignored_checkpoints": ignored,
        }

    def continuation_delta(
        self,
        ref: str,
        *,
        max_events: int = 100,
        event_offset: int = 0,
    ) -> dict[str, Any]:
        """Return a verified checkpoint delta or an explicit full-context fallback."""
        max_events = max(1, min(int(max_events), self.MAX_HISTORY_LIMIT))
        event_offset = max(0, int(event_offset))
        anchor = self.continuation_anchor(ref)
        checkpoint_payload = anchor.get("checkpoint")
        if not anchor.get("trusted") or not isinstance(checkpoint_payload, dict):
            return {
                "ref": ref,
                "mode": "full",
                "reason": anchor.get("reason", "checkpoint_untrusted"),
                "blockers": anchor.get("blockers", []),
                "snapshot": self.continuation_snapshot(ref),
            }

        checkpoint = WorkItemExecutionCheckpoint.model_validate(checkpoint_payload)
        if checkpoint.schema_version != "1.0":
            return {
                "ref": ref,
                "mode": "full",
                "reason": "checkpoint_schema_incompatible",
                "checkpoint_schema_version": checkpoint.schema_version,
                "snapshot": self.continuation_snapshot(ref),
            }

        baseline = checkpoint.delivered_work_item_context
        if not isinstance(baseline, dict):
            return {
                "ref": ref,
                "mode": "full",
                "reason": "checkpoint_baseline_missing",
                "snapshot": self.continuation_snapshot(ref),
            }
        if self._hash_value(baseline) != checkpoint.work_item_hash:
            return {
                "ref": ref,
                "mode": "full",
                "reason": "checkpoint_baseline_corrupt",
                "snapshot": self.continuation_snapshot(ref),
            }

        events = self._events_for_ref(ref)
        parsed = self._parse_event_watermark(checkpoint.event_watermark)
        if parsed is None:
            return {
                "ref": ref,
                "mode": "full",
                "reason": "event_watermark_invalid",
                "snapshot": self.continuation_snapshot(ref),
            }
        event_count, expected_digest = parsed
        if event_count > len(events):
            return {
                "ref": ref,
                "mode": "full",
                "reason": "event_watermark_ahead",
                "snapshot": self.continuation_snapshot(ref),
            }
        prefix = events[:event_count]
        prefix_digest = self._event_watermark(prefix).rsplit(":", 1)[1]
        if prefix_digest != expected_digest:
            return {
                "ref": ref,
                "mode": "full",
                "reason": "event_watermark_stale_or_corrupt",
                "snapshot": self.continuation_snapshot(ref),
            }

        state = self._state(ref)
        current = self._work_item_context(state)
        changed_fields = {
            key: value
            for key, value in current.items()
            if baseline.get(key) != value
        }
        removed_fields = sorted(set(baseline) - set(current))
        new_events = events[event_count:]
        returned_events = new_events[
            event_offset:event_offset + max_events
        ]
        next_event_offset = event_offset + len(returned_events)
        has_more_events = next_event_offset < len(new_events)
        baseline_bytes = len(self._stable_json(baseline).encode("utf-8"))
        delta_payload = {
            "changed_fields": changed_fields,
            "removed_fields": removed_fields,
            "events": [event.model_dump(mode="json") for event in returned_events],
        }
        delta_bytes = len(self._stable_json(delta_payload).encode("utf-8"))
        return {
            "ref": ref,
            "mode": "delta",
            "reason": "verified_checkpoint_delta",
            "checkpoint_id": checkpoint.id,
            "checkpoint_age_seconds": max(0.0, time.time() - checkpoint.created_at),
            "changed_fields": changed_fields,
            "removed_fields": removed_fields,
            "events": delta_payload["events"],
            "event_count_since_checkpoint": len(new_events),
            "event_offset": event_offset,
            "events_returned": len(returned_events),
            "next_event_offset": (
                next_event_offset if has_more_events else None
            ),
            "requires_progressive_retrieval": has_more_events,
            "current": {
                "objective": checkpoint.objective,
                "stage": state.current_stage,
                "blocker": state.blocker,
                "blocking_findings": list(state.blocking_findings),
                "next_action": state.next_action,
            },
            "metrics": {
                "baseline_context_bytes": baseline_bytes,
                "delta_context_bytes": delta_bytes,
                "estimated_tokens_reused": max(0, (baseline_bytes - delta_bytes) // 4),
            },
            "snapshot": self.continuation_snapshot(ref),
        }

    def checkpoint(self, ref: str, payload: WorkItemCheckpointCreate) -> dict[str, Any]:
        state = self._state(ref)
        execution = state.execution
        snapshot = self.continuation_snapshot(ref)
        delivered_baseline = payload.delivered_work_item_context
        if (
            delivered_baseline is None
            and payload.delivery_proven
            and payload.work_item_hash == snapshot["work_item_hash"]
            and payload.event_watermark == snapshot["event_watermark"]
        ):
            delivered_baseline = snapshot["work_item_context"]
        sequence = (
            execution.latest_checkpoint.sequence + 1
            if execution.latest_checkpoint is not None
            else 1
        )
        checkpoint = WorkItemExecutionCheckpoint(
            schema_version=payload.schema_version,
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
            execution_id=payload.execution_id,
            subject_kind=payload.subject_kind,
            subject_ref=payload.subject_ref,
            execution_contract_version=payload.execution_contract_version,
            agent_profile_id=payload.agent_profile_id,
            agent_profile_revision=payload.agent_profile_revision,
            role_id=payload.role_id,
            provider_id=payload.provider_id,
            runtime_id=payload.runtime_id,
            model_id=payload.model_id,
            session_ref=payload.session_ref,
            resource_ids=payload.resource_ids,
            base_revision=payload.base_revision,
            work_item_revision=payload.work_item_revision,
            work_item_hash=payload.work_item_hash,
            event_watermark=payload.event_watermark,
            delivered_context_hash=payload.delivered_context_hash,
            delivery_proven=payload.delivery_proven,
            delivery_proof_ref=payload.delivery_proof_ref,
            definition_refs=payload.definition_refs,
            delivered_work_item_context=delivered_baseline,
            continuation_mode=payload.continuation_mode,
            continuation_reason=payload.continuation_reason,
            continuation_checkpoint_id=payload.continuation_checkpoint_id,
            context_baseline_bytes=payload.context_baseline_bytes,
            context_delta_bytes=payload.context_delta_bytes,
            estimated_tokens_reused=payload.estimated_tokens_reused,
            fallback_to_full_context_reason=payload.fallback_to_full_context_reason,
            execution_outcome=payload.execution_outcome,
            outcome_recorded_at=payload.outcome_recorded_at,
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
                "execution_id": checkpoint.execution_id,
                "subject_kind": checkpoint.subject_kind,
                "subject_ref": checkpoint.subject_ref,
                "model_id": checkpoint.model_id,
                "resource_ids": checkpoint.resource_ids,
                "base_revision": checkpoint.base_revision,
                "work_item_revision": checkpoint.work_item_revision,
                "work_item_hash": checkpoint.work_item_hash,
                "event_watermark": checkpoint.event_watermark,
                "delivery_proven": checkpoint.delivery_proven,
                "delivery_proof_ref": checkpoint.delivery_proof_ref,
                "continuation_mode": checkpoint.continuation_mode,
                "continuation_reason": checkpoint.continuation_reason,
                "continuation_checkpoint_id": checkpoint.continuation_checkpoint_id,
                "context_baseline_bytes": checkpoint.context_baseline_bytes,
                "context_delta_bytes": checkpoint.context_delta_bytes,
                "estimated_tokens_reused": checkpoint.estimated_tokens_reused,
                "fallback_to_full_context_reason": checkpoint.fallback_to_full_context_reason,
                "execution_outcome": checkpoint.execution_outcome,
                "outcome_recorded_at": checkpoint.outcome_recorded_at,
            },
        )
        return {
            "ref": ref,
            "checkpoint": checkpoint.model_dump(mode="json"),
        }

    def run_context(self, ref: str, execution_id: str) -> dict[str, Any] | None:
        """Return bounded checkpoint/continuation provenance for one execution."""
        state = self._state(ref)
        matches = [
            checkpoint
            for checkpoint in state.execution.checkpoint_history
            if checkpoint.execution_id == execution_id
        ]
        if not matches:
            return None
        checkpoint = max(matches, key=lambda value: value.sequence)
        return {
            "id": checkpoint.id,
            "schemaVersion": checkpoint.schema_version,
            "sequence": checkpoint.sequence,
            "createdAt": checkpoint.created_at,
            "summary": checkpoint.summary,
            "objective": checkpoint.objective,
            "executionId": checkpoint.execution_id,
            "subjectKind": checkpoint.subject_kind,
            "subjectRef": checkpoint.subject_ref,
            "executionContractVersion": checkpoint.execution_contract_version,
            "agentProfileId": checkpoint.agent_profile_id,
            "agentProfileRevision": checkpoint.agent_profile_revision,
            "roleId": checkpoint.role_id,
            "providerId": checkpoint.provider_id,
            "runtimeId": checkpoint.runtime_id,
            "modelId": checkpoint.model_id,
            "sessionRef": checkpoint.session_ref,
            "resourceIds": list(checkpoint.resource_ids),
            "baseRevision": checkpoint.base_revision,
            "workItemRevision": checkpoint.work_item_revision,
            "workItemHash": checkpoint.work_item_hash,
            "eventWatermark": checkpoint.event_watermark,
            "deliveryProven": checkpoint.delivery_proven,
            "deliveryProofRef": checkpoint.delivery_proof_ref,
            "definitionRefs": [
                value.model_dump(mode="json")
                for value in checkpoint.definition_refs
            ],
            "changedFiles": list(checkpoint.changed_files),
            "executionOutcome": checkpoint.execution_outcome,
            "outcomeRecordedAt": checkpoint.outcome_recorded_at,
            "continuation": {
                "mode": checkpoint.continuation_mode,
                "reason": checkpoint.continuation_reason,
                "checkpointId": checkpoint.continuation_checkpoint_id,
                "fallbackToFullContextReason": (
                    checkpoint.fallback_to_full_context_reason
                ),
                "metrics": {
                    "baselineContextBytes": checkpoint.context_baseline_bytes,
                    "deltaContextBytes": checkpoint.context_delta_bytes,
                    "estimatedTokensReused": checkpoint.estimated_tokens_reused,
                },
            },
        }

    def record_continuation_delivery(
        self,
        ref: str,
        execution_id: str,
        selection: dict[str, Any],
        delivered_context: dict[str, Any],
        *,
        provenance: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist the canonical context proven delivered to one execution."""
        snapshot = selection.get("snapshot")
        if not isinstance(snapshot, dict):
            raise ValueError("continuation selection is missing canonical snapshot")
        baseline = snapshot.get("work_item_context")
        if not isinstance(baseline, dict):
            raise ValueError("continuation snapshot is missing Work Item context")

        provenance = dict(provenance or {})
        metrics = selection.get("metrics")
        if not isinstance(metrics, dict):
            metrics = {}
        current = selection.get("current")
        if not isinstance(current, dict):
            current = {}

        return self.checkpoint(
            ref,
            WorkItemCheckpointCreate(
                source="turn-execution-runtime",
                reason="provider accepted canonical Work Item context",
                summary="Canonical Work Item context delivered to execution.",
                objective=current.get("objective"),
                execution_id=execution_id,
                subject_kind="work_item",
                subject_ref=ref,
                execution_contract_version=provenance.get(
                    "execution_contract_version"
                ),
                agent_profile_id=provenance.get("agent_profile_id"),
                agent_profile_revision=provenance.get(
                    "agent_profile_revision"
                ),
                role_id=provenance.get("role_id"),
                provider_id=provenance.get("provider_id"),
                runtime_id=provenance.get("runtime_id"),
                model_id=provenance.get("model_id"),
                session_ref=provenance.get("session_ref"),
                resource_ids=list(provenance.get("resource_ids") or []),
                base_revision=provenance.get("base_revision"),
                work_item_revision=snapshot.get("work_item_revision"),
                work_item_hash=snapshot.get("work_item_hash"),
                event_watermark=snapshot.get("event_watermark"),
                delivered_context_hash=self._hash_value(delivered_context),
                delivery_proven=True,
                delivery_proof_ref=f"turn-start:{execution_id}",
                definition_refs=list(provenance.get("definition_refs") or []),
                delivered_work_item_context=baseline,
                continuation_mode=str(selection.get("mode") or "full"),
                continuation_reason=str(
                    selection.get("reason") or "canonical_context"
                ),
                continuation_checkpoint_id=selection.get("checkpoint_id"),
                context_baseline_bytes=metrics.get(
                    "baseline_context_bytes"
                ),
                context_delta_bytes=metrics.get("delta_context_bytes"),
                estimated_tokens_reused=metrics.get(
                    "estimated_tokens_reused"
                ),
                fallback_to_full_context_reason=(
                    str(selection.get("reason"))
                    if selection.get("mode") == "full"
                    and selection.get("reason")
                    else None
                ),
                execution_outcome="pending",
            ),
        )

    def record_continuation_outcome(
        self,
        ref: str,
        execution_id: str,
        outcome: str,
    ) -> dict[str, Any] | None:
        """Finalize trust eligibility for a runtime-created context checkpoint."""
        normalized = str(outcome or "").strip().casefold()
        if normalized not in {
            "succeeded",
            "failed",
            "lost",
            "poisoned",
            "cancelled",
            "ambiguous",
        }:
            raise ValueError("unsupported continuation execution outcome")

        state = self._state(ref)
        execution = state.execution
        target = next(
            (
                checkpoint
                for checkpoint in reversed(execution.checkpoint_history)
                if checkpoint.execution_id == execution_id
                and checkpoint.source == "turn-execution-runtime"
            ),
            None,
        )
        if target is None:
            return None

        recorded_at = time.time()
        updated = target.model_copy(
            update={
                "execution_outcome": normalized,
                "outcome_recorded_at": recorded_at,
            }
        )
        execution.checkpoint_history = [
            updated if checkpoint.id == target.id else checkpoint
            for checkpoint in execution.checkpoint_history
        ]
        if (
            execution.latest_checkpoint is not None
            and execution.latest_checkpoint.id == target.id
        ):
            execution.latest_checkpoint = updated
        state.execution = execution
        state = self._save(state)
        self._event(
            state,
            "execution_checkpoint_outcome_recorded",
            actor=None,
            source="turn-execution-runtime",
            reason=f"execution {normalized}",
            payload={
                "checkpoint_id": updated.id,
                "execution_id": execution_id,
                "execution_outcome": normalized,
                "outcome_recorded_at": recorded_at,
                "continuation_eligible": normalized == "succeeded",
            },
        )
        return {
            "ref": ref,
            "checkpoint": updated.model_dump(mode="json"),
            "continuation_eligible": normalized == "succeeded",
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
