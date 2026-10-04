from __future__ import annotations

import re
import time
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.skills import SKILL_MAX_INSTRUCTIONS_CHARS, SkillAsset, SkillCreate
from codex_web.definitions import DefinitionReference


class SkillSourceType(StrEnum):
    CATALOG_BUNDLE = "catalog_bundle"
    GITHUB_REPOSITORY = "github_repository"
    UI_SKILLS = "ui_skills"


class SkillSourceTransport(StrEnum):
    BUNDLE = "bundle"
    MCP = "mcp"
    CLI = "cli"
    REPOSITORY = "repository"


class SkillSourceTrust(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class SkillSourceLifecycle(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class SkillSourceHealth(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class SkillSourceImport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    upstream_id: str = Field(min_length=1, max_length=500)
    skill_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    record_id: str
    revision: int = Field(ge=1)
    source_revision: str
    upstream_digest: str
    imported_at: float
    upstream_categories: tuple[str, ...] = ()
    upstream_location: str | None = None
    source_transport: SkillSourceTransport = SkillSourceTransport.BUNDLE
    upstream_available: bool = True


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
    transport: SkillSourceTransport = SkillSourceTransport.BUNDLE
    trust: SkillSourceTrust = SkillSourceTrust.PENDING
    lifecycle: SkillSourceLifecycle = SkillSourceLifecycle.ACTIVE
    automatic_sync: bool = False
    auto_import_categories: tuple[str, ...] = ()
    created_by: str
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    last_sync_at: float | None = None
    last_sync_revision: str | None = None
    last_sync_status: str | None = None
    last_sync_error: str | None = None
    last_health_at: float | None = None
    health_status: str = "unknown"
    discovered_count: int = Field(default=0, ge=0)
    discovered_categories: tuple[str, ...] = ()
    imports: tuple[SkillSourceImport, ...] = ()

    @model_validator(mode="after")
    def validate_contract(self) -> "SkillSource":
        if self.schema_version != "1.0":
            raise ValueError("unsupported Skill source schema version")
        if self.source_type == SkillSourceType.GITHUB_REPOSITORY:
            if not re.fullmatch(
                r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?",
                self.location,
            ):
                raise ValueError(
                    "GitHub Skill source must be an exact public repository URL"
                )
        if self.source_type == SkillSourceType.UI_SKILLS:
            if self.location != "https://github.com/ibelick/ui-skills":
                raise ValueError(
                    "ui-skills source must use its canonical public repository URL"
                )
            if self.transport == SkillSourceTransport.BUNDLE:
                raise ValueError(
                    "ui-skills source requires MCP, CLI, or repository transport"
                )
        return self


class SkillSourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    name: str = Field(min_length=1, max_length=160)
    source_type: SkillSourceType = SkillSourceType.CATALOG_BUNDLE
    location: str = Field(min_length=1, max_length=1000)
    source_ref: str | None = Field(default=None, max_length=200)
    transport: SkillSourceTransport = SkillSourceTransport.BUNDLE
    trust: SkillSourceTrust = SkillSourceTrust.PENDING
    automatic_sync: bool = False
    auto_import_categories: tuple[str, ...] = Field(default=(), max_length=50)

    @model_validator(mode="after")
    def validate_location(self) -> "SkillSourceCreate":
        if self.source_type == SkillSourceType.GITHUB_REPOSITORY:
            if not re.fullmatch(
                r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?",
                self.location,
            ):
                raise ValueError(
                    "GitHub Skill source must be an exact public repository URL"
                )
        if self.source_type == SkillSourceType.UI_SKILLS:
            if self.location != "https://github.com/ibelick/ui-skills":
                raise ValueError(
                    "ui-skills source must use its canonical public repository URL"
                )
            if self.transport == SkillSourceTransport.BUNDLE:
                raise ValueError(
                    "ui-skills source requires MCP, CLI, or repository transport"
                )
        return self


class SkillSourceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=160)
    location: str | None = Field(default=None, min_length=1, max_length=1000)
    source_ref: str | None = Field(default=None, max_length=200)
    transport: SkillSourceTransport | None = None
    trust: SkillSourceTrust | None = None
    automatic_sync: bool | None = None
    auto_import_categories: tuple[str, ...] | None = Field(default=None, max_length=50)
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


class UiSkillsEntry(BaseModel):
    """Transport-neutral ui-skills item returned by a governed provider/worker."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    upstream_id: str = Field(min_length=1, max_length=500)
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=4000)
    instructions: str = Field(min_length=1, max_length=SKILL_MAX_INSTRUCTIONS_CHARS)
    categories: tuple[str, ...] = Field(default=(), max_length=50)
    tags: tuple[str, ...] = Field(default=(), max_length=50)
    upstream_location: str | None = Field(default=None, max_length=1000)
    assets: tuple[SkillAsset, ...] = ()

    @model_validator(mode="after")
    def validate_upstream_location(self) -> "UiSkillsEntry":
        if self.upstream_location and not self.upstream_location.startswith(
            "https://github.com/ibelick/ui-skills/"
        ):
            raise ValueError(
                "ui-skills item location must remain within the canonical repository"
            )
        return self


class UiSkillsDiscoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_revision: str = Field(min_length=1, max_length=200)
    transport: SkillSourceTransport
    entries: tuple[UiSkillsEntry, ...] = Field(max_length=5000)
    provider_evidence_id: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def validate_transport(self) -> "UiSkillsDiscoveryRequest":
        if self.transport == SkillSourceTransport.BUNDLE:
            raise ValueError(
                "ui-skills discovery requires MCP, CLI, or repository transport"
            )
        identifiers = [item.upstream_id for item in self.entries]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError(
                "ui-skills discovery contains duplicate upstream identifiers"
            )
        return self


class SkillSourceHealthReport(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    status: SkillSourceHealth
    provider_evidence_id: str = Field(min_length=1, max_length=300)
    error: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def validate_error(self) -> "SkillSourceHealthReport":
        if self.status != SkillSourceHealth.HEALTHY and not self.error:
            raise ValueError("non-healthy Skill source status requires an error")
        if self.status == SkillSourceHealth.HEALTHY and self.error:
            raise ValueError("healthy Skill source status cannot include an error")
        return self


class UiSkillsImportMode(StrEnum):
    SINGLE = "single"
    SELECTED = "selected"
    CATEGORY = "category"
    ALL = "all"


class UiSkillsImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: UiSkillsImportMode
    upstream_ids: tuple[str, ...] = Field(default=(), max_length=5000)
    category: str | None = Field(default=None, max_length=160)

    @model_validator(mode="after")
    def validate_selection(self) -> "UiSkillsImportRequest":
        if (
            self.mode in {UiSkillsImportMode.SINGLE, UiSkillsImportMode.SELECTED}
            and not self.upstream_ids
        ):
            raise ValueError("selected ui-skills import requires upstream identifiers")
        if self.mode == UiSkillsImportMode.SINGLE and len(self.upstream_ids) != 1:
            raise ValueError(
                "single ui-skills import requires exactly one upstream identifier"
            )
        if self.mode == UiSkillsImportMode.CATEGORY and not self.category:
            raise ValueError("category ui-skills import requires a category")
        return self


class ThreadSkillAssignmentsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill_refs: tuple[DefinitionReference, ...] = Field(default=(), max_length=50)
