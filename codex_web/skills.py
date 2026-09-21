from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


SKILL_DEFINITION_KIND = "skill"
SKILL_SCHEMA_VERSION = "1.0"

MAX_SKILL_INSTRUCTIONS_CHARS = 64_000
MAX_SKILL_DESCRIPTION_CHARS = 4_000
MAX_SKILL_TAGS = 32
MAX_SKILL_ASSETS = 16
MAX_SKILL_ASSET_BYTES = 32 * 1024
MAX_SKILL_TOTAL_ASSET_BYTES = 192 * 1024
MAX_SKILL_STRUCTURED_EXPECTATION_BYTES = 16 * 1024
MAX_SKILL_CONTEXT_BYTES = 96 * 1024

SkillAssetKind = Literal["reference", "template", "helper"]
SkillHelperSideEffects = Literal["none", "workspace", "external"]
SkillHelperExecutionPolicy = Literal[
    "never_automatic",
    "worker_only",
    "action_intent_required",
]
SkillWorkerCapability = Literal[
    "git",
    "command_execution",
    "container",
    "network",
    "artifact_upload",
]

_SECRET_KEY_FRAGMENTS = (
    "password",
    "secret_value",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "private_key",
    "credential_value",
)
_SECRET_CONTENT_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\b(?:sk|rk|pk)_[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{12,}\b"),
    re.compile(
        r"(?i)\b(?:password|api[_-]?key|access[_-]?token|secret)\s*[:=]\s*[^\s]{8,}"
    ),
)


def _json_size(value: Any) -> int:
    return len(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    )


def _normalized_strings(values: tuple[str, ...], *, maximum: int) -> tuple[str, ...]:
    result = tuple(
        dict.fromkeys(
            value.strip()
            for value in values
            if value and value.strip()
        )
    )
    if len(result) > maximum:
        raise ValueError(f"skill list cannot exceed {maximum} entries")
    return result


def _forbidden_structured_keys(value: Any, path: str = "$") -> list[str]:
    matches: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).casefold()
            if any(fragment in normalized for fragment in _SECRET_KEY_FRAGMENTS):
                matches.append(f"{path}.{key}")
            matches.extend(
                _forbidden_structured_keys(child, f"{path}.{key}")
            )
    elif isinstance(value, list):
        for index, child in enumerate(value):
            matches.extend(
                _forbidden_structured_keys(child, f"{path}[{index}]")
            )
    return matches


def contains_secret_material(text: str) -> bool:
    return any(pattern.search(text or "") for pattern in _SECRET_CONTENT_PATTERNS)


class SkillProvenance(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    source_type: Literal[
        "manual",
        "verified_execution",
        "import",
        "documentation",
        "migration",
    ] = "manual"
    source_ref: str | None = Field(default=None, max_length=1000)
    source_revision: str | None = Field(default=None, max_length=256)
    source_url: str | None = Field(default=None, max_length=2000)
    imported_at: float | None = None
    imported_by: str | None = Field(default=None, max_length=256)


class SkillAsset(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    name: str = Field(
        min_length=1,
        max_length=240,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$",
    )
    kind: SkillAssetKind
    media_type: str = Field(default="text/plain", min_length=1, max_length=120)
    content: str = Field(default="", max_length=MAX_SKILL_ASSET_BYTES)
    tags: tuple[str, ...] = ()
    side_effects: SkillHelperSideEffects = "none"
    execution_policy: SkillHelperExecutionPolicy = "never_automatic"
    required_worker_capabilities: tuple[SkillWorkerCapability, ...] = ()

    @model_validator(mode="after")
    def validate_asset(self) -> "SkillAsset":
        object.__setattr__(
            self,
            "tags",
            _normalized_strings(self.tags, maximum=MAX_SKILL_TAGS),
        )
        capabilities = tuple(
            dict.fromkeys(self.required_worker_capabilities)
        )
        object.__setattr__(
            self,
            "required_worker_capabilities",
            capabilities,
        )
        if len(self.content.encode("utf-8")) > MAX_SKILL_ASSET_BYTES:
            raise ValueError(
                f"skill asset exceeds {MAX_SKILL_ASSET_BYTES} bytes"
            )
        if contains_secret_material(self.content):
            raise ValueError("skill asset appears to contain raw secret material")

        if self.kind != "helper":
            if self.side_effects != "none":
                raise ValueError(
                    "reference/template assets cannot declare side effects"
                )
            if self.execution_policy != "never_automatic":
                raise ValueError(
                    "reference/template assets are not executable"
                )
            if self.required_worker_capabilities:
                raise ValueError(
                    "reference/template assets cannot require worker capabilities"
                )
            return self

        if self.execution_policy == "never_automatic":
            # A helper may be retained as source/reference without being executable.
            return self
        if "command_execution" not in self.required_worker_capabilities:
            raise ValueError(
                "executable helper requires command_execution capability"
            )
        if (
            self.side_effects == "external"
            and self.execution_policy != "action_intent_required"
        ):
            raise ValueError(
                "external-side-effect helper requires action_intent_required"
            )
        return self


class SkillDefinition(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    name: str = Field(min_length=1, max_length=120)
    description: str = Field(
        default="",
        max_length=MAX_SKILL_DESCRIPTION_CHARS,
    )
    instructions: str = Field(
        min_length=1,
        max_length=MAX_SKILL_INSTRUCTIONS_CHARS,
    )
    applicability_tags: tuple[str, ...] = ()
    capability_tags: tuple[str, ...] = ()
    required_provider_capabilities: tuple[str, ...] = ()
    required_worker_capabilities: tuple[SkillWorkerCapability, ...] = ()
    input_expectations: dict[str, Any] | None = None
    output_expectations: dict[str, Any] | None = None
    assets: tuple[SkillAsset, ...] = ()
    provenance: SkillProvenance = Field(default_factory=SkillProvenance)
    compatibility_tags: tuple[str, ...] = ()
    owner_identity_id: str | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def validate_skill(self) -> "SkillDefinition":
        for field_name in (
            "applicability_tags",
            "capability_tags",
            "required_provider_capabilities",
            "compatibility_tags",
        ):
            object.__setattr__(
                self,
                field_name,
                _normalized_strings(
                    getattr(self, field_name),
                    maximum=MAX_SKILL_TAGS,
                ),
            )
        object.__setattr__(
            self,
            "required_worker_capabilities",
            tuple(dict.fromkeys(self.required_worker_capabilities)),
        )

        if contains_secret_material(self.instructions):
            raise ValueError(
                "skill instructions appear to contain raw secret material"
            )

        if len(self.assets) > MAX_SKILL_ASSETS:
            raise ValueError(
                f"skill cannot exceed {MAX_SKILL_ASSETS} assets"
            )
        names = [asset.name for asset in self.assets]
        if len(names) != len(set(names)):
            raise ValueError("skill asset names must be unique")
        total_asset_bytes = sum(
            len(asset.content.encode("utf-8"))
            for asset in self.assets
        )
        if total_asset_bytes > MAX_SKILL_TOTAL_ASSET_BYTES:
            raise ValueError(
                "skill assets exceed total bounded size"
            )

        for label, value in (
            ("input_expectations", self.input_expectations),
            ("output_expectations", self.output_expectations),
        ):
            if value is None:
                continue
            if _json_size(value) > MAX_SKILL_STRUCTURED_EXPECTATION_BYTES:
                raise ValueError(
                    f"{label} exceeds bounded structured size"
                )
            forbidden = _forbidden_structured_keys(value)
            if forbidden:
                raise ValueError(
                    f"{label} contains secret-bearing fields: "
                    + ", ".join(forbidden)
                )

        return self

    def public_summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "applicabilityTags": list(self.applicability_tags),
            "capabilityTags": list(self.capability_tags),
            "requiredProviderCapabilities": list(
                self.required_provider_capabilities
            ),
            "requiredWorkerCapabilities": list(
                self.required_worker_capabilities
            ),
            "assetCount": len(self.assets),
            "helperCount": sum(
                asset.kind == "helper" for asset in self.assets
            ),
            "provenance": self.provenance.model_dump(mode="json"),
            "ownerIdentityId": self.owner_identity_id,
        }


class SkillContextAsset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    kind: Literal["reference", "template"]
    media_type: str
    content: str


class SkillContextItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    definition: dict[str, Any]
    name: str
    instructions: str
    assets: tuple[SkillContextAsset, ...] = ()
    helper_metadata: tuple[dict[str, Any], ...] = ()


class SkillContextSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[SkillContextItem, ...] = ()
    total_bytes: int = Field(ge=0)
    truncated: bool = False


class SkillBundleAsset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    kind: SkillAssetKind
    media_type: str = "text/plain"
    content: str = ""
    tags: tuple[str, ...] = ()
    side_effects: SkillHelperSideEffects = "none"
    execution_policy: SkillHelperExecutionPolicy = "never_automatic"
    required_worker_capabilities: tuple[SkillWorkerCapability, ...] = ()


class SkillBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    format: Literal["codex-web-skill-bundle"] = "codex-web-skill-bundle"
    version: Literal["1.0"] = "1.0"
    skill_id: str = Field(
        min_length=1,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=MAX_SKILL_DESCRIPTION_CHARS)
    skill_md: str = Field(
        min_length=1,
        max_length=MAX_SKILL_INSTRUCTIONS_CHARS,
    )
    applicability_tags: tuple[str, ...] = ()
    capability_tags: tuple[str, ...] = ()
    required_provider_capabilities: tuple[str, ...] = ()
    required_worker_capabilities: tuple[SkillWorkerCapability, ...] = ()
    input_expectations: dict[str, Any] | None = None
    output_expectations: dict[str, Any] | None = None
    assets: tuple[SkillBundleAsset, ...] = ()
    compatibility_tags: tuple[str, ...] = ()
    source: SkillProvenance = Field(
        default_factory=lambda: SkillProvenance(source_type="import")
    )


class SkillPromotionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    skill_id: str = Field(
        min_length=1,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=MAX_SKILL_DESCRIPTION_CHARS)
    verified_procedure: str = Field(
        min_length=1,
        max_length=MAX_SKILL_INSTRUCTIONS_CHARS,
    )
    source_execution_id: str = Field(min_length=1, max_length=256)
    applicability_tags: tuple[str, ...] = ()
    capability_tags: tuple[str, ...] = ()


def validate_skill_definition(payload: dict[str, Any]) -> dict[str, Any]:
    return SkillDefinition.model_validate(payload).model_dump(mode="json")
