from __future__ import annotations

from types import SimpleNamespace

import hashlib
import json
from contextvars import ContextVar
import math
import re
import time
from collections import defaultdict
from typing import Callable, Iterable

from pydantic import TypeAdapter

from codex_web.authority import (
    AuthorityAutonomyRisk,
    AuthorityDecisionOutcome,
    AuthorityEvaluationRequest,
    AuthorityLevel,
)
from codex_web.data_governance import (
    CLASSIFICATION_RANK,
    ContextFilterRequest,
    DataCategory,
    DataClassification,
    GovernedDataCreate,
    GovernedDataRecord,
    GovernanceAction,
    ExportAuthorizationRequest,
)
from codex_web.identity import AuthenticationActor
from codex_web.organizational_memory import (
    GoverningResourceIds,
    KnowledgeCreate,
    KnowledgeDeniedCandidate,
    KnowledgeEmbedding,
    KnowledgeFreshness,
    KnowledgeIngestBatch,
    KnowledgeInvalidate,
    KnowledgeLifecycle,
    KnowledgeProcedurePromotion,
    KnowledgeProvenance,
    KnowledgeOrigin,
    KnowledgeOriginSource,
    KnowledgeQuery,
    KnowledgeContextSelection,
    KnowledgeRecord,
    KnowledgeRelationship,
    KnowledgeCanonicalRef,
    KnowledgeRelationshipCreate,
    KnowledgeRelationshipType,
    KnowledgeRetrievalItem,
    KnowledgeRetrievalResult,
    KnowledgeRetrievalRun,
    KnowledgeRevise,
    KnowledgeSourceKind,
    KnowledgeObjectType,
    OrganizationalMemoryState,
)
from codex_web.retrieval import (
    EmbeddingModelIdentity,
    LocalVectorRetrievalBackend,
    RetrievalBackend,
    RetrievalBackendStatus,
    RetrievalIndexDocument,
    RetrievalSearchRequest,
)
from codex_web.services.authority_roles import AuthorityRoleService
from codex_web.services.data_governance import DataGovernanceService
from codex_web.services.research_provenance import derived_source_freshness, derive_research_provenance, research_governance_plan, research_sources_readable, research_evidence_freshness
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


EmbeddingIdentityValidator = Callable[
    [EmbeddingModelIdentity, AuthenticationActor],
    None,
]


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


_MEMORY_SOURCE_READ_STACK = ContextVar("memory_source_read_stack", default=frozenset())


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
        embedding_identity_validator: EmbeddingIdentityValidator | None = None,
        artifact_evidence=None,
        clock=time.time,
    ) -> None:
        self.store = store
        self.governance = governance
        self.artifact_evidence = artifact_evidence
        self.authority = authority
        self.encoder = encoder or DeterministicSemanticEncoder()
        self.retrieval_backend = (
            retrieval_backend or LocalVectorRetrievalBackend()
        )
        self.embedding_identity_validator = embedding_identity_validator
        self._retrieval_index_dirty = False
        self.clock = clock
        self.governance.register_action_handler(
            "organizational_memory",
            self._governance_action,
        )
        status = self.retrieval_backend.status()
        if (
            status.embedding_identity is not None
            and not status.embedding_identity.local
        ):
            self._retrieval_index_dirty = True
        else:
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
        resource_ids: tuple[str, ...] = (),
    ):
        return self.authority.evaluate(
            AuthorityEvaluationRequest(
                capability=capability,
                level=level,
                project_id=project_id,
                resource_ids=resource_ids,
                autonomous_risk=AuthorityAutonomyRisk.LOW,
            ),
            actor=actor,
        )

    def _require_write(
        self,
        actor: AuthenticationActor,
        *,
        project_id: str | None,
        resource_ids: tuple[str, ...] = (),
    ) -> None:
        decision = self._authority_decision(
            actor,
            capability="memory.write",
            level=AuthorityLevel.EXECUTE,
            project_id=project_id,
            resource_ids=resource_ids,
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
        resource_ids: tuple[str, ...] = (),
    ) -> tuple[bool, tuple[str, ...]]:
        if resource_ids:
            catalog = getattr(self.authority, "resources", None)
            if catalog is None or not project_id:
                return False, ("canonical governing Resource scope is unavailable",)
            project = SimpleNamespace(id=project_id, organization_id=actor.organization_id,
                                      workspace_id=actor.workspace_id)
            if not set(resource_ids).issubset(item.id for item in catalog.project_resources(project, actor=actor)):
                return False, ("governing Resource is no longer bound to the Memory project",)
        decision = self._authority_decision(
            actor,
            capability=self._read_capability(classification),
            level=AuthorityLevel.READ,
            project_id=project_id,
            resource_ids=resource_ids,
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

    def rebuild_retrieval_index(
        self,
        *,
        actor: AuthenticationActor | None = None,
    ) -> RetrievalBackendStatus:
        state = self.store.load()
        indexable = [
            item
            for item in state.records
            if self._indexable(item)
        ]
        status = self.retrieval_backend.status()
        identity = status.embedding_identity
        if identity is not None and not identity.local:
            if actor is None:
                self._retrieval_index_dirty = True
                raise KnowledgeAuthorizationError(
                    "external embedding index rebuild requires an authenticated "
                    "administrator for canonical export authorization"
                )
            if self.embedding_identity_validator is None:
                self._retrieval_index_dirty = True
                raise KnowledgeValidationError(
                    "external embedding provider requires canonical ModelGateway "
                    "identity validation"
                )
            self.embedding_identity_validator(identity, actor)
            now = float(self.clock())
            indexable = [
                item
                for item in indexable
                if (
                    item.retention_expires_at is None
                    or now < item.retention_expires_at
                )
            ]
            governed_ids = tuple(
                item.governance_record_id
                for item in indexable
            )
            context = self.governance.filter_context(
                ContextFilterRequest(
                    record_ids=governed_ids,
                    max_classification=DataClassification.RESTRICTED,
                ),
                actor=actor,
            )
            manifest = self.governance.authorize_export(
                ExportAuthorizationRequest(
                    record_ids=governed_ids,
                    max_classification=DataClassification.RESTRICTED,
                ),
                actor=actor,
            )
            context_allowed = set(context.allowed_record_ids)
            export_allowed = {
                item.record_id: item
                for item in manifest.items
            }
            provider_residency = set(identity.residency_tags)
            allowed = {
                record_id
                for record_id, item in export_allowed.items()
                if record_id in context_allowed
                and set(item.residency_tags).issubset(provider_residency)
            }
            indexable = [
                item
                for item in indexable
                if item.governance_record_id in allowed
            ]
        records = tuple(
            self._retrieval_document(item)
            for item in indexable
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

    def _index_upsert(
        self,
        item: KnowledgeRecord,
        *,
        actor: AuthenticationActor | None = None,
    ) -> None:
        try:
            if not self._indexable(item):
                self.retrieval_backend.delete(item.id)
                return
            status = self.retrieval_backend.status()
            identity = status.embedding_identity
            if identity is not None and not identity.local:
                if actor is None:
                    self._mark_index_dirty()
                    return
                if self.embedding_identity_validator is None:
                    self._mark_index_dirty()
                    return
                self.embedding_identity_validator(identity, actor)
                if (
                    item.retention_expires_at is not None
                    and float(self.clock()) >= item.retention_expires_at
                ):
                    self.retrieval_backend.delete(item.id)
                    return
                context = self.governance.filter_context(
                    ContextFilterRequest(
                        record_ids=(item.governance_record_id,),
                        max_classification=DataClassification.RESTRICTED,
                    ),
                    actor=actor,
                )
                manifest = self.governance.authorize_export(
                    ExportAuthorizationRequest(
                        record_ids=(item.governance_record_id,),
                        max_classification=DataClassification.RESTRICTED,
                    ),
                    actor=actor,
                )
                export_item = manifest.items[0] if manifest.items else None
                residency_ok = (
                    export_item is not None
                    and set(export_item.residency_tags).issubset(
                        set(identity.residency_tags)
                    )
                )
                if (
                    item.governance_record_id
                    not in set(context.allowed_record_ids)
                    or not residency_ok
                ):
                    self.retrieval_backend.delete(item.id)
                    return
            self.retrieval_backend.upsert(
                self._retrieval_document(item)
            )
        except Exception:
            self._mark_index_dirty()

    def _index_delete(self, knowledge_id: str) -> None:
        try:
            self.retrieval_backend.delete(knowledge_id)
        except Exception:
            self._mark_index_dirty()

    def _ensure_retrieval_index(
        self,
        *,
        actor: AuthenticationActor,
    ) -> RetrievalBackendStatus:
        status = self.retrieval_backend.status()
        identity = status.embedding_identity
        if identity is not None and not identity.local:
            expected = status.document_count
        else:
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
            status = self.rebuild_retrieval_index(actor=actor)
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

    def _prepare_typed_content(self, payload, actor):
        if payload.object_type == KnowledgeObjectType.UX_BRIEF:
            from codex_web.services.ux_briefs import UxBriefService
            return UxBriefService(self)._prepared(payload, project_id=payload.project_id, actor=actor)
        return payload

    def _sources_readable(self, item, actor, records=None):
        stack = _MEMORY_SOURCE_READ_STACK.get()
        identity = (item.organization_id, item.workspace_id, item.id)
        if identity in stack or len(stack) >= 32:
            return False
        token = _MEMORY_SOURCE_READ_STACK.set(stack | {identity})
        try:
            if not research_sources_readable(self, item, actor, records):
                return False
            if item.provenance.origin_sources:
                record_map = records if records is not None else {row.id: row for row in self.store.load().records}
                for ref in item.provenance.origin_sources:
                    source = record_map.get(ref.knowledge_id)
                    if source is not None and source.object_type == KnowledgeObjectType.UX_BRIEF:
                        if not self._sources_readable(source, actor, record_map):
                            return False
            if item.object_type == KnowledgeObjectType.UX_BRIEF and item.content:
                from codex_web.services.ux_brief_sources import brief_sources_readable
                return brief_sources_readable(self, item, actor)
            return True
        finally:
            _MEMORY_SOURCE_READ_STACK.reset(token)

    def _derived_provenance(
        self, provenance, relationships, *, actor, project_id, previous=None,
    ) -> KnowledgeProvenance:
        return derive_research_provenance(self, provenance, relationships,
            actor=actor, project_id=project_id, previous=previous, invalid=KnowledgeValidationError)

    def _freshness(
        self,
        item: KnowledgeRecord,
        *,
        now: float | None = None,
        records: dict[str, KnowledgeRecord] | None = None,
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
        if item.object_type == KnowledgeObjectType.UX_BRIEF and item.content:
            from codex_web.services.ux_brief_sources import brief_source_freshness
            source_status = brief_source_freshness(self, item, records=records)
            if source_status is not None:
                return source_status
        if research_evidence_freshness(self, item) is not None:
            return KnowledgeFreshness.INVALID
        if item.provenance.origin_sources:
            records = records if records is not None else {row.id: row for row in self.store.load().records}
            for ref in item.provenance.origin_sources:
                source = records.get(ref.knowledge_id)
                if source is not None and source.object_type == KnowledgeObjectType.UX_BRIEF:
                    from codex_web.services.ux_brief_sources import brief_source_freshness
                    status = brief_source_freshness(self, source, records=records)
                    if status is not None:
                        return status
            source_status = derived_source_freshness(item, records, now=current)
            if source_status is not None:
                return source_status
        return KnowledgeFreshness.FRESH

    @staticmethod
    def context_header(item, *, include_source_lineage=True):
        header = (f"Research origin: {item.provenance.origin.value}; "
            f"review: {item.provenance.review_status} (metadata, not verification).\n")
        if include_source_lineage:
            header += "".join(f"Source [memory:{row.knowledge_id}@v{row.version}]: {row.origin.value}\n"
                for row in item.provenance.origin_sources)
        return header

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
        if query.knowledge_ids and item.id not in query.knowledge_ids:
            return False
        if query.object_types and item.object_type not in query.object_types:
            return False
        if query.project_ids and item.project_id not in query.project_ids:
            if not (query.include_company_scope and item.project_id is None):
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
        if query.canonical_target_refs:
            # Canonical object identity is distinct from Knowledge-to-Knowledge
            # relationships. Relation labels remain provenance, not authority.
            wanted = {(ref.object_type, ref.object_id) for ref in query.canonical_target_refs}
            if not wanted.intersection((ref.object_type, ref.object_id) for ref in item.canonical_refs):
                return False
        if query.relationship_target_ids:
            targets = relationship_targets.get(item.id, set())
            if not set(query.relationship_target_ids).intersection(targets):
                return False
        return True

    def _governing_resources(self, payload, actor):
        resources = payload.resource_ids
        sources = [self.get(link.target_knowledge_id, actor=actor, include_inactive=True)
                   for link in payload.relationships
                   if link.relationship_type == KnowledgeRelationshipType.DERIVED_FROM]
        # Governed derivation is not limited to Knowledge relationship labels.
        # Resolve exact canonical source owners; no Resource proof is inferred
        # from a free-form reference, source kind, title or company/project label.
        if resources is not None or any(source.resource_ids is not None for source in sources):
            for record_id in payload.provenance.source_governance_record_ids:
                governed = self.governance.get_record(record_id, actor, context_only=True)
                if governed.object_type == "organizational_memory":
                    source = self.get(governed.object_id, actor=actor, include_inactive=True)
                elif governed.object_type in {"artifact", "evidence"} and self.artifact_evidence is not None:
                    owner = getattr(self.artifact_evidence, "get_" + governed.object_type)
                    source = owner(governed.object_id, actor=actor)
                else:
                    source = None
                if source is None or source.governance_record_id != record_id:
                    raise KnowledgeValidationError("governed source has no canonical Resource proof")
                sources.append(source)
            for evidence_id in payload.provenance.evidence_ids:
                if self.artifact_evidence is None:
                    raise KnowledgeValidationError("canonical source Evidence is unavailable")
                proof = self.artifact_evidence.get_evidence(evidence_id, actor=actor)
                sources.append(proof)
                sources.extend(self.artifact_evidence.get_artifact(ref, actor=actor) for ref in proof.artifact_ids)
        if sources:
            if any(not source.resource_ids for source in sources):
                if resources is not None:
                    raise KnowledgeValidationError("derived Memory has unknown source Resource scope")
                return None
            inherited = tuple(dict.fromkeys(resource for source in sources for resource in source.resource_ids))
            if resources is not None and not set(inherited).issubset(resources):
                raise KnowledgeValidationError("derived Memory cannot narrow governing Resources")
            resources = tuple(dict.fromkeys((*(resources or ()), *inherited)))
        if resources is not None:
            try:
                resources = TypeAdapter(GoverningResourceIds).validate_python(resources)
            except ValueError as error:
                raise KnowledgeValidationError("governing Resource envelope exceeds its canonical bounds") from error
            if not payload.project_id:
                raise KnowledgeValidationError("Resource-bound Memory requires an explicit project")
            catalog = getattr(self.authority, "resources", None)
            if catalog is None:
                raise KnowledgeAuthorizationError("canonical Resource Catalog is unavailable")
            project = SimpleNamespace(id=payload.project_id, organization_id=actor.organization_id,
                                      workspace_id=actor.workspace_id)
            bound = {item.id for item in catalog.project_resources(project, actor=actor)}
            if not set(resources).issubset(bound):
                raise KnowledgeAuthorizationError("governing Resource is not bound to the Memory project")
            if any((source.organization_id, source.workspace_id, source.project_id) !=
                   (actor.organization_id, actor.workspace_id, payload.project_id) for source in sources):
                raise KnowledgeValidationError("governing Memory sources must share the exact tenant/project")
            self._require_write(actor, project_id=payload.project_id, resource_ids=resources)
        return resources

    def create(
        self,
        payload: KnowledgeCreate,
        *,
        actor: AuthenticationActor,
    ) -> KnowledgeRecord:
        self._require_write(actor, project_id=payload.project_id, resource_ids=payload.resource_ids or ())
        payload = self._prepare_typed_content(payload, actor)
        self._validate_relationship_targets(
            payload.relationships,
            actor=actor,
        )
        payload = payload.model_copy(update={"provenance": self._derived_provenance(
            payload.provenance, payload.relationships, actor=actor, project_id=payload.project_id,
        )})
        payload = payload.model_copy(update={"resource_ids": self._governing_resources(payload, actor)})

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
            resource_ids=payload.resource_ids,
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
        self._index_upsert(record, actor=actor)
        return record

    def ingest(
        self,
        batch: KnowledgeIngestBatch,
        *,
        actor: AuthenticationActor,
    ) -> tuple[tuple[str, KnowledgeRecord], ...]:
        """Deterministically ingest normalized, provenance-bearing source snapshots.

        Source adapters are responsible for reading Git/code-host/provider data.
        This boundary owns canonical dedupe/versioning so retries never create a
        second memory identity for the same logical key.
        """

        results: list[tuple[str, KnowledgeRecord]] = []
        for payload in batch.items:
            self._require_write(actor, project_id=payload.project_id, resource_ids=payload.resource_ids or ())
            payload = self._prepare_typed_content(payload, actor)
            self._validate_relationship_targets(payload.relationships, actor=actor)
            payload = payload.model_copy(update={"resource_ids": self._governing_resources(payload, actor)})
            existing = [
                item
                for item in self.store.list(
                    organization_id=actor.organization_id,
                    workspace_id=actor.workspace_id,
                )
                if item.logical_key == payload.logical_key
            ]
            if not existing:
                results.append(("created", self.create(payload, actor=actor)))
                continue

            current = next(
                (
                    item
                    for item in existing
                    if item.lifecycle == KnowledgeLifecycle.CURRENT
                ),
                None,
            )
            if current is None:
                raise KnowledgeConflictError(
                    f"knowledge logical_key has no current revision: {payload.logical_key}"
                )
            incoming_digest = KnowledgeRecord.digest_payload(
                title=payload.title,
                summary=payload.summary,
                content=payload.content,
                canonical_refs=payload.canonical_refs,
                tags=payload.tags,
            )
            incoming_provenance = self._derived_provenance(
                payload.provenance, payload.relationships, actor=actor, project_id=payload.project_id, previous=current,
            )
            provenance_unchanged = (
                current.provenance.source_kind == payload.provenance.source_kind
                and current.provenance.source_ref == payload.provenance.source_ref
                and current.provenance.source_revision == payload.provenance.source_revision
                and current.provenance.origin == incoming_provenance.origin
                and current.provenance.review_status == payload.provenance.review_status
                and current.provenance.evidence_ids == payload.provenance.evidence_ids
                and set(payload.provenance.source_governance_record_ids).issubset(
                    current.provenance.source_governance_record_ids
                )
                and {row.target_knowledge_id for row in payload.relationships
                     if row.relationship_type == KnowledgeRelationshipType.DERIVED_FROM}
                == {row.target_knowledge_id for row in self.store.load().relationships
                    if row.source_knowledge_id == current.id
                    and row.relationship_type == KnowledgeRelationshipType.DERIVED_FROM}
            )
            metadata_unchanged = (
                current.object_type == payload.object_type
                and current.project_id == payload.project_id
                and current.resource_ids == payload.resource_ids
                and current.classification == payload.classification
                and current.retention_expires_at == payload.retention_expires_at
                and current.retention_action == payload.retention_action
                and current.deny_model_context == payload.deny_model_context
                and current.required_role_ids == payload.required_role_ids
                and current.review_after == payload.review_after
                and current.valid_until == payload.valid_until
            )
            if (
                current.content_sha256 == incoming_digest
                and provenance_unchanged
                and metadata_unchanged
            ):
                results.append(("unchanged", current))
                continue

            revised = self.revise(
                current.id,
                KnowledgeRevise(
                    title=payload.title,
                    summary=payload.summary,
                    content=payload.content,
                    tags=payload.tags,
                    canonical_refs=payload.canonical_refs,
                    relationships=payload.relationships,
                    resource_ids=payload.resource_ids,
                    provenance=payload.provenance,
                    classification=payload.classification,
                    retention_expires_at=payload.retention_expires_at,
                    retention_action=payload.retention_action,
                    deny_model_context=payload.deny_model_context,
                    required_role_ids=payload.required_role_ids,
                    review_after=payload.review_after,
                    valid_until=payload.valid_until,
                    reason=batch.reason,
                ),
                actor=actor,
            )
            results.append(("revised", revised))
        return tuple(results)

    def promote_procedure(
        self,
        payload: KnowledgeProcedurePromotion,
        *,
        actor: AuthenticationActor,
    ) -> KnowledgeRecord:
        """Create governed reusable procedure memory from independently verified sources."""

        sources = tuple(
            self.get(knowledge_id, actor=actor)
            for knowledge_id in payload.source_knowledge_ids
        )
        source_projects = {item.project_id for item in sources if item.project_id is not None}
        project_id = payload.project_id
        if project_id is None and len(source_projects) == 1:
            project_id = next(iter(source_projects))
        if project_id is not None and any(
            item.project_id not in {None, project_id} for item in sources
        ):
            raise KnowledgeValidationError(
                "procedure promotion sources must be company-scoped or match the target project"
            )

        classification = max(
            (item.classification for item in sources),
            key=lambda value: CLASSIFICATION_RANK[value],
        )
        expiry_values = [
            item.retention_expires_at
            for item in sources
            if item.retention_expires_at is not None
        ]
        retention_expires_at = min(expiry_values) if expiry_values else None
        retention_action = (
            GovernanceAction.DELETE
            if any(item.retention_action == GovernanceAction.DELETE for item in sources)
            else GovernanceAction.REDACT
        )
        required_roles = tuple(
            dict.fromkeys(
                role_id
                for item in sources
                for role_id in item.required_role_ids
            )
        )
        source_governance_ids = tuple(
            dict.fromkeys(item.governance_record_id for item in sources)
        )
        source_refs = tuple(
            KnowledgeCanonicalRef(
                object_type="organizational_memory",
                object_id=item.id,
                relation="derived_from",
            )
            for item in sources
        )
        relationships = tuple(
            KnowledgeRelationshipCreate(
                target_knowledge_id=item.id,
                relationship_type=KnowledgeRelationshipType.DERIVED_FROM,
                note="verified recurring solution promoted to reusable procedure",
            )
            for item in sources
        )
        promotion_ref = hashlib.sha256(
            (
                payload.logical_key
                + ":"
                + ":".join(payload.source_knowledge_ids)
                + ":"
                + ":".join(payload.evidence_ids)
            ).encode("utf-8")
        ).hexdigest()
        return self.create(
            KnowledgeCreate(
                logical_key=payload.logical_key,
                object_type=KnowledgeObjectType.PROCEDURE,
                title=payload.title,
                summary=payload.summary,
                content=payload.content,
                project_id=project_id,
                tags=tuple(dict.fromkeys((*payload.tags, "procedure", "known-pattern"))),
                canonical_refs=source_refs,
                relationships=relationships,
                provenance=KnowledgeProvenance(
                    source_kind=KnowledgeSourceKind.EVIDENCE,
                    source_ref=f"memory-procedure-promotion:{promotion_ref}",
                    authored_by=actor.identity_id,
                    observed_at=float(self.clock()),
                    evidence_ids=payload.evidence_ids,
                    source_governance_record_ids=source_governance_ids,
                ),
                classification=classification,
                retention_expires_at=retention_expires_at,
                retention_action=retention_action,
                deny_model_context=any(item.deny_model_context for item in sources),
                required_role_ids=required_roles,
            ),
            actor=actor,
        )

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
        self._require_write(actor, project_id=current.project_id, resource_ids=current.resource_ids or ())
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
            resource_ids=payload.resource_ids if payload.resource_ids is not None else current.resource_ids,
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
        normalized = self._prepare_typed_content(normalized, actor)
        self._validate_relationship_targets(
            normalized.relationships,
            actor=actor,
        )
        merged_provenance = self._derived_provenance(
            normalized.provenance,
            normalized.relationships,
            actor=actor, project_id=current.project_id,
            previous=current,
        )
        normalized = normalized.model_copy(update={"provenance": merged_provenance})
        resources = self._governing_resources(normalized, actor)
        if current.resource_ids is not None and (resources is None or not set(current.resource_ids).issubset(resources)):
            raise KnowledgeValidationError("Memory revision cannot narrow governing Resources")
        normalized = normalized.model_copy(update={"provenance": merged_provenance, "resource_ids": resources})

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
            resource_ids=normalized.resource_ids,
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
        self._index_upsert(self.store.get(current.id), actor=actor)
        self._index_upsert(next_record, actor=actor)
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
        self._require_write(actor, project_id=current.project_id, resource_ids=current.resource_ids or ())
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
        self._index_upsert(updated, actor=actor)
        return updated

    def validate_assignment_snapshot(self, row, *, actor):
        """Point-check canonical Memory provenance; a copied label is not proof."""
        item = self.get(row.get("knowledge_id", ""), actor=actor, context_only=True)
        if item.resource_ids is None:
            raise KnowledgeAuthorizationError("Memory governing Resource scope is unknown")
        expected = {
            "knowledge_id": item.id, "version": item.version,
            "content_sha256": item.content_sha256, "resource_ids": list(item.resource_ids),
            "project_id": item.project_id, "logical_key": item.logical_key,
            "object_type": item.object_type.value, "title": item.title, "summary": item.summary,
            "governance_record_id": item.governance_record_id,
            "classification": item.classification.value,
            "provenance": item.provenance.model_dump(mode="json"),
        }
        if any(row.get(key) != value for key, value in expected.items()):
            raise KnowledgeAuthorizationError("Memory snapshot provenance changed")
        source = self.context_header(item) + f"{item.title}\n\n{item.summary}\n\n{item.content}"
        excerpt = row.get("context_excerpt")
        suffix = "\n[…truncated by memory retrieval budget…]"
        canonical_excerpt = isinstance(excerpt, str) and (excerpt == source or (
            excerpt.endswith(suffix) and bool(excerpt[:-len(suffix)])
            and source.startswith(excerpt[:-len(suffix)]) and len(excerpt) < len(source)))
        if not canonical_excerpt:
            raise KnowledgeAuthorizationError("Memory snapshot content is not canonical")
        if self.freshness(item) != KnowledgeFreshness.FRESH:
            raise KnowledgeAuthorizationError("Memory snapshot is not current and fresh")
        allowed = self.governance.filter_context(ContextFilterRequest(
            record_ids=(item.governance_record_id,), max_classification=item.classification), actor=actor)
        if item.governance_record_id not in allowed.allowed_record_ids:
            raise KnowledgeAuthorizationError("Memory snapshot model context is denied")
        return item

    def get(
        self,
        knowledge_id: str,
        *,
        actor: AuthenticationActor,
        include_inactive: bool = False,
        context_only: bool = False,
    ) -> KnowledgeRecord:
        item = (self.store.get_for_context(knowledge_id,
                    organization_id=actor.organization_id, workspace_id=actor.workspace_id)
                if context_only else self.store.get(knowledge_id))
        if not self._same_scope(item, actor):
            raise KnowledgeNotFoundError(knowledge_id)
        allowed, reasons = self._can_read(
            actor,
            project_id=item.project_id,
            classification=item.classification,
            resource_ids=item.resource_ids or (),
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
        if not self._sources_readable(item, actor):
            raise KnowledgeAuthorizationError("derived research source read is denied")
        return item

    def list(
        self,
        *,
        actor: AuthenticationActor,
        project_id: str | None = None,
        include_inactive: bool = False,
    ) -> tuple[KnowledgeRecord, ...]:
        rows: list[KnowledgeRecord] = []
        authority_cache: dict[tuple[str | None, DataClassification, tuple[str, ...] | None], bool] = {}
        role_cache: dict[str | None, tuple[str, ...]] = {}
        candidates = self.store.list(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
        )
        record_map = {item.id: item for item in candidates}
        for item in candidates:
            if not self._sources_readable(item, actor, record_map):
                continue
            if project_id is not None and item.project_id != project_id:
                continue
            if not include_inactive and item.lifecycle != KnowledgeLifecycle.CURRENT:
                continue
            authority_key = (item.project_id, item.classification, item.resource_ids)
            if authority_key not in authority_cache:
                authority_cache[authority_key] = self._can_read(
                    actor,
                    project_id=item.project_id,
                    classification=item.classification,
                    resource_ids=item.resource_ids or (),
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

    def freshness(self, item: KnowledgeRecord) -> KnowledgeFreshness:
        return self._freshness(item)

    def retrieval_runs(
        self,
        *,
        actor: AuthenticationActor,
        limit: int = 100,
    ) -> tuple[KnowledgeRetrievalRun, ...]:
        allowed, reasons = self._can_read(
            actor,
            project_id=None,
            classification=DataClassification.INTERNAL,
        )
        if not allowed:
            raise KnowledgeAuthorizationError(
                "organizational memory retrieval inspection denied: "
                + "; ".join(reasons)
            )
        rows = [
            row
            for row in self.store.load().retrieval_runs
            if row.organization_id == actor.organization_id
            and row.workspace_id == actor.workspace_id
        ]
        rows.sort(key=lambda row: (row.created_at, row.id), reverse=True)
        return tuple(rows[: max(1, min(limit, 500))])

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
        context_projection: Callable[[KnowledgeRecord], str] | None = None,
        context_selection: KnowledgeContextSelection | None = None,
    ) -> KnowledgeRetrievalResult:
        if context_projection is not None and context_selection is None:
            raise KnowledgeValidationError("a context projection requires exact selection pins")
        if context_selection is not None and context_selection.max_context_tokens != query.budget.max_context_tokens:
            raise KnowledgeValidationError("selection and retrieval context budgets must agree")
        state = self.store.load()
        relationships = self._relationship_targets(state)
        record_map = {item.id: item for item in state.records}
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
            tuple[str | None, DataClassification, tuple[str, ...] | None],
            tuple[bool, tuple[str, ...]],
        ] = {}
        role_cache: dict[str | None, tuple[str, ...]] = {}
        lifecycle_filtered: list[tuple[KnowledgeRecord, KnowledgeFreshness]] = []

        for item in candidates:
            if query.resource_ids is not None:
                if item.resource_ids is None:
                    denied.append(KnowledgeDeniedCandidate(knowledge_id=item.id, reason="resource_scope:unknown"))
                    continue
                if not set(item.resource_ids).issubset(query.resource_ids):
                    denied.append(KnowledgeDeniedCandidate(knowledge_id=item.id, reason="resource_scope:outside_requested_resources"))
                    continue
            if not self._sources_readable(item, actor, record_map):
                denied.append(KnowledgeDeniedCandidate(knowledge_id=item.id, reason="authority:research_source_read_denied"))
                continue
            freshness = self._freshness(item, now=now, records=record_map)
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

            authority_key = (item.project_id, item.classification, item.resource_ids)
            if authority_key not in authority_cache:
                authority_cache[authority_key] = self._can_read(
                    actor,
                    project_id=item.project_id,
                    classification=item.classification,
                    resource_ids=item.resource_ids or (),
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

        for item, _freshness in lifecycle_filtered:
            if item.governance_record_id not in governance_allowed:
                continue
            brief_sources = [item] if item.object_type == KnowledgeObjectType.UX_BRIEF else []
            brief_sources.extend(record_map[ref.knowledge_id] for ref in item.provenance.origin_sources
                if ref.knowledge_id in record_map and record_map[ref.knowledge_id].object_type == KnowledgeObjectType.UX_BRIEF)
            if brief_sources:
                from codex_web.services.ux_brief_sources import brief_context_allowed
                if not all(brief_context_allowed(self, source, actor, query.max_classification) for source in brief_sources):
                    governance_allowed.discard(item.governance_record_id)
                    denied.append(KnowledgeDeniedCandidate(knowledge_id=item.id, reason="governance:brief_source_context_denied"))

        authorized_rows = [
            (item, freshness)
            for item, freshness in lifecycle_filtered
            if item.governance_record_id in governance_allowed
        ]
        canonical_by_id = {
            item.id: (item, freshness)
            for item, freshness in authorized_rows
        }
        retrieval_status = self._ensure_retrieval_index(actor=actor)
        retrieval_request = RetrievalSearchRequest(
            text=query.text,
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            allowed_knowledge_ids=tuple(canonical_by_id),
            project_ids=(
                ()
                if query.include_company_scope
                else query.project_ids
            ),
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
            retrieval_status = self.rebuild_retrieval_index(actor=actor)
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
            if context_selection is not None and (
                item.id != context_selection.knowledge_id or item.version != context_selection.version
                or item.content_sha256 != context_selection.sha256
            ):
                denied.append(KnowledgeDeniedCandidate(knowledge_id=item.id, reason="context_selection_revision_mismatch"))
                continue
            projected_context = (context_projection(item) if context_projection is not None
                else f"{item.title}\n\n{item.summary}\n\n{item.content}")
            if not projected_context.strip():
                continue
            source = self.context_header(item, include_source_lineage=context_projection is None) + projected_context
            allowance = min(remaining, per_item_soft_cap)
            if context_projection is not None and self._token_estimate(source) > allowance:
                denied.append(KnowledgeDeniedCandidate(knowledge_id=item.id, reason="context_projection_budget_exceeded"))
                continue
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
                    content_sha256=item.content_sha256,
                    logical_key=item.logical_key,
                    version=item.version,
                    object_type=item.object_type,
                    project_id=item.project_id,
                    resource_ids=item.resource_ids,
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
        if context_selection is not None:
            filters["context_selection"] = context_selection.model_dump(mode="json")
            query_digest = hashlib.sha256(json.dumps({"query": query.model_dump(mode="json"), "selection": filters["context_selection"]}, sort_keys=True).encode()).hexdigest()
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

        self.store.append_retrieval_run(run)
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
        *, cascade: bool = True,
    ) -> str:
        target_id = governed_record.object_id
        plan = research_governance_plan(self.governance, governed_record) if cascade else ()
        dependent_proof = [row for row in plan if row.object_type in {"artifact", "evidence"}]
        dependent_skills = [row for row in plan if row.object_type == "procedure_skill"]
        if dependent_skills and getattr(self, "procedure_outcomes", None) is None:
            raise KnowledgeValidationError("derived Skill governance adapter is unavailable")
        if dependent_proof and self.artifact_evidence is None:
            raise KnowledgeValidationError("derived Artifact/Evidence governance adapter is unavailable")
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
                        "provenance": item.provenance.model_copy(update={
                            "origin": KnowledgeOrigin.UNKNOWN,
                            "origin_sources": (),
                            "review_status": "unreviewed",
                            **({"source_ref":"[removed by governance]", "source_revision":None,
                                "source_url":None, "evidence_ids":(), "authored_by":None,
                                "authored_at":None} if item.logical_key.startswith("procedure-outcome/") else {}),
                        }),
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
        for dependent in dependent_proof:
            self.artifact_evidence.apply_governance_action(dependent, action, cascade=False)
        for dependent in dependent_skills:
            self.procedure_outcomes.erase(dependent, action)
        return (
            f"memory:{target_id}:{action.value}:"
            + ",".join(sorted(affected))
        )


def uuid_hash(*parts: str) -> str:
    return hashlib.sha256(
        ":".join(parts).encode("utf-8")
    ).hexdigest()[:32]
