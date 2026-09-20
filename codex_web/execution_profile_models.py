from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.execution_workers import WorkerCapability
from codex_web.models import SandboxMode


EXECUTION_PROFILE_CATALOG_KIND = "execution-profile-catalog"
EXECUTION_PROFILE_CATALOG_ID = "execution-profiles.default"
EXECUTION_PROFILE_CATALOG_SCHEMA_VERSION = "1.0"


class ExecutionWorkspaceMode(StrEnum):
    REPOSITORY = "repository"
    SCRATCH = "scratch"


class ExecutionRepositoryAccess(StrEnum):
    NONE = "none"
    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"


class ExecutionProfileContract(BaseModel):
    """Versioned execution authority profile resolved from Definition Registry."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    workspace_mode: ExecutionWorkspaceMode
    repository_access: ExecutionRepositoryAccess
    required_worker_capabilities: tuple[WorkerCapability, ...]
    allowed_sandboxes: tuple[SandboxMode, ...]
    allowed_control_plane_operations: tuple[str, ...] = ()
    authority_explanation: str = Field(min_length=1)
    lifecycle: str = Field(default="active", pattern=r"^(active|deprecated|disabled)$")

    @model_validator(mode="after")
    def validate_profile(self) -> "ExecutionProfileContract":
        if not self.required_worker_capabilities:
            raise ValueError("execution profile requires worker capabilities")
        if not self.allowed_sandboxes:
            raise ValueError("execution profile requires at least one sandbox")
        if self.workspace_mode == ExecutionWorkspaceMode.SCRATCH:
            if self.repository_access != ExecutionRepositoryAccess.NONE:
                raise ValueError("scratch execution profile cannot grant repository access")
            if WorkerCapability.GIT in self.required_worker_capabilities:
                raise ValueError("scratch execution profile cannot require git capability")
        elif self.repository_access == ExecutionRepositoryAccess.NONE:
            raise ValueError("repository workspace profile requires repository access")
        if any(not item.strip() for item in self.allowed_control_plane_operations):
            raise ValueError("control-plane operation names cannot be empty")
        return self

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "workspaceMode": self.workspace_mode.value,
            "repositoryAccess": self.repository_access.value,
            "requiredWorkerCapabilities": [
                item.value for item in self.required_worker_capabilities
            ],
            "allowedSandboxes": list(self.allowed_sandboxes),
            "allowedControlPlaneOperations": list(
                self.allowed_control_plane_operations
            ),
            "authorityExplanation": self.authority_explanation,
            "lifecycle": self.lifecycle,
        }


class ExecutionProfileCatalogDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    profiles: tuple[ExecutionProfileContract, ...]
    default_profile_id: str
    role_to_profile: dict[str, str] = {}

    @model_validator(mode="after")
    def validate_catalog(self) -> "ExecutionProfileCatalogDefinition":
        ids = [profile.id for profile in self.profiles]
        if len(ids) != len(set(ids)):
            raise ValueError("execution profile ids must be unique")
        known = set(ids)
        if self.default_profile_id not in known:
            raise ValueError("default execution profile is unknown")
        unknown = set(self.role_to_profile.values()) - known
        if unknown:
            raise ValueError(
                "execution role mapping references unknown profiles: "
                + ", ".join(sorted(unknown))
            )
        disabled = {
            profile.id
            for profile in self.profiles
            if profile.lifecycle == "disabled"
        }
        if self.default_profile_id in disabled:
            raise ValueError("default execution profile cannot be disabled")
        if disabled.intersection(self.role_to_profile.values()):
            raise ValueError("execution role mapping cannot target disabled profiles")
        return self

    @property
    def profile_map(self) -> dict[str, ExecutionProfileContract]:
        return {profile.id: profile for profile in self.profiles}


def validate_execution_profile_catalog(payload: dict[str, Any]) -> dict[str, Any]:
    return ExecutionProfileCatalogDefinition.model_validate(payload).model_dump(
        mode="json"
    )
