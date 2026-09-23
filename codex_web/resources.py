from __future__ import annotations

import time
import uuid
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.identity import TenantScope


class ResourceType(StrEnum):
    REPOSITORY = "repository"
    SERVICE = "service"
    ENVIRONMENT = "environment"
    DEPLOYMENT_TARGET = "deployment_target"
    CLOUD_ACCOUNT = "cloud_account"
    DATABASE = "database"
    OTHER = "other"


class ResourceLifecycle(StrEnum):
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    DISABLED = "disabled"
    DELETED = "deleted"


class ResourceSensitivity(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class ResourceRisk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class RepositoryTargetSource(StrEnum):
    WORK_ITEM = "work_item"
    EXPLICIT = "explicit"
    THREAD_PROFILE = "thread_profile"
    ROUTING_RULE = "routing_rule"
    SINGLE_REPOSITORY = "single_repository"
    PROJECT_POLICY = "project_policy"
    ORCHESTRATION_ONLY = "orchestration_only"


class RepositoryWriteMode(StrEnum):
    SINGLE = "single"
    COORDINATED = "coordinated"


class RepositoryTargetEvidence(BaseModel):
    """One canonical selector that contributed to repository authority."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    source: RepositoryTargetSource
    repository_id: str = Field(min_length=1)
    source_ref: str | None = None


class RepositoryExecutionTarget(BaseModel):
    """Canonical repository authority selected for one execution."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    mutable_repository_id: str | None = None
    read_only_repository_ids: tuple[str, ...] = ()
    source: RepositoryTargetSource
    source_ref: str | None = None
    selection_evidence: tuple[RepositoryTargetEvidence, ...] = ()

    @model_validator(mode="after")
    def normalize(self) -> "RepositoryExecutionTarget":
        read_only = tuple(
            dict.fromkeys(
                value.strip()
                for value in self.read_only_repository_ids
                if value and value.strip()
            )
        )
        if self.mutable_repository_id:
            mutable = self.mutable_repository_id.strip()
            object.__setattr__(self, "mutable_repository_id", mutable)
            read_only = tuple(value for value in read_only if value != mutable)
        object.__setattr__(self, "read_only_repository_ids", read_only)
        if self.source == RepositoryTargetSource.ORCHESTRATION_ONLY:
            if self.mutable_repository_id is not None:
                raise ValueError("orchestration-only target cannot be mutable")
        elif self.mutable_repository_id is None:
            raise ValueError("repository execution target requires mutable repository")
        return self


class RepositoryExecutionScope(BaseModel):
    """Canonical writable/read-only repository set fixed before execution."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    writable_repository_ids: tuple[str, ...] = ()
    read_only_repository_ids: tuple[str, ...] = ()
    write_mode: RepositoryWriteMode = RepositoryWriteMode.SINGLE
    source: RepositoryTargetSource
    source_ref: str | None = None
    selection_evidence: tuple[RepositoryTargetEvidence, ...] = ()

    @model_validator(mode="after")
    def normalize(self) -> "RepositoryExecutionScope":
        writable = tuple(
            dict.fromkeys(
                value.strip()
                for value in self.writable_repository_ids
                if value and value.strip()
            )
        )
        read_only = tuple(
            dict.fromkeys(
                value.strip()
                for value in self.read_only_repository_ids
                if value and value.strip()
            )
        )
        overlap = set(writable) & set(read_only)
        if overlap:
            raise ValueError("writable repositories cannot also be read-only context")
        if self.source == RepositoryTargetSource.ORCHESTRATION_ONLY:
            if writable:
                raise ValueError("orchestration-only scope cannot contain writable repositories")
            if self.write_mode == RepositoryWriteMode.COORDINATED:
                raise ValueError("orchestration-only scope cannot use coordinated write mode")
        elif self.write_mode == RepositoryWriteMode.COORDINATED:
            if len(writable) < 2:
                raise ValueError("coordinated repository scope requires at least two writable repositories")
        else:
            if len(writable) > 1:
                raise ValueError("single repository scope cannot contain multiple writable repositories")
            if not writable:
                raise ValueError("repository execution scope requires a writable repository")
        object.__setattr__(self, "writable_repository_ids", writable)
        object.__setattr__(self, "read_only_repository_ids", read_only)
        return self

    @classmethod
    def from_target(cls, target: RepositoryExecutionTarget) -> "RepositoryExecutionScope":
        return cls(
            organization_id=target.organization_id,
            workspace_id=target.workspace_id,
            project_id=target.project_id,
            writable_repository_ids=(
                (target.mutable_repository_id,)
                if target.mutable_repository_id is not None
                else ()
            ),
            read_only_repository_ids=target.read_only_repository_ids,
            write_mode=RepositoryWriteMode.SINGLE,
            source=target.source,
            source_ref=target.source_ref,
            selection_evidence=target.selection_evidence,
        )


class ResourceRelationshipType(StrEnum):
    CONTAINS = "contains"
    DEPENDS_ON = "depends_on"
    IMPLEMENTS = "implements"
    DEPLOYS_TO = "deploys_to"
    HOSTED_IN = "hosted_in"
    CONNECTS_TO = "connects_to"


class ResourceAlias(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    namespace: str = Field(min_length=1)
    value: str = Field(min_length=1)
    provider: str | None = None

    def key(self) -> tuple[str, str, str]:
        return (
            self.namespace.casefold(),
            (self.provider or "").casefold(),
            self.value.casefold(),
        )


class ResourceProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    provider: str | None = None
    provider_instance: str | None = None
    external_id: str | None = None
    external_url: str | None = None
    discovered_at: float | None = None
    last_seen_at: float | None = None


class Resource(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(default_factory=lambda: f"resource-{uuid.uuid4().hex}")
    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    resource_type: ResourceType
    name: str = Field(min_length=1)
    description: str | None = None
    owner_identity_id: str | None = None
    sensitivity: ResourceSensitivity = ResourceSensitivity.INTERNAL
    risk: ResourceRisk = ResourceRisk.MEDIUM
    lifecycle: ResourceLifecycle = ResourceLifecycle.ACTIVE
    aliases: list[ResourceAlias] = Field(default_factory=list)
    provenance: ResourceProvenance | None = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    @model_validator(mode="after")
    def normalize_aliases(self) -> "Resource":
        unique: dict[tuple[str, str, str], ResourceAlias] = {}
        for alias in self.aliases:
            unique[alias.key()] = alias
        self.aliases = list(unique.values())
        return self

    @property
    def tenant(self) -> TenantScope:
        return TenantScope(
            organization_id=self.organization_id,
            workspace_id=self.workspace_id,
        )


class ResourceRelationship(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"relationship-{uuid.uuid4().hex}")
    organization_id: str
    workspace_id: str
    from_resource_id: str
    to_resource_id: str
    relationship_type: ResourceRelationshipType
    created_at: float = Field(default_factory=time.time)
    created_by: str | None = None


class ProjectResourceBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    organization_id: str
    workspace_id: str
    resource_id: str
    purpose: str | None = None
    created_at: float = Field(default_factory=time.time)
    created_by: str | None = None


class ResourceCatalogState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    resources: list[Resource] = Field(default_factory=list)
    relationships: list[ResourceRelationship] = Field(default_factory=list)
    project_bindings: list[ProjectResourceBinding] = Field(default_factory=list)


class ResourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    resource_type: ResourceType
    name: str = Field(min_length=1)
    description: str | None = None
    owner_identity_id: str | None = None
    sensitivity: ResourceSensitivity = ResourceSensitivity.INTERNAL
    risk: ResourceRisk = ResourceRisk.MEDIUM
    aliases: list[ResourceAlias] = Field(default_factory=list)
    provenance: ResourceProvenance | None = None


class ResourceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str | None = None
    description: str | None = None
    owner_identity_id: str | None = None
    sensitivity: ResourceSensitivity | None = None
    risk: ResourceRisk | None = None
    lifecycle: ResourceLifecycle | None = None
    aliases: list[ResourceAlias] | None = None
    provenance: ResourceProvenance | None = None


class RelationshipCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_resource_id: str = Field(min_length=1)
    to_resource_id: str = Field(min_length=1)
    relationship_type: ResourceRelationshipType


class ProjectResourceBindCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    project_id: str = Field(min_length=1)
    resource_id: str = Field(min_length=1)
    purpose: str | None = None
