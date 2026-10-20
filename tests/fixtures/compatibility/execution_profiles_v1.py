from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


EXECUTION_PROFILE_CATALOG_KIND = "execution-profile-catalog"
EXECUTION_PROFILE_CATALOG_ID = "execution.profiles.default"
EXECUTION_PROFILE_CATALOG_SCHEMA_VERSION = "1.0"

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

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "workspaceMode": self.workspace_mode,
            "repositoryAccess": self.repository_access,
            "requiredWorkerCapabilities": list(self.required_worker_capabilities),
            "controlPlaneOperations": list(self.control_plane_operations),
            "networkEnabled": self.network_enabled,
            "hostMutation": self.host_mutation,
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
    return ExecutionProfileCatalogDefinition.model_validate(payload).model_dump(
        mode="json"
    )
