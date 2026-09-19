from __future__ import annotations

import hashlib
import math
import re
import threading
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EmbeddingModelIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    provider_id: str = Field(min_length=1, max_length=200)
    model_id: str = Field(min_length=1, max_length=500)
    model_revision: str = Field(min_length=1, max_length=500)
    dimensions: int = Field(ge=8, le=65536)
    capability_revision: int = Field(default=1, ge=1)
    local: bool = True
    residency_tags: tuple[str, ...] = ()


class EmbeddingBatchResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    identity: EmbeddingModelIdentity
    vectors: tuple[tuple[float, ...], ...]
    input_tokens: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0.0)

    @model_validator(mode="after")
    def validate_vectors(self) -> "EmbeddingBatchResult":
        for vector in self.vectors:
            if len(vector) != self.identity.dimensions:
                raise ValueError("embedding vector dimensions do not match identity")
        return self


@runtime_checkable
class EmbeddingProvider(Protocol):
    @property
    def identity(self) -> EmbeddingModelIdentity: ...

    def embed(self, texts: tuple[str, ...]) -> EmbeddingBatchResult: ...


class DeterministicLocalEmbeddingProvider:
    """Offline deterministic embedding provider for the default local index.

    The identity uses the same provider/model/revision vocabulary as the model
    registry without creating a second provider registry. Deployments may
    replace this provider with a governed adapter resolved from their canonical
    model/provider configuration.
    """

    def __init__(
        self,
        *,
        provider_id: str = "local",
        model_id: str = "concept-hash",
        model_revision: str = "1",
        dimensions: int = 128,
        capability_revision: int = 1,
        local: bool = True,
        residency_tags: tuple[str, ...] = (),
    ) -> None:
        self._identity = EmbeddingModelIdentity(
            provider_id=provider_id,
            model_id=model_id,
            model_revision=model_revision,
            dimensions=dimensions,
            capability_revision=capability_revision,
            local=local,
            residency_tags=residency_tags,
        )

    @property
    def identity(self) -> EmbeddingModelIdentity:
        return self._identity

    @staticmethod
    def _tokens(text: str) -> tuple[str, ...]:
        aliases = {
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
        raw = re.findall(r"[a-z0-9][a-z0-9_.:/-]*", text.lower())
        tokens: list[str] = []
        for token in raw:
            normalized = aliases.get(token, token)
            tokens.append(f"w:{normalized}")
            core = normalized.replace("-", "")
            if len(core) >= 5:
                tokens.extend(
                    f"g:{core[index:index + 3]}"
                    for index in range(len(core) - 2)
                )
        return tuple(tokens)

    def _encode(self, text: str) -> tuple[float, ...]:
        vector = [0.0] * self.identity.dimensions
        for token in self._tokens(text):
            digest = hashlib.blake2b(
                token.encode("utf-8"),
                digest_size=8,
            ).digest()
            value = int.from_bytes(digest, "big")
            index = value % self.identity.dimensions
            sign = -1.0 if value & 1 else 1.0
            vector[index] += sign
        magnitude = math.sqrt(sum(value * value for value in vector))
        if not magnitude:
            return tuple(vector)
        return tuple(value / magnitude for value in vector)

    def embed(self, texts: tuple[str, ...]) -> EmbeddingBatchResult:
        return EmbeddingBatchResult(
            identity=self.identity,
            vectors=tuple(self._encode(text) for text in texts),
            input_tokens=sum(max(1, (len(text) + 3) // 4) for text in texts),
            cost_usd=0.0,
        )


class RetrievalIndexDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    knowledge_id: str = Field(min_length=1)
    canonical_version: int = Field(ge=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    project_id: str | None = None
    object_type: str = Field(min_length=1)
    lifecycle: str = Field(min_length=1)
    title: str
    summary: str
    content: str
    tags: tuple[str, ...] = ()
    indexed_text: str


class RetrievalSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = ""
    organization_id: str
    workspace_id: str
    allowed_knowledge_ids: tuple[str, ...]
    project_ids: tuple[str, ...] = ()
    object_types: tuple[str, ...] = ()
    lifecycles: tuple[str, ...] = ()
    limit: int = Field(default=100, ge=1, le=500)


class RetrievalSearchHit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    knowledge_id: str
    canonical_version: int
    content_sha256: str
    score: float = Field(ge=0.0)
    lexical_score: float = Field(default=0.0, ge=0.0)
    vector_score: float = Field(default=0.0, ge=0.0)
    reasons: tuple[str, ...] = ()
    index_revision: str
    embedding_identity: EmbeddingModelIdentity | None = None


class RetrievalBackendStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    backend_id: str
    backend_type: str
    capabilities: tuple[str, ...]
    index_revision: str
    document_count: int = Field(ge=0)
    healthy: bool
    last_error: str | None = None
    embedding_identity: EmbeddingModelIdentity | None = None


@runtime_checkable
class RetrievalBackend(Protocol):
    backend_id: str

    def status(self) -> RetrievalBackendStatus: ...

    def upsert(self, document: RetrievalIndexDocument) -> None: ...

    def delete(self, knowledge_id: str) -> None: ...

    def rebuild(self, documents: tuple[RetrievalIndexDocument, ...]) -> None: ...

    def search(self, request: RetrievalSearchRequest) -> tuple[RetrievalSearchHit, ...]: ...


class _LocalRetrievalBase:
    def __init__(self, *, backend_id: str, backend_type: str) -> None:
        self.backend_id = backend_id
        self.backend_type = backend_type
        self._documents: dict[str, RetrievalIndexDocument] = {}
        self._revision = 0
        self._last_error: str | None = None
        self._lock = threading.RLock()

    def _bump(self) -> None:
        self._revision += 1
        self._last_error = None

    def _revision_id(self) -> str:
        return f"{self.backend_id}:{self._revision}"

    def upsert(self, document: RetrievalIndexDocument) -> None:
        with self._lock:
            self._documents[document.knowledge_id] = document
            self._after_upsert(document)
            self._bump()

    def delete(self, knowledge_id: str) -> None:
        with self._lock:
            self._documents.pop(knowledge_id, None)
            self._after_delete(knowledge_id)
            self._bump()

    def rebuild(self, documents: tuple[RetrievalIndexDocument, ...]) -> None:
        with self._lock:
            self._documents = {
                item.knowledge_id: item
                for item in documents
            }
            self._after_rebuild(documents)
            self._bump()

    def _after_upsert(self, document: RetrievalIndexDocument) -> None:
        return None

    def _after_delete(self, knowledge_id: str) -> None:
        return None

    def _after_rebuild(self, documents: tuple[RetrievalIndexDocument, ...]) -> None:
        return None

    @staticmethod
    def _lexical_terms(text: str) -> set[str]:
        return set(re.findall(r"[a-z0-9][a-z0-9_.:/-]*", text.lower()))

    @classmethod
    def _lexical_score(cls, query: str, text: str) -> float:
        left = cls._lexical_terms(query)
        right = cls._lexical_terms(text)
        if not left:
            return 1.0
        if not right:
            return 0.0
        overlap = len(left & right)
        if not overlap:
            return 0.0
        coverage = overlap / len(left)
        precision = overlap / len(right)
        return max(0.0, min(1.0, 0.85 * coverage + 0.15 * precision))

    @staticmethod
    def _eligible(
        document: RetrievalIndexDocument,
        request: RetrievalSearchRequest,
    ) -> bool:
        if document.organization_id != request.organization_id:
            return False
        if document.workspace_id != request.workspace_id:
            return False
        if document.knowledge_id not in set(request.allowed_knowledge_ids):
            return False
        if request.project_ids and document.project_id not in request.project_ids:
            return False
        if request.object_types and document.object_type not in request.object_types:
            return False
        if request.lifecycles and document.lifecycle not in request.lifecycles:
            return False
        return True


class LocalLexicalRetrievalBackend(_LocalRetrievalBase):
    """Rebuildable local lexical index with no model dependency."""

    def __init__(self, *, backend_id: str = "local-lexical") -> None:
        super().__init__(backend_id=backend_id, backend_type="local_lexical")

    def status(self) -> RetrievalBackendStatus:
        with self._lock:
            return RetrievalBackendStatus(
                backend_id=self.backend_id,
                backend_type=self.backend_type,
                capabilities=("lexical", "structured", "rebuild"),
                index_revision=self._revision_id(),
                document_count=len(self._documents),
                healthy=True,
            )

    def search(self, request: RetrievalSearchRequest) -> tuple[RetrievalSearchHit, ...]:
        with self._lock:
            rows: list[RetrievalSearchHit] = []
            revision = self._revision_id()
            allowed = set(request.allowed_knowledge_ids)
            for item in self._documents.values():
                if item.knowledge_id not in allowed or not self._eligible(item, request):
                    continue
                lexical = self._lexical_score(request.text, item.indexed_text)
                if request.text and lexical <= 0:
                    continue
                rows.append(
                    RetrievalSearchHit(
                        knowledge_id=item.knowledge_id,
                        canonical_version=item.canonical_version,
                        content_sha256=item.content_sha256,
                        score=lexical,
                        lexical_score=lexical,
                        vector_score=0.0,
                        reasons=(f"lexical:{lexical:.3f}",) if request.text else ("structured",),
                        index_revision=revision,
                    )
                )
            rows.sort(
                key=lambda row: (
                    row.score,
                    row.canonical_version,
                    row.knowledge_id,
                ),
                reverse=True,
            )
            return tuple(rows[: request.limit])


class LocalVectorRetrievalBackend(_LocalRetrievalBase):
    """Local vector-capable backend with versioned embedding identity."""

    def __init__(
        self,
        embedding_provider: EmbeddingProvider | None = None,
        *,
        backend_id: str = "local-vector",
    ) -> None:
        super().__init__(backend_id=backend_id, backend_type="local_vector")
        self.embedding_provider = (
            embedding_provider or DeterministicLocalEmbeddingProvider()
        )
        self._vectors: dict[str, tuple[float, ...]] = {}
        self._indexed_identity: EmbeddingModelIdentity | None = None

    def _after_upsert(self, document: RetrievalIndexDocument) -> None:
        result = self.embedding_provider.embed((document.indexed_text,))
        self._vectors[document.knowledge_id] = result.vectors[0]
        self._indexed_identity = result.identity

    def _after_delete(self, knowledge_id: str) -> None:
        self._vectors.pop(knowledge_id, None)

    def _after_rebuild(self, documents: tuple[RetrievalIndexDocument, ...]) -> None:
        if not documents:
            self._vectors = {}
            self._indexed_identity = self.embedding_provider.identity
            return
        result = self.embedding_provider.embed(
            tuple(item.indexed_text for item in documents)
        )
        self._vectors = {
            item.knowledge_id: vector
            for item, vector in zip(documents, result.vectors)
        }
        self._indexed_identity = result.identity

    @staticmethod
    def _similarity(
        left: tuple[float, ...],
        right: tuple[float, ...],
    ) -> float:
        if not left or not right or len(left) != len(right):
            return 0.0
        return max(
            0.0,
            min(1.0, sum(a * b for a, b in zip(left, right))),
        )

    def status(self) -> RetrievalBackendStatus:
        with self._lock:
            current_identity = self.embedding_provider.identity
            identity_matches = (
                not self._documents
                or self._indexed_identity == current_identity
            )
            return RetrievalBackendStatus(
                backend_id=self.backend_id,
                backend_type=self.backend_type,
                capabilities=("lexical", "vector", "structured", "rebuild"),
                index_revision=self._revision_id(),
                document_count=len(self._documents),
                healthy=identity_matches,
                last_error=(
                    None
                    if identity_matches
                    else "embedding model identity changed; rebuild required"
                ),
                embedding_identity=current_identity,
            )

    def search(self, request: RetrievalSearchRequest) -> tuple[RetrievalSearchHit, ...]:
        query_vector = (
            self.embedding_provider.embed((request.text,)).vectors[0]
            if request.text
            else ()
        )
        with self._lock:
            rows: list[RetrievalSearchHit] = []
            revision = self._revision_id()
            allowed = set(request.allowed_knowledge_ids)
            identity = self.embedding_provider.identity
            for item in self._documents.values():
                if item.knowledge_id not in allowed or not self._eligible(item, request):
                    continue
                lexical = self._lexical_score(request.text, item.indexed_text)
                vector = (
                    self._similarity(
                        query_vector,
                        self._vectors.get(item.knowledge_id, ()),
                    )
                    if request.text
                    else 1.0
                )
                score = (
                    0.72 * vector + 0.28 * lexical
                    if request.text
                    else 1.0
                )
                if request.text and score <= 0:
                    continue
                reasons = (
                    (
                        f"semantic:{vector:.3f}",
                        f"lexical:{lexical:.3f}",
                    )
                    if request.text
                    else ("structured",)
                )
                rows.append(
                    RetrievalSearchHit(
                        knowledge_id=item.knowledge_id,
                        canonical_version=item.canonical_version,
                        content_sha256=item.content_sha256,
                        score=score,
                        lexical_score=lexical,
                        vector_score=vector,
                        reasons=reasons,
                        index_revision=revision,
                        embedding_identity=identity,
                    )
                )
            rows.sort(
                key=lambda row: (
                    row.score,
                    row.canonical_version,
                    row.knowledge_id,
                ),
                reverse=True,
            )
            return tuple(rows[: request.limit])
