from __future__ import annotations

from enum import StrEnum
import unicodedata
from typing import Literal, Any, Awaitable, Callable, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from codex_web.resources import Resource
from codex_web.code_host_merge_requirements import CodeHostMergeRequirementsFact


class CodeHostCapability(StrEnum):
    REPOSITORY_READ = "repository.read"
    REFS_READ = "refs.read"
    COMMITS_READ = "commits.read"
    PULL_REQUEST_READ = "pull_request.read"
    REVIEW_READ = "review.read"
    MERGE_REQUIREMENTS_READ = "pull_request.merge_requirements.read"
    CHECKS_READ = "checks.read"
    JOB_LOGS_READ = "job_logs.read"
    ARTIFACTS_READ = "artifacts.read"
    RELEASE_READ = "release.read"
    COMPARE_READ = "compare.read"
    WEBHOOK_NORMALIZE = "webhook.normalize"


class CodeHostError(RuntimeError):
    pass


class CodeHostUnsupportedCapabilityError(CodeHostError):
    pass


# Provider-neutral query protocol, not a mutable operational definition.
PULL_REQUEST_QUERY_STATES = ("open", "closed", "all")


def normalize_pull_request_state(state: str | None) -> str:
    normalized = str(state or "open").strip().casefold()
    if normalized not in PULL_REQUEST_QUERY_STATES:
        raise CodeHostError("pull-request state must be open, closed, or all")
    return normalized


def validate_artifact_member_path(path: str) -> str:
    # Provider paths are data, never URLs or filesystem destinations.
    if (
        not isinstance(path, str) or not path or len(path) > 1024
        or any(unicodedata.category(char) == "Cc" for char in path)
        or any(char in path for char in "\\:%")
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        raise CodeHostError("artifact member_path must be a bounded relative path without traversal")
    return path


class CodeHostTransientError(CodeHostError):
    pass


class CodeHostProviderBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    id: str = Field(min_length=1)
    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    provider_type: str = Field(min_length=1)
    provider_instance: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    credential_ref: str | None = None
    capabilities: tuple[CodeHostCapability, ...]


class CodeHostRepositoryFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    resource_id: str
    provider_type: str
    provider_instance: str
    external_id: str
    name: str
    full_name: str
    default_branch: str | None = None
    web_url: str | None = None
    archived: bool = False
    visibility: str | None = None


class CodeHostRefFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    kind: str
    revision: str


class CodeHostCommitFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    revision: str
    message: str = ""
    author_external_id: str | None = None
    authored_at: float | None = None
    web_url: str | None = None


class CodeHostRevisionMaterializationFact(BaseModel):
    """Attested provider revision copied into an assignment workspace object store."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    resource_id: str
    provider_type: str
    provider_instance: str
    repository_full_name: str
    default_branch: str
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    fetched: bool = True
    history_state: Literal["complete"] = "complete"
    readiness: Literal["ready"] = "ready"
    next_action: Literal["refresh_exact_revision"] = "refresh_exact_revision"


class CodeHostPullRequestFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    external_id: str
    number: int | None = None
    title: str
    state: str
    source_ref: str | None = None
    target_ref: str | None = None
    author_external_id: str | None = None
    web_url: str | None = None
    draft: bool = False
    merge_revision: str | None = None


class CodeHostReviewFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    external_id: str
    state: str
    author_external_id: str | None = None
    submitted_at: float | None = None
    web_url: str | None = None


class CodeHostCheckFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    external_id: str
    name: str
    state: str
    conclusion: str | None = None
    revision: str | None = None
    web_url: str | None = None


class CodeHostValidationRunFact(BaseModel):
    """Provider-observed workflow provenance; callers cannot assert applicability."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    external_id: str
    workflow_path: str
    event: str
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    head_branch: str | None = None
    state: str
    conclusion: str | None = None
    selection_full: bool | None = None
    selection_revision: str | None = None
    tested_revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    control_revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    security_configuration: dict[str, Any] | None = None
    security_result: dict[str, Any] | None = None
    recovery_publication: dict[str, Any] | None = None
    checks: tuple[CodeHostCheckFact, ...]


class CodeHostJobLogFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: int = Field(ge=1)
    content: str
    byte_count: int = Field(ge=0)
    truncated: bool = False
    redacted: bool = False


class CodeHostArtifactFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    external_id: str
    name: str
    size_bytes: int = Field(default=0, ge=0)
    expired: bool = False
    created_at: float | None = None
    expires_at: float | None = None
    web_url: str | None = None


class CodeHostArtifactDownloadFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    external_id: str
    content_base64: str
    byte_count: int = Field(ge=0)
    truncated: bool = False
    media_type: str = "application/zip"


class CodeHostReleaseFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    external_id: str
    tag: str
    name: str
    state: str = "published"
    web_url: str | None = None


class CodeHostCompareFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    base_revision: str
    head_revision: str
    ahead_by: int | None = None
    behind_by: int | None = None
    total_commits: int | None = None
    changed_files: tuple[str, ...] = ()


class CodeHostWebhookFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_type: str
    provider_instance: str
    event_id: str
    event_kind: str
    canonical_event_type: str
    repository_external_id: str | None = None
    subject_external_id: str | None = None
    action: str | None = None
    state: str | None = None
    web_url: str | None = None
    occurred_at: float | None = None
    provider_payload: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class CodeHostProvider(Protocol):
    provider_type: str

    def capabilities(self) -> frozenset[CodeHostCapability]: ...

    async def merge_requirements(self, binding: CodeHostProviderBinding, resource: Resource,
        external_id: str, *, credential: str | None,
        checkpoint: Callable[[], Awaitable[None]] | None = None) -> CodeHostMergeRequirementsFact: ...

    async def repository(
        self,
        binding: CodeHostProviderBinding,
        resource: Resource,
        *,
        credential: str | None,
    ) -> CodeHostRepositoryFact: ...

    async def refs(
        self,
        binding: CodeHostProviderBinding,
        resource: Resource,
        *,
        credential: str | None,
    ) -> tuple[CodeHostRefFact, ...]: ...

    async def commit(
        self,
        binding: CodeHostProviderBinding,
        resource: Resource,
        revision: str,
        *,
        credential: str | None,
    ) -> CodeHostCommitFact: ...

    async def pull_request(
        self,
        binding: CodeHostProviderBinding,
        resource: Resource,
        external_id: str,
        *,
        credential: str | None,
    ) -> CodeHostPullRequestFact: ...

    async def reviews(
        self,
        binding: CodeHostProviderBinding,
        resource: Resource,
        external_id: str,
        *,
        credential: str | None,
    ) -> tuple[CodeHostReviewFact, ...]: ...

    async def checks(
        self,
        binding: CodeHostProviderBinding,
        resource: Resource,
        revision: str,
        *,
        credential: str | None,
    ) -> tuple[CodeHostCheckFact, ...]: ...

    async def releases(
        self,
        binding: CodeHostProviderBinding,
        resource: Resource,
        *,
        credential: str | None,
    ) -> tuple[CodeHostReleaseFact, ...]: ...

    async def compare(
        self,
        binding: CodeHostProviderBinding,
        resource: Resource,
        base_revision: str,
        head_revision: str,
        *,
        credential: str | None,
    ) -> CodeHostCompareFact: ...

    def normalize_webhook(
        self,
        binding: CodeHostProviderBinding,
        payload: dict[str, Any],
        *,
        event_kind: str,
        event_id: str,
    ) -> CodeHostWebhookFact: ...
