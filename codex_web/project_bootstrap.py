from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from codex_web.execution_workers import WorkerCapability
from codex_web.models import SandboxMode
from codex_web.workspaces import WorkspaceMapper, WorkspacePathError


PROJECT_BOOTSTRAP_API_VERSION = "codex-web/v1"
PROJECT_BOOTSTRAP_KIND = "ProjectBootstrap"
PROJECT_BOOTSTRAP_CONTRACT_VERSION = "1.0"


class ProjectBootstrapManifestError(ValueError):
    """Secret-safe manifest validation error."""


class _StrictManifestModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        str_strip_whitespace=True,
    )


class ProjectBootstrapProject(_StrictManifestModel):
    name: str = Field(min_length=1)
    organization: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    workspace: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ProjectBootstrapRepository(_StrictManifestModel):
    id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    path: str = Field(min_length=1)
    default: bool = False


class ProjectBootstrapTaskSource(_StrictManifestModel):
    type: str = Field(min_length=1)
    authoritative: bool = True
    secret_ref: str | None = Field(default=None, alias="secretRef")

    @model_validator(mode="after")
    def canonical_authority(self) -> "ProjectBootstrapTaskSource":
        if not self.authoritative:
            raise ValueError(
                "ProjectBootstrap taskSource must be authoritative; "
                "non-authoritative provider configuration belongs outside the Project binding"
            )
        return self


class ProjectBootstrapExecution(_StrictManifestModel):
    repository_selection: Literal[
        "explicit",
        "coordinated",
        "single",
        "default",
    ] = Field(
        default="explicit",
        alias="repositorySelection",
    )
    required_capabilities: tuple[WorkerCapability, ...] = Field(
        default=(WorkerCapability.COMMAND_EXECUTION,),
        alias="requiredCapabilities",
    )
    sandbox: SandboxMode = "workspace-write"

    @model_validator(mode="after")
    def normalize_capabilities(self) -> "ProjectBootstrapExecution":
        normalized = tuple(dict.fromkeys(self.required_capabilities))
        object.__setattr__(self, "required_capabilities", normalized)
        return self


class ProjectBootstrapSlackBackfill(_StrictManifestModel):
    enabled: bool = False


class ProjectBootstrapSlack(_StrictManifestModel):
    connection_ref: str = Field(min_length=1, alias="connectionRef")
    backfill: ProjectBootstrapSlackBackfill = Field(
        default_factory=ProjectBootstrapSlackBackfill
    )


class ProjectBootstrapIntegrations(_StrictManifestModel):
    slack: ProjectBootstrapSlack | None = None


class ProjectBootstrapManifest(_StrictManifestModel):
    api_version: Literal["codex-web/v1"] = Field(alias="apiVersion")
    kind: Literal["ProjectBootstrap"] = PROJECT_BOOTSTRAP_KIND
    project: ProjectBootstrapProject
    repositories: tuple[ProjectBootstrapRepository, ...] = ()
    task_source: ProjectBootstrapTaskSource | None = Field(
        default=None,
        alias="taskSource",
    )
    execution: ProjectBootstrapExecution = Field(
        default_factory=ProjectBootstrapExecution
    )
    integrations: ProjectBootstrapIntegrations = Field(
        default_factory=ProjectBootstrapIntegrations
    )

    @model_validator(mode="after")
    def validate_repository_topology(self) -> "ProjectBootstrapManifest":
        ids = [item.id.casefold() for item in self.repositories]
        if len(ids) != len(set(ids)):
            raise ValueError("repository IDs must be unique")
        defaults = [item for item in self.repositories if item.default]
        if len(defaults) > 1:
            raise ValueError("at most one repository may be marked default")
        if (
            self.execution.repository_selection == "single"
            and len(self.repositories) != 1
        ):
            raise ValueError(
                "repositorySelection=single requires exactly one repository"
            )
        if (
            self.execution.repository_selection == "default"
            and len(defaults) != 1
        ):
            raise ValueError(
                "repositorySelection=default requires exactly one default repository"
            )
        return self

    def normalized(self) -> dict[str, Any]:
        return self.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=True,
        )

    def digest(self) -> str:
        encoded = json.dumps(
            self.normalized(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


_ALLOWED_REFERENCE_KEYS = {
    "secretref",
    "connectionref",
}
_FORBIDDEN_SECRET_KEYS = {
    "token",
    "password",
    "passwd",
    "apikey",
    "api_key",
    "credential",
    "credentials",
    "authorization",
    "cookie",
    "secret",
    "secretvalue",
    "secret_value",
    "privatekey",
    "private_key",
}


def _normalized_key(value: object) -> str:
    return str(value).strip().replace("-", "_").casefold()


def _reject_secret_material(value: Any, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            raw = str(key)
            normalized = _normalized_key(raw)
            compact = normalized.replace("_", "")
            if compact not in _ALLOWED_REFERENCE_KEYS and (
                normalized in _FORBIDDEN_SECRET_KEYS
                or compact in {
                    item.replace("_", "")
                    for item in _FORBIDDEN_SECRET_KEYS
                }
            ):
                location = ".".join((*path, raw))
                raise ProjectBootstrapManifestError(
                    f"raw credential material is not allowed at {location}; "
                    "use a canonical reference field instead"
                )
            _reject_secret_material(child, (*path, raw))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_secret_material(child, (*path, str(index)))


def _safe_validation_error(exc: ValidationError) -> ProjectBootstrapManifestError:
    details: list[str] = []
    for error in exc.errors(include_url=False):
        location = ".".join(str(item) for item in error.get("loc", ())) or "manifest"
        details.append(f"{location}: {error.get('msg', 'invalid value')}")
    return ProjectBootstrapManifestError("; ".join(details))


def parse_project_bootstrap_manifest(payload: Any) -> ProjectBootstrapManifest:
    if not isinstance(payload, dict):
        raise ProjectBootstrapManifestError(
            "ProjectBootstrap manifest root must be a mapping"
        )
    _reject_secret_material(payload)
    try:
        return ProjectBootstrapManifest.model_validate(payload)
    except ValidationError as exc:
        raise _safe_validation_error(exc) from None


def load_project_bootstrap_manifest(path: str | Path) -> ProjectBootstrapManifest:
    manifest_path = Path(path)
    try:
        raw = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProjectBootstrapManifestError(
            f"cannot read bootstrap manifest: {manifest_path}"
        ) from exc
    try:
        payload = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        suffix = (
            f" at line {mark.line + 1}, column {mark.column + 1}"
            if mark is not None
            else ""
        )
        raise ProjectBootstrapManifestError(
            f"invalid bootstrap YAML{suffix}"
        ) from None
    return parse_project_bootstrap_manifest(payload)


def resolve_bootstrap_repository_paths(
    manifest: ProjectBootstrapManifest,
    *,
    workspace_mapper: WorkspaceMapper | None = None,
) -> dict[str, Path]:
    mapper = workspace_mapper or WorkspaceMapper.from_environment()
    if mapper.root is None:
        raise ProjectBootstrapManifestError(
            "CODEX_WEB_WORKSPACE_ROOT must be configured for ProjectBootstrap "
            "repository validation"
        )

    resolved: dict[str, Path] = {}
    seen: dict[Path, str] = {}
    for repository in manifest.repositories:
        try:
            path = mapper.runtime_path(repository.path).resolve(strict=True)
        except (OSError, WorkspacePathError) as exc:
            raise ProjectBootstrapManifestError(
                f"repository {repository.id!r} is outside the approved workspace "
                "root or does not exist"
            ) from exc
        if not path.is_dir():
            raise ProjectBootstrapManifestError(
                f"repository {repository.id!r} path is not a directory"
            )
        marker = path / ".git"
        if not (marker.is_dir() or marker.is_file()):
            raise ProjectBootstrapManifestError(
                f"repository {repository.id!r} is not a Git repository"
            )
        if path in seen:
            raise ProjectBootstrapManifestError(
                f"repositories {seen[path]!r} and {repository.id!r} resolve "
                "to the same filesystem path"
            )
        seen[path] = repository.id
        resolved[repository.id] = path
    return resolved


def scaffold_project_bootstrap_manifest(
    *,
    project_name: str,
    organization: str,
    workspace: str,
    repository_path: str,
    repository_id: str = "repository",
) -> ProjectBootstrapManifest:
    return ProjectBootstrapManifest(
        apiVersion=PROJECT_BOOTSTRAP_API_VERSION,
        kind=PROJECT_BOOTSTRAP_KIND,
        project=ProjectBootstrapProject(
            name=project_name,
            organization=organization,
            workspace=workspace,
        ),
        repositories=(
            ProjectBootstrapRepository(
                id=repository_id,
                path=repository_path,
                default=True,
            ),
        ),
        execution=ProjectBootstrapExecution(
            repositorySelection="single",
            requiredCapabilities=(WorkerCapability.COMMAND_EXECUTION,),
            sandbox="workspace-write",
        ),
    )


def dump_project_bootstrap_yaml(manifest: ProjectBootstrapManifest) -> str:
    return yaml.safe_dump(
        manifest.normalized(),
        sort_keys=False,
        allow_unicode=True,
    )
