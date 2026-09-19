from __future__ import annotations

import hashlib
import math
import re
import time
from collections import defaultdict
from typing import Iterable

from codex_web.authority import (
    AuthorityAutonomyRisk,
    AuthorityDecisionOutcome,
    AuthorityEvaluationRequest,
    AuthorityLevel,
)
from codex_web.data_governance import (
    ContextFilterRequest,
    DataCategory,
    DataClassification,
    GovernedDataCreate,
    GovernedDataRecord,
    GovernanceAction,
)
from codex_web.identity import AuthenticationActor
from codex_web.organizational_memory import (
    KnowledgeCreate,
    KnowledgeDeniedCandidate,
    KnowledgeEmbedding,
    KnowledgeFreshness,
    KnowledgeInvalidate,
    KnowledgeLifecycle,
    KnowledgeQuery,
    KnowledgeRecord,
    KnowledgeRelationship,
    KnowledgeRelationshipCreate,
    KnowledgeRelationshipType,
    KnowledgeRetrievalItem,
    KnowledgeRetrievalResult,
    KnowledgeRetrievalRun,
    KnowledgeRevise,
    OrganizationalMemoryState,
)
from codex_web.retrieval import (
    LocalVectorRetrievalBackend,
    RetrievalBackend,
    RetrievalBackendStatus,
    RetrievalIndexDocument,
    RetrievalSearchRequest,
)
from codex_web.services.authority_roles import AuthorityRoleService
from codex_web.services.data_governance import DataGovernanceService
from codex_web.storage.organizational_memory import (
    KnowledgeConflictError,
    KnowledgeNotFoundError,
    OrganizationalMemoryStore,
)


class OrganizationalMemoryError(RuntimeError):
    pass


class KnowledgeAuthorizationError(OrganizationalMemoryError):
    pass


class KnowledgeValidationError(OrganizationalMemoryError):
    pass


class DeterministicSemanticEncoder:
    """Small local similarity encoder used for bounded semantic retrieval.

    It intentionally avoids model calls. Stable concept aliases, word features,
    and character trigrams produce a deterministic vector suitable for local
    hybrid similarity. A future embedding backend can replace this interface
    without changing canonical memory records or retrieval provenance.
    """

    encoder_id = "local-concept-hash-v1"
    dimensions = 128

    _ALIASES = {
        "db": "database",
        "postgres": "database",
        "postgresql": "database",
        "mysql": "database",
        "k8s": "kubernetes",
        "auth": "authentication",
        "authn": "authentication",
        "login": "authentication",
        "outage": "incident",
        "downtime": "incident",
        "failure": "incident",
        "adr": "architecture-decision",
        "architecturedecision": "architecture-decision",
        "repo": "repository",
        "repos": "repository",
        "svc": "service",
        "policy": "governance",
        "policies": "governance",
        "customer": "account",
        "client": "account",
    }

    @classmethod
    def _tokens(cls, text: str) -> tuple[str, ...]:
        raw = re.findall(r"[a-z0-9][a-z0-9_.:/-]*", text.lower())
        tokens: list[str] = []
        for token in raw:
            normalized = cls._ALIASES.get(token, token)
            tokens.append(f"w:{normalized}")
            core = normalized.replace("-", "")
            if len(core) >= 5:
                tokens.extend(
                    f"g:{core[index:index + 3]}"
                    for index in range(len(core) - 2)
                )
        return tuple(tokens)

    @classmethod
    def encode(cls, text: str) -> tuple[float, ...]:
        vector = [0.0] * cls.dimensions
        for token in cls._tokens(text):
            digest = hashlib.blake2b(
                token.encode("utf-8"),
                digest_size=8,
            ).digest()
            value = int.from_bytes(digest, "big")
            index = value % cls.dimensions
            sign = -1.0 if value & 1 else 1.0
            vector[index] += sign
        magnitude = math.sqrt(sum(value * value for value in vector))
        if not magnitude:
            return tuple(vector)
        return tuple(value / magnitude for value in vector)

    @staticmethod
    def similarity(
        left: tuple[float, ...],
        right: tuple[float, ...],
    ) -> float:
        if not left or not right or len(left) != len(right):
            return 0.0
        return max(
            0.0,
            min(1.0, sum(a * b for a, b in zip(left, right))),
        )


class OrganizationalMemoryService:
    """Governed, provenance-aware, authority-scoped organizational retrieval."""

    def __init__(
        self,
        store: OrganizationalMemoryStore,
        governance: DataGovernanceService,
        authority: AuthorityRoleService,
        *,
        encoder: DeterministicSemanticEncoder | None = None,
        retrieval_backend: RetrievalBackend | None = None,
        clock=time.time,
    ) -> None:
        self.store = store
        self.governance = governance
        self.authority = authority
        self.encoder = encoder or DeterministicSemanticEncoder()
        self.retrieval_backend = (
            retrieval_backend or LocalVectorRetrievalBackend()
        )
        self._retrieval_index_dirty = False
        self.clock = clock
        self.governance.register_action_handler(
            "organizational_memory",
            self._governance_action,
        )
        self.rebuild_retrieval_index()

    @staticmethod
    def _same_scope(
        item: KnowledgeRecord,
        actor: AuthenticationActor,
    ) -> bool:
        return (
            item.organization_id == actor.organization_id
            and item.workspace_id == actor.workspace_id
        )

    def _authority_decision(
        self,
        actor: AuthenticationActor,
        *,
        capability: str,
        level: AuthorityLevel,
        project_id: str | None,
    ):
        return self.authority.evaluate(
            AuthorityEvaluationRequest(
                capability=capability,
                level=level,
                project_id=project_id,
                autonomous_risk=AuthorityAutonomyRisk.LOW,
            ),
            actor=actor,
        )

    def _require_write(
        self,
        actor: AuthenticationActor,
        *,
        project_id: str | None,
    ) -> None:
        decision = self._authority_decision(
            actor,
            capability="memory.write",
            level=AuthorityLevel.EXECUTE,
            project_id=project_id,
        )
        if decision.outcome != AuthorityDecisionOutcome.ALLOW:
            raise KnowledgeAuthorizationError(
                "organizational memory write denied: "
                + "; ".join(decision.reasons)
            )

    @staticmethod
    def _read_capability(
        classification: DataClassification,
    ) -> str:
        if classification == DataClassification.RESTRICTED:
            return "memory.read.restricted"
        if classification == DataClassification.SECRET:
            return "memory.read.secret"
        return "memory.read"

    def _can_read(
        self,
        actor: AuthenticationActor,
        *,
        project_id: str | None,
        classification: DataClassification = DataClassification.INTERNAL,
    ) -> tuple[bool, tuple[str, ...]]:
        decision = self._authority_decision(
            actor,
            capability=self._read_capability(classification),
            level=AuthorityLevel.READ,
            project_id=project_id,
        )
        return (
            decision.outcome == AuthorityDecisionOutcome.ALLOW,
            decision.reasons,
        )

    def _role_ids(
        self,
        actor: AuthenticationActor,
        *,
        project_id: str | None,
    ) -> tuple[str, ...]:
        try:
            return self.authority.role_ids_for_actor(
                actor,
                project_id=project_id,
            )
        except Exception:
            return ()

    @staticmethod
    def _text_for_index(item: KnowledgeRecord | KnowledgeCreate) -> str:
        refs = " ".join(
            f"{ref.object_type} {ref.object_id} {ref.relation}"
            for ref in item.canonical_refs
        )
        return " ".join(
            (
                item.title,
                item.summary,
                item.content,
                " ".join(item.tags),
                refs,
            )
        )


    def _retrieval_document(
        self,
        item: KnowledgeRecord,
    ) -> RetrievalIndexDocument:
        return RetrievalIndexDocument(
            knowledge_id=item.id,
            canonical_version=item.version,
            content_sha256=item.content_sha256,
            organization_id=item.organization_id,
            workspace_id=item.workspace_id,
            project_id=item.project_id,
            object_type=item.object_type.value,
            lifecycle=item.lifecycle.value,
            title=item.title,
            summary=item.summary,
            content=item.content,
            tags=item.tags,
            indexed_text=self._text_for_index(item),
        )

    @staticmethod
    def _indexable(item: KnowledgeRecord) -> bool:
        return item.lifecycle not in {
            KnowledgeLifecycle.REDACTED,
            KnowledgeLifecycle.DELETED,
        }

    def retrieval_status(self) -> RetrievalBackendStatus:
        return self.retrieval_backend.status()

    def rebuild_retrieval_index(self) -> RetrievalBackendStatus:
        records = tuple(
            self._retrieval_document(item)
            for item in self.store.load().records
            if self._indexable(item)
        )
        try:
            self.retrieval_backend.rebuild(records)
        except Exception:
            self._retrieval_index_dirty = True
            raise
        self._retrieval_index_dirty = False
        return self.retrieval_backend.status()

    def _mark_index_dirty(self) -> None:
        self._retrieval_index_dirty = True

    def _index_upsert(self, item: KnowledgeRecord) -> None:
        try:
            if self._indexable(item):
                self.retrieval_backend.upsert(
                    self._retrieval_document(item)
                )
            else:
                self.retrieval_backend.delete(item.id)
        except Exception:
            self._mark_index_dirty()

    def _index_delete(self, knowledge_id: str) -> None:
        try:
            self.retrieval_backend.delete(knowledge_id)
        except Exception:
            self._mark_index_dirty()

    def _ensure_retrieval_index(self) -> RetrievalBackendStatus:
        status = self.retrieval_backend.status()
        expected = sum(
            1
            for item in self.store.load().records
            if self._indexable(item)
        )
        if (
            self._retrieval_index_dirty
            or not status.healthy
            or status.document_count != expected
        ):
            status = self.rebuild_retrieval_index()
        if not status.healthy:
            raise KnowledgeValidationError(
                "organizational memory retrieval index is unavailable: "
                + str(status.last_error or "unhealthy backend")
            )
        return status

    def _embedding_for(self, item: KnowledgeRecord) -> KnowledgeEmbedding:
        vector = self.encoder.encode(self._text_for_index(item))
        return KnowledgeEmbedding(
            knowledge_id=item.id,
            encoder_id=self.encoder.encoder_id,
            dimensions=self.encoder.dimensions,
            vector=vector,
            indexed_at=float(self.clock()),
        )

    def _validate_relationship_targets(
        self,
        rows: tuple[KnowledgeRelationshipCreate, ...],
        *,
        actor: AuthenticationActor,
    ) -> None:
        for row in rows:
            try:
                target = self.store.get(row.target_knowledge_id)
            except KnowledgeNotFoundError as exc:
                raise KnowledgeValidationError(
                    f"knowledge relationship target does not exist: "
                    f"{row.target_knowledge_id}"
                ) from exc
            if not self._same_scope(target, actor):
                raise KnowledgeValidationError(
                    "knowledge relationship target is outside tenant scope"
                )

    def _freshness(
        self,
        item: KnowledgeRecord,
        *,
        now: float | None = None,
    ) -> KnowledgeFreshness:
        current = float(self.clock()) if now is None else now
        if item.lifecycle == KnowledgeLifecycle.SUPERSEDED:
            return KnowledgeFreshness.SUPERSEDED
        if item.lifecycle == KnowledgeLifecycle.INVALID:
            return KnowledgeFreshness.INVALID
        if item.lifecycle == KnowledgeLifecycle.REDACTED:
            return KnowledgeFreshness.REDACTED
        if item.lifecycle == KnowledgeLifecycle.DELETED:
            return KnowledgeFreshness.DELETED
        if item.valid_until is not None and current >= item.valid_until:
            return KnowledgeFreshness.EXPIRED
        if (
            item.retention_expires_at is not None
            and current >= item.retention_expires_at
        ):
            return KnowledgeFreshness.EXPIRED
        if item.review_after is not None and current >= item.review_after:
            return KnowledgeFreshness.STALE
        return KnowledgeFreshness.FRESH

    @staticmethod
    def _token_estimate(text: str) -> int:
        if not text:
            return 0
        return max(1, (len(text) + 3) // 4)

    @staticmethod
    def _truncate_to_tokens(text: str, max_tokens: int) -> str:
        if max_tokens <= 0:
            return ""
        max_chars = max_tokens * 4
        if len(text) <= max_chars:
            return text
        suffix = "\n[…truncated by memory retrieval budget…]"
        keep = max(0, max_chars - len(suffix))
        return text[:keep].rstrip() + suffix

    @staticmethod
    def _lexical_terms(text: str) -> set[str]:
        return set(
            re.findall(
                r"[a-z0-9][a-z0-9_.:/-]*",
                text.lower(),
            )
        )

    def _lexical_score(
        self,
        query: str,
        item: KnowledgeRecord,
    ) -> float:
        query_terms = self._lexical_terms(query)
        if not query_terms:
            return 1.0
        record_terms = self._lexical_terms(
            self._text_for_index(item)
        )
        if not record_terms:
            return 0.0
        overlap = len(query_terms & record_terms)
        return overlap / max(1, len(query_terms))

    def _semantic_score(
        self,
        query_vector: tuple[float, ...],
        embedding: KnowledgeEmbedding | None,
    ) -> float:
        if embedding is None or embedding.encoder_id != self.encoder.encoder_id:
            return 0.0
        return self.encoder.similarity(
            query_vector,
            embedding.vector,
        )

    def _relationship_targets(
        self,
        state: OrganizationalMemoryState,
    ) -> dict[str, set[str]]:
        out: dict[str, set[str]] = defaultdict(set)
        for relation in state.relationships:
            out[relation.source_knowledge_id].add(
                relation.target_knowledge_id
            )
        return out

    def _structured_match(
        self,
        item: KnowledgeRecord,
        query: KnowledgeQuery,
        relationship_targets: dict[str, set[str]],
    ) -> bool:
        if query.object_types and item.object_type not in query.object_types:
            return False
        if query.project_ids and item.project_id not in query.project_ids:
            return False
        if query.logical_keys and item.logical_key not in query.logical_keys:
            return False
        if query.tags and not set(query.tags).issubset(set(item.tags)):
            return False
        if (
            query.required_role_ids
            and not set(query.required_role_ids).intersection(
                item.required_role_ids
            )
        ):
            return False
        if query.authored_by:
            if item.provenance.authored_by not in query.authored_by:
                return False
        if query.relationship_target_ids:
            targets = relationship_targets.get(item.id, set())
            if not set(query.relationship_target_ids).intersection(targets):
                return False
        return True

    def create(
        self,
        payload: KnowledgeCreate,
        *,
        actor: AuthenticationActor,
    ) -> KnowledgeRecord:
        self._require_write(actor, project_id=payload.project_id)
        self._validate_relationship_targets(
            payload.relationships,
            actor=actor,
        )

        existing = [
            item
            for item in self.store.list(
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
            )
            if item.logical_key == payload.logical_key
        ]
        if existing:
            raise KnowledgeConflictError(
                "knowledge logical_key already exists; create a revision instead"
            )

        knowledge_id = f"knowledge-{hashlib.sha256(
            f'{actor.organization_id}:{actor.workspace_id}:{payload.logical_key}:{time.time_ns()}'.encode('utf-8')
        ).hexdigest()[:32]}"
        governance = self.governance.register_domain_record(
            GovernedDataCreate(
                project_id=payload.project_id,
                object_type="organizational_memory",
                object_id=knowledge_id,
                category=DataCategory.MEMORY,
                classification=payload.classification,
                retention_expires_at=payload.retention_expires_at,
                retention_action=payload.retention_action,
                source_record_ids=payload.provenance.source_governance_record_ids,
                deny_model_context=payload.deny_model_context,
                reason="canonical organizational memory created",
            ),
            actor=actor,
        )
        now = float(self.clock())
        record = KnowledgeRecord(
            id=knowledge_id,
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            project_id=payload.project_id,
            logical_key=payload.logical_key,
            version=1,
            object_type=payload.object_type,
            title=payload.title,
            summary=payload.summary,
            content=payload.content,
            tags=payload.tags,
            canonical_refs=payload.canonical_refs,
            provenance=payload.provenance,
            classification=governance.classification,
            governance_record_id=governance.id,
            retention_expires_at=governance.retention_expires_at,
            retention_action=governance.retention_action,
            deny_model_context=governance.deny_model_context,
            required_role_ids=payload.required_role_ids,
            review_after=payload.review_after,
            valid_until=payload.valid_until,
            content_sha256=KnowledgeRecord.digest_payload(
                title=payload.title,
                summary=payload.summary,
                content=payload.content,
                canonical_refs=payload.canonical_refs,
                tags=payload.tags,
            ),
            created_by=actor.identity_id,
            created_at=now,
            updated_at=now,
        )
        relationships = tuple(
            KnowledgeRelationship(
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                source_knowledge_id=record.id,
                target_knowledge_id=row.target_knowledge_id,
                relationship_type=row.relationship_type,
                note=row.note,
                created_by=actor.identity_id,
                created_at=now,
            )
            for row in payload.relationships
        )
        def apply(state: OrganizationalMemoryState) -> OrganizationalMemoryState:
            if any(item.id == record.id for item in state.records):
                raise KnowledgeConflictError(
                    f"knowledge record already exists: {record.id}"
                )
            if any(
                item.logical_key == record.logical_key
                and item.organization_id == actor.organization_id
                and item.workspace_id == actor.workspace_id
                for item in state.records
            ):
                raise KnowledgeConflictError(
                    "knowledge logical_key already exists"
                )
            state.records.append(record)
            state.relationships.extend(relationships)
            return state

        self.store.update_state(apply)
        self._index_upsert(record)
        return record

    def revise(
        self,
        knowledge_id: str,
        payload: KnowledgeRevise,
        *,
        actor: AuthenticationActor,
    ) -> KnowledgeRecord:
        current = self.get(
            knowledge_id,
            actor=actor,
            include_inactive=True,
        )
        self._require_write(actor, project_id=current.project_id)
        if current.lifecycle != KnowledgeLifecycle.CURRENT:
            raise KnowledgeConflictError(
                "only the current knowledge version can be revised"
            )

        state_snapshot = self.store.load()
        prior_relationships = tuple(
            KnowledgeRelationshipCreate(
                target_knowledge_id=row.target_knowledge_id,
                relationship_type=row.relationship_type,
                note=row.note,
            )
            for row in state_snapshot.relationships
            if row.source_knowledge_id == current.id
            and row.relationship_type != KnowledgeRelationshipType.SUPERSEDES
        )
        merged_provenance = payload.provenance.model_copy(
            update={
                "source_governance_record_ids": tuple(
                    dict.fromkeys(
                        (
                            current.governance_record_id,
                            *payload.provenance.source_governance_record_ids,
                        )
                    )
                )
            }
        )
        normalized = KnowledgeCreate(
            logical_key=current.logical_key,
            object_type=current.object_type,
            title=payload.title if payload.title is not None else current.title,
            summary=(
                payload.summary
                if payload.summary is not None
                else current.summary
            ),
            content=(
                payload.content
                if payload.content is not None
                else current.content
            ),
            project_id=current.project_id,
            tags=payload.tags if payload.tags is not None else current.tags,
            canonical_refs=(
                payload.canonical_refs
                if payload.canonical_refs is not None
                else current.canonical_refs
            ),
            relationships=(
                payload.relationships
                if payload.relationships is not None
                else prior_relationships
            ),
            provenance=merged_provenance,
            classification=payload.classification or current.classification,
            retention_expires_at=(
                payload.retention_expires_at
                if "retention_expires_at" in payload.model_fields_set
                else current.retention_expires_at
            ),
            retention_action=(
                payload.retention_action
                if payload.retention_action is not None
                else current.retention_action
            ),
            deny_model_context=(
                payload.deny_model_context
                if payload.deny_model_context is not None
                else current.deny_model_context
            ),
            required_role_ids=(
                payload.required_role_ids
                if payload.required_role_ids is not None
                else current.required_role_ids
            ),
            review_after=(
                payload.review_after
                if "review_after" in payload.model_fields_set
                else current.review_after
            ),
            valid_until=(
                payload.valid_until
                if "valid_until" in payload.model_fields_set
                else current.valid_until
            ),
        )
        self._validate_relationship_targets(
            normalized.relationships,
            actor=actor,
        )

        next_id = f"knowledge-{uuid_hash(
            current.id,
            str(current.version + 1),
            str(time.time_ns()),
        )}"
        governance = self.governance.register_domain_record(
            GovernedDataCreate(
                project_id=current.project_id,
                object_type="organizational_memory",
                object_id=next_id,
                category=DataCategory.MEMORY,
                classification=normalized.classification,
                retention_expires_at=normalized.retention_expires_at,
                retention_action=normalized.retention_action,
                source_record_ids=merged_provenance.source_governance_record_ids,
                deny_model_context=normalized.deny_model_context,
                reason=f"organizational memory revised: {payload.reason}",
            ),
            actor=actor,
        )
        now = float(self.clock())
        next_record = KnowledgeRecord(
            id=next_id,
            organization_id=current.organization_id,
            workspace_id=current.workspace_id,
            project_id=current.project_id,
            logical_key=current.logical_key,
            version=current.version + 1,
            object_type=current.object_type,
            title=normalized.title,
            summary=normalized.summary,
            content=normalized.content,
            tags=normalized.tags,
            canonical_refs=normalized.canonical_refs,
            provenance=merged_provenance,
            classification=governance.classification,
            governance_record_id=governance.id,
            retention_expires_at=governance.retention_expires_at,
            retention_action=governance.retention_action,
            deny_model_context=governance.deny_model_context,
            required_role_ids=normalized.required_role_ids,
            review_after=normalized.review_after,
            valid_until=normalized.valid_until,
            previous_version_id=current.id,
            content_sha256=KnowledgeRecord.digest_payload(
                title=normalized.title,
                summary=normalized.summary,
                content=normalized.content,
                canonical_refs=normalized.canonical_refs,
                tags=normalized.tags,
            ),
            created_by=actor.identity_id,
            created_at=now,
            updated_at=now,
        )
        new_relationships = [
            KnowledgeRelationship(
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                source_knowledge_id=next_record.id,
                target_knowledge_id=current.id,
                relationship_type=KnowledgeRelationshipType.SUPERSEDES,
                note=payload.reason,
                created_by=actor.identity_id,
                created_at=now,
            )
        ]
        new_relationships.extend(
            KnowledgeRelationship(
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                source_knowledge_id=next_record.id,
                target_knowledge_id=row.target_knowledge_id,
                relationship_type=row.relationship_type,
                note=row.note,
                created_by=actor.identity_id,
                created_at=now,
            )
            for row in normalized.relationships
        )
        def apply(state: OrganizationalMemoryState) -> OrganizationalMemoryState:
            stored = next(
                (item for item in state.records if item.id == current.id),
                None,
            )
            if stored is None:
                raise KnowledgeNotFoundError(current.id)
            if stored.lifecycle != KnowledgeLifecycle.CURRENT:
                raise KnowledgeConflictError(
                    "knowledge version changed during revision"
                )
            superseded = stored.model_copy(
                update={
                    "lifecycle": KnowledgeLifecycle.SUPERSEDED,
                    "superseded_by_id": next_record.id,
                    "updated_at": now,
                }
            )
            state.records = [
                superseded if item.id == stored.id else item
                for item in state.records
            ]
            state.records.append(next_record)
            state.relationships.extend(new_relationships)
            return state

        self.store.update_state(apply)
        self._index_upsert(self.store.get(current.id))
        self._index_upsert(next_record)
        return next_record

    def invalidate(
        self,
        knowledge_id: str,
        payload: KnowledgeInvalidate,
        *,
        actor: AuthenticationActor,
    ) -> KnowledgeRecord:
        current = self.get(
            knowledge_id,
            actor=actor,
            include_inactive=True,
        )
        self._require_write(actor, project_id=current.project_id)
        if current.lifecycle == KnowledgeLifecycle.DELETED:
            raise KnowledgeConflictError("deleted knowledge cannot be invalidated")

        def apply(
            state: OrganizationalMemoryState,
            stored: KnowledgeRecord,
        ):
            now = float(self.clock())
            updated = stored.model_copy(
                update={
                    "lifecycle": KnowledgeLifecycle.INVALID,
                    "invalid_reason": payload.reason,
                    "updated_at": now,
                }
            )
            state.records = [
                updated if item.id == stored.id else item
                for item in state.records
            ]
            return state, updated

        updated = self.store.update(knowledge_id, apply)
        self._index_upsert(updated)
        return updated

    def get(
        self,
        knowledge_id: str,
        *,
        actor: AuthenticationActor,
        include_inactive: bool = False,
    ) -> KnowledgeRecord:
        item = self.store.get(knowledge_id)
        if not self._same_scope(item, actor):
            raise KnowledgeNotFoundError(knowledge_id)
        allowed, reasons = self._can_read(
            actor,
            project_id=item.project_id,
            classification=item.classification,
        )
        if not allowed:
            raise KnowledgeAuthorizationError(
                "organizational memory read denied: "
                + "; ".join(reasons)
            )
        if (
            item.required_role_ids
            and not set(item.required_role_ids).intersection(
                self._role_ids(actor, project_id=item.project_id)
            )
        ):
            raise KnowledgeAuthorizationError(
                "organizational memory requires an operational Role not held by actor"
            )
        if (
            not include_inactive
            and item.lifecycle != KnowledgeLifecycle.CURRENT
        ):
            raise KnowledgeNotFoundError(knowledge_id)
        return item

    def list(
        self,
        *,
        actor: AuthenticationActor,
        project_id: str | None = None,
        include_inactive: bool = False,
    ) -> tuple[KnowledgeRecord, ...]:
        rows: list[KnowledgeRecord] = []
        authority_cache: dict[tuple[str | None, DataClassification], bool] = {}
        role_cache: dict[str | None, tuple[str, ...]] = {}
        for item in self.store.list(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
        ):
            if project_id is not None and item.project_id != project_id:
                continue
            if not include_inactive and item.lifecycle != KnowledgeLifecycle.CURRENT:
                continue
            authority_key = (item.project_id, item.classification)
            if authority_key not in authority_cache:
                authority_cache[authority_key] = self._can_read(
                    actor,
                    project_id=item.project_id,
                    classification=item.classification,
                )[0]
            if not authority_cache[authority_key]:
                continue
            if item.required_role_ids:
                if item.project_id not in role_cache:
                    role_cache[item.project_id] = self._role_ids(
                        actor,
                        project_id=item.project_id,
                    )
                if not set(item.required_role_ids).intersection(
                    role_cache[item.project_id]
                ):
                    continue
            rows.append(item)
        return tuple(rows)

    def versions(
        self,
        knowledge_id: str,
        *,
        actor: AuthenticationActor,
    ) -> tuple[KnowledgeRecord, ...]:
        item = self.get(
            knowledge_id,
            actor=actor,
            include_inactive=True,
        )
        rows = [
            row
            for row in self.list(
                actor=actor,
                include_inactive=True,
            )
            if row.logical_key == item.logical_key
        ]
        rows.sort(key=lambda row: row.version)
        return tuple(rows)

    def relationships(
        self,
        knowledge_id: str,
        *,
        actor: AuthenticationActor,
    ) -> tuple[KnowledgeRelationship, ...]:
        self.get(
            knowledge_id,
            actor=actor,
            include_inactive=True,
        )
        state = self.store.load()
        rows = [
            item
            for item in state.relationships
            if (
                item.source_knowledge_id == knowledge_id
                or item.target_knowledge_id == knowledge_id
            )
            and item.organization_id == actor.organization_id
            and item.workspace_id == actor.workspace_id
        ]
        rows.sort(key=lambda item: (item.created_at, item.id))
        return tuple(rows)

    def retrieval_run(
        self,
        retrieval_id: str,
        *,
        actor: AuthenticationActor,
    ) -> KnowledgeRetrievalRun:
        item = next(
            (
                row
                for row in self.store.load().retrieval_runs
                if row.id == retrieval_id
                and row.organization_id == actor.organization_id
                and row.workspace_id == actor.workspace_id
            ),
            None,
        )
        if item is None:
            raise KnowledgeNotFoundError(retrieval_id)
        return item

    def search(
        self,
        query: KnowledgeQuery,
        *,
        actor: AuthenticationActor,
    ) -> KnowledgeRetrievalResult:
        state = self.store.load()
        relationships = self._relationship_targets(state)
        now = float(self.clock())
        candidates = [
            item
            for item in state.records
            if self._same_scope(item, actor)
            and self._structured_match(
                item,
                query,
                relationships,
            )
        ]
        candidate_count = len(candidates)
        denied: list[KnowledgeDeniedCandidate] = []
        authority_cache: dict[
            tuple[str | None, DataClassification],
            tuple[bool, tuple[str, ...]],
        ] = {}
        role_cache: dict[str | None, tuple[str, ...]] = {}
        lifecycle_filtered: list[tuple[KnowledgeRecord, KnowledgeFreshness]] = []

        for item in candidates:
            freshness = self._freshness(item, now=now)
            if freshness == KnowledgeFreshness.SUPERSEDED:
                if not query.include_superseded:
                    continue
            elif freshness == KnowledgeFreshness.INVALID:
                if not query.include_invalid:
                    continue
            elif freshness in {
                KnowledgeFreshness.REDACTED,
                KnowledgeFreshness.DELETED,
            }:
                continue
            elif freshness in {
                KnowledgeFreshness.STALE,
                KnowledgeFreshness.EXPIRED,
            } and not query.include_stale:
                continue

            authority_key = (item.project_id, item.classification)
            if authority_key not in authority_cache:
                authority_cache[authority_key] = self._can_read(
                    actor,
                    project_id=item.project_id,
                    classification=item.classification,
                )
            allowed, reasons = authority_cache[authority_key]
            if not allowed:
                denied.append(
                    KnowledgeDeniedCandidate(
                        knowledge_id=item.id,
                        reason="authority:" + "; ".join(reasons),
                    )
                )
                continue

            if item.required_role_ids:
                if item.project_id not in role_cache:
                    role_cache[item.project_id] = self._role_ids(
                        actor,
                        project_id=item.project_id,
                    )
                if not set(item.required_role_ids).intersection(
                    role_cache[item.project_id]
                ):
                    denied.append(
                        KnowledgeDeniedCandidate(
                            knowledge_id=item.id,
                            reason="required_operational_role_missing",
                        )
                    )
                    continue
            lifecycle_filtered.append((item, freshness))

        governance_allowed: set[str] = set()
        if lifecycle_filtered:
            filter_result = self.governance.filter_context(
                ContextFilterRequest(
                    record_ids=tuple(
                        item.governance_record_id
                        for item, _freshness in lifecycle_filtered
                    ),
                    max_classification=query.max_classification,
                ),
                actor=actor,
            )
            governance_allowed = set(filter_result.allowed_record_ids)
            decision_by_id = {
                decision.record_id: decision
                for decision in filter_result.decisions
            }
            for item, _freshness in lifecycle_filtered:
                if item.governance_record_id in governance_allowed:
                    continue
                decision = decision_by_id[item.governance_record_id]
                denied.append(
                    KnowledgeDeniedCandidate(
                        knowledge_id=item.id,
                        reason=f"governance:{decision.reason}",
                    )
                )

        authorized_rows = [
            (item, freshness)
            for item, freshness in lifecycle_filtered
            if item.governance_record_id in governance_allowed
        ]
        canonical_by_id = {
            item.id: (item, freshness)
            for item, freshness in authorized_rows
        }
        retrieval_status = self._ensure_retrieval_index()
        retrieval_request = RetrievalSearchRequest(
            text=query.text,
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            allowed_knowledge_ids=tuple(canonical_by_id),
            project_ids=query.project_ids,
            object_types=tuple(item.value for item in query.object_types),
            lifecycles=tuple(
                dict.fromkeys(
                    item.lifecycle.value
                    for item, _freshness in authorized_rows
                )
            ),
            limit=query.budget.candidate_limit,
        )
        hits = self.retrieval_backend.search(retrieval_request)
        stale_hits = [
            hit
            for hit in hits
            if hit.knowledge_id not in canonical_by_id
            or hit.canonical_version
            != canonical_by_id[hit.knowledge_id][0].version
            or hit.content_sha256
            != canonical_by_id[hit.knowledge_id][0].content_sha256
        ]
        if stale_hits:
            retrieval_status = self.rebuild_retrieval_index()
            hits = self.retrieval_backend.search(retrieval_request)

        scored: list[
            tuple[
                float,
                float,
                float,
                KnowledgeRecord,
                KnowledgeFreshness,
                tuple[str, ...],
            ]
        ] = []

        for hit in hits:
            row = canonical_by_id.get(hit.knowledge_id)
            if row is None:
                continue
            item, freshness = row
            semantic = hit.vector_score
            lexical = hit.lexical_score
            score = hit.score
            reasons = list(hit.reasons)
            if query.logical_keys and item.logical_key in query.logical_keys:
                score += 0.15
                reasons.append("logical_key")
            if query.tags:
                score += min(0.10, 0.02 * len(query.tags))
                reasons.append("tag_filter")
            if freshness in {
                KnowledgeFreshness.STALE,
                KnowledgeFreshness.EXPIRED,
            }:
                score *= 0.65
                reasons.append(f"freshness:{freshness.value}")
            elif freshness == KnowledgeFreshness.SUPERSEDED:
                score *= 0.40
                reasons.append("historical:superseded")
            elif freshness == KnowledgeFreshness.INVALID:
                score *= 0.20
                reasons.append("historical:invalid")
            scored.append(
                (
                    score,
                    semantic,
                    lexical,
                    item,
                    freshness,
                    tuple(reasons),
                )
            )

        scored.sort(
            key=lambda row: (
                row[0],
                row[3].version,
                row[3].updated_at,
                row[3].id,
            ),
            reverse=True,
        )
        scored = scored[: query.budget.candidate_limit]

        if query.text:
            primary = [row for row in scored if row[0] >= 0.18]
            if query.budget.progressive and len(primary) < query.budget.top_k:
                secondary = [
                    row
                    for row in scored
                    if row not in primary and row[0] >= 0.06
                ]
                selected_pool = [*primary, *secondary]
                if len(selected_pool) < query.budget.top_k:
                    selected_pool.extend(
                        row
                        for row in scored
                        if row not in selected_pool
                    )
            else:
                selected_pool = primary
        else:
            selected_pool = scored

        items: list[KnowledgeRetrievalItem] = []
        remaining = query.budget.max_context_tokens
        per_item_soft_cap = max(
            64,
            min(
                2400,
                query.budget.max_context_tokens
                // max(1, query.budget.top_k),
            ),
        )
        for (
            score,
            semantic,
            lexical,
            item,
            freshness,
            reasons,
        ) in selected_pool:
            if len(items) >= query.budget.top_k or remaining < 32:
                break
            source = (
                f"{item.title}\n\n{item.summary}\n\n{item.content}"
            )
            allowance = min(remaining, per_item_soft_cap)
            excerpt = self._truncate_to_tokens(source, allowance)
            estimated = self._token_estimate(excerpt)
            if estimated > remaining:
                excerpt = self._truncate_to_tokens(source, remaining)
                estimated = self._token_estimate(excerpt)
            if estimated <= 0:
                continue
            items.append(
                KnowledgeRetrievalItem(
                    knowledge_id=item.id,
                    logical_key=item.logical_key,
                    version=item.version,
                    object_type=item.object_type,
                    project_id=item.project_id,
                    title=item.title,
                    summary=item.summary,
                    context_excerpt=excerpt,
                    tags=item.tags,
                    canonical_refs=item.canonical_refs,
                    provenance=item.provenance,
                    classification=item.classification,
                    governance_record_id=item.governance_record_id,
                    freshness=freshness,
                    lifecycle=item.lifecycle,
                    score=round(score, 6),
                    semantic_score=round(semantic, 6),
                    lexical_score=round(lexical, 6),
                    estimated_tokens=estimated,
                    reasons=reasons,
                    retrieval_backend_id=retrieval_status.backend_id,
                    retrieval_index_revision=retrieval_status.index_revision,
                    embedding_provider_id=(
                        retrieval_status.embedding_identity.provider_id
                        if retrieval_status.embedding_identity is not None
                        else None
                    ),
                    embedding_model_id=(
                        retrieval_status.embedding_identity.model_id
                        if retrieval_status.embedding_identity is not None
                        else None
                    ),
                    embedding_model_revision=(
                        retrieval_status.embedding_identity.model_revision
                        if retrieval_status.embedding_identity is not None
                        else None
                    ),
                )
            )
            remaining -= estimated

        packed = sum(item.estimated_tokens for item in items)
        query_digest = hashlib.sha256(
            query.model_dump_json(
                exclude_none=True,
            ).encode("utf-8")
        ).hexdigest()
        filters = query.model_dump(
            mode="json",
            exclude={"text"},
        )
        run = KnowledgeRetrievalRun(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            actor_id=actor.identity_id,
            query_sha256=query_digest,
            filters=filters,
            selected_knowledge_ids=tuple(
                item.knowledge_id for item in items
            ),
            selected_scores={
                item.knowledge_id: item.score for item in items
            },
            selected_freshness={
                item.knowledge_id: item.freshness for item in items
            },
            denied=tuple(denied),
            packed_tokens=packed,
            candidate_count=candidate_count,
            top_k=query.budget.top_k,
            max_context_tokens=query.budget.max_context_tokens,
            retrieval_backend_id=retrieval_status.backend_id,
            retrieval_index_revision=retrieval_status.index_revision,
            embedding_provider_id=(
                retrieval_status.embedding_identity.provider_id
                if retrieval_status.embedding_identity is not None
                else None
            ),
            embedding_model_id=(
                retrieval_status.embedding_identity.model_id
                if retrieval_status.embedding_identity is not None
                else None
            ),
            embedding_model_revision=(
                retrieval_status.embedding_identity.model_revision
                if retrieval_status.embedding_identity is not None
                else None
            ),
            created_at=now,
        )

        def save_run(
            current: OrganizationalMemoryState,
        ) -> OrganizationalMemoryState:
            current.retrieval_runs.append(run)
            if len(current.retrieval_runs) > 5000:
                current.retrieval_runs = current.retrieval_runs[-5000:]
            return current

        self.store.update_state(save_run)
        return KnowledgeRetrievalResult(
            retrieval_id=run.id,
            items=tuple(items),
            denied=tuple(denied),
            packed_tokens=packed,
            candidate_count=candidate_count,
            top_k=query.budget.top_k,
            max_context_tokens=query.budget.max_context_tokens,
            retrieval_backend_id=retrieval_status.backend_id,
            retrieval_index_revision=retrieval_status.index_revision,
            embedding_provider_id=(
                retrieval_status.embedding_identity.provider_id
                if retrieval_status.embedding_identity is not None
                else None
            ),
            embedding_model_id=(
                retrieval_status.embedding_identity.model_id
                if retrieval_status.embedding_identity is not None
                else None
            ),
            embedding_model_revision=(
                retrieval_status.embedding_identity.model_revision
                if retrieval_status.embedding_identity is not None
                else None
            ),
        )

    def _governance_action(
        self,
        governed_record: GovernedDataRecord,
        action: GovernanceAction,
    ) -> str:
        target_id = governed_record.object_id
        state = self.store.load()
        direct = next(
            (item for item in state.records if item.id == target_id),
            None,
        )
        if direct is None:
            return f"memory:{target_id}:{action.value}:not-found"

        affected = {target_id}
        changed = True
        while changed:
            changed = False
            for item in state.records:
                if item.id in affected:
                    continue
                if any(
                    source in {
                        state_item.governance_record_id
                        for state_item in state.records
                        if state_item.id in affected
                    }
                    for source in item.provenance.source_governance_record_ids
                ):
                    affected.add(item.id)
                    changed = True

        lifecycle = (
            KnowledgeLifecycle.DELETED
            if action == GovernanceAction.DELETE
            else KnowledgeLifecycle.REDACTED
        )
        now = float(self.clock())

        def apply(
            current: OrganizationalMemoryState,
        ) -> OrganizationalMemoryState:
            replacements: list[KnowledgeRecord] = []
            for item in current.records:
                if item.id not in affected:
                    replacements.append(item)
                    continue
                replacement = item.model_copy(
                    update={
                        "title": (
                            "[deleted]"
                            if action == GovernanceAction.DELETE
                            else "[redacted]"
                        ),
                        "summary": (
                            "[deleted by governance]"
                            if action == GovernanceAction.DELETE
                            else "[redacted by governance]"
                        ),
                        "content": "",
                        "tags": (),
                        "canonical_refs": (),
                        "lifecycle": lifecycle,
                        "invalid_reason": f"governance:{action.value}",
                        "updated_at": now,
                        "content_sha256": KnowledgeRecord.digest_payload(
                            title=(
                                "[deleted]"
                                if action == GovernanceAction.DELETE
                                else "[redacted]"
                            ),
                            summary=(
                                "[deleted by governance]"
                                if action == GovernanceAction.DELETE
                                else "[redacted by governance]"
                            ),
                            content="",
                            canonical_refs=(),
                            tags=(),
                        ),
                    }
                )
                replacements.append(replacement)
            current.records = replacements
            current.embeddings = [
                item
                for item in current.embeddings
                if item.knowledge_id not in affected
            ]
            current.relationships = [
                item
                for item in current.relationships
                if item.source_knowledge_id not in affected
                and item.target_knowledge_id not in affected
            ]
            return current

        self.store.update_state(apply)
        for knowledge_id in affected:
            self._index_delete(knowledge_id)
        return (
            f"memory:{target_id}:{action.value}:"
            + ",".join(sorted(affected))
        )


def uuid_hash(*parts: str) -> str:
    return hashlib.sha256(
        ":".join(parts).encode("utf-8")
    ).hexdigest()[:32]
