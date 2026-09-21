from __future__ import annotations

import re
import time
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.agent_providers import AgentProviderCapability
from codex_web.definitions import DefinitionReference, DefinitionScope
from codex_web.execution_workers import WorkerCapability


SKILL_DEFINITION_KIND = "agent.skill"
SKILL_SCHEMA_VERSION = "1.0"
SKILL_BUNDLE_FORMAT = "codex-web-skill-bundle"
SKILL_BUNDLE_VERSION = "1.0"

MAX_SKILL_BODY_CHARS = 64_000
MAX_SKILL_DESCRIPTION_CHARS = 4_000
MAX_SKILL_ASSETS = 16
MAX_SKILL_ASSET_CHARS = 64_000
MAX_SKILL_TOTAL_ASSET_CHARS = 256_000
MAX_SKILL_TAGS = 32
MAX_SKILL_IO_FIELDS = 32
MAX_SKILL_CAPABILITIES = 32
MAX_SKILL_BUNDLE_FILES = 32
MAX_SKILL_BUNDLE_TOTAL_CHARS = 384_000

_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private_key",
        re.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----",
            re.IGNORECASE,
        ),
    ),
    (
        "aws_access_key",
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    ),
    (
        "github_token",
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    ),
    (
        "slack_token",
        re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    ),
    (
        "bearer_token",
        re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{20,}\b", re.IGNORECASE),
    ),
    (
        "credential_assignment",
        re.compile(
            r"(?im)^\s*(?:password|passwd|secret|api[_-]?key|"
            r"access[_-]?token|refresh[_-]?token|private[_-]?key)"
            r"\s*[:=]\s*(?!<|\$\{|secret://|ref:|example|placeholder|"
            r"redacted|xxxx)[^\s#]{8,}"
        ),
    ),
)


def secret_material_reason(value: str) -> str | None:
    text = str(value or "")
    for label, pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            return label
    return None


class SkillAssetKind(StrEnum):
    REFERENCE = "reference"
    TEMPLATE = "template"
    HELPER = "helper"


class SkillSecurityClassification(StrEnum):
    PASSIVE = "passive"
    EXECUTABLE_HELPER = "executable_helper"
    SIDE_EFFECTING_HELPER = "side_effecting_helper"


class SkillSourceType(StrEnum):
    MANUAL = "manual"
    IMPORTED_BUNDLE = "imported_bundle"
    VERIFIED_PROCEDURE = "verified_procedure"


class SkillUpdatePolicy(StrEnum):
    MANUAL = "manual"


class SkillApplicability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    purposes: tuple[str, ...] = ()
    model_classes: tuple[str, ...] = ()
    capability_tags: tuple[str, ...] = ()

    @model_validator(mode="after")
    def normalize(self) -> "SkillApplicability":
        for field_name in ("purposes", "model_classes", "capability_tags"):
            values = tuple(
                sorted(
                    {
                        str(value).strip()
                        for value in getattr(self, field_name)
                        if str(value).strip()
                    }
                )
            )
            object.__setattr__(self, field_name, values)
        return self

    def matches(
        self,
        *,
        purpose: str | None = None,
        model_class: str | None = None,
        capability_tags: tuple[str, ...] = (),
    ) -> bool:
        if self.purposes and str(purpose or "") not in self.purposes:
            return False
        if self.model_classes and str(model_class or "") not in self.model_classes:
            return False
        if self.capability_tags and not set(self.capability_tags).issubset(
            set(capability_tags)
        ):
            return False
        return True


class SkillIoField(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=128)
    type: str = Field(default="string", min_length=1, max_length=128)
    description: str = Field(default="", max_length=1000)
    required: bool = False


class SkillSourceMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    source_type: SkillSourceType = SkillSourceType.MANUAL
    source_uri: str | None = Field(default=None, max_length=2000)
    external_revision: str | None = Field(default=None, max_length=500)
    source_execution_id: str | None = Field(default=None, max_length=500)
    update_policy: SkillUpdatePolicy = SkillUpdatePolicy.MANUAL
    imported_at: float | None = None


class SkillAsset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    name: str = Field(min_length=1, max_length=200)
    kind: SkillAssetKind
    media_type: str = Field(default="text/plain", min_length=1, max_length=200)
    content: str = Field(max_length=MAX_SKILL_ASSET_CHARS)
    security_classification: SkillSecurityClassification = (
        SkillSecurityClassification.PASSIVE
    )
    include_by_default: bool = False
    applicability: SkillApplicability = Field(default_factory=SkillApplicability)
    source_path: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_security(self) -> "SkillAsset":
        if secret_material_reason(self.content):
            raise ValueError(
                f"skill asset {self.id} contains secret-like material"
            )
        if self.kind == SkillAssetKind.HELPER:
            if self.security_classification == SkillSecurityClassification.PASSIVE:
                raise ValueError(
                    "helper assets require executable_helper or "
                    "side_effecting_helper classification"
                )
            if self.include_by_default:
                raise ValueError(
                    "executable helper assets cannot be included by default"
                )
        elif (
            self.security_classification
            != SkillSecurityClassification.PASSIVE
        ):
            raise ValueError(
                "reference/template assets must use passive classification"
            )
        return self


class SkillDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=MAX_SKILL_DESCRIPTION_CHARS)
    body: str = Field(min_length=1, max_length=MAX_SKILL_BODY_CHARS)
    owner_identity_id: str | None = Field(default=None, max_length=500)
    tags: tuple[str, ...] = ()
    applicability: SkillApplicability = Field(default_factory=SkillApplicability)
    assets: tuple[SkillAsset, ...] = ()
    required_provider_capabilities: tuple[str, ...] = ()
    required_worker_capabilities: tuple[str, ...] = ()
    inputs: tuple[SkillIoField, ...] = ()
    outputs: tuple[SkillIoField, ...] = ()
    compatibility_tags: tuple[str, ...] = ()
    source: SkillSourceMetadata = Field(default_factory=SkillSourceMetadata)

    @model_validator(mode="after")
    def validate_skill(self) -> "SkillDefinition":
        if secret_material_reason(self.body):
            raise ValueError("skill body contains secret-like material")
        if secret_material_reason(self.description):
            raise ValueError("skill description contains secret-like material")

        for field_name, maximum in (
            ("tags", MAX_SKILL_TAGS),
            ("required_provider_capabilities", MAX_SKILL_CAPABILITIES),
            ("required_worker_capabilities", MAX_SKILL_CAPABILITIES),
            ("compatibility_tags", MAX_SKILL_CAPABILITIES),
        ):
        provider_capabilities = {
            item.value for item in AgentProviderCapability
        }
        unknown_provider = sorted(
            set(self.required_provider_capabilities) - provider_capabilities
        )
        if unknown_provider:
            raise ValueError(
                "unknown AgentProvider capabilities: "
                + ", ".join(unknown_provider)
            )
        worker_capabilities = {
            item.value for item in WorkerCapability
        }
        unknown_worker = sorted(
            set(self.required_worker_capabilities) - worker_capabilities
        )
        if unknown_worker:
            raise ValueError(
                "unknown worker capabilities: "
                + ", ".join(unknown_worker)
            )

            values = tuple(
                sorted(
                    {
                        str(value).strip()
                        for value in getattr(self, field_name)
                        if str(value).strip()
                    }
                )
            )
            if len(values) > maximum:
                raise ValueError(
                    f"skill {field_name} cannot exceed {maximum} values"
                )
            setattr(self, field_name, values)

        if len(self.assets) > MAX_SKILL_ASSETS:
            raise ValueError(
                f"skill assets cannot exceed {MAX_SKILL_ASSETS}"
            )
        if len({item.id for item in self.assets}) != len(self.assets):
            raise ValueError("skill asset ids must be unique")
        if sum(len(item.content) for item in self.assets) > MAX_SKILL_TOTAL_ASSET_CHARS:
            raise ValueError(
                "skill assets exceed total content size limit"
            )
        if len(self.inputs) > MAX_SKILL_IO_FIELDS:
            raise ValueError(
                f"skill inputs cannot exceed {MAX_SKILL_IO_FIELDS}"
            )
        if len(self.outputs) > MAX_SKILL_IO_FIELDS:
            raise ValueError(
                f"skill outputs cannot exceed {MAX_SKILL_IO_FIELDS}"
            )
        if len({item.name for item in self.inputs}) != len(self.inputs):
            raise ValueError("skill input names must be unique")
        if len({item.name for item in self.outputs}) != len(self.outputs):
            raise ValueError("skill output names must be unique")
        return self


class SkillBundleAsset(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    path: str = Field(min_length=1, max_length=500)
    content: str = Field(max_length=MAX_SKILL_ASSET_CHARS)
    media_type: str = Field(default="text/plain", min_length=1, max_length=200)
    kind: SkillAssetKind = SkillAssetKind.REFERENCE
    security_classification: SkillSecurityClassification = (
        SkillSecurityClassification.PASSIVE
    )
    include_by_default: bool = False


class SkillBundleManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    skill_id: str = Field(
        min_length=1,
        max_length=200,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=MAX_SKILL_DESCRIPTION_CHARS)
    owner_identity_id: str | None = Field(default=None, max_length=500)
    tags: tuple[str, ...] = ()
    applicability: SkillApplicability = Field(default_factory=SkillApplicability)
    required_provider_capabilities: tuple[str, ...] = ()
    required_worker_capabilities: tuple[str, ...] = ()
    inputs: tuple[SkillIoField, ...] = ()
    outputs: tuple[SkillIoField, ...] = ()
    compatibility_tags: tuple[str, ...] = ()
    source_uri: str | None = Field(default=None, max_length=2000)
    external_revision: str | None = Field(default=None, max_length=500)


class SkillBundleImport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: str = SKILL_BUNDLE_FORMAT
    version: str = SKILL_BUNDLE_VERSION
    manifest: SkillBundleManifest
    skill_md: str = Field(min_length=1, max_length=MAX_SKILL_BODY_CHARS)
    assets: tuple[SkillBundleAsset, ...] = ()

    @model_validator(mode="after")
    def validate_bundle(self) -> "SkillBundleImport":
        if self.format != SKILL_BUNDLE_FORMAT:
            raise ValueError("unsupported skill bundle format")
        if self.version != SKILL_BUNDLE_VERSION:
            raise ValueError("unsupported skill bundle version")
        if len(self.assets) > MAX_SKILL_BUNDLE_FILES:
            raise ValueError(
                f"skill bundle cannot exceed {MAX_SKILL_BUNDLE_FILES} assets"
            )
        total = len(self.skill_md) + sum(len(item.content) for item in self.assets)
        if total > MAX_SKILL_BUNDLE_TOTAL_CHARS:
            raise ValueError("skill bundle exceeds total size limit")
        if len({item.path for item in self.assets}) != len(self.assets):
            raise ValueError("skill bundle asset paths must be unique")
        if secret_material_reason(self.skill_md):
            raise ValueError("SKILL.md contains secret-like material")
        for item in self.assets:
            if item.path.startswith("/") or ".." in item.path.split("/"):
                raise ValueError("skill bundle asset paths must be relative and traversal-free")
            if secret_material_reason(item.content):
                raise ValueError(
                    f"skill bundle asset {item.path} contains secret-like material"
                )
        return self


class VerifiedProcedurePromotion(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    skill_id: str = Field(
        min_length=1,
        max_length=200,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=MAX_SKILL_DESCRIPTION_CHARS)
    procedure_summary: str = Field(min_length=1, max_length=32_000)
    checklist: tuple[str, ...] = ()
    source_execution_id: str = Field(min_length=1, max_length=500)
    tags: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_promotion(self) -> "VerifiedProcedurePromotion":
        if len(self.checklist) > 64:
            raise ValueError("verified procedure checklist cannot exceed 64 steps")
        if secret_material_reason(self.procedure_summary):
            raise ValueError(
                "verified procedure summary contains secret-like material"
            )
        for item in self.checklist:
            if len(item) > 2000:
                raise ValueError("verified procedure checklist item is too large")
            if secret_material_reason(item):
                raise ValueError(
                    "verified procedure checklist contains secret-like material"
                )
        self.tags = tuple(
            sorted({str(value).strip() for value in self.tags if str(value).strip()})
        )
        return self


def validate_skill_definition(payload: dict[str, Any]) -> dict[str, Any]:
    return SkillDefinition.model_validate(payload).model_dump(mode="json")


def skill_definition_from_bundle(
    bundle: SkillBundleImport,
    *,
    imported_at: float | None = None,
) -> SkillDefinition:
    assets = tuple(
        SkillAsset(
            id=re.sub(r"[^a-z0-9._-]+", "-", item.path.casefold()).strip("-")
            or f"asset-{index}",
            name=item.path.rsplit("/", 1)[-1],
            kind=item.kind,
            media_type=item.media_type,
            content=item.content,
            security_classification=item.security_classification,
            include_by_default=item.include_by_default,
            source_path=item.path,
        )
        for index, item in enumerate(bundle.assets, start=1)
    )
    manifest = bundle.manifest
    return SkillDefinition(
        name=manifest.name,
        description=manifest.description,
        body=bundle.skill_md,
        owner_identity_id=manifest.owner_identity_id,
        tags=manifest.tags,
        applicability=manifest.applicability,
        assets=assets,
        required_provider_capabilities=manifest.required_provider_capabilities,
        required_worker_capabilities=manifest.required_worker_capabilities,
        inputs=manifest.inputs,
        outputs=manifest.outputs,
        compatibility_tags=manifest.compatibility_tags,
        source=SkillSourceMetadata(
            source_type=SkillSourceType.IMPORTED_BUNDLE,
            source_uri=manifest.source_uri,
            external_revision=manifest.external_revision,
            update_policy=SkillUpdatePolicy.MANUAL,
            imported_at=imported_at or time.time(),
        ),
    )


class SkillDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill_id: str = Field(
        min_length=1,
        max_length=200,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    skill: SkillDefinition
    reason: str | None = Field(default=None, max_length=1000)
    scope_type: DefinitionScope = DefinitionScope.WORKSPACE


class SkillEditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill: SkillDefinition
    reason: str = Field(min_length=1, max_length=1000)


class SkillPublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=1000)
    expected_active_revision: int | None = Field(default=None, ge=1)
    approval_metadata: dict[str, str] = Field(default_factory=dict)


class SkillRollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=1000)
    expected_active_revision: int | None = Field(default=None, ge=1)
    approval_metadata: dict[str, str] = Field(default_factory=dict)


class SkillAttachmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=1000)


class SkillExecutionMaterialRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    purpose: str | None = Field(default=None, max_length=200)
    model_class: str | None = Field(default=None, max_length=200)
    capability_tags: tuple[str, ...] = ()
    include_asset_ids: tuple[str, ...] = ()


class SkillExecutionMaterial(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reference: DefinitionReference
    name: str
    description: str
    body: str
    passive_assets: tuple[SkillAsset, ...] = ()
    helper_assets: tuple[SkillAsset, ...] = ()
    required_provider_capabilities: tuple[str, ...] = ()
    required_worker_capabilities: tuple[str, ...] = ()
