"""Read-only projections of canonical batch admissions; no native inference."""

import json
import time

from fastapi import HTTPException
from codex_web.identity import AuthenticationAssurance
from codex_web.services.identity import IdentityService


class NotificationBatchInspection:
    def __init__(self, preflight, *, outcomes=None, clock=time.time):
        self.preflight = preflight
        self.outcomes = outcomes
        self.clock = clock

    @staticmethod
    def _authorize(batch, *, thread_id, project, actor):
        if (
            batch is None
            or batch.plan.organization_id != actor.organization_id
            or batch.plan.workspace_id != actor.workspace_id
            or (project.organization_id, project.workspace_id)
            != (actor.organization_id, actor.workspace_id)
            or any(
                member.thread_id != batch.plan.members[0].thread_id
                or member.project_id != project.id
                for member in batch.plan.members
            )
            or any(
                json.loads(raw).get("project_id") != project.id
                for raw in batch.plan.original_snapshots_json
            )
            or thread_id
            not in {
                batch.plan.members[0].thread_id,
                batch.recovery.target_thread_id if batch.recovery else None,
            }
        ):
            raise HTTPException(status_code=404, detail="notification batch not found")

    @staticmethod
    def _can_review_recovery(actor):
        try:
            IdentityService.require_admin(actor)
            IdentityService.require_assurance(actor, AuthenticationAssurance.MFA)
            return True
        except (PermissionError, RuntimeError, AttributeError):
            return False

    def _outcomes(self, batch, state):
        if self.outcomes is None:
            return {member.id: {"outcome": "unresolved", "blockers": ["outcome_inspection_unavailable"],
                "evidence_ids": [], "verification_ids": []} for member in batch.plan.members}
        results = self.outcomes.project(batch, state=state, now=self.clock())
        for attempt_id, raw in zip(batch.attempt_ids, batch.plan.original_snapshots_json, strict=True):
            original = json.loads(raw)
            retained = self.preflight.get(attempt_id)
            if results[original["id"]]["outcome"] != "unresolved" and (
                retained is None or retained.queued_turn_snapshot != original
                or retained.notification_batch_id != batch.id
                or retained.queued_admission_claim_id != batch.claim_id
                or retained.status != "reconciliation_required"
            ):
                results[original["id"]]["outcome"] = "unresolved"
                results[original["id"]]["blockers"].append("retained_original_changed_or_missing")
        return results

    def _summary(self, batch, actor=None, outcomes=None):
        originals = [json.loads(raw) for raw in batch.plan.original_snapshots_json]
        oldest = min(item["created_at"] for item in originals)
        member = batch.plan.members[0]
        counts = {key: 0 for key in ("reconciled", "follow_up_required", "unresolved")}
        for item in (outcomes or self._outcomes(batch, None)).values():
            counts[item["outcome"]] += 1
        return {
            "id": batch.id,
            "schema_version": batch.schema_version,
            "result_contract_version": batch.plan.result_contract_version,
            "can_capture_results": self._can_review_recovery(actor)
            and batch.plan.result_contract_version is not None and batch.native_receipt is not None,
            "can_review_processing": self._can_review_recovery(actor) and batch.native_receipt is not None,
            "processing_count": len(batch.processing),
            "phase": batch.phase,
            "thread_id": member.thread_id,
            "project_id": member.project_id,
            "organization_id": batch.plan.organization_id,
            "workspace_id": batch.plan.workspace_id,
            "original_count": len(originals),
            "oldest_created_at": oldest,
            "oldest_age_seconds": max(0, self.clock() - oldest),
            "agent_profile_id": member.agent_profile_id,
            "agent_profile_revision": member.agent_profile_revision,
            "execution_profile_id": member.execution_profile_id,
            "repository_resource_id": member.repository_resource_id,
            "writable_repository_resource_ids": member.writable_repository_resource_ids,
            "read_only_repository_resource_ids": member.read_only_repository_resource_ids,
            "authority_scope_sha256": batch.plan.authority_scope_sha256,
            "original_ids": [item["id"] for item in originals],
            "native_receipt": batch.native_receipt.model_dump(mode="json")
            if batch.native_receipt
            else None,
            "prestart_refusal_code": batch.prestart_refusal_code,
            "recovery_state": "prepared_recovery_requires_current_authority"
            if batch.phase in {"prepared", "prestart_refused"}
            else "released_to_original_queue"
            if batch.phase == "released"
            else "batch_reconciliation_required",
            "per_original_completion": "unresolved" if counts["unresolved"] else "verified_reconciliation",
            "outcome_counts": counts,
            "detail_href": f"/api/threads/{member.thread_id}/notification-batches/{batch.id}",
            "recovery": batch.recovery.model_dump(
                mode="json", exclude={"restoration_snapshots_json"}
            )
            if batch.recovery
            else None,
            "can_review_recovery": self._can_review_recovery(actor)
            and batch.phase in {"prepared", "prestart_refused"},
        }

    def list(self, *, thread_id, project, actor, limit=20, after=None):
        if (project.organization_id, project.workspace_id) != (
            actor.organization_id,
            actor.workspace_id,
        ):
            raise HTTPException(status_code=404, detail="notification batch not found")
        try:
            batches, cursor = self.preflight.notification_batches_for_thread(
                thread_id,
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                project_id=project.id,
                limit=limit,
                after=after,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        visible = []
        state = self.outcomes.snapshot() if self.outcomes else None
        for batch in batches:
            try:
                self._authorize(
                    batch, thread_id=thread_id, project=project, actor=actor
                )
            except HTTPException:
                continue
            visible.append(self._summary(batch, actor, self._outcomes(batch, state)))
        return {
            "schema_version": "1.0",
            "threadId": thread_id,
            "items": visible,
            "next_cursor": cursor,
            "history_scope": "indexed_admissions",
        }

    def detail(self, batch_id, *, thread_id, project, actor):
        batch = self.preflight.get_notification_batch(batch_id)
        self._authorize(batch, thread_id=thread_id, project=project, actor=actor)
        outcomes = self._outcomes(batch, self.outcomes.snapshot() if self.outcomes else None)
        payload = self._summary(batch, actor, outcomes)
        originals = []
        for attempt_id, raw, route in zip(
            batch.attempt_ids,
            batch.plan.original_snapshots_json,
            batch.plan.queue_snapshots_json,
            strict=True,
        ):
            retained = self.preflight.get(attempt_id)
            if retained is not None and (
                retained.organization_id,
                retained.workspace_id,
                retained.project_id,
            ) != (actor.organization_id, actor.workspace_id, project.id):
                raise HTTPException(
                    status_code=404, detail="notification batch not found"
                )
            route_values = json.loads(route)
            originals.append(
                {
                    "original": json.loads(raw),
                    "admitted_route": {
                        key: route_values.get(key)
                        for key in ("id", "thread_id", "reply_target")
                    },
                    "attempt_id": attempt_id,
                    "processing_receipt": next((row.model_dump(mode="json") for row in batch.processing
                        if row.original_id == json.loads(raw)["id"]), None),
                    "retained_status": retained.status if retained else "missing",
                    "current_batch_id": retained.notification_batch_id
                    if retained
                    else None,
                    "retention_integrity": "matches_original"
                    if retained and retained.queued_turn_snapshot == json.loads(raw)
                    else "changed_or_missing",
                    **outcomes[json.loads(raw)["id"]],
                }
            )
        return {
            **payload,
            "originals": originals,
            "control_snapshot": batch.plan.control_snapshot.model_dump(mode="json")
            if batch.plan.control_snapshot
            else None,
        }
