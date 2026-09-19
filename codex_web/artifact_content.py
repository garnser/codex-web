from __future__ import annotations

from collections.abc import Iterable, Iterator
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ArtifactContentError(RuntimeError):
    pass


class ArtifactContentNotFoundError(ArtifactContentError):
    pass


class ArtifactContentAccessError(ArtifactContentError):
    pass


class ArtifactContentIntegrityError(ArtifactContentError):
    pass


class ArtifactContentCapability(StrEnum):
    PUT = "put"
    READ = "read"
    RANGE_READ = "range_read"
    HEAD = "head"
    VERIFY = "verify"
    DELETE = "delete"
    HEALTH = "health"


class ArtifactContentState(StrEnum):
    AVAILABLE = "available"
    TOMBSTONED = "tombstoned"


class ArtifactContentScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)


class ArtifactContentRange(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    start: int = Field(ge=0)
    end_exclusive: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_range(self) -> "ArtifactContentRange":
        if self.end_exclusive <= self.start:
            raise ValueError("content range end must be greater than start")
        return self


class ArtifactContentPointer(BaseModel):
    """Infrastructure reference persisted on the canonical Artifact record."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    backend_id: str = Field(min_length=1)
    locator: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    media_type: str | None = None
    state: ArtifactContentState = ArtifactContentState.AVAILABLE
    encryption_key_ref: str | None = None
    verified_at: float | None = None


class ArtifactContentWriteResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    backend_id: str
    locator: str
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_type: str | None = None
    created_at: float


class ArtifactContentHead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    backend_id: str
    locator: str
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_type: str | None = None
    tombstoned: bool = False
    modified_at: float | None = None


class ArtifactContentHealth(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    backend_id: str
    healthy: bool
    capabilities: tuple[ArtifactContentCapability, ...]
    detail: str | None = None


@runtime_checkable
class ArtifactContentStore(Protocol):
    backend_id: str

    def capabilities(self) -> frozenset[ArtifactContentCapability]:
        ...

    def put(
        self,
        scope: ArtifactContentScope,
        chunks: Iterable[bytes],
        *,
        media_type: str | None = None,
        expected_sha256: str | None = None,
    ) -> ArtifactContentWriteResult:
        ...

    def open(
        self,
        scope: ArtifactContentScope,
        locator: str,
        *,
        byte_range: ArtifactContentRange | None = None,
        chunk_size: int = 65536,
    ) -> Iterator[bytes]:
        ...

    def head(
        self,
        scope: ArtifactContentScope,
        locator: str,
    ) -> ArtifactContentHead:
        ...

    def verify(
        self,
        scope: ArtifactContentScope,
        locator: str,
        expected_sha256: str,
    ) -> ArtifactContentHead:
        ...

    def delete(
        self,
        scope: ArtifactContentScope,
        locator: str,
    ) -> ArtifactContentHead:
        ...

    def health(self) -> ArtifactContentHealth:
        ...
