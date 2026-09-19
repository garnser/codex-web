from __future__ import annotations

import time
from typing import Iterable

from codex_web.action_intents import (
    ActionDecisionOutcome,
    ActionDecisionSnapshot,
    ActionIntentCreate,
    ActionIntentStatus,
)
from codex_web.action_providers import ActionRequest
from codex_web.approval_requests import (
    ApprovalConsumeRequest,
    ApprovalRequestCreate,
    ApprovalRequestStatus,
    ApprovalRequirement,
    ApprovalTarget,
)
from codex_web.artifact_evidence import (
    ArtifactLifecycle,
    ArtifactType,
    EvidenceCreate,
    EvidenceLifecycle,
    EvidenceResult,
    EvidenceType,
    VerificationResult,
)
from codex_web.identity import AuthenticationActor, AuthenticationAssurance
from codex_web.releases import (
    PromotionCreate,
    PromotionGateEvaluation,
    PromotionQueueRequest,
    PromotionStatus,
    ReleaseArtifactSignature,
    ReleaseArtifactSigner,
    ReleaseBuild,
    ReleaseCreate,
    ReleasePromotion,
    ReleaseRecord,
    ReleaseStatus,
    RollbackQueueRequest,
)
from codex_web.resources import ResourceRisk, ResourceType
from codex_web.services.action_intents import ActionIntentService
from codex_web.services.approval_requests import ApprovalRequestService
from codex_web.services.artifact_evidence import ArtifactEvidenceService
from codex_web.services.resources import ResourceCatalogService
from codex_web.storage.releases import ReleaseNotFoundError, ReleaseStore


class ReleaseError(RuntimeError):
    pass


class ReleaseConflictError(ReleaseError):
    pass


class ReleaseGateError(ReleaseError):
    pass


class ReleaseService:
    def __init__(
        self,
        store: ReleaseStore,
        *,
        artifacts: ArtifactEvidenceService,
        resources: ResourceCatalogService,
        approvals: ApprovalRequestService,
        action_intents: ActionIntentService,
        signer: ReleaseArtifactSigner | None = None,
        clock=time.time,
    ) -> None:
        self.store = store
        self.artifacts = artifacts
        self.resources = resources
        self.approvals = approvals
        self.action_intents = action_intents
        self.signer = signer
        self.clock = clock

    @staticmethod
    def _same_scope(item: ReleaseRecord, actor: AuthenticationActor) -> bool:
        return (
            item.organization_id == actor.organization_id
            and item.workspace_id == actor.workspace_id
        )

    def get(self, release_id: str, *, actor: AuthenticationActor) -> ReleaseRecord:
        try:
            item = self.store.get(release_id)
        except ReleaseNotFoundError as exc:
            raise ReleaseError("release not found") from exc
        if not self._same_scope(item, actor):
            raise ReleaseError("release not found")
        return item

    def list(self, actor: AuthenticationActor) -> list[ReleaseRecord]:
        return self.store.list(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
        )

    def _update_release(
        self,
        release_id: str,
        actor: AuthenticationActor,
        updater,
    ) -> ReleaseRecord:
        result: list[ReleaseRecord] = []

        def apply(state):
            current = state.releases.get(release_id)
            if current is None or not self._same_scope(current, actor):
                raise ReleaseError("release not found")
            updated = updater(current)
            state.releases[release_id] = updated
            result.append(updated)
            return state

        self.store.update(apply)
        return result[0]

    def _artifact_digest(
        self,
        artifact_id: str,
        actor: AuthenticationActor,
    ) -> tuple[str, object]:
        artifact = self.artifacts.get_artifact(artifact_id, actor=actor)
        if artifact.lifecycle != ArtifactLifecycle.ACTIVE:
            raise ReleaseConflictError("release artifact is not active")
        if artifact.artifact_type not in {
            ArtifactType.BUILD,
            ArtifactType.PACKAGE,
        }:
            raise ReleaseConflictError(
                "release artifact must be a build or package artifact"
            )
        if artifact.digest is None:
            raise ReleaseConflictError(
                "release artifact must have an immutable SHA-256 digest"
            )
        return f"sha256:{artifact.digest.value}", artifact

    def _require_evidence(
        self,
        evidence_id: str,
        actor: AuthenticationActor,
        *,
        artifact_id: str | None = None,
        evidence_type: EvidenceType | None = None,
    ):
        evidence = self.artifacts.get_evidence(evidence_id, actor=actor)
        if evidence.lifecycle != EvidenceLifecycle.VALID:
            raise ReleaseGateError(f"evidence is not valid: {evidence_id}")
        if evidence.result != EvidenceResult.PASS:
            raise ReleaseGateError(f"evidence does not pass: {evidence_id}")
        if evidence_type is not None and evidence.evidence_type != evidence_type:
            raise ReleaseGateError(
                f"evidence {evidence_id} is not {evidence_type.value}"
            )
        if artifact_id is not None and artifact_id not in evidence.artifact_ids:
            raise ReleaseGateError(
                f"evidence {evidence_id} is not bound to release artifact"
            )
        return evidence

    def _independently_verified(
        self,
        evidence_id: str,
        artifact_id: str,
        actor: AuthenticationActor,
    ) -> bool:
        return any(
            item.independent
            and item.result == VerificationResult.VERIFIED
            and (
                evidence_id in item.evidence_ids
                or artifact_id in item.artifact_ids
            )
            for item in self.artifacts.list_verifications(actor)
        )

    def create(
        self,
        payload: ReleaseCreate,
        *,
        actor: AuthenticationActor,
    ) -> ReleaseRecord:
        digest, artifact = self._artifact_digest(payload.artifact_id, actor)
        if artifact.revision and artifact.revision != payload.source_revision:
            raise ReleaseConflictError(
                "release source revision differs from immutable build artifact"
            )

        if payload.sbom_artifact_id is not None:
            sbom = self.artifacts.get_artifact(
                payload.sbom_artifact_id,
                actor=actor,
            )
            if sbom.lifecycle != ArtifactLifecycle.ACTIVE:
                raise ReleaseConflictError("SBOM artifact is not active")

        if payload.provenance_evidence_id is not None:
            self._require_evidence(
                payload.provenance_evidence_id,
                actor,
                artifact_id=payload.artifact_id,
            )

        if payload.rollback_release_id is not None:
            rollback = self.get(payload.rollback_release_id, actor=actor)
            if rollback.project_id != payload.project_id:
                raise ReleaseConflictError(
                    "rollback release belongs to a different project"
                )

        if payload.contains_state_migration:
            if payload.migration_evidence_id is None:
                raise ReleaseConflictError(
                    "state-changing release requires migration compatibility evidence"
                )
            self._require_evidence(
                payload.migration_evidence_id,
                actor,
                artifact_id=payload.artifact_id,
                evidence_type=EvidenceType.POLICY_EVALUATION,
            )

        signature: ReleaseArtifactSignature | None = None
        if payload.signing_credential_ref and self.signer is not None:
            signature = self.signer.sign_digest(
                digest,
                credential_ref=payload.signing_credential_ref,
            )
            if signature.signed_digest != digest or not self.signer.verify_digest(
                digest,
                signature,
            ):
                raise ReleaseConflictError(
                    "release artifact signature could not be verified"
                )

        build = ReleaseBuild(
            artifact_id=payload.artifact_id,
            digest=digest,
            source_revision=payload.source_revision,
            build_id=payload.build_id,
            sbom_artifact_id=payload.sbom_artifact_id,
            provenance_evidence_id=payload.provenance_evidence_id,
            signature=signature,
            signing_credential_ref=payload.signing_credential_ref,
        )
        release = ReleaseRecord(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            project_id=payload.project_id,
            name=payload.name,
            version=payload.version,
            build=build,
            policy=payload.policy,
            policy_fingerprint=payload.policy.fingerprint(),
            evidence_ids=tuple(dict.fromkeys(payload.evidence_ids)),
            rollback_release_id=payload.rollback_release_id,
            contains_state_migration=payload.contains_state_migration,
            migration_evidence_id=payload.migration_evidence_id,
            release_notes_artifact_id=payload.release_notes_artifact_id,
            created_by=actor.identity_id,
            created_at=float(self.clock()),
            updated_at=float(self.clock()),
        )
        self.store.create(release)

        if signature is not None:
            signature_evidence = self.artifacts.create_evidence(
                EvidenceCreate(
                    project_id=payload.project_id,
                    evidence_type=EvidenceType.ARTIFACT_VERIFICATION,
                    artifact_ids=(payload.artifact_id,),
                    provider="codex-web",
                    source="release-signature",
                    result=EvidenceResult.PASS,
                    summary="Release artifact signature verified",
                    metadata={
                        "release_id": release.id,
                        "algorithm": signature.algorithm,
                        "key_ref": signature.key_ref,
                        "signed_digest": signature.signed_digest,
                    },
                ),
                actor=actor,
            )
            release = self._update_release(
                release.id,
                actor,
                lambda current: current.model_copy(
                    update={
                        "evidence_ids": tuple(
                            dict.fromkeys(
                                (*current.evidence_ids, signature_evidence.id)
                            )
                        ),
                        "updated_at": float(self.clock()),
                    }
                ),
            )
        return release

    def add_promotion(
        self,
        release_id: str,
        payload: PromotionCreate,
        *,
        actor: AuthenticationActor,
    ) -> ReleaseRecord:
        release = self.get(release_id, actor=actor)
        environment = self.resources.get(
            payload.environment_resource_id,
            actor,
        )
        if environment.resource_type not in {
            ResourceType.ENVIRONMENT,
            ResourceType.DEPLOYMENT_TARGET,
        }:
            raise ReleaseConflictError(
                "promotion target must be an environment/deployment target"
            )
        promotion = ReleasePromotion(
            environment_resource_id=environment.id,
            environment_name=payload.environment_name,
            artifact_id=release.build.artifact_id,
            artifact_digest=release.build.digest,
            rollout=payload.rollout,
            evidence_ids=tuple(dict.fromkeys(payload.evidence_ids)),
            created_at=float(self.clock()),
            updated_at=float(self.clock()),
        )
        return self._update_release(
            release_id,
            actor,
            lambda current: current.model_copy(
                update={
                    "promotions": (*current.promotions, promotion),
                    "updated_at": float(self.clock()),
                }
            ),
        )

    @staticmethod
    def _promotion(release: ReleaseRecord, promotion_id: str) -> ReleasePromotion:
        item = next(
            (value for value in release.promotions if value.id == promotion_id),
            None,
        )
        if item is None:
            raise ReleaseError("release promotion not found")
        return item

    def _is_production(
        self,
        release: ReleaseRecord,
        promotion: ReleasePromotion,
        actor: AuthenticationActor,
    ) -> bool:
        environment = self.resources.get(
            promotion.environment_resource_id,
            actor,
        )
        normalized = {
            promotion.environment_name.casefold(),
            environment.name.casefold(),
        }
        return (
            bool(normalized.intersection(release.policy.production_environment_labels))
            or environment.risk in {ResourceRisk.HIGH, ResourceRisk.CRITICAL}
        )

    def evaluate_promotion(
        self,
        release_id: str,
        promotion_id: str,
        *,
        actor: AuthenticationActor,
        now: float | None = None,
    ) -> PromotionGateEvaluation:
        current_time = float(self.clock()) if now is None else float(now)
        release = self.get(release_id, actor=actor)
        promotion = self._promotion(release, promotion_id)
        blockers: list[str] = []
        matched: list[str] = []

        digest, artifact = self._artifact_digest(
            release.build.artifact_id,
            actor,
        )
        if digest != release.build.digest or promotion.artifact_digest != digest:
            blockers.append("immutable_artifact_digest_mismatch")
        if promotion.artifact_id != release.build.artifact_id:
            blockers.append("promotion_artifact_changed")

        if release.policy.require_signature:
            signature = release.build.signature
            if signature is None:
                blockers.append("artifact_signature_required")
            elif self.signer is None:
                blockers.append("artifact_signature_verifier_unavailable")
            elif not self.signer.verify_digest(release.build.digest, signature):
                blockers.append("artifact_signature_invalid")

        if release.build.sbom_artifact_id is None:
            blockers.append("sbom_required")
        else:
            try:
                sbom = self.artifacts.get_artifact(
                    release.build.sbom_artifact_id,
                    actor=actor,
                )
                if sbom.lifecycle != ArtifactLifecycle.ACTIVE:
                    blockers.append("sbom_not_active")
            except Exception:
                blockers.append("sbom_missing")

        if release.build.provenance_evidence_id is None:
            blockers.append("build_provenance_required")
        else:
            try:
                self._require_evidence(
                    release.build.provenance_evidence_id,
                    actor,
                    artifact_id=release.build.artifact_id,
                )
                matched.append(release.build.provenance_evidence_id)
            except ReleaseGateError:
                blockers.append("build_provenance_invalid")

        evidence_ids = tuple(
            dict.fromkeys((*release.evidence_ids, *promotion.evidence_ids))
        )
        for requirement in release.policy.required_evidence:
            candidates = []
            for evidence_id in evidence_ids:
                try:
                    evidence = self._require_evidence(
                        evidence_id,
                        actor,
                        artifact_id=release.build.artifact_id,
                        evidence_type=requirement.evidence_type,
                    )
                except ReleaseGateError:
                    continue
                if requirement.provider and evidence.provider != requirement.provider:
                    continue
                if (
                    requirement.max_age_seconds is not None
                    and current_time - evidence.observed_at
                    > requirement.max_age_seconds
                ):
                    continue
                if (
                    requirement.independent_verification
                    and not self._independently_verified(
                        evidence.id,
                        release.build.artifact_id,
                        actor,
                    )
                ):
                    continue
                candidates.append(evidence.id)
            if not candidates:
                blockers.append(f"evidence_requirement:{requirement.id}")
            matched.extend(candidates)

        production = self._is_production(release, promotion, actor)
        if (
            production
            and release.policy.require_rollback_target_for_production
            and release.rollback_release_id is None
        ):
            blockers.append("known_good_rollback_release_required")

        if (
            release.contains_state_migration
            and release.policy.require_migration_compatibility
        ):
            if release.migration_evidence_id is None:
                blockers.append("migration_compatibility_evidence_required")
            else:
                try:
                    self._require_evidence(
                        release.migration_evidence_id,
                        actor,
                        artifact_id=release.build.artifact_id,
                        evidence_type=EvidenceType.POLICY_EVALUATION,
                    )
                    matched.append(release.migration_evidence_id)
                except ReleaseGateError:
                    blockers.append("migration_compatibility_evidence_invalid")

        return PromotionGateEvaluation(
            release_id=release.id,
            promotion_id=promotion.id,
            satisfied=not blockers,
            blockers=tuple(dict.fromkeys(blockers)),
            matched_evidence_ids=tuple(dict.fromkeys(matched)),
            policy_fingerprint=release.policy_fingerprint,
            evaluated_at=current_time,
        )

    async def request_promotion_approval(
        self,
        release_id: str,
        promotion_id: str,
        *,
        actor: AuthenticationActor,
    ) -> ReleaseRecord:
        release = self.get(release_id, actor=actor)
        promotion = self._promotion(release, promotion_id)
        evaluation = self.evaluate_promotion(
            release_id,
            promotion_id,
            actor=actor,
        )
        if not evaluation.satisfied:
            return self._replace_promotion(
                release,
                promotion.model_copy(
                    update={
                        "status": PromotionStatus.BLOCKED,
                        "blockers": evaluation.blockers,
                        "updated_at": float(self.clock()),
                    }
                ),
                actor,
                release_status=ReleaseStatus.BLOCKED,
            )

        request = await self.approvals.create(
            ApprovalRequestCreate(
                target=ApprovalTarget(
                    operation="release.promote",
                    object_type="release_promotion",
                    object_id=promotion.id,
                    target_version=release.version,
                    target_digest=release.build.digest,
                    resource_ids=(promotion.environment_resource_id,),
                ),
                project_id=release.project_id,
                reason=(
                    f"Promote immutable release {release.name} {release.version} "
                    f"to {promotion.environment_name}"
                ),
                policy_source=f"release-policy:{release.policy.id}:{release.policy.version}",
                authority_source="canonical:role-authority",
                requirement=ApprovalRequirement(
                    quorum=release.policy.approval_quorum,
                    required_assurance=AuthenticationAssurance.MFA,
                    distinct_humans=release.policy.require_distinct_humans,
                    allow_self_approval=False,
                ),
                evidence_refs=evaluation.matched_evidence_ids,
            ),
            requester=actor,
        )
        return self._replace_promotion(
            release,
            promotion.model_copy(
                update={
                    "approval_request_id": request.id,
                    "status": PromotionStatus.AWAITING_APPROVAL,
                    "blockers": (),
                    "updated_at": float(self.clock()),
                }
            ),
            actor,
            release_status=ReleaseStatus.QUALIFIED,
        )

    def _replace_promotion(
        self,
        release: ReleaseRecord,
        promotion: ReleasePromotion,
        actor: AuthenticationActor,
        *,
        release_status: ReleaseStatus | None = None,
    ) -> ReleaseRecord:
        return self._update_release(
            release.id,
            actor,
            lambda current: current.model_copy(
                update={
                    "promotions": tuple(
                        promotion if item.id == promotion.id else item
                        for item in current.promotions
                    ),
                    "status": release_status or current.status,
                    "updated_at": float(self.clock()),
                }
            ),
        )

    async def queue_promotion(
        self,
        release_id: str,
        promotion_id: str,
        payload: PromotionQueueRequest,
        *,
        actor: AuthenticationActor,
    ) -> ReleaseRecord:
        release = self.get(release_id, actor=actor)
        promotion = self._promotion(release, promotion_id)
        evaluation = self.evaluate_promotion(
            release_id,
            promotion_id,
            actor=actor,
        )
        if not evaluation.satisfied:
            raise ReleaseGateError(
                "; ".join(evaluation.blockers) or "release gate blocked promotion"
            )
        if promotion.approval_request_id is None:
            raise ReleaseGateError("canonical promotion approval is required")
        approval = self.approvals.get(
            promotion.approval_request_id,
            actor=actor,
        )
        if approval.status != ApprovalRequestStatus.APPROVED:
            raise ReleaseGateError("promotion approval is not approved")

        target = ApprovalTarget(
            operation="release.promote",
            object_type="release_promotion",
            object_id=promotion.id,
            target_version=release.version,
            target_digest=release.build.digest,
            resource_ids=(promotion.environment_resource_id,),
        )
        await self.approvals.consume(
            approval.id,
            ApprovalConsumeRequest(
                target=target,
                idempotency_key=f"release-promotion:{promotion.id}",
                resulting_operation_reference=promotion.id,
            ),
            actor=actor,
        )

        rollback = (
            self.get(release.rollback_release_id, actor=actor)
            if release.rollback_release_id
            else None
        )
        parameters = {
            "release_id": release.id,
            "promotion_id": promotion.id,
            "artifact_id": release.build.artifact_id,
            "artifact_digest": release.build.digest,
            "source_revision": release.build.source_revision,
            "sbom_artifact_id": release.build.sbom_artifact_id,
            "provenance_evidence_id": release.build.provenance_evidence_id,
            "policy_fingerprint": release.policy_fingerprint,
            "rollout": promotion.rollout.model_dump(mode="json"),
            "rollback_release_id": release.rollback_release_id,
            "rollback_artifact_id": (
                rollback.build.artifact_id if rollback is not None else None
            ),
            "rollback_artifact_digest": (
                rollback.build.digest if rollback is not None else None
            ),
        }
        intent = self.action_intents.create(
            ActionIntentCreate(
                binding_id=payload.provider_binding_id,
                request=ActionRequest(
                    action_id=payload.deployment_action_id,
                    organization_id=actor.organization_id,
                    workspace_id=actor.workspace_id,
                    project_id=release.project_id,
                    resource_ids=(promotion.environment_resource_id,),
                    parameters=parameters,
                    credential_ref=payload.credential_ref,
                    idempotency_key=f"release:{release.id}:promotion:{promotion.id}",
                ),
                policy_decision=ActionDecisionSnapshot(
                    outcome=ActionDecisionOutcome.ALLOW,
                    source="canonical:release-promotion",
                    reason="release gates and canonical promotion approval satisfied",
                    evaluated_at=float(self.clock()),
                ),
                verification_required=True,
                rollback_required=bool(rollback),
            ),
            actor=actor,
        )
        if intent.status == ActionIntentStatus.CANCELLED:
            raise ReleaseGateError(
                intent.last_error or "canonical action authority denied promotion"
            )
        return self._replace_promotion(
            release,
            promotion.model_copy(
                update={
                    "deployment_action_intent_id": intent.id,
                    "status": PromotionStatus.QUEUED,
                    "updated_at": float(self.clock()),
                }
            ),
            actor,
            release_status=ReleaseStatus.PROMOTING,
        )

    def sync_promotion(
        self,
        release_id: str,
        promotion_id: str,
        *,
        actor: AuthenticationActor,
    ) -> ReleaseRecord:
        release = self.get(release_id, actor=actor)
        promotion = self._promotion(release, promotion_id)
        if promotion.deployment_action_intent_id is None:
            return release
        intent = self.action_intents.get(
            promotion.deployment_action_intent_id,
            actor,
        )
        if intent.status == ActionIntentStatus.SUCCEEDED:
            status = PromotionStatus.DEPLOYED
            release_status = ReleaseStatus.DEPLOYED
            deployed_at = intent.completed_at or float(self.clock())
        elif intent.status in {
            ActionIntentStatus.FAILED,
            ActionIntentStatus.CANCELLED,
            ActionIntentStatus.UNCERTAIN,
            ActionIntentStatus.REQUIRES_RECONCILIATION,
        }:
            status = PromotionStatus.FAILED
            release_status = ReleaseStatus.BLOCKED
            deployed_at = None
        else:
            return release
        return self._replace_promotion(
            release,
            promotion.model_copy(
                update={
                    "status": status,
                    "deployed_at": deployed_at,
                    "updated_at": float(self.clock()),
                }
            ),
            actor,
            release_status=release_status,
        )

    def queue_rollback(
        self,
        release_id: str,
        promotion_id: str,
        payload: RollbackQueueRequest,
        *,
        actor: AuthenticationActor,
    ) -> ReleaseRecord:
        release = self.get(release_id, actor=actor)
        promotion = self._promotion(release, promotion_id)
        if release.rollback_release_id is None:
            raise ReleaseGateError("release has no known-good rollback target")
        rollback = self.get(release.rollback_release_id, actor=actor)
        intent = self.action_intents.create(
            ActionIntentCreate(
                binding_id=payload.provider_binding_id,
                request=ActionRequest(
                    action_id=payload.rollback_action_id,
                    organization_id=actor.organization_id,
                    workspace_id=actor.workspace_id,
                    project_id=release.project_id,
                    resource_ids=(promotion.environment_resource_id,),
                    parameters={
                        "release_id": release.id,
                        "promotion_id": promotion.id,
                        "rollback_release_id": rollback.id,
                        "artifact_id": rollback.build.artifact_id,
                        "artifact_digest": rollback.build.digest,
                        "source_revision": rollback.build.source_revision,
                    },
                    credential_ref=payload.credential_ref,
                    idempotency_key=f"release:{release.id}:rollback:{promotion.id}",
                ),
                policy_decision=ActionDecisionSnapshot(
                    outcome=ActionDecisionOutcome.ALLOW,
                    source="canonical:release-rollback",
                    reason="rollback targets a pre-recorded immutable known-good artifact",
                    evaluated_at=float(self.clock()),
                ),
                verification_required=True,
            ),
            actor=actor,
        )
        if intent.status == ActionIntentStatus.CANCELLED:
            raise ReleaseGateError(
                intent.last_error or "canonical action authority denied rollback"
            )
        return self._replace_promotion(
            release,
            promotion.model_copy(
                update={
                    "rollback_action_intent_id": intent.id,
                    "updated_at": float(self.clock()),
                }
            ),
            actor,
        )
