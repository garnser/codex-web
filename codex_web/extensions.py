from __future__ import annotations

import re
import time
import uuid
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


EXTENSION_MANIFEST_SCHEMA_VERSION = "codex-web.extension/v1"
_EXTENSION_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$")
_SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_CAPABILITY_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]*$")


class ExtensionType(StrEnum):
    TASK_SOURCE = "task_source"
    CONVERSATION_CHANNEL = "conversation_channel"
    ACTION_PROVIDER = "action_provider"
    MODEL_PROVIDER = "model_provider"
    SECRET_BACKEND = "secret_backend"
    CONFIG_BACKEND = "config_backend"
    WORKER = "worker"


class ExtensionLifecycleState(StrEnum):
    DISCOVERED = "discovered"
    INSTALLED = "installed"
    CONFIGURED = "configured"
    ENABLED = "enabled"
    DISABLED = "disabled"
    QUARANTINED = "quarantined"
    UPGRADING = "upgrading"
    INCOMPATIBLE = "incompatible"
    DEPRECATED = "deprecated"
    REMOVED = "removed"


class ExtensionPublisher(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)


class ExtensionProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    source: str = Field(min_length=1)
    digest: str = Field(pattern=_DIGEST_RE.pattern)
    signature: str | None = None


class ExtensionCompatibility(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    codex_web: str = Field(min_length=1)


class ExtensionCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    requested: tuple[str, ...] = ()
    mandatory: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_capabilities(self) -> "ExtensionCapabilities":
        requested = tuple(dict.fromkeys(self.requested))
        mandatory = tuple(dict.fromkeys(self.mandatory))
        invalid = [value for value in (*requested, *mandatory) if not _CAPABILITY_RE.fullmatch(value)]
        if invalid:
            raise ValueError(f"invalid extension capability: {invalid[0]!r}")
        missing = set(mandatory) - set(requested)
        if missing:
            raise ValueError("mandatory capabilities must also be requested")
        object.__setattr__(self, "requested", requested)
        object.__setattr__(self, "mandatory", mandatory)
        return self


class ExtensionEvents(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    subscribes: tuple[str, ...] = ()
    publishes: tuple[str, ...] = ()


class ExtensionConfiguration(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        populate_by_name=True,
    )

    schema_path: str | None = Field(default=None, alias="schema")
    secret_refs: tuple[str, ...] = ()


class ExtensionResources(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scopes: tuple[str, ...] = ()


class ExtensionUIContribution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    kind: Literal["navigation", "form", "status_panel"]
    route_id: str = Field(min_length=1)
    label: str | None = None
    schema_ref: str | None = None


class ExtensionUI(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contributions: tuple[ExtensionUIContribution, ...] = ()


class ExtensionMigrations(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    entrypoint: str | None = None


class ExtensionHealth(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    timeout_seconds: float = Field(default=5.0, gt=0.0, le=300.0)
    interval_seconds: float = Field(default=60.0, gt=0.0, le=86400.0)


class ExtensionEntrypoints(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_source: str | None = None
    conversation_channel: str | None = None
    action_provider: str | None = None
    model_provider: str | None = None
    secret_backend: str | None = None
    config_backend: str | None = None
    worker: str | None = None


class ExtensionManifest(BaseModel):
    """Immutable, non-secret package declaration validated before extension code executes."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal[EXTENSION_MANIFEST_SCHEMA_VERSION] = EXTENSION_MANIFEST_SCHEMA_VERSION
    id: str = Field(min_length=3)
    version: str = Field(min_length=5)
    publisher: ExtensionPublisher
    provenance: ExtensionProvenance
    compatibility: ExtensionCompatibility
    types: tuple[ExtensionType, ...] = Field(min_length=1)
    capabilities: ExtensionCapabilities = Field(default_factory=ExtensionCapabilities)
    events: ExtensionEvents = Field(default_factory=ExtensionEvents)
    configuration: ExtensionConfiguration = Field(default_factory=ExtensionConfiguration)
    resources: ExtensionResources = Field(default_factory=ExtensionResources)
    ui: ExtensionUI = Field(default_factory=ExtensionUI)
    migrations: ExtensionMigrations = Field(default_factory=ExtensionMigrations)
    health: ExtensionHealth = Field(default_factory=ExtensionHealth)
    entrypoints: ExtensionEntrypoints

    @model_validator(mode="after")
    def validate_identity_and_entrypoints(self) -> "ExtensionManifest":
        if not _EXTENSION_ID_RE.fullmatch(self.id):
            raise ValueError("extension id must be a lowercase reverse-DNS identifier")
        if not _SEMVER_RE.fullmatch(self.version):
            raise ValueError("extension version must use SemVer")
        types = tuple(dict.fromkeys(self.types))
        for extension_type in types:
            if not getattr(self.entrypoints, extension_type.value):
                raise ValueError(f"extension type {extension_type.value!r} requires a matching entrypoint")
        object.__setattr__(self, "types", types)
        return self

    @property
    def identity(self) -> tuple[str, str, str]:
        return (self.id, self.version, self.provenance.digest)


# This is the compatibility level of the extension host contract, not a
# marketing/product release number. Packages negotiate against this value.
EXTENSION_HOST_COMPATIBILITY_VERSION = "3.0.0"


class ExtensionDeploymentMode(StrEnum):
    SELF_HOSTED = "self_hosted"
    HOSTED = "hosted"


class ExtensionSignatureStatus(StrEnum):
    VERIFIED = "verified"
    UNSIGNED = "unsigned"
    UNVERIFIED = "unverified"
    INVALID = "invalid"


class ExtensionHealthStatus(StrEnum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


class ExtensionPackageVerification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    digest_verified: bool
    signature_status: ExtensionSignatureStatus
    verifier: str = Field(min_length=1)
    observed_digest: str = Field(pattern=_DIGEST_RE.pattern)
    verified_at: float = Field(default_factory=time.time)
    detail: str | None = None


class ExtensionCapabilityGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(default_factory=lambda: f"ext-grant-{uuid.uuid4().hex}")
    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    installation_id: str = Field(min_length=1)
    capability: str = Field(pattern=_CAPABILITY_RE.pattern)
    resource_ids: tuple[str, ...] = ()
    granted_by: str = Field(min_length=1)
    granted_at: float = Field(default_factory=time.time)
    revoked_at: float | None = None
    revoked_by: str | None = None
    revoke_reason: str | None = None

    @model_validator(mode="after")
    def normalize_resources(self) -> "ExtensionCapabilityGrant":
        self.resource_ids = tuple(dict.fromkeys(self.resource_ids))
        return self

    @property
    def active(self) -> bool:
        return self.revoked_at is None


class ExtensionInstallation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(default_factory=lambda: f"extension-{uuid.uuid4().hex}")
    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    manifest: ExtensionManifest
    manifest_history: tuple[ExtensionManifest, ...] = ()
    package_verification: ExtensionPackageVerification
    package_ref: str | None = None
    deployment_mode: ExtensionDeploymentMode
    lifecycle: ExtensionLifecycleState = ExtensionLifecycleState.INSTALLED
    installed_by: str = Field(min_length=1)
    installed_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    configuration_record_ids: tuple[str, ...] = ()
    secret_bindings: dict[str, str] = Field(default_factory=dict)
    health_status: ExtensionHealthStatus = ExtensionHealthStatus.UNKNOWN
    consecutive_health_failures: int = Field(default=0, ge=0)
    last_health_at: float | None = None
    last_health_detail: str | None = None
    quarantine_reason: str | None = None
    disabled_reason: str | None = None
    removed_at: float | None = None
    removal_reason: str | None = None
    incompatible_reason: str | None = None

    @model_validator(mode="after")
    def normalize_references(self) -> "ExtensionInstallation":
        self.configuration_record_ids = tuple(dict.fromkeys(self.configuration_record_ids))
        self.secret_bindings = {
            key: self.secret_bindings[key]
            for key in sorted(self.secret_bindings)
        }
        return self

    @property
    def extension_id(self) -> str:
        return self.manifest.id

    @property
    def version(self) -> str:
        return self.manifest.version


class ExtensionAuditEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(default_factory=lambda: f"ext-event-{uuid.uuid4().hex}")
    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    installation_id: str = Field(min_length=1)
    extension_id: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    actor_id: str = Field(min_length=1)
    occurred_at: float = Field(default_factory=time.time)
    details: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class ExtensionState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    installations: list[ExtensionInstallation] = Field(default_factory=list)
    grants: list[ExtensionCapabilityGrant] = Field(default_factory=list)
    events: list[ExtensionAuditEvent] = Field(default_factory=list)


class ExtensionInstallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifest: ExtensionManifest
    observed_digest: str = Field(pattern=_DIGEST_RE.pattern)
    deployment_mode: ExtensionDeploymentMode = ExtensionDeploymentMode.SELF_HOSTED


class ExtensionPackageInstallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deployment_mode: ExtensionDeploymentMode = ExtensionDeploymentMode.SELF_HOSTED


class ExtensionConfigureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    configuration_record_ids: tuple[str, ...] = ()
    secret_bindings: dict[str, str] = Field(default_factory=dict)


class ExtensionGrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capabilities: tuple[str, ...] = ()
    resource_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def normalize(self) -> "ExtensionGrantRequest":
        capabilities = tuple(dict.fromkeys(self.capabilities))
        invalid = [value for value in capabilities if not _CAPABILITY_RE.fullmatch(value)]
        if invalid:
            raise ValueError(f"invalid extension capability: {invalid[0]!r}")
        self.capabilities = capabilities
        self.resource_ids = tuple(dict.fromkeys(self.resource_ids))
        return self


class ExtensionGrantRevokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: str = Field(default="revoked", min_length=1)


class ExtensionLifecycleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: str | None = None


class ExtensionHealthReport(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    status: ExtensionHealthStatus
    detail: str | None = None


class ExtensionUpgradeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifest: ExtensionManifest
    observed_digest: str = Field(pattern=_DIGEST_RE.pattern)
    migration_evidence_id: str | None = None


class ExtensionPackageUpgradeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    migration_evidence_id: str | None = None


class ExtensionRemoveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: str = Field(default="removed", min_length=1)
    preserve_tombstone: bool = True
