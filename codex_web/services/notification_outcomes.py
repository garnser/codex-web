"""Read-only evidence projection; does not advance attempts or replay inputs."""

import json

from pydantic import ValidationError

from codex_web.artifact_evidence import (
    ArtifactLifecycle, EvidenceLifecycle, EvidenceResult, EvidenceType,
    VerificationResult,
)
from codex_web.notification_outcomes import (
    NotificationOriginalOutcomeBinding, OUTCOME_METADATA_KEY,
    OUTCOME_REVIEW_METHOD, original_snapshot_digest,
    NOTIFICATION_RESULT_SOURCE,
)


class NotificationOutcomeInspection:
    def __init__(self, artifact_evidence):
        self.artifact_evidence = artifact_evidence

    def snapshot(self):
        # One canonical snapshot per inspection request; never persist a cache.
        return self.artifact_evidence.store.load()

    @staticmethod
    def project(batch, *, state, now):
        receipt = batch.native_receipt
        scope = (batch.plan.organization_id, batch.plan.workspace_id)
        artifacts = {item.id: item for item in state.artifacts}
        bindings = {}
        for evidence in state.evidence:
            if (evidence.organization_id, evidence.workspace_id) != scope:
                continue
            raw = evidence.metadata.get(OUTCOME_METADATA_KEY)
            if not isinstance(raw, str) or len(raw.encode("utf-8")) > 4096:
                continue
            try:
                binding = NotificationOriginalOutcomeBinding.model_validate_json(raw)
            except ValidationError:
                continue
            if binding.admission_id == batch.id:
                bindings.setdefault(binding.original_id, []).append((evidence, binding))

        outcomes = {}
        for raw in batch.plan.original_snapshots_json:
            original = json.loads(raw)
            digest = original_snapshot_digest(raw)
            proof = []
            candidates = set()
            blockers = set()
            review_conflict = False
            for evidence, binding in bindings.get(original["id"], []):
                if binding.original_snapshot_sha256 != digest:
                    blockers.add("original_binding_mismatch")
                    continue
                if (
                    receipt is None
                    or evidence.project_id != original["project_id"]
                    or evidence.work_item_ref != original.get("work_item_ref")
                    or evidence.execution_id != receipt.execution_id
                    or evidence.external_id != receipt.provider_native_turn_id
                    or evidence.provider != receipt.provider_id
                    or evidence.observed_at < receipt.observed_at
                    or evidence.evidence_type != EvidenceType.RUNTIME_RESULT
                    or not (evidence.result == EvidenceResult.INFO and evidence.source == NOTIFICATION_RESULT_SOURCE)
                    and evidence.result != {"reconciled": EvidenceResult.PASS,
                        "follow_up_required": EvidenceResult.WARN, "unresolved": EvidenceResult.INFO}[binding.disposition]
                ):
                    blockers.add("execution_or_scope_binding_mismatch")
                    continue
                if (
                    evidence.lifecycle != EvidenceLifecycle.VALID
                    or (evidence.retention_expires_at is not None
                        and evidence.retention_expires_at <= now)
                    or any(
                        artifact_id not in artifacts
                        or (artifacts[artifact_id].organization_id,
                            artifacts[artifact_id].workspace_id) != scope
                        or artifacts[artifact_id].lifecycle != ArtifactLifecycle.ACTIVE
                        or (artifacts[artifact_id].retention_expires_at is not None
                            and artifacts[artifact_id].retention_expires_at <= now)
                        for artifact_id in evidence.artifact_ids
                    )
                ):
                    blockers.add("evidence_inactive_or_expired")
                    continue
                candidates.add(evidence.id)
                if binding.disposition == "unresolved":
                    blockers.add("native_proposal_unresolved")
                    continue
                reviews = [review for review in state.verifications if (
                    (review.organization_id, review.workspace_id) == scope
                    and review.evidence_ids == (evidence.id,)
                    and not review.artifact_ids
                    and review.work_item_ref == evidence.work_item_ref
                    and review.execution_id == receipt.execution_id
                    and review.method == OUTCOME_REVIEW_METHOD
                    and review.independent
                    and review.verifier_identity_id != evidence.producer_identity_id
                    and review.verifier_identity_id != original.get("agent_profile_actor_id")
                    and review.verified_at >= evidence.created_at
                )]
                if not reviews:
                    blockers.add("independent_review_required")
                    continue
                # Do not select a convenient latest result when reviews disagree.
                if any(review.result != VerificationResult.VERIFIED for review in reviews):
                    blockers.add("review_not_verified_or_conflicting")
                    review_conflict = True
                    continue
                candidates.discard(evidence.id)
                proof.append((binding.disposition, evidence.id, tuple(review.id for review in reviews)))

            dispositions = {item[0] for item in proof}
            if len(dispositions) > 1:
                blockers.add("conflicting_dispositions")
            # Unreviewed/mismatched/inactive metadata cannot revoke an otherwise
            # verified result. Only current exact independent reviews conflict.
            outcome = next(iter(dispositions)) if len(dispositions) == 1 and not review_conflict else "unresolved"
            outcomes[original["id"]] = {
                "outcome": outcome,
                "original_snapshot_sha256": digest,
                "evidence_ids": sorted({item[1] for item in proof})[:16],
                "candidate_evidence_ids": sorted(candidates)[:16],
                "verification_ids": sorted({review_id for item in proof for review_id in item[2]})[:16],
                "warnings": sorted(blockers) if outcome != "unresolved" else [],
                "blockers": [] if outcome != "unresolved" else sorted(blockers) if blockers else ([] if proof else [
                    "per_original_evidence_required" if receipt else "shared_native_receipt_required"
                ]),
            }
        return outcomes
