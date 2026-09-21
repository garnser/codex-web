from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


SKILL_DEFINITION_KIND = "agent.skill"
SKILL_DEFINITION_SCHEMA_VERSION = "1.0"

MAX_SKILL_BODY_CHARACTERS = 64_000
MAX_SKILL_ASSETS = 16
MAX_SKILL_ASSET_CHARACTERS = 32_000
MAX_SKILL_TOTAL_ASSET_CHARACTERS = 128_000
MAX_SKILL_TAGS = 32
MAX_SKILL_CAPABILITIES = 32

_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    re.compile(
        r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|"
        r"refresh[_-]?token|client[_-]?secret)\s*[:=]\s*"
        r"[\"']?[A-Za-z0-9+/=_-]{12,}"
    ),
)


class SkillAssetKind(StrEnum):
    REFERENCE = "reference"
    TEMPLATE = "template"
    HELPER = "helper"


class SkillAssetSecurity(StrEnum):
    TEXT = "text"
    UNTRUSTED_REFERENCE = "untrusted_reference"
    EXECUTABLE_UNTRUSTED = "executable_untrusted"


class SkillHelperExecutionPolicy(StrEnum):
    NEVER = "never"
    MANUAL_WORKER_ONLY = "manual_worker_only"


class SkillSourceKind(StrEnum):
    MANUAL = "manual"
    IMPORTED = "imported"
    VERIFIED_PROCEDURE = "verified_procedure"


class SkillSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    kind: SkillSourceKind = SkillSourceKind.MANUAL
    reference: str | None = Field(default=None, max_length=1000)
    source_revision: str | None = Field(default=None, max_length=250)
    verified_evidence_refs: tuple[str, ...] = Field(default=(), max_length=32)


class SkillAsset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    path: str = Field(min_length=1, max_length=240)
    kind: SkillAssetKind
    media_type: str = Field(default="text/plain", min_length=1, max_length=120)
    content: str = Field(default="", max_length=MAX_SKILL_ASSET_CHARACTERS)
    executable: bool = False
    security: SkillAssetSecurity = SkillAssetSecurity.TEXT
    helper_execution_policy: SkillHelperExecutionPolicy = SkillHelperExecutionPolicy.NEVER
    capability_tags: tuple[str, ...] = Field(default=(), max_length=MAX_SKILL_TAGS)

    @model_validator(mode="after")
    def validate_asset(self) -> "SkillAsset":
        if self.path.startswith("/") or ".." in self.path.split("/"):
            raise ValueError("skill asset path must be relative and cannot traverse")
        if "\x00" in self.path:
            raise ValueError("skill asset path contains NUL")
        if self.executable:
            if self.kind != SkillAssetKind.HELPER:
                raise ValueError("only helper assets may be marked executable")
            if self.security != SkillAssetSecurity.EXECUTABLE_UNTRUSTED:
                raise ValueError(
                    "executable helper must be classified executable_untrusted"
                )
            if (
                self.helper_execution_policy
                != SkillHelperExecutionPolicy.MANUAL_WORKER_ONLY
            ):
                raise ValueError(
                    "executable helper requires manual_worker_only execution policy"
                )
        elif self.helper_execution_policy != SkillHelperExecutionPolicy.NEVER:
            raise ValueError(
                "non-executable skill asset cannot request an execution policy"
            )
        return self


class SkillDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=4000)
    body: str = Field(min_length=1, max_length=MAX_SKILL_BODY_CHARACTERS)
    tags: tuple[str, ...] = Field(default=(), max_length=MAX_SKILL_TAGS)
    applicability: tuple[str, ...] = Field(default=(), max_length=MAX_SKILL_TAGS)
    assets: tuple[SkillAsset, ...] = Field(default=(), max_length=MAX_SKILL_ASSETS)
    required_provider_capabilities: tuple[str, ...] = Field(
        default=(),
        max_length=MAX_SKILL_CAPABILITIES,
    )
    required_worker_capabilities: tuple[str, ...] = Field(
        default=(),
        max_length=MAX_SKILL_CAPABILITIES,
    )
    input_expectations: dict[str, str] = Field(default_factory=dict)
    output_expectations: dict[str, str] = Field(default_factory=dict)
    source: SkillSource = Field(default_factory=SkillSource)
    compatibility: tuple[str, ...] = ()
    owner_identity_id: str | None = Field(default=None, max_length=250)

    @staticmethod
    def _normalized(values: tuple[str, ...], *, label: str) -> tuple[str, ...]:
        normalized = tuple(
            dict.fromkeys(
                str(value).strip().lower()
                for value in values
                if str(value).strip()
            )
        )
        if any(len(value) > 120 for value in normalized):
            raise ValueError(f"{label} value exceeds 120 characters")
        return normalized

    @model_validator(mode="after")
    def validate_skill(self) -> "SkillDefinition":
        self.tags = self._normalized(self.tags, label="skill tag")
        self.applicability = self._normalized(
            self.applicability,
            label="skill applicability",
        )
        self.required_provider_capabilities = self._normalized(
            self.required_provider_capabilities,
            label="provider capability",
        )
        self.required_worker_capabilities = self._normalized(
            self.required_worker_capabilities,
            label="worker capability",
        )
        paths = [asset.path for asset in self.assets]
        if len(paths) != len(set(paths)):
            raise ValueError("skill asset paths must be unique")
        total_assets = sum(len(asset.content) for asset in self.assets)
        if total_assets > MAX_SKILL_TOTAL_ASSET_CHARACTERS:
            raise ValueError(
                "skill assets exceed aggregate content size limit"
            )
        _reject_secret_material(
            {
                "body": self.body,
                "assets": [asset.content for asset in self.assets],
                "input_expectations": self.input_expectations,
                "output_expectations": self.output_expectations,
            }
        )
        return self


class SkillContextSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    definition_id: str
    record_id: str
    revision: int = Field(ge=1)
    name: str
    body: str
    selected_assets: tuple[SkillAsset, ...] = ()
    omitted_asset_paths: tuple[str, ...] = ()
    required_provider_capabilities: tuple[str, ...] = ()
    required_worker_capabilities: tuple[str, ...] = ()
    characters: int = Field(ge=0)
    truncated: bool = False


class SkillBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: str = "codex-web-skill"
    version: str = "1.0"
    skill_id: str = Field(min_length=1, max_length=120)
    skill_md: str = Field(min_length=1, max_length=MAX_SKILL_BODY_CHARACTERS)
    metadata: dict[str, Any] = Field(default_factory=dict)
    assets: tuple[SkillAsset, ...] = Field(default=(), max_length=MAX_SKILL_ASSETS)

    @model_validator(mode="after")
    def validate_bundle(self) -> "SkillBundle":
        if self.format != "codex-web-skill" or self.version != "1.0":
            raise ValueError("unsupported skill bundle format")
        total_assets = sum(len(asset.content) for asset in self.assets)
        if total_assets > MAX_SKILL_TOTAL_ASSET_CHARACTERS:
            raise ValueError("skill bundle assets exceed aggregate size limit")
        _reject_secret_material(
            {
                "skill_md": self.skill_md,
                "metadata": self.metadata,
                "assets": [asset.content for asset in self.assets],
            }
        )
        return self


def _reject_secret_material(value: Any) -> None:
    def walk(item: Any) -> None:
        if isinstance(item, str):
            for pattern in _SECRET_PATTERNS:
                if pattern.search(item):
                    raise ValueError(
                        "skill content appears to contain raw secret material; "
                        "use a SecretReference instead"
                    )
            return
        if isinstance(item, dict):
            for key, child in item.items():
                key_text = str(key).casefold()
                if key_text in {
                    "password",
                    "passwd",
                    "secret",
                    "api_key",
                    "apikey",
                    "access_token",
                    "refresh_token",
                    "client_secret",
                } and str(child).strip():
                    raise ValueError(
                        "skill metadata cannot contain raw secret-bearing fields"
                    )
                walk(child)
            return
        if isinstance(item, (list, tuple, set)):
            for child in item:
                walk(child)

    walk(value)


def validate_skill_definition(payload: dict[str, Any]) -> dict[str, Any]:
    """Definition Registry validator for canonical agent.skill revisions."""

    skill = SkillDefinition.model_validate(payload)
    return skill.model_dump(mode="json")
