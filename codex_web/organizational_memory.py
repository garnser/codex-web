from __future__ import annotations

import hashlib
import json
import time
import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.compatibility import ContractSpec
from codex_web.data_governance import (
    DataClassification,
    GovernanceAction,
)


ORGANIZATIONAL_MEMORY_CONTRACT = ContractSpec(
    "organizational-memory-state",
    "1.0",
    ("1.0",),
)

MAX_MEMORY_RELATIONSHIPS = 100
MAX_MEMORY_CANONICAL_REFS = 100
MAX_MEMORY_TAGS = 50
MAX_MEMORY_QUERY_TOP_K = 50
MAX_MEMORY_QUERY_CANDIDATES = 500
MAX_MEMORY_CONTEXT_TOKENS = 32000


class KnowledgeObjectType(StrEnum):
    ARCHITECTURE_DECISION = "architecture_decision"
    POLICY = "policy"
    PRODUCT = "product"
    REPOSITORY = "repository"
    SERVICE = "service"
    INCIDENT = "incident"
    POSTMORTEM = "postmortem"
    CUSTOMER_ACCOUNT = "customer_account"
    PROJECT = "project"
    GOAL = "goal"
    DECISION = "decision"
    PROCEDURE = "procedure"
    OTHER = "other"


class KnowledgeLifecycle(StrEnum):
    CURRENT = "current"
    SUPERSEDED = "superseded"
    INVALID = "invalid"
    REDACTED = "redacted"
    DELETED = "deleted"


class KnowledgeFreshness(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"
    INVALID = "invalid"
    REDACTED = "redacted"
    DELETED = "deleted"


class KnowledgeRelationshipType(StrEnum):
    RELATES_TO = "relates_to"
    DERIVED_FROM = "derived_from"
    SUPERSEDES = "supersedes"
    IMPLEMENTS = "implements"
    GOVERNS = "governs"
    DOCUMENTS = "documents"
    AFFECTS = "affects"
    CAUSED_BY = "caused_by"
    RESOLVES = "resolves"


class KnowledgeSourceKind(StrEnum):
    MANUAL = "manual"
    REPOSITORY = "repository"
    PULL_REQUEST = "pull_request"
    ISSUE = "issue"
    ARTIFACT = "artifact"
    EVIDENCE = "evidence"
    GOAL = "goal"
    DECISION = "decision"
    INCIDENT = "incident"
    EXTERNAL_RECORD = "external_record"
    IMPORT = "import"
    OTHER = "other"


class KnowledgeCanonicalRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    object_type: str = Field(min_length=1, max_length=100)
    object_id: str = Field(min_length=1, max_length=1000)
    relation: str = Field(default="references", min_length=1, max_length=100)


class KnowledgeProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    source_kind: KnowledgeSourceKind
    source_ref: str = Field(min_length=1, max_length=2000)
    source_url: str | None = Field(default=None, max_length=4000)
    source_revision: str | None = Field(default=None, max_length=1000)
    authored_by: str | None = Field(default=None, max_length=1000)
    authored_at: float | None = None
    observed_at: float = Field(default_factory=time.time)
    evidence_ids: tuple[str, ...] = ()
    source_governance_record_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def normalize(self) -> "KnowledgeProvenance":
        object.__setattr__(
            self,
            "evidence_ids",
            tuple(dict.fromkeys(self.evidence_ids)),
        )
        object.__setattr__(
            self,
            "source_governance_record_ids",
            tuple(dict.fromkeys(self.source_governance_record_ids)),
        )
        return self


class KnowledgeRelationshipCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    target_knowledge_id: str = Field(min_length=1, max_length=1000)
    relationship_type: KnowledgeRelationshipType
    note: str | None = Field(default=None, max_length=4000)


class KnowledgeRelationship(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    id: str = Field(default_factory=lambda: f"knowledge-rel-{uuid.uuid4().hex}")
    organization_id: str
    workspace_id: str
    source_knowledge_id: str
    target_knowledge_id: str
    relationship_type: KnowledgeRelationshipType
    note: str | None = None
    created_by: str
    created_at: float = Field(default_factory=time.time)


class KnowledgeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    logical_key: str = Field(
        min_length=1,
        max_length=500,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$",
    )
    object_type: KnowledgeObjectType
    title: str = Field(min_length=1, max_length=1000)
    summary: str = Field(min_length=1, max_length=8000)
    content: str = Field(min_length=1, max_length=200000)
    project_id: str | None = Field(default=None, max_length=500)
    tags: tuple[str, ...] = ()
    canonical_refs: tuple[KnowledgeCanonicalRef, ...] = ()
    relationships: tuple[KnowledgeRelationshipCreate, ...] = ()
    provenance: KnowledgeProvenance
    classification: DataClassification = DataClassification.INTERNAL
    retention_expires_at: float | None = None
    retention_action: GovernanceAction = GovernanceAction.REDACT
    deny_model_context: bool = False
    required_role_ids: tuple[str, ...] = ()
    review_after: float | None = None
    valid_until: float | None = None

    @model_validator(mode="after")
    def normalize(self) -> "KnowledgeCreate":
        self.tags = tuple(
            dict.fromkeys(
                value.strip().lower()
                for value in self.tags
                if value.strip()
            )
        )
        self.required_role_ids = tuple(
            dict.fromkeys(
                value.strip()
                for value in self.required_role_ids
                if value.strip()
            )
        )
        self.canonical_refs = tuple(dict.fromkeys(self.canonical_refs))
        if len(self.tags) > MAX_MEMORY_TAGS:
            raise ValueError(f"knowledge supports at most {MAX_MEMORY_TAGS} tags")
        if len(self.canonical_refs) > MAX_MEMORY_CANONICAL_REFS:
            raise ValueError(
                f"knowledge supports at most {MAX_MEMORY_CANONICAL_REFS} canonical references"
            )
        if len(self.relationships) > MAX_MEMORY_RELATIONSHIPS:
            raise ValueError(
                f"knowledge supports at most {MAX_MEMORY_RELATIONSHIPS} relationships"
            )
        if self.review_after is not None and self.review_after <= 0:
            raise ValueError("review_after must be a positive timestamp")
        if self.valid_until is not None and self.valid_until <= 0:
            raise ValueError("valid_until must be a positive timestamp")
        if (
            self.review_after is not None
            and self.valid_until is not None
            and self.valid_until < self.review_after
        ):
            raise ValueError("valid_until must be >= review_after")
        return self


class KnowledgeRevise(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str | None = Field(default=None, min_length=1, max_length=1000)
    summary: str | None = Field(default=None, min_length=1, max_length=8000)
    content: str | None = Field(default=None, min_length=1, max_length=200000)
    tags: tuple[str, ...] | None = None
    canonical_refs: tuple[KnowledgeCanonicalRef, ...] | None = None
    relationships: tuple[KnowledgeRelationshipCreate, ...] | None = None
    provenance: KnowledgeProvenance
    classification: DataClassification | None = None
    retention_expires_at: float | None = None
    retention_action: GovernanceAction | None = None
    deny_model_context: bool | None = None
    required_role_ids: tuple[str, ...] | None = None
    review_after: float | None = None
    valid_until: float | None = None
    reason: str = Field(min_length=1, max_length=4000)


class KnowledgeInvalidate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: str = Field(min_length=1, max_length=4000)


class KnowledgeRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(default_factory=lambda: f"knowledge-{uuid.uuid4().hex}")
    organization_id: str
    workspace_id: str
    project_id: str | None = None
    logical_key: str
    version: int = Field(ge=1)
    object_type: KnowledgeObjectType
    title: str
    summary: str
    content: str
    tags: tuple[str, ...] = ()
    canonical_refs: tuple[KnowledgeCanonicalRef, ...] = ()
    provenance: KnowledgeProvenance
    classification: DataClassification
    governance_record_id: str
    retention_expires_at: float | None = None
    deny_model_context: bool = False
    required_role_ids: tuple[str, ...] = ()
    review_after: float | None = None
    valid_until: float | None = None
    lifecycle: KnowledgeLifecycle = KnowledgeLifecycle.CURRENT
    previous_version_id: str | None = None
    superseded_by_id: str | None = None
    invalid_reason: str | None = None
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_by: str
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    @staticmethod
    def digest_payload(
        *,
        title: str,
        summary: str,
        content: str,
        canonical_refs: tuple[KnowledgeCanonicalRef, ...],
        tags: tuple[str, ...],
    ) -> str:
        payload = {
            "title": title,
            "summary": summary,
            "content": content,
            "canonical_refs": [
                item.model_dump(mode="json") for item in canonical_refs
            ],
            "tags": tags,
        }
        raw = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


class KnowledgeEmbedding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    knowledge_id: str
    encoder_id: str
    dimensions: int = Field(ge=8, le=4096)
    vector: tuple[float, ...]
    indexed_at: float = Field(default_factory=time.time)

    @model_validator(mode="after")
    def validate_dimensions(self) -> "KnowledgeEmbedding":
        if len(self.vector) != self.dimensions:
            raise ValueError("embedding dimensions do not match vector")
        return self


class KnowledgeRetrievalBudget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    top_k: int = Field(default=8, ge=1, le=MAX_MEMORY_QUERY_TOP_K)
    candidate_limit: int = Field(
        default=100,
        ge=1,
        le=MAX_MEMORY_QUERY_CANDIDATES,
    )
    max_context_tokens: int = Field(
        default=6000,
        ge=128,
        le=MAX_MEMORY_CONTEXT_TOKENS,
    )
    progressive: bool = True


class KnowledgeQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    text: str = Field(default="", max_length=20000)
    object_types: tuple[KnowledgeObjectType, ...] = ()
    project_ids: tuple[str, ...] = ()
    logical_keys: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    relationship_target_ids: tuple[str, ...] = ()
    required_role_ids: tuple[str, ...] = ()
    authored_by: tuple[str, ...] = ()
    max_classification: DataClassification = DataClassification.CONFIDENTIAL
    include_stale: bool = False
    include_superseded: bool = False
    include_invalid: bool = False
    budget: KnowledgeRetrievalBudget = Field(
        default_factory=KnowledgeRetrievalBudget
    )

    @model_validator(mode="after")
    def normalize(self) -> "KnowledgeQuery":
        for field_name in (
            "project_ids",
            "logical_keys",
            "relationship_target_ids",
            "required_role_ids",
            "authored_by",
        ):
            setattr(
                self,
                field_name,
                tuple(dict.fromkeys(getattr(self, field_name))),
            )
        self.tags = tuple(
            dict.fromkeys(
                value.strip().lower()
                for value in self.tags
                if value.strip()
            )
        )
        return self


class KnowledgeRetrievalItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    knowledge_id: str
    logical_key: str
    version: int
    object_type: KnowledgeObjectType
    project_id: str | None = None
    title: str
    summary: str
    context_excerpt: str
    tags: tuple[str, ...] = ()
    canonical_refs: tuple[KnowledgeCanonicalRef, ...] = ()
    provenance: KnowledgeProvenance
    classification: DataClassification
    governance_record_id: str
    freshness: KnowledgeFreshness
    lifecycle: KnowledgeLifecycle
    score: float
    semantic_score: float
    lexical_score: float
    estimated_tokens: int
    reasons: tuple[str, ...] = ()


class KnowledgeDeniedCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    knowledge_id: str
    reason: str


class KnowledgeRetrievalRun(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: f"memory-retrieval-{uuid.uuid4().hex}")
    organization_id: str
    workspace_id: str
    actor_id: str
    query_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    filters: dict[str, Any]
    selected_knowledge_ids: tuple[str, ...]
    denied: tuple[KnowledgeDeniedCandidate, ...] = ()
    packed_tokens: int = Field(ge=0)
    candidate_count: int = Field(ge=0)
    top_k: int = Field(ge=1)
    max_context_tokens: int = Field(ge=1)
    created_at: float = Field(default_factory=time.time)


class KnowledgeRetrievalResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    retrieval_id: str
    items: tuple[KnowledgeRetrievalItem, ...]
    denied: tuple[KnowledgeDeniedCandidate, ...] = ()
    packed_tokens: int
    candidate_count: int
    top_k: int
    max_context_tokens: int


class OrganizationalMemoryState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = ORGANIZATIONAL_MEMORY_CONTRACT.current
    records: list[KnowledgeRecord] = Field(default_factory=list)
    relationships: list[KnowledgeRelationship] = Field(default_factory=list)
    embeddings: list[KnowledgeEmbedding] = Field(default_factory=list)
    retrieval_runs: list[KnowledgeRetrievalRun] = Field(default_factory=list)

    def model_post_init(self, __context: Any) -> None:
        ORGANIZATIONAL_MEMORY_CONTRACT.require(self.schema_version)
