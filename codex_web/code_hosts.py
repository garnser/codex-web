from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from codex_web.resources import Resource


class CodeHostCapability(StrEnum):
    REPOSITORY_READ = "repository.read"
    REFS_READ = "refs.read"
    COMMITS_READ = "commits.read"
    PULL_REQUEST_READ = "pull_request.read"
    REVIEW_READ = "review.read"
    CHECKS_READ = "checks.read"
    RELEASE_READ = "release.read"
    COMPARE_READ = "compare.read"
    WEBHOOK_NORMALIZE = "webhook.normalize"


class CodeHostError(RuntimeError):
    pass


class CodeHostUnsupportedCapabilityError(CodeHostError):
    pass


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
