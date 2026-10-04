from __future__ import annotations

import re
import time
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.skills import SkillCreate
from codex_web.definitions import DefinitionReference


class SkillSourceType(StrEnum):
    CATALOG_BUNDLE = "catalog_bundle"
    GITHUB_REPOSITORY = "github_repository"


class SkillSourceTrust(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class SkillSourceLifecycle(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class SkillSourceImport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    upstream_id: str = Field(min_length=1, max_length=500)
    skill_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    record_id: str
    revision: int = Field(ge=1)
    source_revision: str
    upstream_digest: str
    imported_at: float


class SkillSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    source_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    organization_id: str
    workspace_id: str
    name: str = Field(min_length=1, max_length=160)
    source_type: SkillSourceType
    location: str = Field(min_length=1, max_length=1000)
    source_ref: str | None = Field(default=None, max_length=200)
    trust: SkillSourceTrust = SkillSourceTrust.PENDING
    lifecycle: SkillSourceLifecycle = SkillSourceLifecycle.ACTIVE
    automatic_sync: bool = False
    created_by: str
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    last_sync_at: float | None = None
    last_sync_revision: str | None = None
    last_sync_status: str | None = None
    last_sync_error: str | None = None
    imports: tuple[SkillSourceImport, ...] = ()

    @model_validator(mode="after")
    def validate_contract(self) -> "SkillSource":
        if self.schema_version != "1.0":
            raise ValueError("unsupported Skill source schema version")
        if self.source_type == SkillSourceType.GITHUB_REPOSITORY:
            if not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?", self.location):
                raise ValueError("GitHub Skill source must be an exact public repository URL")
        return self


class SkillSourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    name: str = Field(min_length=1, max_length=160)
    source_type: SkillSourceType = SkillSourceType.CATALOG_BUNDLE
    location: str = Field(min_length=1, max_length=1000)
    source_ref: str | None = Field(default=None, max_length=200)
    trust: SkillSourceTrust = SkillSourceTrust.PENDING
    automatic_sync: bool = False

    @model_validator(mode="after")
    def validate_location(self) -> "SkillSourceCreate":
        if self.source_type == SkillSourceType.GITHUB_REPOSITORY:
            if not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?", self.location):
                raise ValueError("GitHub Skill source must be an exact public repository URL")
        return self


class SkillSourceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=160)
    location: str | None = Field(default=None, min_length=1, max_length=1000)
    source_ref: str | None = Field(default=None, max_length=200)
    trust: SkillSourceTrust | None = None
    automatic_sync: bool | None = None
    lifecycle: SkillSourceLifecycle | None = None


class SkillCatalogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    upstream_id: str = Field(min_length=1, max_length=500)
    manifest: SkillCreate


class SkillSourceSyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_revision: str = Field(min_length=1, max_length=200)
    entries: tuple[SkillCatalogEntry, ...] = Field(max_length=5000)

    @model_validator(mode="after")
    def unique_entries(self) -> "SkillSourceSyncRequest":
        upstream = [item.upstream_id for item in self.entries]
        skill_ids = [item.manifest.skill_id for item in self.entries]
        if len(upstream) != len(set(upstream)):
            raise ValueError("catalog contains duplicate upstream identifiers")
        if len(skill_ids) != len(set(skill_ids)):
            raise ValueError("catalog contains duplicate canonical Skill IDs")
        return self


class ThreadSkillAssignmentsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill_refs: tuple[DefinitionReference, ...] = Field(default=(), max_length=50)
