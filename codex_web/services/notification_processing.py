"""Explicit reviewed processing over preflight, Evidence and canonical Work Items.

No native input, external action, owner dispatch or delivery occurs here. Linking
existing actionable work avoids a second scheduler and does not acknowledge it.
"""

import json
import time

from fastapi import HTTPException

from codex_web.authority import AuthorityEvaluationRequest, AuthorityLevel
from codex_web.identity import AuthenticationAssurance
from codex_web.models import ExecutionPreflightAttempt, WorkItemState
from codex_web.notification_batches import NotificationBatchAdmission, _json, _eligible
from codex_web.notification_processing import NotificationProcessingReceipt, processing_digest
from codex_web.services.identity import IdentityService
from codex_web.services.notification_batch_inspection import NotificationBatchInspection
from codex_web.services.turn_queue_policy import TurnQueuePolicy


class NotificationProcessingService:
    def __init__(self, *, preflight, outcomes, controls, work_items, clock=time.time):
        self.preflight, self.outcomes, self.controls = preflight, outcomes, controls
        self.work_items, self.clock = work_items, clock
        if preflight.store is not outcomes.artifact_evidence.store.store or preflight.store is not work_items.store:
            raise ValueError("notification processing requires the shared canonical transaction store")

    def _authorize(self, batch, thread_id, project, actor):
        NotificationBatchInspection._authorize(batch, thread_id=thread_id, project=project, actor=actor)
        IdentityService.require_admin(actor)
        IdentityService.require_assurance(actor, AuthenticationAssurance.MFA)
        fresh = self.controls.actor_resolver(actor.identity_id, project)
        IdentityService.require_admin(fresh)
        if (fresh.identity_id, fresh.organization_id, fresh.workspace_id) != (
            actor.identity_id, actor.organization_id, actor.workspace_id):
            raise PermissionError("processing actor scope changed")
        targets = tuple(sorted({resource for member in batch.plan.members for resource in (
            *member.writable_repository_resource_ids, *member.read_only_repository_resource_ids,
            *((member.repository_resource_id,) if member.repository_resource_id else ()),
        )}))
        decision = self.controls.authority.evaluate(AuthorityEvaluationRequest(
            capability="api.threads.write", level=AuthorityLevel.EXECUTE,
            project_id=project.id, resource_ids=targets), actor=fresh)
        if decision.outcome != "allow":
            raise PermissionError("current Role authority denies notification processing")
        return decision

    def _view(self, batch, member, attempt, evidence, work):
        original = next(json.loads(raw) for raw in batch.plan.original_snapshots_json
            if json.loads(raw)["id"] == member.id)
        outcome = self.outcomes.project(batch, state=evidence, now=self.clock())[member.id]
        blockers = list(outcome["blockers"])
        if (batch.phase != "native_input_started" or batch.native_receipt is None
            or attempt is None or attempt.notification_batch_id != batch.id
            or attempt.queued_admission_claim_id != batch.claim_id
            or attempt.status != "reconciliation_required"
            or attempt.queued_turn_snapshot != original):
            blockers.append("retained_original_or_native_claim_changed")
        if attempt and _eligible(member, attempt.model_copy(update={
            "status":"queued", "notification_batch_id":None}),
            organization_id=batch.plan.organization_id, workspace_id=batch.plan.workspace_id,
            notification_source_origin=TurnQueuePolicy.source_origin(member.source)) is None:
            blockers.append("retained_attempt_scope_changed")
        if work is None or work.ref != member.work_item_ref or (work.organization_id, work.workspace_id, work.project_id) != (
            batch.plan.organization_id, batch.plan.workspace_id, member.project_id):
            blockers.append("canonical_work_item_unavailable")
        controls = self.controls.resolve_processing(member, self.controls.projects.get(member.project_id))
        if controls is None:
            blockers.append("current_profile_definition_or_authority_unavailable")
        prior = batch.plan.control_snapshot
        # Legacy 1.0 admissions never retained execution/authority Definition
        # references. Neither current catalogs nor reviewer assertions attest
        # which revisions authorized the original. Preserve reads, refuse writes.
        # No existing independently retained canonical attestation supplies
        # those missing admission pins through this workflow.
        if prior is None:
            blockers.append("original_control_definition_evidence_unavailable")
        if controls and prior and any(getattr(controls, key) != getattr(prior, key) for key in (
            "actor_id", "agent_profile_id", "agent_profile_revision", "agent_profile_record_id",
            "agent_profile_sha256", "execution_profile_id", "execution_definition_ref")):
            blockers.append("admitted_profile_or_execution_definition_changed")
        follow_up = None
        if outcome["outcome"] == "follow_up_required" and work is not None:
            # Link only this original's existing canonical work. Current owner,
            # action and handoff are authoritative; linking never supplies ACK.
            owner = work.next_owner or work.current_owner
            if work.current_stage == "closed" or not owner or not work.next_action:
                blockers.append("actionable_follow_up_owner_and_action_required")
            elif work.handoff and work.handoff.status in {"rejected", "superseded"}:
                blockers.append("follow_up_handoff_requires_reconciliation")
            else:
                follow_up = work.model_dump(mode="json", include={
                    "ref", "organization_id", "workspace_id", "project_id", "resource_ids",
                    "current_stage", "current_owner", "next_owner", "next_action", "handoff"})
        ids = set(outcome["evidence_ids"])
        reviews = set(outcome["verification_ids"])
        resources = {*member.writable_repository_resource_ids, *member.read_only_repository_resource_ids,
            *((member.repository_resource_id,) if member.repository_resource_id else ())}
        proof = [item.model_dump(mode="json") for item in evidence.evidence if item.id in ids]
        artifacts = {key for item in evidence.evidence if item.id in ids for key in item.artifact_ids}
        if any(not set(item.resource_ids).issubset(resources) for item in evidence.evidence if item.id in ids) or any(
            item.project_id != member.project_id or item.work_item_ref not in {None,member.work_item_ref}
            or not set(item.resource_ids).issubset(resources)
            for item in evidence.artifacts if item.id in artifacts):
            blockers.append("proof_repository_or_artifact_scope_mismatch")
        proof += [item.model_dump(mode="json") for item in evidence.artifacts if item.id in artifacts]
        proof += [item.model_dump(mode="json") for item in evidence.verifications if item.id in reviews]
        token = processing_digest({
            "admission_id": batch.id, "plan": batch.plan.model_dump(mode="json"),
            "claim": batch.claim_id, "receipt": batch.native_receipt.model_dump(mode="json") if batch.native_receipt else None,
            "attempt": attempt.model_dump(mode="json") if attempt else None,
            "outcome": outcome, "proof": proof, "follow_up": follow_up,
            "controls": controls.model_dump(mode="json") if controls else None,
        })
        recorded = next((row for row in batch.processing if row.original_id == member.id), None)
        return {
            "original_id": member.id, "outcome": outcome["outcome"],
            "expected_processing_sha256": token,
            "can_process": not blockers and recorded is None,
            "blockers": sorted(set(blockers)),
            "processing_state": "processed_current" if recorded and not blockers and
                recorded.reviewed_processing_sha256 == token else "historical_requires_reconciliation"
                if recorded else "review_required" if blockers else "ready",
            "historical_receipt": recorded.model_dump(mode="json") if recorded else None,
            "follow_up": follow_up,
            "reply": {
                "state": recorded.reply_state if recorded else "preview",
                "destination": original.get("reply_target"),
                "admitted_destination": member.reply_target.model_dump(mode="json") if member.reply_target else None,
                "delivery_state": "not_sent", "provider_receipt_id": None,
                "text": f"Notification {member.id}: {outcome['outcome']}. " + (
                    f"Follow-up remains with {follow_up['next_owner'] or follow_up['current_owner']} on {member.work_item_ref}. "
                    if follow_up else "") + "No business acceptance, ACK or external delivery is established.",
            },
            "evidence_ids": outcome["evidence_ids"], "verification_ids": outcome["verification_ids"],
            "_controls": controls,
        }

    @staticmethod
    def public(view):
        return {key: value for key, value in view.items() if not key.startswith("_")}

    def preview(self, batch_id, *, thread_id, project, actor):
        try:
            batch = self.preflight.get_notification_batch(batch_id)
            self._authorize(batch, thread_id, project, actor)
            evidence = self.outcomes.snapshot()
            rows = [self.public(self._view(batch, member, self.preflight.get(attempt_id),
                evidence, self.work_items.get(member.work_item_ref)))
                for member, attempt_id in zip(batch.plan.members, batch.attempt_ids, strict=True)]
            return {"schema_version": "1.0", "batch_id": batch.id, "originals": rows}
        except (PermissionError, RuntimeError) as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    def process(self, batch_id, request, *, thread_id, project, actor):
        batch = self.preflight.get_notification_batch(batch_id)
        try:
            self._authorize(batch, thread_id, project, actor)
            member = next((row for row in batch.plan.members if row.id == request.original_id), None)
            if member is None:
                raise ValueError("original is outside this admission")
            # Materialize legacy keyed work state before entering the transaction.
            self.work_items.get(member.work_item_ref)
            preflight_ns = self.preflight.NAMESPACE
            evidence_store = self.outcomes.artifact_evidence.store
            batch_key = "b/" + batch.id
            attempt_key = "a/preflight-queued-" + member.id
            result = None

            def commit(values):
                nonlocal result
                current = NotificationBatchAdmission.model_validate(values[preflight_ns][batch_key])
                decision = self._authorize(current, thread_id, project, actor)
                raw = values[preflight_ns][attempt_key]
                attempt = ExecutionPreflightAttempt.model_validate(raw) if raw else None
                raw_work = values[self.work_items.namespace][member.work_item_ref]
                work = WorkItemState.model_validate(raw_work) if raw_work else None
                evidence = evidence_store._decode(values[evidence_store.namespace][None])
                view = self._view(current, member, attempt, evidence, work)
                recorded = view["historical_receipt"]
                if recorded:
                    if recorded["reviewed_processing_sha256"] != request.expected_processing_sha256:
                        raise ValueError("original already processed; reconcile its historical receipt")
                    result = self.public(view)
                    return {}  # An uncertain HTTP response never causes another effect.
                if view["blockers"] or request.expected_processing_sha256 != view["expected_processing_sha256"]:
                    raise ValueError("reviewed proof, original, follow-up or current controls changed; reload")
                receipt = NotificationProcessingReceipt(
                    original_id=member.id, original_snapshot_sha256=self.outcomes.project(
                        current, state=evidence, now=self.clock())[member.id]["original_snapshot_sha256"],
                    reviewed_processing_sha256=request.expected_processing_sha256,
                    disposition=view["outcome"], evidence_ids=tuple(view["evidence_ids"]),
                    verification_ids=tuple(view["verification_ids"]), processed_by=actor.identity_id,
                    processed_at=self.clock(), control_snapshot_json=_json(view["_controls"].model_dump(mode="json")),
                    authority_decision_json=decision.model_dump_json(),
                    follow_up_snapshot_json=_json(view["follow_up"]) if view["follow_up"] else None,
                    reply_state="prepared" if member.reply_target else "no_destination")
                updated = NotificationBatchAdmission.model_validate({**current.model_dump(mode="json"),
                    "schema_version": "1.2", "processing": [*current.processing, receipt]})
                result = self.public(self._view(updated, member, attempt, evidence, work))
                return {preflight_ns: {batch_key: updated.model_dump(mode="json")}}

            self.preflight.store.record_mutate_many({
                preflight_ns: (batch_key, attempt_key), evidence_store.namespace: (None,),
                self.work_items.namespace: (member.work_item_ref,),
            }, commit)
            return {"schema_version": "1.0", "batch_id": batch_id, "original": result}
        except (ValueError, KeyError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (PermissionError, RuntimeError) as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
