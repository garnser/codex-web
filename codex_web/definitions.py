from __future__ import annotations

import hashlib
import json
import time
import uuid
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.compatibility import ContractSpec


DEFINITION_RECORD_CONTRACT = ContractSpec("definition-record", "1.0", ("1.0",))


class DefinitionScope(StrEnum):
    GLOBAL = "global"
    ORGANIZATION = "organization"
    WORKSPACE = "workspace"
    PROJECT = "project"


DEFINITION_SCOPE_PRECEDENCE: dict[DefinitionScope, int] = {
    DefinitionScope.GLOBAL: 0,
    DefinitionScope.ORGANIZATION: 1,
    DefinitionScope.WORKSPACE: 2,
    DefinitionScope.PROJECT: 3,
}


class DefinitionLifecycle(StrEnum):
    DRAFT = "draft"
    VALIDATED = "validated"
    PUBLISHED = "published"
    SUPERSEDED = "superseded"
    DEPRECATED = "deprecated"
    DISABLED = "disabled"
    QUARANTINED = "quarantined"


class DefinitionRecord(BaseModel):
    """One immutable revision of a mutable operational definition."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: str = DEFINITION_RECORD_CONTRACT.current
    record_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    definition_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    kind: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    definition_schema_version: str = Field(min_length=1)
    revision: int = Field(ge=1)
    scope_type: DefinitionScope = DefinitionScope.GLOBAL
    scope_id: str | None = None
    lifecycle: DefinitionLifecycle = DefinitionLifecycle.DRAFT
    payload: dict[str, Any]
    checksum: str = Field(min_length=64, max_length=64)
    created_by: str = Field(min_length=1)
    create_reason: str | None = None
    created_at: float = Field(default_factory=time.time)
    validated_by: str | None = None
    validated_at: float | None = None
    published_by: str | None = None
    publish_reason: str | None = None
    published_at: float | None = None
    effective_from: float | None = None
    effective_until: float | None = None
    supersedes_record_id: str | None = None
    superseded_by_record_id: str | None = None
    rollback_of_record_id: str | None = None
    approval_metadata: dict[str, str] = Field(default_factory=dict)
    min_engine_version: str | None = None
    max_engine_version: str | None = None

    @model_validator(mode="after")
    def validate_record(self) -> "DefinitionRecord":
        DEFINITION_RECORD_CONTRACT.require(self.schema_version)
        if self.scope_type == DefinitionScope.GLOBAL:
            self.scope_id = None
        elif not self.scope_id:
            raise ValueError(f"{self.scope_type.value} definition requires scope_id")
        expected = definition_checksum(
            definition_id=self.definition_id,
            kind=self.kind,
            definition_schema_version=self.definition_schema_version,
            payload=self.payload,
        )
        if self.checksum != expected:
            raise ValueError("definition checksum mismatch")
        if (
            self.effective_from is not None
            and self.effective_until is not None
            and self.effective_until <= self.effective_from
        ):
            raise ValueError("definition effective_until must be after effective_from")
        return self


class DefinitionContext(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    organization_id: str | None = None
    workspace_id: str | None = None
    project_id: str | None = None

    def scope_ids(self) -> dict[DefinitionScope, str | None]:
        return {
            DefinitionScope.GLOBAL: None,
            DefinitionScope.ORGANIZATION: self.organization_id,
            DefinitionScope.WORKSPACE: self.workspace_id,
            DefinitionScope.PROJECT: self.project_id,
        }


class DefinitionDraftCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    definition_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    definition_schema_version: str = Field(min_length=1)
    scope_type: DefinitionScope = DefinitionScope.GLOBAL
    scope_id: str | None = None
    payload: dict[str, Any]
    actor: str = Field(min_length=1)
    reason: str | None = None
    effective_from: float | None = None
    effective_until: float | None = None
    min_engine_version: str | None = None
    max_engine_version: str | None = None


class DefinitionPublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    actor: str = Field(min_length=1)
    reason: str | None = None
    expected_active_revision: int | None = None
    approval_metadata: dict[str, str] = Field(default_factory=dict)


class DefinitionRollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    definition_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    scope_type: DefinitionScope = DefinitionScope.GLOBAL
    scope_id: str | None = None
    target_revision: int = Field(ge=1)
    actor: str = Field(min_length=1)
    reason: str | None = None
    expected_active_revision: int | None = None


class DefinitionReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    definition_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    revision: int = Field(ge=1)
    record_id: str = Field(min_length=1)
    checksum: str = Field(min_length=64, max_length=64)
    definition_schema_version: str = Field(min_length=1)


def definition_checksum(
    *,
    definition_id: str,
    kind: str,
    definition_schema_version: str,
    payload: dict[str, Any],
) -> str:
    canonical = json.dumps(
        {
            "definition_id": definition_id,
            "kind": kind,
            "definition_schema_version": definition_schema_version,
            "payload": payload,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def definition_is_effective(record: DefinitionRecord, *, now: float | None = None) -> bool:
    current = time.time() if now is None else now
    if record.lifecycle != DefinitionLifecycle.PUBLISHED:
        return False
    if record.effective_from is not None and current < record.effective_from:
        return False
    if record.effective_until is not None and current >= record.effective_until:
        return False
    return True


def reference_for(record: DefinitionRecord) -> DefinitionReference:
    return DefinitionReference(
        definition_id=record.definition_id,
        kind=record.kind,
        revision=record.revision,
        record_id=record.record_id,
        checksum=record.checksum,
        definition_schema_version=record.definition_schema_version,
    )
