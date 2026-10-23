from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_serializer, model_validator


EXECUTION_PROFILE_CATALOG_KIND = "execution-profile-catalog"
EXECUTION_PROFILE_CATALOG_ID = "execution.profiles.default"
EXECUTION_PROFILE_CATALOG_SCHEMA_VERSION = "1.0"
ADMINISTRATIVE_EXECUTION_PROFILE_SCHEMA_VERSION = "1.1"
COORDINATION_EXECUTION_PROFILE_SCHEMA_VERSION = "1.2"
HOME_OPERATOR_EXECUTION_PROFILE_SCHEMA_VERSION = "1.3"
HOME_HOST_COMMAND_CONTRACT = "home-host-command/1.0"


class CoordinationChildProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    profile_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1)


class ProjectCoordinationEnvelope(BaseModel):
    """Opt-in declaration; neither this data nor repository access grants authority."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    repository_resource_id: str = Field(pattern=r"^resource-[A-Za-z0-9-]+$", max_length=128)
    child_profiles: tuple[CoordinationChildProfile, ...] = Field(min_length=1, max_length=2)
    max_delivery_threads: int = Field(default=2, ge=1, le=2)

    @model_validator(mode="after")
    def distinct_children(self):
        if len({v.profile_id for v in self.child_profiles}) != len(self.child_profiles):
            raise ValueError("coordination child profiles must be distinct")
        return self


class HomeChildProfilePin(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)
    profile_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1, strict=True)


class HomeProjectTarget(BaseModel):
    """Explicit targets and invokable pins; declarations never grant authority."""
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)
    project_id: str = Field(min_length=1, max_length=128)
    child_profiles: tuple[HomeChildProfilePin, ...] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def distinct_profiles(self):
        if len({p.profile_id for p in self.child_profiles}) != len(self.child_profiles):
            raise ValueError("Home target child profiles must be distinct")
        return self


class HomeOperatorEnvelope(BaseModel):
    """Project-scoped opt-in to trusted worker actions and exact delegation."""
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)
    host_resource_id: str = Field(pattern=r"^resource-[A-Za-z0-9-]+$", max_length=128)
    trusted_worker_ids: tuple[str, ...] = Field(min_length=1, max_length=8)
    # This describes the privileged suboperation, never the isolated native turn.
    host_network_access: Literal[True]
    host_command_contract: Literal["home-host-command/1.0"] = HOME_HOST_COMMAND_CONTRACT
    max_command_seconds: int = Field(default=60, ge=1, le=600, strict=True)
    max_output_bytes: int = Field(default=16384, ge=1024, le=65536, strict=True)
    host_output_key_id: str | None = Field(default=None, min_length=1, max_length=128)
    project_targets: tuple[HomeProjectTarget, ...] = Field(default=(), max_length=8)

    @field_validator('host_network_access', mode='before')
    @classmethod
    def explicit_network_acknowledgement(cls, value):
        if value is not True:
            raise ValueError('Trusted host networking requires explicit true acknowledgement')
        return value

    @model_validator(mode="after")
    def exact_targets(self):
        if (len(set(self.trusted_worker_ids)) != len(self.trusted_worker_ids)
                or any(not v or v != v.strip() or len(v) > 128 for v in self.trusted_worker_ids)):
            raise ValueError("Home trusted workers must be distinct canonical worker IDs")
        if len({t.project_id for t in self.project_targets}) != len(self.project_targets):
            raise ValueError("Home target Projects must be distinct")
        return self

ExecutionWorkspaceMode = Literal["repository", "scratch"]
ExecutionRepositoryAccess = Literal["mutable", "read-only", "none"]
ExecutionWorkerCapabilityName = Literal[
    "git",
    "command_execution",
    "container",
    "network",
    "artifact_upload",
]


class ExecutionProfileContract(BaseModel):
    """Definition-owned execution environment and worker-capability contract."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    workspace_mode: ExecutionWorkspaceMode
    repository_access: ExecutionRepositoryAccess
    required_worker_capabilities: tuple[ExecutionWorkerCapabilityName, ...]
    control_plane_operations: tuple[str, ...] = ()
    administrative_repository_ids: tuple[str, ...] = Field(default=(), max_length=32)
    project_coordination: ProjectCoordinationEnvelope | None = None
    home_operator: HomeOperatorEnvelope | None = None
    network_enabled: Literal[False] = False
    host_mutation: Literal[False] = False

    @model_validator(mode="after")
    def validate_profile(self) -> "ExecutionProfileContract":
        capabilities = tuple(dict.fromkeys(self.required_worker_capabilities))
        operations = tuple(
            dict.fromkeys(
                value.strip()
                for value in self.control_plane_operations
                if value.strip()
            )
        )
        object.__setattr__(self, "required_worker_capabilities", capabilities)
        object.__setattr__(self, "control_plane_operations", operations)
        administrative = self.administrative_repository_ids
        if self.home_operator and (self.id != "orchestration-only" or administrative
                or self.project_coordination or self.workspace_mode != "scratch"
                or self.repository_access != "none"):
            raise ValueError("Home operator requires isolated orchestration-only scratch execution")
        if self.project_coordination and (self.id != "project-governor" or administrative):
            raise ValueError("project coordination requires the project-governor engine profile")
        if self.id == "project-governor" and (not self.project_coordination or self.workspace_mode != "repository"
                or self.repository_access != "mutable" or not operations):
            raise ValueError("project-governor requires an explicit coordination envelope and operation allowlist")
        if administrative:
            if self.id != "orchestration-admin":
                raise ValueError("administrative targets require the orchestration-admin engine profile")
            if len(set(administrative)) != len(administrative):
                raise ValueError("administrative repository targets must be distinct")
            if any(not value.startswith("resource-") or len(value) > 128
                   or value != value.strip() for value in administrative):
                raise ValueError("administrative targets must be bounded canonical resource IDs")
            if self.workspace_mode != "scratch" or self.repository_access != "none":
                raise ValueError("administrative targets require scratch execution without repository access")
            if not operations:
                raise ValueError("administrative targets require explicit control-plane operations")
        if "command_execution" not in capabilities:
            raise ValueError("execution profile requires command_execution capability")
        if self.workspace_mode == "repository":
            if self.repository_access == "none":
                raise ValueError("repository workspace requires repository access")
            if "git" not in capabilities:
                raise ValueError("repository workspace requires git capability")
        else:
            if self.repository_access != "none":
                raise ValueError("scratch workspace cannot grant repository access")
            if "git" in capabilities:
                raise ValueError("scratch workspace cannot require git capability")
        if "network" in capabilities:
            raise ValueError(
                "execution profile network access requires a separately governed network profile"
            )
        return self

    @model_serializer(mode="wrap")
    def serialize(self, handler):
        result = handler(self)
        if not self.administrative_repository_ids:
            result.pop("administrative_repository_ids", None)
        if self.project_coordination is None:
            result.pop("project_coordination", None)
        if self.home_operator is None:
            result.pop("home_operator", None)
        return result

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "workspaceMode": self.workspace_mode,
            "repositoryAccess": self.repository_access,
            "requiredWorkerCapabilities": list(self.required_worker_capabilities),
            "controlPlaneOperations": list(self.control_plane_operations),
            "administrativeRepositoryIds": list(self.administrative_repository_ids),
            "networkEnabled": self.network_enabled,
            "hostMutation": self.host_mutation,
            **({"projectCoordination": self.project_coordination.model_dump(mode="json")}
               if self.project_coordination else {}),
            **({"homeOperator": self.home_operator.model_dump(mode="json")}
               if self.home_operator else {}),
        }


class ExecutionProfileCatalogDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    profiles: tuple[ExecutionProfileContract, ...]
    default_profile_id: str = "repository-write"

    @model_validator(mode="after")
    def validate_catalog(self) -> "ExecutionProfileCatalogDefinition":
        ids = [profile.id for profile in self.profiles]
        if len(ids) != len(set(ids)):
            raise ValueError("execution profile ids must be unique")
        required = {"repository-write", "orchestration-only"}
        missing = required - set(ids)
        if missing:
            raise ValueError(
                "execution profile catalog missing required profiles: "
                + ", ".join(sorted(missing))
            )
        if self.default_profile_id not in set(ids):
            raise ValueError("default execution profile is not defined")
        return self

    @property
    def profile_map(self) -> dict[str, ExecutionProfileContract]:
        return {profile.id: profile for profile in self.profiles}


def validate_execution_profile_catalog(payload: dict[str, Any]) -> dict[str, Any]:
    catalog = ExecutionProfileCatalogDefinition.model_validate(payload)
    if any(profile.home_operator for profile in catalog.profiles):
        raise ValueError("Home operator requires execution-profile-catalog schema 1.3")
    if any(profile.administrative_repository_ids for profile in catalog.profiles):
        raise ValueError("administrative targets require execution-profile-catalog schema 1.1")
    if any(profile.project_coordination for profile in catalog.profiles):
        raise ValueError("project coordination requires execution-profile-catalog schema 1.2")
    return catalog.model_dump(mode="json")


def validate_administrative_execution_profile_catalog(payload: dict[str, Any]) -> dict[str, Any]:
    catalog = ExecutionProfileCatalogDefinition.model_validate(payload)
    if any(profile.home_operator for profile in catalog.profiles):
        raise ValueError("Home operator requires execution-profile-catalog schema 1.3")
    if any(profile.project_coordination for profile in catalog.profiles):
        raise ValueError("project coordination requires execution-profile-catalog schema 1.2")
    return catalog.model_dump(mode="json")


def validate_coordination_execution_profile_catalog(payload: dict[str, Any]) -> dict[str, Any]:
    catalog = ExecutionProfileCatalogDefinition.model_validate(payload)
    if any(profile.home_operator for profile in catalog.profiles):
        raise ValueError("Home operator requires execution-profile-catalog schema 1.3")
    return catalog.model_dump(mode="json")


def validate_home_execution_profile_catalog(payload: dict[str, Any]) -> dict[str, Any]:
    return ExecutionProfileCatalogDefinition.model_validate(payload).model_dump(mode="json")
