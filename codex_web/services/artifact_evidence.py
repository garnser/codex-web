from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Callable, Iterable, Iterator

from codex_web.artifact_content import (
    ArtifactContentError,
    ArtifactContentIntegrityError,
    ArtifactContentPointer,
    ArtifactContentRange,
    ArtifactContentScope,
    ArtifactContentState,
)
from codex_web.artifact_evidence import (
    EvidenceType,
    Artifact,
    ArtifactCreate,
    ArtifactDigest,
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
from codex_web.notification_outcomes import NOTIFICATION_RESULT_SOURCE, OUTCOME_METADATA_KEY
from codex_web.services.artifact_content import ArtifactContentService
from codex_web.services.data_governance import DataGovernanceService
from codex_web.services.identity import AuthorizationError, TenantIsolationError
from codex_web.services.resources import ResourceCatalogService
from codex_web.storage.artifact_evidence import ArtifactEvidenceStore
from codex_web.services.research_evidence import (
    OBSERVED_RESEARCH_METHOD, ResearchSourceError, research_sources,
    research_origin, research_source_snapshots, observed_verifications,
)
from codex_web.services.research_provenance import research_governance_plan


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


class _ArtifactDecodeBeforeCreation(Exception):
    """Internal pre-write domain-decode refusal; never an updater/DB failure."""

    def __init__(self, original):
        self.original = original
        super().__init__("artifact catalog validation refused before creation")


class _GovernedCreation:
    """Prepared domain/governance mutation for one owning StateStore transaction."""

    def __init__(self, service, updater, catalog_guard=None):
        self.service, self.updater = service, updater
        self.catalog_guard, self.reused = catalog_guard, None
        self.domains = {service.store.namespace: (None,)}
        if service.governance is not None:
            if service.governance.store.store is not service.store.store:
                raise ArtifactEvidenceConflictError("creation requires one shared canonical governance store")
            self.domains[service.governance.store.namespace] = (None,)

    def apply(self, rows):
        service = self.service
        try:
            state = service.store._decode(rows[service.store.namespace][None])
        except (ValueError, TypeError) as error:
            if self.catalog_guard is None:
                raise
            raise _ArtifactDecodeBeforeCreation(error) from error
        # A trusted internal guard may reuse an existing exact scoped Artifact.
        # Run before Governance decoding/registration or any export/upsert.
        if self.catalog_guard is not None:
            self.reused = self.catalog_guard(state)
            if self.reused is not None:
                if not isinstance(self.reused, Artifact):
                    raise TypeError("catalog guard must return an Artifact or None")
                return {}
        governance = (service.governance.store._decode(rows[service.governance.store.namespace][None])
                      if service.governance is not None else None)
        updated = self.updater(state, governance)
        changes = {service.store.namespace: {None: updated.model_dump(mode="json")}}
        if governance is not None:
            changes[service.governance.store.namespace] = {None: governance.model_dump(mode="json")}
        return changes

    def __call__(self, current):
        # Old composed writers must fail before metadata registration rather than
        # accidentally committing governance outside their guarded transaction.
        if self.service.governance is not None:
            raise ArtifactEvidenceConflictError("governed creation requires its complete transaction domains")
        return self.updater(current, None)


class ArtifactEvidenceService:
    def __init__(
        self,
        store: ArtifactEvidenceStore,
        *,
        resources: ResourceCatalogService | None = None,
        work_item_host: Any | None = None,
        governance: DataGovernanceService | None = None,
        content: ArtifactContentService | None = None,
        content_backend_resolver: Callable[[AuthenticationActor, str | None], str] | None = None,
        organizational_memory: Any | None = None,
        mr_observation_configuration=None,
    ) -> None:
        self.store = store
        self.resources = resources
        self.work_item_host = work_item_host
        self.governance = governance
        self.content = content
        self.content_backend_resolver = content_backend_resolver
        self.organizational_memory = organizational_memory
        self.mr_observation_configuration = mr_observation_configuration

    def _commit_creation(self, updater, composed_commit, *, catalog_guard=None):
        if catalog_guard is not None and composed_commit is not None:
            raise ArtifactEvidenceConflictError("catalog guard requires ordinary creation")
        creation = _GovernedCreation(self, updater, catalog_guard)
        if composed_commit is not None:
            composed_commit(creation)
        elif self.governance is None and catalog_guard is None:
            self.store.update(creation)
        else:
            self.store.store.record_mutate_many(creation.domains, creation.apply)
        return creation

    def _research_sources(self, ids, *, actor, project_id):
        try:
            return research_sources(self.organizational_memory, ids, actor=actor, project_id=project_id)
        except ResearchSourceError as error:
            raise ArtifactEvidenceConflictError(str(error)) from error

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

    def get_artifact(
        self,
        artifact_id: str,
        *,
        actor: AuthenticationActor,
        state: ArtifactEvidenceState | None = None,
    ) -> Artifact:
        current = state if state is not None else self.store.artifact_state(artifact_id,
            organization_id=actor.organization_id, workspace_id=actor.workspace_id)
        return self._artifact(current, artifact_id, actor)

    def get_evidence(
        self,
        evidence_id: str,
        *,
        actor: AuthenticationActor,
        state: ArtifactEvidenceState | None = None,
    ) -> Evidence:
        current = state if state is not None else self.store.evidence_state(evidence_id,
            organization_id=actor.organization_id, workspace_id=actor.workspace_id)
        return self._evidence(current, evidence_id, actor)

    def get_evidence_batch(
        self,
        evidence_ids: tuple[str, ...],
        *,
        actor: AuthenticationActor,
    ) -> tuple[Evidence, ...]:
        """Fresh scoped point semantics, in request order including repeats."""
        if not evidence_ids:
            return ()
        # A custom getter may enforce additional checks. Keep its actual owner
        # instead of routing around it through the canonical optimization.
        if (getattr(self.get_evidence, "__self__", None) is not self
                or getattr(self.get_evidence, "__func__", None) is not _CANONICAL_EVIDENCE_GETTER):
            return tuple(self.get_evidence(item_id, actor=actor) for item_id in evidence_ids)
        states = self.store.evidence_states(evidence_ids,
            organization_id=actor.organization_id, workspace_id=actor.workspace_id)
        return tuple(self.get_evidence(item_id, actor=actor, state=states[item_id])
                     for item_id in evidence_ids)

    def get_verification(
        self,
        verification_id: str,
        *,
        actor: AuthenticationActor,
        work_item_ref: str | None = None,
    ) -> Verification:
        """Read a known canonical ID, refusing absent or ambiguous scoped rows."""
        if not isinstance(verification_id, str) or not verification_id:
            raise VerificationNotFoundError("verification not found")
        state = self.store.verification_state(verification_id,
            organization_id=actor.organization_id, workspace_id=actor.workspace_id)
        rows = [item for item in state.verifications
                if item.id == verification_id and self._same_scope(item, actor)
                and (work_item_ref is None or item.work_item_ref == work_item_ref)]
        if len(rows) != 1:
            raise VerificationNotFoundError("verification unavailable or ambiguous")
        return rows[0]

    def can_batch_mr_input_reads(self) -> bool:
        """Only canonical owners may combine these internal read boundaries."""
        backend = getattr(self.store, 'store', None)
        governance_check = getattr(
            self.governance, "can_filter_derived_context_for_actors", None
        )
        return (
            type(self) is ArtifactEvidenceService
            and all(
                getattr(getattr(self, name), "__self__", None) is self
                and getattr(getattr(self, name), "__func__", None) is method
                for name, method in _CANONICAL_MR_INPUT_READS.items()
            )
            and type(self.store) is ArtifactEvidenceStore
            and all(
                getattr(getattr(self.store, name), "__self__", None) is self.store
                and getattr(getattr(self.store, name), "__func__", None) is method
                for name, method in _CANONICAL_MR_STORE_READS.items()
            )
            and getattr(backend, "supports_artifact_input_envelope", False) is True
            and callable(getattr(backend, "artifact_input_envelope", None))
            and callable(governance_check)
            and governance_check()
        )

    def get_mr_input_records(
        self,
        artifact_id: str,
        verification_id: str,
        evidence_ids: tuple[str, ...],
        *,
        actor: AuthenticationActor,
        work_item_ref: str | None = None,
    ) -> tuple[Artifact, Verification, tuple[Evidence, ...]]:
        """Fresh post-content records; supplied IDs are selection hints only."""
        if not self.can_batch_mr_input_reads():
            artifact = self.get_artifact(artifact_id, actor=actor)
            record = self.get_verification(
                verification_id, actor=actor, work_item_ref=work_item_ref
            )
            return (
                artifact,
                record,
                self.get_evidence_batch(record.evidence_ids, actor=actor),
            )
        state = self.store.input_records_state(
            artifact_id,
            verification_id,
            evidence_ids,
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
        )
        artifact = self.get_artifact(artifact_id, actor=actor, state=state)
        rows = [
            row
            for row in state.verifications
            if row.id == verification_id
            and self._same_scope(row, actor)
            and (work_item_ref is None or row.work_item_ref == work_item_ref)
        ]
        if len(rows) != 1:
            raise VerificationNotFoundError("verification unavailable or ambiguous")
        record = rows[0]
        if sorted(record.evidence_ids) != sorted(evidence_ids):
            raise ValueError("mr_input_evidence_identity_or_expired")
        proofs = tuple(
            self.get_evidence(item_id, actor=actor, state=state)
            for item_id in record.evidence_ids
        )
        return artifact, record, proofs

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
        state: ArtifactEvidenceState | None = None,
    ) -> list[Evidence]:
        state = state if state is not None else self.store.load()
        items = [item for item in state.evidence if self._same_scope(item, actor)]
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

    @staticmethod
    def _content_scope_for(item: Artifact | AuthenticationActor) -> ArtifactContentScope:
        return ArtifactContentScope(
            organization_id=item.organization_id,
            workspace_id=item.workspace_id,
        )

    def _content_backend_id(
        self,
        actor: AuthenticationActor,
        project_id: str | None,
    ) -> str:
        if self.content_backend_resolver is not None:
            return self.content_backend_resolver(actor, project_id)
        if self.content is None:
            raise ArtifactEvidenceError("artifact content service is not configured")
        return self.content.default_backend_id

    def _require_content_service(self) -> ArtifactContentService:
        if self.content is None:
            raise ArtifactEvidenceError("artifact content service is not configured")
        return self.content

    def _verify_content_pointer(self, artifact: Artifact) -> None:
        pointer = artifact.content
        if pointer is None:
            return
        if pointer.state != ArtifactContentState.AVAILABLE:
            raise ArtifactContentIntegrityError("artifact content is not available")
        if artifact.digest is None:
            raise ArtifactContentIntegrityError("content-backed artifact is missing canonical digest")
        service = self._require_content_service()
        store = service.backend(pointer.backend_id)
        head = store.verify(
            self._content_scope_for(artifact),
            pointer.locator,
            artifact.digest.value,
        )
        if head.size_bytes != pointer.size_bytes:
            raise ArtifactContentIntegrityError("artifact content size metadata does not match stored content")

    def _tombstone_content_pointer(
        self,
        artifact: Artifact,
    ) -> ArtifactContentPointer | None:
        pointer = artifact.content
        if pointer is None or pointer.state == ArtifactContentState.TOMBSTONED:
            return pointer
        self._require_content_service().backend(pointer.backend_id).delete(
            self._content_scope_for(artifact),
            pointer.locator,
        )
        return pointer.model_copy(
            update={"state": ArtifactContentState.TOMBSTONED, "verified_at": time.time()}
        )

    def attach_artifact_content(
        self,
        artifact_id: str,
        chunks: Iterable[bytes],
        *,
        actor: AuthenticationActor,
        media_type: str | None = None,
        backend_id: str | None = None,
        expected_sha256: str | None = None,
        encryption_key_ref: str | None = None,
        _state: ArtifactEvidenceState | None = None,
        _commit=None,
    ) -> Artifact:
        if _state is not None and _commit is None:
            raise ArtifactEvidenceConflictError("atomic content state requires its canonical commit owner")
        state = _state if _state is not None else self.store.load()
        artifact = self._artifact(state, artifact_id, actor)
        self._require_record_mutation(artifact.producer_identity_id, actor)
        if artifact.lifecycle != ArtifactLifecycle.ACTIVE:
            raise ArtifactEvidenceConflictError("content can only be attached to an active artifact")
        if artifact.content is not None:
            raise ArtifactEvidenceConflictError(
                "artifact content is immutable; supersede or migrate the artifact content instead"
            )
        service = self._require_content_service()
        selected_backend = backend_id or self._content_backend_id(actor, artifact.project_id)
        canonical_sha256 = artifact.digest.value if artifact.digest is not None else None
        if (
            expected_sha256 is not None
            and canonical_sha256 is not None
            and expected_sha256 != canonical_sha256
        ):
            raise ArtifactContentIntegrityError(
                "requested content digest does not match canonical artifact digest"
            )
        result = service.put(
            self._content_scope_for(artifact),
            chunks,
            backend_id=selected_backend,
            media_type=media_type,
            expected_sha256=canonical_sha256 or expected_sha256,
        )
        pointer = ArtifactContentPointer(
            backend_id=result.backend_id,
            locator=result.locator,
            size_bytes=result.size_bytes,
            media_type=result.media_type,
            encryption_key_ref=encryption_key_ref,
            verified_at=time.time(),
        )
        digest = ArtifactDigest(value=result.sha256)

        def apply(current: ArtifactEvidenceState) -> ArtifactEvidenceState:
            for index, item in enumerate(current.artifacts):
                if item.id == artifact.id:
                    current.artifacts[index] = item.model_copy(
                        update={"digest": digest, "content": pointer}
                    )
                    break
            return current

        updated = self.store.update(apply) if _commit is None else _commit(apply)
        return next(item for item in updated.artifacts if item.id == artifact.id)

    def open_artifact_content(
        self,
        artifact_id: str,
        *,
        actor: AuthenticationActor,
        byte_range: ArtifactContentRange | None = None,
        chunk_size: int = 65536,
        verify: bool = True,
    ) -> tuple[ArtifactContentPointer, Iterator[bytes]]:
        artifact = self.get_artifact(artifact_id, actor=actor)
        return self._open_artifact_content_record(
            artifact, byte_range=byte_range, chunk_size=chunk_size, verify=verify
        )

    def open_artifact_content_with_metadata(
        self,
        artifact_id,
        *,
        actor,
        byte_range=None,
        chunk_size=65536,
        verify=True,
        _admit=None,
    ):
        """Content-open's current header is a hint, not post-I/O authority."""
        artifact = self.get_artifact(artifact_id, actor=actor)
        if _admit is not None:
            _admit(artifact)
        pointer, stream = self._open_artifact_content_record(
            artifact, byte_range=byte_range, chunk_size=chunk_size, verify=verify
        )
        return artifact, pointer, stream

    def _open_artifact_content_record(self, artifact, *, byte_range, chunk_size, verify):
        pointer = artifact.content
        if pointer is None:
            raise ArtifactEvidenceError("artifact has no managed content")
        if pointer.state != ArtifactContentState.AVAILABLE:
            raise ArtifactContentError("artifact content is tombstoned")
        if verify:
            self._verify_content_pointer(artifact)
        stream = self._require_content_service().open(
            self._content_scope_for(artifact),
            backend_id=pointer.backend_id,
            locator=pointer.locator,
            byte_range=byte_range,
            chunk_size=chunk_size,
        )
        return pointer, stream

    def verify_artifact_content(
        self,
        artifact_id: str,
        *,
        actor: AuthenticationActor,
    ) -> Artifact:
        artifact = self._artifact(self.store.load(), artifact_id, actor)
        self._verify_content_pointer(artifact)
        if artifact.content is None:
            return artifact
        pointer = artifact.content.model_copy(update={"verified_at": time.time()})

        def apply(current: ArtifactEvidenceState) -> ArtifactEvidenceState:
            for index, item in enumerate(current.artifacts):
                if item.id == artifact.id:
                    current.artifacts[index] = item.model_copy(update={"content": pointer})
                    break
            return current

        updated = self.store.update(apply)
        return next(item for item in updated.artifacts if item.id == artifact.id)

    def migrate_artifact_content(
        self,
        artifact_id: str,
        target_backend_id: str,
        *,
        actor: AuthenticationActor,
        delete_source: bool = True,
    ) -> Artifact:
        state = self.store.load()
        artifact = self._artifact(state, artifact_id, actor)
        self._require_record_mutation(artifact.producer_identity_id, actor)
        pointer = artifact.content
        if pointer is None or artifact.digest is None:
            raise ArtifactEvidenceError("artifact has no managed content to migrate")
        if pointer.state != ArtifactContentState.AVAILABLE:
            raise ArtifactEvidenceConflictError("tombstoned artifact content cannot be migrated")
        if pointer.backend_id == target_backend_id:
            self._verify_content_pointer(artifact)
            return artifact
        service = self._require_content_service()
        result = service.copy(
            self._content_scope_for(artifact),
            source_backend_id=pointer.backend_id,
            source_locator=pointer.locator,
            target_backend_id=target_backend_id,
            expected_sha256=artifact.digest.value,
            media_type=pointer.media_type,
        )
        target_pointer = ArtifactContentPointer(
            backend_id=result.backend_id,
            locator=result.locator,
            size_bytes=result.size_bytes,
            media_type=result.media_type,
            encryption_key_ref=pointer.encryption_key_ref,
            verified_at=time.time(),
        )

        def apply(current: ArtifactEvidenceState) -> ArtifactEvidenceState:
            for index, item in enumerate(current.artifacts):
                if item.id == artifact.id:
                    current.artifacts[index] = item.model_copy(update={"content": target_pointer})
                    break
            return current

        updated = self.store.update(apply)
        if delete_source:
            service.backend(pointer.backend_id).delete(
                self._content_scope_for(artifact),
                pointer.locator,
            )
        return next(item for item in updated.artifacts if item.id == artifact.id)

    def delete_artifact_content(
        self,
        artifact_id: str,
        *,
        actor: AuthenticationActor,
    ) -> Artifact:
        state = self.store.load()
        artifact = self._artifact(state, artifact_id, actor)
        self._require_record_mutation(artifact.producer_identity_id, actor)
        pointer = artifact.content
        if pointer is None or pointer.state == ArtifactContentState.TOMBSTONED:
            return artifact
        tombstone = self._tombstone_content_pointer(artifact)

        def apply(current: ArtifactEvidenceState) -> ArtifactEvidenceState:
            for index, item in enumerate(current.artifacts):
                if item.id == artifact.id:
                    current.artifacts[index] = item.model_copy(update={"content": tombstone})
                    break
            return current

        updated = self.store.update(apply)
        return next(item for item in updated.artifacts if item.id == artifact.id)

    def create_artifact(self, payload: ArtifactCreate, *, actor: AuthenticationActor,
                        source_artifact_ids: tuple[str, ...] = (),
                        source_evidence_ids: tuple[str, ...] = (), _commit=None,
                        idempotency_key: str | None = None, _catalog_guard=None) -> Artifact:
        if _catalog_guard is not None and _commit is not None:
            raise ArtifactEvidenceConflictError("catalog guard requires ordinary creation")
        sources = self._research_sources(payload.knowledge_ids, actor=actor, project_id=payload.project_id)
        self._validate_resources(payload.resource_ids, actor)
        self._validate_work_item_ref(payload.work_item_ref, actor)
        # Ordinary creation validates the full current catalog in its atomic write.
        # Only supersession also needs the existing producer/lifecycle precheck.
        state = self.store.load() if payload.supersedes_artifact_id else None
        if len(source_artifact_ids) + len(source_evidence_ids) > 64:
            raise ArtifactEvidenceConflictError("derived Artifact source limit exceeded")
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
            knowledge_ids=tuple(dict.fromkeys(payload.knowledge_ids)),
            research_origin=research_origin(sources),
            research_sources=research_source_snapshots(sources),
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
        if idempotency_key is not None:
            if not payload.source or not idempotency_key or len(idempotency_key) > 256:
                raise ArtifactEvidenceConflictError("bounded Artifact idempotency key and source required")
            artifact.id = self._idempotent_evidence_id(actor, payload.source, idempotency_key).replace('evidence-', 'artifact-', 1)
            artifact.metadata = {**artifact.metadata, 'idempotency_payload_sha256': hashlib.sha256(
                json.dumps({'payload': payload.model_dump(mode='json'),
                    'source_artifact_ids': sorted(set(source_artifact_ids)),
                    'source_evidence_ids': sorted(set(source_evidence_ids))}, sort_keys=True).encode()).hexdigest()}
        now = time.time()

        def apply(current: ArtifactEvidenceState, governance) -> ArtifactEvidenceState:
            nonlocal artifact
            if idempotency_key is not None:
                previous = next((item for item in current.artifacts if item.id == artifact.id), None)
                if previous is not None:
                    if previous.metadata.get('idempotency_payload_sha256') != artifact.metadata['idempotency_payload_sha256']:
                        raise ArtifactEvidenceConflictError('Artifact idempotency key has different content')
                    artifact = previous
                    return current
            derived_sources = [self._artifact(current, sid, actor) for sid in source_artifact_ids]
            derived_sources += [self._evidence(current, sid, actor) for sid in source_evidence_ids]
            if any(source.project_id != payload.project_id or source.lifecycle not in {"active", "valid"}
                   or not source.governance_record_id for source in derived_sources):
                raise ArtifactEvidenceConflictError("derived Artifact requires current governed sources in the same Project")
            current_superseded = self._artifact(current, superseded.id, actor) if superseded else None
            if current_superseded is not None and current_superseded.lifecycle != ArtifactLifecycle.ACTIVE:
                raise ArtifactEvidenceConflictError("only an active artifact can be superseded")
            if self.governance is not None:
                source_record_ids = (
                    (current_superseded.governance_record_id,)
                    if current_superseded is not None and current_superseded.governance_record_id
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
                        source_record_ids=(*source_record_ids, *(source.governance_record_id for source in sources),
                            *(source.governance_record_id for source in derived_sources)),
                        deny_model_context=payload.deny_model_context,
                        reason="artifact_created",
                    ),
                    actor=actor, state=governance,
                )
                artifact = artifact.model_copy(
                    update={
                        "governance_record_id": governance_record.id,
                        "retention_expires_at": governance_record.retention_expires_at,
                    }
                )
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

        if _catalog_guard is None:
            self._commit_creation(apply, _commit)
        else:
            creation = self._commit_creation(apply, _commit, catalog_guard=_catalog_guard)
            if creation.reused is not None:
                return creation.reused
        return artifact

    @staticmethod
    def _idempotent_evidence_id(actor: AuthenticationActor, source: str, key: str) -> str:
        scoped_key = "\0".join((actor.organization_id, actor.workspace_id, actor.identity_id, source, key))
        return "evidence-" + hashlib.sha256(scoped_key.encode()).hexdigest()

    def find_idempotent_artifact(self, *, actor: AuthenticationActor, source: str,
                                 idempotency_key: str) -> Artifact | None:
        key = self._idempotent_evidence_id(actor, source, idempotency_key).replace('evidence-', 'artifact-', 1)
        try:
            return self.get_artifact(key, actor=actor)
        except ArtifactNotFoundError:
            return None

    def find_idempotent_artifacts(self, *, actor: AuthenticationActor,
                                  requests: tuple[tuple[str, str], ...]) -> tuple[Artifact | None, ...]:
        """Resolve actor-scoped source/key identities from one canonical read."""
        ids = tuple(self._idempotent_evidence_id(actor, source, key).replace(
            'evidence-', 'artifact-', 1) for source, key in requests)
        states = self.store.artifact_states(ids, organization_id=actor.organization_id,
            workspace_id=actor.workspace_id)
        found = []
        for key in ids:
            try:
                found.append(self.get_artifact(key, actor=actor, state=states[key]))
            except ArtifactNotFoundError:
                found.append(None)
        return tuple(found)

    def find_idempotent_evidence(self, *, actor: AuthenticationActor, source: str,
                                idempotency_key: str) -> Evidence | None:
        try:
            return self.get_evidence(self._idempotent_evidence_id(actor, source, idempotency_key), actor=actor)
        except EvidenceNotFoundError:
            return None

    def create_evidence(self, payload: EvidenceCreate, *, actor: AuthenticationActor,
                        idempotency_key: str | None = None,
                        source_evidence_ids: tuple[str, ...] = (), _commit=None,
                        _state: ArtifactEvidenceState | None = None) -> Evidence:
        if _state is not None and (_commit is None or not isinstance(_state, ArtifactEvidenceState)):
            raise ArtifactEvidenceConflictError("composed Evidence state requires its owning commit")
        sources = self._research_sources(payload.knowledge_ids, actor=actor, project_id=payload.project_id)
        self._validate_resources(payload.resource_ids, actor)
        self._validate_work_item_ref(payload.work_item_ref, actor)
        state = _state if _state is not None else self.store.load()
        source_evidence = [self._evidence(state, evidence_id, actor) for evidence_id in source_evidence_ids]
        if any(item.project_id != payload.project_id for item in source_evidence):
            raise ArtifactEvidenceConflictError("derived Evidence sources must match its Project")
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
            resource_ids=payload.resource_ids,
            evidence_type=payload.evidence_type,
            knowledge_ids=tuple(dict.fromkeys(payload.knowledge_ids)),
            research_origin=research_origin(sources),
            research_sources=research_source_snapshots(sources),
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
        if idempotency_key is not None:
            evidence.id = self._idempotent_evidence_id(actor, payload.source or "", idempotency_key)
            evidence.metadata = {**evidence.metadata, "idempotency_payload_sha256":
                hashlib.sha256(json.dumps({"payload":payload.model_dump(mode="json"),
                    "source_evidence_ids":sorted(set(source_evidence_ids))}, sort_keys=True).encode()).hexdigest()}

        def existing_or_conflict(current):
            if idempotency_key is None:
                return None
            previous = next((item for item in current.evidence if item.id == evidence.id), None)
            if previous is None:
                return None
            if previous.metadata.get("idempotency_payload_sha256") != evidence.metadata["idempotency_payload_sha256"]:
                raise ArtifactEvidenceConflictError("evidence idempotency key has different content")
            return previous

        previous = existing_or_conflict(state)
        if previous is not None:
            return previous
        returned = []
        def apply(current: ArtifactEvidenceState, governance) -> ArtifactEvidenceState:
            nonlocal evidence
            previous = existing_or_conflict(current)
            if previous is not None:
                returned.append(previous)
                return current
            artifacts = [self._artifact(current, artifact_id, actor) for artifact_id in payload.artifact_ids]
            if any(item.lifecycle != ArtifactLifecycle.ACTIVE for item in artifacts):
                raise ArtifactEvidenceConflictError("evidence cannot reference inactive artifacts")
            source_evidence = [self._evidence(current, sid, actor) for sid in source_evidence_ids]
            if any(item.project_id != payload.project_id for item in source_evidence):
                raise ArtifactEvidenceConflictError("derived Evidence sources must match its Project")
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
                        source_record_ids=(*source_record_ids, *(source.governance_record_id for source in sources),
                            *(item.governance_record_id for item in source_evidence if item.governance_record_id)),
                        deny_model_context=payload.deny_model_context,
                        reason="evidence_created",
                    ),
                    actor=actor, state=governance,
                )
                evidence = evidence.model_copy(
                    update={
                        "governance_record_id": governance_record.id,
                        "retention_expires_at": governance_record.retention_expires_at,
                    }
                )

            current.evidence.append(evidence)
            returned.append(evidence)
            return current

        # Internal composition seam: caller commits this prepared mutation in its
        # existing canonical multi-document transaction; never opens a nested store.
        self._commit_creation(apply, _commit)
        return returned[0]

    def create_verification(
        self,
        payload: VerificationCreate,
        *,
        actor: AuthenticationActor,
        _commit=None,
    ) -> Verification:
        returned = []
        def apply(state: ArtifactEvidenceState) -> ArtifactEvidenceState:
            if payload.method == OBSERVED_RESEARCH_METHOD and actor.principal_kind != PrincipalKind.HUMAN:
                raise AuthorizationError("observed-user research verification requires a human actor")
            self._validate_work_item_ref(payload.work_item_ref, actor)
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
                verifier_principal_kind=actor.principal_kind,
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
            state.verifications.append(verification)
            returned.append(verification)
            return state

        # Internal composition seam, matching create_evidence: referenced
        # lifecycle/provenance and append share the caller's canonical transaction.
        # No public payload field can supply this callback or verifier identity.
        (self.store.update if _commit is None else _commit)(apply)
        return returned[0]

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
        *, cascade: bool = True,
    ) -> str:
        """Apply governance to canonical metadata and managed content.

        Managed byte content is tombstoned before the canonical mutation is
        recorded. External Git/provider content remains outside this handler.
        """
        plan = research_governance_plan(self.governance, record) if cascade else ()
        if self.organizational_memory is None and any(row.object_type == "organizational_memory" for row in plan):
            raise ArtifactEvidenceError("derived Memory governance adapter is unavailable")
        if getattr(self, "procedure_outcomes", None) is None and any(row.object_type == "procedure_skill" for row in plan):
            raise ArtifactEvidenceError("derived Skill governance adapter is unavailable")
        now = time.time()
        changed = False
        governance_content_tombstone: ArtifactContentPointer | None = None
        if record.object_type == "artifact":
            current = self.store.load()
            governed_artifact = next(
                (
                    artifact
                    for artifact in current.artifacts
                    if artifact.id == record.object_id
                    and artifact.organization_id == record.organization_id
                    and artifact.workspace_id == record.workspace_id
                ),
                None,
            )
            if governed_artifact is not None:
                governance_content_tombstone = self._tombstone_content_pointer(
                    governed_artifact
                )

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
                            "content": governance_content_tombstone or artifact.content,
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
        for dependent in plan:
            if dependent.object_type == "procedure_skill":
                self.procedure_outcomes.erase(dependent, action)
            elif dependent.object_type == "organizational_memory":
                self.organizational_memory._governance_action(dependent, action, cascade=False)
            else:
                self.apply_governance_action(dependent, action, cascade=False)
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
        retention_content_tombstones: dict[str, ArtifactContentPointer] = {}
        snapshot = self.store.load()
        for artifact in snapshot.artifacts:
            if (
                artifact.lifecycle == ArtifactLifecycle.ACTIVE
                and artifact.retention_expires_at is not None
                and artifact.retention_expires_at <= current_time
                and artifact.content is not None
            ):
                tombstone = self._tombstone_content_pointer(artifact)
                if tombstone is not None:
                    retention_content_tombstones[artifact.id] = tombstone

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
                            "content": retention_content_tombstones.get(
                                artifact.id,
                                artifact.content,
                            ),
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
            # Product outcome feedback measures acceptance; it cannot act as
            # technical verification, even under a permissive review requirement.
            and item.source != "human-outcome/1.0"
            # Notification proposals/reconciliation never establish technical
            # business completion, even under a permissive evidence requirement.
            and item.source != NOTIFICATION_RESULT_SOURCE
            and OUTCOME_METADATA_KEY not in item.metadata
            and item.evidence_type != EvidenceType.USER_OUTCOME
        ]
        outcomes: list[EvidenceRequirementOutcome] = []
        for requirement in requirements:
            matches: list[Evidence] = []
            verification_ids: set[str] = set()
            for item in valid_evidence:
                from codex_web.mr_observation_evidence import current_observation
                if not current_observation(item, now=current_time, configuration=self.mr_observation_configuration):
                    continue
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
                content_integrity_ok = True
                for artifact in linked:
                    if artifact is None or artifact.content is None:
                        continue
                    try:
                        self._verify_content_pointer(artifact)
                    except ArtifactContentError:
                        content_integrity_ok = False
                        break
                if not content_integrity_ok:
                    continue
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
                if requirement.observed_research:
                    def intact(source):
                        try:
                            if source.content is not None:
                                self._verify_content_pointer(source)
                        except ArtifactContentError:
                            return False
                        return True
                    observed = observed_verifications(item, memory=self.organizational_memory,
                        state=state, actor=actor, verify_content=intact, now=current_time,
                        max_age_seconds=requirement.max_age_seconds)
                    if not observed:
                        continue
                    verification_ids.update(observed)
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
                        + (" with current observed sources and independent human observed-user-research verification" if requirement.observed_research else "")
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


# Preserve the original owner boundary even when a class-level getter is replaced.
_CANONICAL_EVIDENCE_GETTER = ArtifactEvidenceService.get_evidence

_CANONICAL_MR_INPUT_READS={name:getattr(ArtifactEvidenceService,name) for name in (
    'get_artifact','get_evidence','get_evidence_batch','get_verification','open_artifact_content',
    '_artifact','_evidence','_verify_content_pointer')}
_CANONICAL_MR_STORE_READS={name:getattr(ArtifactEvidenceStore,name) for name in (
    '_decode','artifact_state','evidence_state','evidence_states','verification_state','input_records_state')}
