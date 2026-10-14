"""Capture unverified proposals from the canonical native transcript only."""

import hashlib
import json
import math

from codex_web.artifact_evidence import EvidenceCreate, EvidenceResult, EvidenceType, sha256_digest
from codex_web.notification_outcomes import (
    NotificationOriginalOutcomeBinding, OUTCOME_METADATA_KEY, original_snapshot_digest,
    NOTIFICATION_RESULT_SOURCE,
)
from codex_web.notification_results import MAX_RESULT_BYTES, NotificationBatchResult


def _unique_keys(pairs):
    values = {}
    for key, value in pairs:
        if key in values:
            raise ValueError("duplicate notification result JSON key")
        values[key] = value
    return values


class NotificationResultCapture:
    def __init__(self, *, preflight, transcript, evidence, actor_for, project_for):
        self.preflight, self.transcript, self.evidence = preflight, transcript, evidence
        self.actor_for, self.project_for = actor_for, project_for

    def on_terminal(self, message):
        if message.get("method") != "turn/completed":
            return None
        params = message.get("params") or {}
        turn = params.get("turn") or {}
        thread_id = params.get("threadId") or turn.get("threadId")
        turn_id = params.get("turnId") or turn.get("id")
        if not thread_id or not turn_id:
            return None
        batch = self.preflight.notification_batch_for_native_turn(thread_id, turn_id)
        return self.collect(batch.id) if batch else None

    def collect_for_operator(self, batch_id, payload, *, thread_id, project, actor):
        from fastapi import HTTPException
        from codex_web.identity import AuthenticationAssurance
        from codex_web.services.identity import IdentityService
        from codex_web.services.notification_batch_inspection import NotificationBatchInspection

        batch = self.preflight.get_notification_batch(batch_id)
        NotificationBatchInspection._authorize(batch, thread_id=thread_id, project=project, actor=actor)
        try:
            IdentityService.require_admin(actor)
            IdentityService.require_assurance(actor, AuthenticationAssurance.MFA)
        except RuntimeError as exc:
            raise HTTPException(status_code=403, detail="administrator with MFA required") from exc
        if batch.native_receipt is None or batch.native_receipt.provider_native_turn_id != payload.expected_native_turn_id:
            raise HTTPException(status_code=409, detail="reviewed shared native turn changed or is unavailable")
        try:
            result = self.collect(batch_id)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=409, detail="canonical native result cannot be captured") from exc
        if result is None:
            raise HTTPException(status_code=409, detail="canonical native result is not completed or lacks the result contract")
        return result

    def collect(self, batch_id):
        batch = self.preflight.get_notification_batch(batch_id)
        if batch is None or batch.plan.result_contract_version is None or batch.native_receipt is None:
            return None
        receipt = batch.native_receipt
        project = self.project_for(batch.plan.members[0].project_id)
        scope = (batch.plan.organization_id, batch.plan.workspace_id)
        if project is None or (project.organization_id, project.workspace_id) != scope:
            raise ValueError("notification result Project scope changed")
        history = self.transcript.read(receipt.thread_id)
        if not history.get("turns"):
            return None
        if (history.get("providerId"), history.get("runtimeId")) != (receipt.provider_id, receipt.runtime_id):
            raise ValueError("notification result runtime scope mismatch")
        turns = [turn for turn in history.get("turns", []) if receipt.provider_native_turn_id in {
            turn.get("id"), turn.get("providerTurnId")
        }]
        if not turns:
            return None
        if len(turns) != 1:
            raise ValueError("notification result native turn is ambiguous")
        turn = turns[0]
        if turn.get("status") != "completed" or turn.get("failure"):
            return None
        completed_at = turn.get("completedAt")
        if (type(completed_at) not in {int, float} or not math.isfinite(completed_at)
            or completed_at < batch.claimed_at):
            raise ValueError("notification result terminal provenance is invalid")
        messages = [item for item in turn.get("items", []) if item.get("type") == "agentMessage"
            and item.get("status") == "completed" and item.get("phase") != "commentary"]
        final = [item for item in messages if item.get("phase") in {"final_answer", "final"}]
        if len(final) > 1:
            raise ValueError("notification result final output is ambiguous")
        selected = final[0] if final else messages[-1] if messages else None
        raw = selected.get("text") if selected else None
        if not isinstance(raw, str) or not raw or len(raw.encode("utf-8")) > MAX_RESULT_BYTES:
            raise ValueError("notification result final JSON is missing or oversized")
        json.loads(raw, object_pairs_hook=_unique_keys)
        result = NotificationBatchResult.model_validate_json(raw)
        expected = {json.loads(value)["id"]: original_snapshot_digest(value)
            for value in batch.plan.original_snapshots_json}
        if result.admission_id != batch.id or {
            item.original_id: item.original_snapshot_sha256 for item in result.results
        } != expected:
            raise ValueError("notification result original membership or binding changed")
        # Validate all members before the first Evidence write. Partial captures
        # resume through canonical per-record idempotency, never native replay.
        for attempt_id, original in zip(batch.attempt_ids, batch.plan.original_snapshots_json, strict=True):
            retained = self.preflight.get(attempt_id)
            if (retained is None or retained.queued_turn_snapshot != json.loads(original)
                or retained.notification_batch_id != batch.id
                or retained.queued_admission_claim_id != batch.claim_id
                or retained.status != "reconciliation_required"):
                raise ValueError("notification result retained member changed")
        actor = self.actor_for(batch)
        if (actor.organization_id, actor.workspace_id) != scope:
            raise ValueError("notification result producer scope mismatch")
        originals = {json.loads(value)["id"]: json.loads(value) for value in batch.plan.original_snapshots_json}
        evidence_ids = []
        for item in result.results:
            original = originals[item.original_id]
            binding = NotificationOriginalOutcomeBinding(
                schema_version="1.1", admission_id=batch.id, original_id=item.original_id,
                original_snapshot_sha256=item.original_snapshot_sha256, disposition=item.disposition,
            )
            evidence = self.evidence.create_evidence(EvidenceCreate(
                project_id=project.id, work_item_ref=original.get("work_item_ref"),
                execution_id=receipt.execution_id, evidence_type=EvidenceType.RUNTIME_RESULT,
                provider=receipt.provider_id, external_id=receipt.provider_native_turn_id,
                source=NOTIFICATION_RESULT_SOURCE, result=EvidenceResult.INFO,
                summary="Native notification proposal; independent verification is required.",
                digest=sha256_digest(raw.encode("utf-8")),
                observed_at=max(completed_at, receipt.observed_at), deny_model_context=True,
                metadata={OUTCOME_METADATA_KEY: binding.model_dump_json(),
                    "native_result_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                    "reason_sha256": hashlib.sha256(item.reason.encode("utf-8")).hexdigest(),
                    "execution_actor_id": original.get("agent_profile_actor_id"),
                    "runtime_id": receipt.runtime_id, "native_completed_at": completed_at},
            ), actor=actor, idempotency_key=batch.id + ":" + item.original_id)
            evidence_ids.append(evidence.id)
        return {"admission_id": batch.id, "evidence_ids": evidence_ids, "independently_verified": False}
