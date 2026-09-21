from __future__ import annotations

import re
import time
from enum import StrEnum
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.agent_providers import AgentProviderCapability
from codex_web.execution_workers import WorkerCapability


SKILL_DEFINITION_KIND = "agent.skill"
SKILL_DEFINITION_SCHEMA_VERSION = "1.0"
SKILL_MAX_INSTRUCTIONS_CHARS = 32_000
SKILL_MAX_ASSETS = 32
SKILL_MAX_ASSET_CHARS = 64_000
SKILL_MAX_TOTAL_ASSET_CHARS = 256_000
SKILL_MAX_CONTEXT_CHARS = 48_000


class SkillLifecycle(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class SkillAssetKind(StrEnum):
    REFERENCE = "reference"
    TEMPLATE = "template"
    HELPER_SCRIPT = "helper_script"


class SkillAssetContextMode(StrEnum):
    ALWAYS = "always"
    RELEVANT = "relevant"
    NEVER = "never"


class SkillAssetSecurityClass(StrEnum):
    UNTRUSTED_REFERENCE = "untrusted_reference"
    TEMPLATE = "template"
    EXECUTABLE_UNTRUSTED = "executable_untrusted"


_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
)


def _contains_secret_material(value: str) -> bool:
    return any(pattern.search(value) for pattern in _SECRET_PATTERNS)


def _normalized_words(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            normalized
            for value in values
            if (normalized := str(value).strip().casefold())
        )
    )


class SkillAsset(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    path: str = Field(min_length=1, max_length=240)
    kind: SkillAssetKind
    media_type: str = Field(default="text/plain", min_length=1, max_length=120)
    content: str = Field(max_length=SKILL_MAX_ASSET_CHARS)
    tags: tuple[str, ...] = Field(default=(), max_length=24)
    context_mode: SkillAssetContextMode = SkillAssetContextMode.RELEVANT
    security_class: SkillAssetSecurityClass

    @model_validator(mode="after")
    def validate_asset(self) -> "SkillAsset":
        if "\x00" in self.path or "\\" in self.path:
            raise ValueError("skill asset path must be a safe POSIX relative path")
        path = PurePosixPath(self.path)
        if path.is_absolute() or ".." in path.parts or "." in path.parts:
            raise ValueError("skill asset path must remain within the skill bundle")
        if self.kind == SkillAssetKind.HELPER_SCRIPT:
            if self.security_class != SkillAssetSecurityClass.EXECUTABLE_UNTRUSTED:
                raise ValueError(
                    "helper scripts must be classified executable_untrusted"
                )
            if self.context_mode != SkillAssetContextMode.NEVER:
                raise ValueError(
                    "helper scripts cannot be injected into model context automatically"
                )
        elif self.kind == SkillAssetKind.TEMPLATE:
            if self.security_class != SkillAssetSecurityClass.TEMPLATE:
                raise ValueError("template assets must use template security class")
        elif self.security_class != SkillAssetSecurityClass.UNTRUSTED_REFERENCE:
            raise ValueError(
                "reference assets must use untrusted_reference security class"
            )
        if _contains_secret_material(self.content):
            raise ValueError("skill asset appears to contain raw secret material")
        object.__setattr__(self, "tags", _normalized_words(self.tags))
        return self


class SkillProvenance(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    source_type: str = Field(default="manual", min_length=1, max_length=80)
    source_ref: str | None = Field(default=None, max_length=500)
    evidence_ids: tuple[str, ...] = Field(default=(), max_length=50)
    imported_at: float | None = None

    @model_validator(mode="after")
    def normalize(self) -> "SkillProvenance":
        object.__setattr__(
            self,
            "evidence_ids",
            tuple(
                dict.fromkeys(
                    str(value).strip()
                    for value in self.evidence_ids
                    if str(value).strip()
                )
            ),
        )
        return self


class SkillDefinition(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=4000)
    instructions: str = Field(min_length=1, max_length=SKILL_MAX_INSTRUCTIONS_CHARS)
    applicability_tags: tuple[str, ...] = Field(default=(), max_length=50)
    capability_tags: tuple[str, ...] = Field(default=(), max_length=50)
    assets: tuple[SkillAsset, ...] = Field(default=(), max_length=SKILL_MAX_ASSETS)
    required_provider_capabilities: tuple[AgentProviderCapability, ...] = ()
    required_worker_capabilities: tuple[WorkerCapability, ...] = ()
    input_expectations: tuple[str, ...] = Field(default=(), max_length=30)
    output_expectations: tuple[str, ...] = Field(default=(), max_length=30)
    owner_identity_id: str = Field(min_length=1, max_length=300)
    lifecycle: SkillLifecycle = SkillLifecycle.ACTIVE
    provenance: SkillProvenance = Field(default_factory=SkillProvenance)

    @model_validator(mode="after")
    def normalize(self) -> "SkillDefinition":
        if _contains_secret_material(self.instructions):
            raise ValueError("skill instructions appear to contain raw secret material")
        if sum(len(asset.content) for asset in self.assets) > SKILL_MAX_TOTAL_ASSET_CHARS:
            raise ValueError("skill assets exceed the total content size limit")
        paths = [asset.path for asset in self.assets]
        if len(paths) != len(set(paths)):
            raise ValueError("skill asset paths must be unique")
        object.__setattr__(
            self,
            "applicability_tags",
            _normalized_words(self.applicability_tags),
        )
        object.__setattr__(
            self,
            "capability_tags",
            _normalized_words(self.capability_tags),
        )
        object.__setattr__(
            self,
            "required_provider_capabilities",
            tuple(dict.fromkeys(self.required_provider_capabilities)),
        )
        object.__setattr__(
            self,
            "required_worker_capabilities",
            tuple(dict.fromkeys(self.required_worker_capabilities)),
        )
        object.__setattr__(
            self,
            "input_expectations",
            tuple(
                dict.fromkeys(
                    str(value).strip()
                    for value in self.input_expectations
                    if str(value).strip()
                )
            ),
        )
        object.__setattr__(
            self,
            "output_expectations",
            tuple(
                dict.fromkeys(
                    str(value).strip()
                    for value in self.output_expectations
                    if str(value).strip()
                )
            ),
        )
        return self


class SkillCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    skill_id: str = Field(
        min_length=1,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=4000)
    instructions: str = Field(min_length=1, max_length=SKILL_MAX_INSTRUCTIONS_CHARS)
    applicability_tags: tuple[str, ...] = ()
    capability_tags: tuple[str, ...] = ()
    assets: tuple[SkillAsset, ...] = ()
    required_provider_capabilities: tuple[AgentProviderCapability, ...] = ()
    required_worker_capabilities: tuple[WorkerCapability, ...] = ()
    input_expectations: tuple[str, ...] = ()
    output_expectations: tuple[str, ...] = ()
    provenance: SkillProvenance = Field(default_factory=SkillProvenance)
    reason: str | None = Field(default=None, max_length=1000)


class SkillUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=4000)
    instructions: str | None = Field(
        default=None,
        min_length=1,
        max_length=SKILL_MAX_INSTRUCTIONS_CHARS,
    )
    applicability_tags: tuple[str, ...] | None = None
    capability_tags: tuple[str, ...] | None = None
    assets: tuple[SkillAsset, ...] | None = None
    required_provider_capabilities: tuple[AgentProviderCapability, ...] | None = None
    required_worker_capabilities: tuple[WorkerCapability, ...] | None = None
    input_expectations: tuple[str, ...] | None = None
    output_expectations: tuple[str, ...] | None = None
    provenance: SkillProvenance | None = None
    reason: str = Field(min_length=1, max_length=1000)


class SkillLifecycleChange(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: str = Field(min_length=1, max_length=1000)


class SkillPublish(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: str | None = Field(default=None, max_length=1000)
    expected_active_revision: int | None = Field(default=None, ge=1)


class SkillRollback(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    target_revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=1000)
    expected_active_revision: int | None = Field(default=None, ge=1)


class SkillBundleImport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: str = "codex-web-skill-bundle"
    version: str = "1.0"
    manifest: SkillCreate
    files: dict[str, str] = Field(default_factory=dict)


class SkillPromotionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    skill_id: str = Field(
        min_length=1,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=4000)
    procedure: str = Field(min_length=1, max_length=SKILL_MAX_INSTRUCTIONS_CHARS)
    source_execution_id: str = Field(min_length=1, max_length=300)
    evidence_ids: tuple[str, ...] = Field(default=(), max_length=50)
    reason: str = Field(
        default="promote verified procedure to draft skill",
        min_length=1,
        max_length=1000,
    )


class SkillContextSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str
    definition_record_ids: tuple[str, ...]
    included_assets: tuple[str, ...] = ()
    omitted_assets: tuple[str, ...] = ()
    truncated: bool = False
    character_count: int = Field(ge=0)
