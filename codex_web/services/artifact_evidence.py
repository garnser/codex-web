from __future__ import annotations

import time
from typing import Any, Iterable

from codex_web.artifact_evidence import (
    Artifact,
    ArtifactCreate,
    ArtifactEvidenceState,
    ArtifactLifecycle,
    Evidence,
    EvidenceCreate,
    EvidenceGateEvaluation,
    EvidenceLifecycle,
    EvidenceRequirement,
    EvidenceRequirementOutcome,
    Verification,
    VerificationCreate,
    VerificationResult,
)
from codex_web.data_governance import (
    DataCategory,
    DataClassification,
    GovernedDataCreate,
    GovernedDataRecord,
    GovernanceAction,
)
from codex_web.identity import AuthenticationActor, MembershipRole, PrincipalKind
from codex_web.services.data_governance import DataGovernanceService
from codex_web.services.identity import AuthorizationError, TenantIsolationError
from codex_web.services.resources import ResourceCatalogService
from codex_web.storage.artifact_evidence import ArtifactEvidenceStore


class ArtifactEvidenceError(RuntimeError):
    pass


class ArtifactNotFoundError(ArtifactEvidenceError):
    pass


class EvidenceNotFoundError(ArtifactEvidenceError):
    pass


class VerificationNotFoundError(ArtifactEvidenceError):
    pass


class ArtifactEvidenceConflictError(ArtifactEvidenceError):
    pass


class ArtifactEvidenceService:
    def __init__(
        self,
        store: ArtifactEvidenceStore,
        *,
        resources: ResourceCatalogService | None = None,
        work_item_host: Any | None = None,
        governance: DataGovernanceService | None = None,
    ) -> None:
        self.store = store
        self.resources = resources
        self.work_item_host = work_item_host
        self.governance = governance

    @staticmethod
    def _admin(actor: AuthenticationActor) -> bool:
        if actor.principal_kind == PrincipalKind.SERVICE:
            return "artifact-evidence:admin" in actor.service_scopes
        return actor.has_role(MembershipRole.OWNER, MembershipRole.ADMIN)

    @staticmethod
    def _same_scope(item: Any, actor: AuthenticationActor) -> bool:
        return (
            item.organization_id == actor.organization_id
            and item.workspace_id == actor.workspace_id
        )

    def _artifact(self, state: ArtifactEvidenceState, artifact_id: str, actor: AuthenticationActor) -> Artifact:
        item = next(
            (
                value
                for value in state.artifacts
                if value.id == artifact_id and self._same_scope(value, actor)
            ),
            None,
        )
        if item is None:
            raise ArtifactNotFoundError("artifact not found")
        return item

    def _evidence(self, state: ArtifactEvidenceState, evidence_id: str, actor: AuthenticationActor) -> Evidence:
        item = next(
            (
                value
                for value in state.evidence
                if value.id == evidence_id and self._same_scope(value, actor)
            ),
            None,
        )
        if item is None:
            raise EvidenceNotFoundError("evidence not found")
        return item

    def _verification(
        self,
        state: ArtifactEvidenceState,
        verification_id: str,
        actor: AuthenticationActor,
    ) -> Verification:
        item = next(
            (
                value
                for value in state.verifications
                if value.id == verification_id and self._same_scope(value, actor)
            ),
            None,
        )
        if item is None:
            raise VerificationNotFoundError("verification not found")
        return item

    def _validate_work_item_ref(
        self,
        work_item_ref: str | None,
        actor: AuthenticationActor,
    ) -> None:
        if work_item_ref is None or self.work_item_host is None:
            return
        states = self.work_item_host._load_work_item_states()
        state = states.get(work_item_ref)
        if state is None or (
            state.organization_id != actor.organization_id
            or state.workspace_id != actor.workspace_id
        ):
            raise ArtifactNotFoundError("work item not found")

    @classmethod
    def _require_record_mutation(
        cls,
        producer_identity_id: str,
        actor: AuthenticationActor,
    ) -> None:
        if producer_identity_id == actor.identity_id or cls._admin(actor):
            return
        raise AuthorizationError("artifact/evidence producer or administrator required")

    def _validate_resources(
        self,
        resource_ids: Iterable[str],
        actor: AuthenticationActor,
    ) -> None:
        if self.resources is None:
            return
        for resource_id in resource_ids:
            self.resources.get(resource_id, actor)

    @staticmethod
    def _invalidate_dependents(
        state: ArtifactEvidenceState,
        *,
        artifact_ids: set[str] | None = None,
        evidence_ids: set[str] | None = None,
        reason: str,
        now: float,
    ) -> None:
        artifact_ids = artifact_ids or set()
        evidence_ids = evidence_ids or set()

        invalidated_evidence: set[str] = set(evidence_ids)
        for index, evidence in enumerate(state.evidence):
            if evidence.lifecycle != EvidenceLifecycle.VALID:
                continue
            if evidence.id in evidence_ids or artifact_ids.intersection(evidence.artifact_ids):
                state.evidence[index] = evidence.model_copy(
                    update={
                        "lifecycle": EvidenceLifecycle.INVALIDATED,
                        "invalidated_at": now,
                        "invalidation_reason": reason,
                    }
                )
                invalidated_evidence.add(evidence.id)

        for index, verification in enumerate(state.verifications):
            if verification.result == VerificationResult.INVALIDATED:
                continue
            if (
                artifact_ids.intersection(verification.artifact_ids)
                or invalidated_evidence.intersection(verification.evidence_ids)
            ):
                state.verifications[index] = verification.model_copy(
                    update={
                        "result": VerificationResult.INVALIDATED,
                        "findings": tuple(
                            dict.fromkeys((*verification.findings, f"invalidated: {reason}"))
                        ),
                    }
                )

    def list_artifacts(
        self,
        actor: AuthenticationActor,
        *,
        work_item_ref: str | None = None,
        include_inactive: bool = True,
    ) -> list[Artifact]:
        items = [item for item in self.store.load().artifacts if self._same_scope(item, actor)]
        if work_item_ref is not None:
            items = [item for item in items if item.work_item_ref == work_item_ref]
        if not include_inactive:
            items = [item for item in items if item.lifecycle == ArtifactLifecycle.ACTIVE]
        return sorted(items, key=lambda item: (item.created_at, item.id), reverse=True)

    def list_evidence(
        self,
        actor: AuthenticationActor,
        *,
        work_item_ref: str | None = None,
        include_inactive: bool = True,
    ) -> list[Evidence]:
        items = [item for item in self.store.load().evidence if self._same_scope(item, actor)]
        if work_item_ref is not None:
            items = [item for item in items if item.work_item_ref == work_item_ref]
        if not include_inactive:
            items = [item for item in items if item.lifecycle == EvidenceLifecycle.VALID]
        return sorted(items, key=lambda item: (item.observed_at, item.id), reverse=True)

    def list_verifications(
        self,
        actor: AuthenticationActor,
        *,
        work_item_ref: str | None = None,
    ) -> list[Verification]:
        items = [item for item in self.store.load().verifications if self._same_scope(item, actor)]
        if work_item_ref is not None:
            items = [item for item in items if item.work_item_ref == work_item_ref]
        return sorted(items, key=lambda item: (item.verified_at, item.id), reverse=True)

    def create_artifact(self, payload: ArtifactCreate, *, actor: AuthenticationActor) -> Artifact:
        self._validate_resources(payload.resource_ids, actor)
        self._validate_work_item_ref(payload.work_item_ref, actor)
        state = self.store.load()
        superseded = None
        if payload.supersedes_artifact_id:
            superseded = self._artifact(state, payload.supersedes_artifact_id, actor)
            self._require_record_mutation(superseded.producer_identity_id, actor)
            if superseded.lifecycle != ArtifactLifecycle.ACTIVE:
                raise ArtifactEvidenceConflictError("only an active artifact can be superseded")
        artifact = Artifact(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            producer_identity_id=actor.identity_id,
            project_id=payload.project_id,
            work_item_ref=payload.work_item_ref,
            execution_id=payload.execution_id,
            execution_workspace_id=payload.execution_workspace_id,
            resource_ids=payload.resource_ids,
            artifact_type=payload.artifact_type,
            name=payload.name,
            provider=payload.provider,
            source=payload.source,
            external_id=payload.external_id,
            external_url=payload.external_url,
            revision=payload.revision,
            digest=payload.digest,
            supersedes_id=payload.supersedes_artifact_id,
            retention_expires_at=payload.retention_expires_at,
            produced_at=payload.produced_at or time.time(),
            metadata=payload.metadata,
        )
        if self.governance is not None:
            source_record_ids = (
                (superseded.governance_record_id,)
                if superseded is not None and superseded.governance_record_id
                else ()
            )
            governance_record = self.governance.register_domain_record(
                GovernedDataCreate(
                    project_id=payload.project_id,
                    object_type="artifact",
                    object_id=artifact.id,
                    category=DataCategory.ARTIFACT,
                    classification=payload.classification,
                    retention_policy_ref=payload.retention_policy_ref,
                    retention_expires_at=payload.retention_expires_at,
                    retention_action=payload.retention_action,
                    residency_tags=payload.residency_tags,
                    source_record_ids=source_record_ids,
                    deny_model_context=payload.deny_model_context,
                    reason="artifact_created",
                ),
                actor=actor,
            )
            artifact = artifact.model_copy(
                update={
                    "governance_record_id": governance_record.id,
                    "retention_expires_at": governance_record.retention_expires_at,
                }
            )
        now = time.time()

        def apply(current: ArtifactEvidenceState) -> ArtifactEvidenceState:
            if superseded is not None:
                for index, item in enumerate(current.artifacts):
                    if item.id == superseded.id:
                        current.artifacts[index] = item.model_copy(
                            update={
                                "lifecycle": ArtifactLifecycle.SUPERSEDED,
                                "superseded_by_id": artifact.id,
                                "invalidated_at": now,
                                "invalidation_reason": f"superseded by {artifact.id}",
                            }
                        )
                        break
                self._invalidate_dependents(
                    current,
                    artifact_ids={superseded.id},
                    reason=f"artifact superseded by {artifact.id}",
                    now=now,
                )
            current.artifacts.append(artifact)
            return current

        self.store.update(apply)
        return artifact

    def create_evidence(self, payload: EvidenceCreate, *, actor: AuthenticationActor) -> Evidence:
        self._validate_work_item_ref(payload.work_item_ref, actor)
        state = self.store.load()
        artifacts = [self._artifact(state, artifact_id, actor) for artifact_id in payload.artifact_ids]
        inactive = [item.id for item in artifacts if item.lifecycle != ArtifactLifecycle.ACTIVE]
        if inactive:
            raise ArtifactEvidenceConflictError(
                "evidence cannot reference inactive artifacts: " + ", ".join(sorted(inactive))
            )
        work_item_ref = payload.work_item_ref
        refs = {item.work_item_ref for item in artifacts if item.work_item_ref}
        if work_item_ref is not None and refs and refs != {work_item_ref}:
            raise ArtifactEvidenceConflictError(
                "evidence work item does not match referenced artifacts"
            )
        if work_item_ref is None and len(refs) == 1:
            work_item_ref = next(iter(refs))
        evidence = Evidence(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            producer_identity_id=actor.identity_id,
            project_id=payload.project_id,
            work_item_ref=work_item_ref,
            execution_id=payload.execution_id,
            execution_workspace_id=payload.execution_workspace_id,
            evidence_type=payload.evidence_type,
            artifact_ids=payload.artifact_ids,
            provider=payload.provider,
            source=payload.source,
            external_id=payload.external_id,
            deep_link=payload.deep_link,
            result=payload.result,
            summary=payload.summary,
            digest=payload.digest,
            observed_at=payload.observed_at or time.time(),
            retention_expires_at=payload.retention_expires_at,
            metadata=payload.metadata,
        )
        if self.governance is not None:
            source_record_ids = tuple(
                item.governance_record_id
                for item in artifacts
                if item.governance_record_id is not None
            )
            governance_record = self.governance.register_domain_record(
                GovernedDataCreate(
                    project_id=payload.project_id,
                    object_type="evidence",
                    object_id=evidence.id,
                    category=DataCategory.EVIDENCE,
                    classification=payload.classification,
                    retention_policy_ref=payload.retention_policy_ref,
                    retention_expires_at=payload.retention_expires_at,
                    retention_action=payload.retention_action,
                    residency_tags=payload.residency_tags,
                    source_record_ids=source_record_ids,
                    deny_model_context=payload.deny_model_context,
                    reason="evidence_created",
                ),
                actor=actor,
            )
            evidence = evidence.model_copy(
                update={
                    "governance_record_id": governance_record.id,
                    "retention_expires_at": governance_record.retention_expires_at,
                }
            )

        def apply(current: ArtifactEvidenceState) -> ArtifactEvidenceState:
            current.evidence.append(evidence)
            return current

        self.store.update(apply)
        return evidence

    def create_verification(
        self,
        payload: VerificationCreate,
        *,
        actor: AuthenticationActor,
    ) -> Verification:
        self._validate_work_item_ref(payload.work_item_ref, actor)
        state = self.store.load()
        artifacts = [self._artifact(state, artifact_id, actor) for artifact_id in payload.artifact_ids]
        evidence = [self._evidence(state, evidence_id, actor) for evidence_id in payload.evidence_ids]
        if any(item.lifecycle != ArtifactLifecycle.ACTIVE for item in artifacts):
            raise ArtifactEvidenceConflictError("verification cannot reference inactive artifacts")
        if any(item.lifecycle != EvidenceLifecycle.VALID for item in evidence):
            raise ArtifactEvidenceConflictError("verification cannot reference invalid evidence")
        producers = {item.producer_identity_id for item in artifacts}
        producers.update(item.producer_identity_id for item in evidence)
        independent = bool(producers) and actor.identity_id not in producers
        work_item_ref = payload.work_item_ref
        refs = {
            item.work_item_ref
            for item in [*artifacts, *evidence]
            if item.work_item_ref
        }
        if work_item_ref is not None and refs and refs != {work_item_ref}:
            raise ArtifactEvidenceConflictError(
                "verification work item does not match referenced records"
            )
        if work_item_ref is None and len(refs) == 1:
            work_item_ref = next(iter(refs))
        verification = Verification(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            verifier_identity_id=actor.identity_id,
            independent=independent,
            work_item_ref=work_item_ref,
            execution_id=payload.execution_id,
            artifact_ids=payload.artifact_ids,
            evidence_ids=payload.evidence_ids,
            method=payload.method,
            result=payload.result,
            findings=payload.findings,
            provider=payload.provider,
            source=payload.source,
            deep_link=payload.deep_link,
        )

        def apply(current: ArtifactEvidenceState) -> ArtifactEvidenceState:
            current.verifications.append(verification)
            return current

        self.store.update(apply)
        return verification

    def sync_governance_records(
        self,
        *,
        actor: AuthenticationActor,
    ) -> dict[str, int]:
        """Backfill governance metadata for legacy artifact/evidence rows."""
        if self.governance is None:
            raise ArtifactEvidenceError("data governance service is not configured")
        if not self._admin(actor):
            raise AuthorizationError("artifact/evidence administrator required")

        state = self.store.load()
        artifact_updates: dict[str, tuple[str, float | None]] = {}
        for artifact in state.artifacts:
            if not self._same_scope(artifact, actor) or artifact.governance_record_id:
                continue
            record = self.governance.register_domain_record(
                GovernedDataCreate(
                    project_id=artifact.project_id,
                    object_type="artifact",
                    object_id=artifact.id,
                    category=DataCategory.ARTIFACT,
                    classification=DataClassification.CONFIDENTIAL,
                    retention_expires_at=artifact.retention_expires_at,
                    reason="legacy_artifact_governance_backfill",
                ),
                actor=actor,
            )
            artifact_updates[artifact.id] = (record.id, record.retention_expires_at)

        if artifact_updates:
            def apply_artifacts(current: ArtifactEvidenceState) -> ArtifactEvidenceState:
                for index, item in enumerate(current.artifacts):
                    update = artifact_updates.get(item.id)
                    if update is not None and item.governance_record_id is None:
                        current.artifacts[index] = item.model_copy(
                            update={
                                "governance_record_id": update[0],
                                "retention_expires_at": update[1],
                            }
                        )
                return current
            state = self.store.update(apply_artifacts)
        else:
            state = self.store.load()

        artifact_governance = {
            item.id: item.governance_record_id
            for item in state.artifacts
            if self._same_scope(item, actor) and item.governance_record_id
        }
        evidence_updates: dict[str, tuple[str, float | None]] = {}
        for evidence in state.evidence:
            if not self._same_scope(evidence, actor) or evidence.governance_record_id:
                continue
            source_record_ids = tuple(
                artifact_governance[artifact_id]
                for artifact_id in evidence.artifact_ids
                if artifact_id in artifact_governance
            )
            record = self.governance.register_domain_record(
                GovernedDataCreate(
                    project_id=evidence.project_id,
                    object_type="evidence",
                    object_id=evidence.id,
                    category=DataCategory.EVIDENCE,
                    classification=DataClassification.CONFIDENTIAL,
                    retention_expires_at=evidence.retention_expires_at,
                    source_record_ids=source_record_ids,
                    reason="legacy_evidence_governance_backfill",
                ),
                actor=actor,
            )
            evidence_updates[evidence.id] = (record.id, record.retention_expires_at)

        if evidence_updates:
            def apply_evidence(current: ArtifactEvidenceState) -> ArtifactEvidenceState:
                for index, item in enumerate(current.evidence):
                    update = evidence_updates.get(item.id)
                    if update is not None and item.governance_record_id is None:
                        current.evidence[index] = item.model_copy(
                            update={
                                "governance_record_id": update[0],
                                "retention_expires_at": update[1],
                            }
                        )
                return current
            self.store.update(apply_evidence)

        return {
            "artifacts": len(artifact_updates),
            "evidence": len(evidence_updates),
        }

    def apply_governance_action(
        self,
        record: GovernedDataRecord,
        action: GovernanceAction,
    ) -> str:
        """Redact codex-web artifact/evidence metadata while preserving tombstone IDs.

        External Git/provider content is outside this handler and is never
        claimed deleted by this receipt.
        """
        now = time.time()
        changed = False

        def apply(state: ArtifactEvidenceState) -> ArtifactEvidenceState:
            nonlocal changed
            if record.object_type == "artifact":
                for index, artifact in enumerate(state.artifacts):
                    if (
                        artifact.id != record.object_id
                        or artifact.organization_id != record.organization_id
                        or artifact.workspace_id != record.workspace_id
                    ):
                        continue
                    identity = (
                        "anonymized"
                        if action == GovernanceAction.ANONYMIZE
                        else "deleted"
                        if action == GovernanceAction.DELETE
                        else artifact.producer_identity_id
                    )
                    state.artifacts[index] = artifact.model_copy(
                        update={
                            "name": (
                                "[deleted]"
                                if action == GovernanceAction.DELETE
                                else "[redacted]"
                            ),
                            "producer_identity_id": identity,
                            "provider": None,
                            "source": None,
                            "external_id": None,
                            "external_url": None,
                            "revision": None,
                            "digest": None if action == GovernanceAction.DELETE else artifact.digest,
                            "metadata": {},
                            "lifecycle": ArtifactLifecycle.INVALIDATED,
                            "invalidated_at": now,
                            "invalidation_reason": f"governance {action.value}",
                        }
                    )
                    self._invalidate_dependents(
                        state,
                        artifact_ids={artifact.id},
                        reason=f"governance {action.value}",
                        now=now,
                    )
                    changed = True
                    break
            elif record.object_type == "evidence":
                for index, evidence in enumerate(state.evidence):
                    if (
                        evidence.id != record.object_id
                        or evidence.organization_id != record.organization_id
                        or evidence.workspace_id != record.workspace_id
                    ):
                        continue
                    identity = (
                        "anonymized"
                        if action == GovernanceAction.ANONYMIZE
                        else "deleted"
                        if action == GovernanceAction.DELETE
                        else evidence.producer_identity_id
                    )
                    state.evidence[index] = evidence.model_copy(
                        update={
                            "producer_identity_id": identity,
                            "provider": None,
                            "source": None,
                            "external_id": None,
                            "deep_link": None,
                            "summary": None,
                            "digest": None if action == GovernanceAction.DELETE else evidence.digest,
                            "metadata": {},
                            "lifecycle": EvidenceLifecycle.INVALIDATED,
                            "invalidated_at": now,
                            "invalidation_reason": f"governance {action.value}",
                        }
                    )
                    self._invalidate_dependents(
                        state,
                        evidence_ids={evidence.id},
                        reason=f"governance {action.value}",
                        now=now,
                    )
                    changed = True
                    break
            else:
                raise ArtifactEvidenceError(
                    f"unsupported governance object type: {record.object_type}"
                )
            return state

        self.store.update(apply)
        suffix = "applied" if changed else "already_absent"
        return f"artifact-evidence:{record.object_type}:{record.object_id}:{action.value}:{suffix}"

    def invalidate_artifact(
        self,
        artifact_id: str,
        reason: str,
        *,
        actor: AuthenticationActor,
    ) -> Artifact:
        state = self.store.load()
        artifact = self._artifact(state, artifact_id, actor)
        self._require_record_mutation(artifact.producer_identity_id, actor)
        if artifact.lifecycle != ArtifactLifecycle.ACTIVE:
            return artifact
        now = time.time()

        def apply(current: ArtifactEvidenceState) -> ArtifactEvidenceState:
            for index, item in enumerate(current.artifacts):
                if item.id == artifact_id:
                    current.artifacts[index] = item.model_copy(
                        update={
                            "lifecycle": ArtifactLifecycle.INVALIDATED,
                            "invalidated_at": now,
                            "invalidation_reason": reason,
                        }
                    )
                    break
            self._invalidate_dependents(
                current,
                artifact_ids={artifact_id},
                reason=reason,
                now=now,
            )
            return current

        updated = self.store.update(apply)
        return next(item for item in updated.artifacts if item.id == artifact_id)

    def invalidate_evidence(
        self,
        evidence_id: str,
        reason: str,
        *,
        actor: AuthenticationActor,
    ) -> Evidence:
        state = self.store.load()
        evidence = self._evidence(state, evidence_id, actor)
        self._require_record_mutation(evidence.producer_identity_id, actor)
        if evidence.lifecycle != EvidenceLifecycle.VALID:
            return evidence
        now = time.time()

        def apply(current: ArtifactEvidenceState) -> ArtifactEvidenceState:
            self._invalidate_dependents(
                current,
                evidence_ids={evidence_id},
                reason=reason,
                now=now,
            )
            return current

        updated = self.store.update(apply)
        return next(item for item in updated.evidence if item.id == evidence_id)

    def expire_retention(self, *, now: float | None = None) -> dict[str, list[str]]:
        current_time = time.time() if now is None else now
        expired_artifacts: list[str] = []
        expired_evidence: list[str] = []

        def apply(state: ArtifactEvidenceState) -> ArtifactEvidenceState:
            for index, artifact in enumerate(state.artifacts):
                if (
                    artifact.lifecycle == ArtifactLifecycle.ACTIVE
                    and artifact.retention_expires_at is not None
                    and artifact.retention_expires_at <= current_time
                ):
                    state.artifacts[index] = artifact.model_copy(
                        update={
                            "lifecycle": ArtifactLifecycle.EXPIRED,
                            "invalidated_at": current_time,
                            "invalidation_reason": "retention expired",
                        }
                    )
                    expired_artifacts.append(artifact.id)
            for index, evidence in enumerate(state.evidence):
                if (
                    evidence.lifecycle == EvidenceLifecycle.VALID
                    and evidence.retention_expires_at is not None
                    and evidence.retention_expires_at <= current_time
                ):
                    state.evidence[index] = evidence.model_copy(
                        update={
                            "lifecycle": EvidenceLifecycle.EXPIRED,
                            "invalidated_at": current_time,
                            "invalidation_reason": "retention expired",
                        }
                    )
                    expired_evidence.append(evidence.id)
            if expired_artifacts or expired_evidence:
                self._invalidate_dependents(
                    state,
                    artifact_ids=set(expired_artifacts),
                    evidence_ids=set(expired_evidence),
                    reason="retention expired",
                    now=current_time,
                )
            return state

        self.store.update(apply)
        return {
            "artifacts": expired_artifacts,
            "evidence": expired_evidence,
        }

    def evaluate(
        self,
        work_item_ref: str,
        requirements: Iterable[EvidenceRequirement],
        *,
        actor: AuthenticationActor,
        now: float | None = None,
    ) -> EvidenceGateEvaluation:
        current_time = time.time() if now is None else now
        state = self.store.load()
        artifacts = {
            item.id: item
            for item in state.artifacts
            if self._same_scope(item, actor) and item.lifecycle == ArtifactLifecycle.ACTIVE
        }
        verifications = [
            item
            for item in state.verifications
            if self._same_scope(item, actor)
            and item.result == VerificationResult.VERIFIED
            and item.work_item_ref in {None, work_item_ref}
        ]
        valid_evidence = [
            item
            for item in state.evidence
            if self._same_scope(item, actor)
            and item.work_item_ref == work_item_ref
            and item.lifecycle == EvidenceLifecycle.VALID
        ]
        outcomes: list[EvidenceRequirementOutcome] = []
        for requirement in requirements:
            matches: list[Evidence] = []
            verification_ids: set[str] = set()
            for item in valid_evidence:
                if item.evidence_type != requirement.evidence_type:
                    continue
                if item.result not in requirement.accepted_results:
                    continue
                if requirement.provider and item.provider != requirement.provider:
                    continue
                if (
                    requirement.max_age_seconds is not None
                    and current_time - item.observed_at > requirement.max_age_seconds
                ):
                    continue
                linked = [artifacts.get(artifact_id) for artifact_id in item.artifact_ids]
                if requirement.artifact_type is not None and not any(
                    artifact is not None and artifact.artifact_type == requirement.artifact_type
                    for artifact in linked
                ):
                    continue
                if requirement.independent_verification:
                    qualifying = [
                        verification
                        for verification in verifications
                        if verification.independent
                        and (
                            item.id in verification.evidence_ids
                            or bool(set(item.artifact_ids).intersection(verification.artifact_ids))
                        )
                    ]
                    if not qualifying:
                        continue
                    verification_ids.update(value.id for value in qualifying)
                matches.append(item)
            satisfied = len(matches) >= requirement.min_count
            outcomes.append(
                EvidenceRequirementOutcome(
                    requirement_id=requirement.id,
                    satisfied=satisfied,
                    matching_evidence_ids=tuple(item.id for item in matches),
                    verification_ids=tuple(sorted(verification_ids)),
                    reason=None if satisfied else (
                        f"requires {requirement.min_count} valid {requirement.evidence_type.value} evidence record(s)"
                    ),
                )
            )
        return EvidenceGateEvaluation(
            work_item_ref=work_item_ref,
            satisfied=all(item.satisfied for item in outcomes),
            evaluated_at=current_time,
            outcomes=tuple(outcomes),
        )

    def work_item_requirements(
        self,
        work_item_ref: str,
        *,
        actor: AuthenticationActor,
    ) -> tuple[EvidenceRequirement, ...]:
        host = self.work_item_host
        if host is None:
            return ()
        states = host._load_work_item_states()
        state = states.get(work_item_ref)
        if state is None or (
            state.organization_id != actor.organization_id
            or state.workspace_id != actor.workspace_id
        ):
            raise ArtifactNotFoundError("work item not found")
        return tuple(state.execution.evidence_requirements)

    def set_work_item_requirements(
        self,
        work_item_ref: str,
        requirements: Iterable[EvidenceRequirement],
        *,
        actor: AuthenticationActor,
    ) -> tuple[EvidenceRequirement, ...]:
        host = self.work_item_host
        if host is None:
            raise ArtifactEvidenceError("work item state is unavailable")
        if not self._admin(actor):
            raise AuthorizationError("evidence requirement administration authority required")
        states = host._load_work_item_states()
        state = states.get(work_item_ref)
        if state is None or (
            state.organization_id != actor.organization_id
            or state.workspace_id != actor.workspace_id
        ):
            raise ArtifactNotFoundError("work item not found")
        normalized = list(requirements)
        ids = [item.id for item in normalized]
        if len(ids) != len(set(ids)):
            raise ArtifactEvidenceConflictError("evidence requirement ids must be unique")
        state.execution.evidence_requirements = normalized
        state.updated_at = time.time()
        states[state.ref] = state
        host._save_work_item_states(states)
        append = getattr(host, "_append_work_item_event", None)
        event_factory = getattr(host, "_work_item_event", None)
        if callable(append) and callable(event_factory):
            append(
                event_factory(
                    state.ref,
                    "evidence_requirements_updated",
                    actor=actor.identity_id,
                    payload={
                        "requirement_ids": ids,
                        "requirement_count": len(ids),
                    },
                )
            )
        return tuple(normalized)
