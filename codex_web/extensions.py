from __future__ import annotations

import re
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
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

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
