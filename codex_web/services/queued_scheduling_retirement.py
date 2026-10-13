from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import HTTPException

from codex_web.identity import AuthenticationActor
from codex_web.models import (
    QueuedSchedulingRetirement,
    QueuedTurn,
    TaskSourceIdentity,
    WorkItemEvent,
    WorkItemState,
)
from codex_web.services.execution_preflight import ExecutionPreflightService
from codex_web.services.task_source_reconciliation import same_task_source_identity
from codex_web.storage.turn_queue import TurnQueueRepository


WorkItemGetter = Callable[[str], WorkItemState]
SourceDetailReader = Callable[[str], Awaitable[dict[str, Any]]]
ThreadActive = Callable[[str | None], bool]
AuditAppender = Callable[[WorkItemEvent], None]
ReplacementResolver = Callable[..., str | None]


class QueuedSchedulingRetirementService:
    """Retire obsolete scheduling while retaining unknown native outcomes."""

    TERMINAL_SOURCE_STATES = frozenset({"closed", "merged"})

    def __init__(
        self,
        *,
        preflight: ExecutionPreflightService,
        queues: TurnQueueRepository,
        work_item: WorkItemGetter,
        source_detail: SourceDetailReader,
        replacement_resolver: ReplacementResolver,
        thread_is_active: ThreadActive,
        append_audit: AuditAppender,
        clock=time.time,
    ) -> None:
        self.preflight = preflight
        self.queues = queues
        self.work_item = work_item
        self.source_detail = source_detail
        self.replacement_resolver = replacement_resolver
        self.thread_is_active = thread_is_active
        self.append_audit = append_audit
        self.clock = clock

    @staticmethod
    def _conflict(code: str, message: str) -> HTTPException:
        return HTTPException(status_code=409, detail={"code": code, "message": message})

    @staticmethod
    def _snapshot_digest(snapshot: dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps(
            snapshot, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()

    @staticmethod
    def _validate_retained_identity(attempt, queued: QueuedTurn) -> None:
        fields = (
            "project_id",
            "repository_resource_id",
            "writable_repository_resource_ids",
            "read_only_repository_resource_ids",
            "execution_profile_id",
            "agent_profile_id",
            "agent_profile_revision",
            "agent_profile_actor_id",
        )
        if any(getattr(attempt, field) != getattr(queued, field) for field in fields):
            raise QueuedSchedulingRetirementService._conflict(
                "scheduling_retirement_scope_changed",
                "Retained queue scope or profile provenance changed.",
            )

    def _project_scheduling_identity(
        self,
        attempt,
    ) -> tuple[QueuedTurn, QueuedTurn, str]:
        """Project only verified replacement routing onto immutable evidence."""
        try:
            original = QueuedTurn.model_validate(attempt.queued_turn_snapshot)
        except Exception as exc:
            raise self._conflict(
                "scheduling_retirement_snapshot_invalid",
                "The retained queue snapshot is invalid.",
            ) from exc
        if original.thread_id != attempt.thread_id:
            raise self._conflict(
                "scheduling_retirement_scope_changed",
                "The retained queue thread no longer matches its immutable preflight identity.",
            )
        try:
            resolved = self.replacement_resolver(
                attempt.thread_id,
                project_id=attempt.project_id,
                organization_id=attempt.organization_id,
                workspace_id=attempt.workspace_id,
            )
        except Exception as exc:
            raise self._conflict(
                "scheduling_retirement_route_unverified",
                "The retained queue replacement route is invalid or outside its original scope.",
            ) from exc
        target_thread = resolved or attempt.thread_id
        if attempt.replacement_thread_id != resolved:
            raise self._conflict(
                "scheduling_retirement_route_unverified",
                "The retained queue replacement does not match canonical verified routing.",
            )

        projected = original.model_dump(mode="json")
        projected["thread_id"] = target_thread
        reply = projected.get("reply_target")
        if isinstance(reply, dict) and reply.get("thread_id") == original.thread_id:
            projected["reply_target"] = dict(reply, thread_id=target_thread)
        return original, QueuedTurn.model_validate(projected), target_thread

    def _validate_inactive_route(self, original_thread: str, target_thread: str) -> None:
        if self.thread_is_active(original_thread) or (
            target_thread != original_thread and self.thread_is_active(target_thread)
        ):
            raise self._conflict(
                "scheduling_retirement_turn_active",
                "Scheduling cannot be retired while the original or replacement Thread is active.",
            )

    @staticmethod
    def _validate_work_item_scope(attempt, queued: QueuedTurn, item: WorkItemState) -> None:
        if (
            item.organization_id != attempt.organization_id
            or item.workspace_id != attempt.workspace_id
            or item.project_id != attempt.project_id
        ):
            raise QueuedSchedulingRetirementService._conflict(
                "scheduling_retirement_cross_tenant",
                "The retained request and terminal WorkItem do not share tenant and Project scope.",
            )
        target_resources = set(item.resource_ids)
        requested_writable = set(queued.writable_repository_resource_ids)
        if queued.repository_resource_id:
            requested_writable.add(queued.repository_resource_id)
        if not requested_writable or not requested_writable.issubset(target_resources):
            raise QueuedSchedulingRetirementService._conflict(
                "scheduling_retirement_resource_mismatch",
                "The terminal WorkItem does not prove the retained writable repository scope.",
            )
        if item.current_stage != "closed":
            raise QueuedSchedulingRetirementService._conflict(
                "scheduling_retirement_work_item_open",
                "The canonical WorkItem is not terminal.",
            )
        if item.source_identity is None:
            raise QueuedSchedulingRetirementService._conflict(
                "scheduling_retirement_source_missing",
                "The terminal WorkItem has no authoritative source identity.",
            )

    async def retire(
        self,
        attempt_id: str,
        *,
        thread_id: str,
        actor: AuthenticationActor,
    ):
        attempt = self.preflight.authorize_scheduling_retirement(
            attempt_id,
            actor=actor,
        )
        if thread_id != (attempt.replacement_thread_id or attempt.thread_id):
            raise HTTPException(status_code=404, detail="preflight attempt not found")
        if (
            attempt.status != "reconciliation_required"
            or attempt.queued_turn_snapshot is None
            or attempt.queued_start_receipt is not None
            or attempt.started_at is not None
        ):
            raise self._conflict(
                "scheduling_retirement_unavailable",
                "Only retained scheduling with an unknown native outcome can be retired.",
            )

        queued, projected, target_thread = self._project_scheduling_identity(attempt)
        self._validate_retained_identity(attempt, queued)
        work_item_ref = str(queued.work_item_ref or "").strip()
        if not work_item_ref:
            raise self._conflict(
                "scheduling_retirement_work_item_missing",
                "The retained queue entry has no exact WorkItem provenance.",
            )
        self._validate_inactive_route(attempt.thread_id, target_thread)

        try:
            item = self.work_item(work_item_ref)
        except Exception as exc:
            raise self._conflict(
                "scheduling_retirement_work_item_missing",
                "The exact canonical WorkItem is unavailable.",
            ) from exc
        self._validate_work_item_scope(attempt, queued, item)

        source = await self.source_detail(work_item_ref)
        raw_snapshot = source.get("snapshot") if isinstance(source, dict) else None
        if not isinstance(raw_snapshot, dict):
            raise self._conflict(
                "scheduling_retirement_source_unavailable",
                "A fresh authoritative source snapshot is required.",
            )
        try:
            source_identity = TaskSourceIdentity.model_validate(raw_snapshot.get("identity"))
        except Exception as exc:
            raise self._conflict(
                "scheduling_retirement_source_invalid",
                "The authoritative source snapshot has invalid identity provenance.",
            ) from exc
        assert item.source_identity is not None
        if not same_task_source_identity(item.source_identity, source_identity):
            raise self._conflict(
                "scheduling_retirement_source_conflict",
                "The authoritative source identity no longer matches the canonical WorkItem.",
            )
        source_state = str(raw_snapshot.get("source_state") or "").strip().casefold()
        if source_state not in self.TERMINAL_SOURCE_STATES:
            raise self._conflict(
                "scheduling_retirement_source_open",
                "The current authoritative provider object is not terminal.",
            )

        # Re-read every mutable canonical input after the provider await.  This
        # is the retirement CAS boundary: stale task copies cannot remove a
        # queue entry after another administrator, receipt, or source sync won.
        initial_snapshot = attempt.queued_turn_snapshot
        attempt = self.preflight.get(attempt_id, actor=actor)
        if (
            attempt.status != "reconciliation_required"
            or attempt.queued_turn_snapshot != initial_snapshot
            or attempt.queued_start_receipt is not None
            or attempt.started_at is not None
        ):
            raise self._conflict(
                "scheduling_retirement_conflict",
                "Retained execution evidence changed during terminal verification.",
            )
        current_queued, current_projected, current_target = (
            self._project_scheduling_identity(attempt)
        )
        if (
            thread_id != current_target
            or current_queued != queued
            or current_projected != projected
            or current_target != target_thread
        ):
            raise self._conflict(
                "scheduling_retirement_conflict",
                "Verified replacement routing changed during terminal verification.",
            )
        item = self.work_item(work_item_ref)
        self._validate_work_item_scope(attempt, queued, item)
        assert item.source_identity is not None
        if not same_task_source_identity(item.source_identity, source_identity):
            raise self._conflict(
                "scheduling_retirement_source_conflict",
                "Canonical source provenance changed during terminal verification.",
            )
        self._validate_inactive_route(attempt.thread_id, target_thread)

        if (
            attempt.scheduling_retirement is not None
            and attempt.scheduling_retirement.state == "retired"
        ):
            return self._audit_if_needed(attempt, actor=actor)

        current = self.queues.get(target_thread)
        matching = [entry for entry in current if entry.id == queued.id]
        if len(matching) != 1 or matching[0] != projected:
            code = (
                "scheduling_retirement_reconciliation_required"
                if attempt.scheduling_retirement is not None
                else "scheduling_retirement_queue_missing"
                if not matching
                else "scheduling_retirement_queue_changed"
            )
            raise self._conflict(
                code,
                "The current queue no longer contains the exact retained request.",
            )

        prepared = attempt.scheduling_retirement
        if prepared is None:
            prepared = QueuedSchedulingRetirement(
                queued_turn_id=queued.id,
                queued_snapshot_sha256=self._snapshot_digest(attempt.queued_turn_snapshot),
                work_item_ref=work_item_ref,
                source_identity=source_identity,
                source_state=source_state,
                canonical_terminal_outcome=item.terminal_outcome,
                retired_by=actor.identity_id,
                prepared_at=float(self.clock()),
            )
            attempt = self.preflight.prepare_scheduling_retirement(
                attempt.id,
                evidence=prepared,
                expected_snapshot=attempt.queued_turn_snapshot,
            )
        elif (
            prepared.queued_turn_id != queued.id
            or prepared.queued_snapshot_sha256 != self._snapshot_digest(attempt.queued_turn_snapshot)
            or prepared.work_item_ref != work_item_ref
            or not same_task_source_identity(prepared.source_identity, source_identity)
        ):
            raise self._conflict(
                "scheduling_retirement_conflict",
                "Prepared scheduling retirement provenance no longer matches.",
            )

        # The replacement graph can advance independently of provider I/O and
        # preflight persistence.  Resolve it once more immediately before the
        # exact queue CAS; an extension must be reconciled, never guessed.
        final_queued, final_projected, final_target = (
            self._project_scheduling_identity(attempt)
        )
        if (
            final_queued != queued
            or final_projected != projected
            or final_target != target_thread
        ):
            raise self._conflict(
                "scheduling_retirement_conflict",
                "Verified replacement routing changed before queue retirement.",
            )
        self._validate_inactive_route(attempt.thread_id, target_thread)
        removal = self.queues.remove_exact(target_thread, projected)
        if removal != "removed":
            raise self._conflict(
                "scheduling_retirement_reconciliation_required",
                "Queue removal did not produce a confirmed exact retirement; retained evidence remains fenced.",
            )
        completed = self.preflight.complete_scheduling_retirement(
            attempt.id,
            prepared=prepared,
        )
        return self._audit_if_needed(completed, actor=actor)

    def _audit_if_needed(self, attempt, *, actor: AuthenticationActor):
        evidence = attempt.scheduling_retirement
        if evidence is None or evidence.state != "retired":
            return attempt
        if evidence.audit_recorded_at is not None:
            return attempt
        self.append_audit(WorkItemEvent(
            ref=evidence.work_item_ref,
            event_type="queued_scheduling_retired",
            created_at=float(self.clock()),
            actor=actor.identity_id,
            source="execution-preflight-admin",
            reason="authoritative terminal WorkItem scheduling retirement",
            payload={
                "preflightAttemptId": attempt.id,
                "queuedTurnId": evidence.queued_turn_id,
                "threadId": attempt.replacement_thread_id or attempt.thread_id,
                "originalThreadId": attempt.thread_id,
                "nativeOutcome": evidence.native_outcome,
                "sourceState": evidence.source_state,
                "sourceExternalId": evidence.source_identity.external_id,
                "queueSnapshotSha256": evidence.queued_snapshot_sha256,
            },
        ))
        return self.preflight.mark_scheduling_retirement_audited(
            attempt.id,
            retired=evidence,
        )
