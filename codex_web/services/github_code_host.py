from __future__ import annotations

from datetime import datetime
from urllib.parse import quote, urlparse

import httpx

from codex_web.canonical_events import CanonicalEventType
from codex_web.code_hosts import (
    CodeHostCapability,
    CodeHostCheckFact,
    CodeHostCommitFact,
    CodeHostCompareFact,
    CodeHostError,
    CodeHostProviderBinding,
    CodeHostPullRequestFact,
    CodeHostRefFact,
    CodeHostReleaseFact,
    CodeHostReviewFact,
    CodeHostRepositoryFact,
    CodeHostTransientError,
    CodeHostWebhookFact,
)
from codex_web.integrations.github_client import GitHubClient
from codex_web.resources import Resource


def _repository_name(resource: Resource) -> str:
    for alias in resource.aliases:
        if (alias.provider or "").casefold() == "github" and alias.namespace in {
            "provider",
            "repository",
            "github-repository",
        }:
            return alias.value
    provenance = resource.provenance
    if provenance and provenance.external_url:
        path = urlparse(provenance.external_url).path.strip("/")
        if path:
            return path.removesuffix(".git")
    if provenance and provenance.external_id and "/" in provenance.external_id:
        return provenance.external_id
    raise CodeHostError(
        "GitHub repository Resource needs a provider alias or external URL locator"
    )


def _timestamp(value: object) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class GitHubCodeHostProvider:
    provider_type = "github"

    def __init__(self, client: GitHubClient | None = None) -> None:
        self.client = client or GitHubClient()

    def capabilities(self) -> frozenset[CodeHostCapability]:
        return frozenset(
            {
                CodeHostCapability.REPOSITORY_READ,
                CodeHostCapability.REFS_READ,
                CodeHostCapability.COMMITS_READ,
                CodeHostCapability.PULL_REQUEST_READ,
                CodeHostCapability.REVIEW_READ,
                CodeHostCapability.CHECKS_READ,
                CodeHostCapability.RELEASE_READ,
                CodeHostCapability.COMPARE_READ,
                CodeHostCapability.WEBHOOK_NORMALIZE,
            }
        )

    @staticmethod
    def _classify(exc: Exception) -> CodeHostError:
        if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
            return CodeHostTransientError(f"{type(exc).__name__}: {exc}")
        status = getattr(exc, "status_code", None)
        if status in {408, 409, 429} or (isinstance(status, int) and status >= 500):
            return CodeHostTransientError(str(exc))
        return CodeHostError(str(exc))

    async def repository(
        self,
        binding: CodeHostProviderBinding,
        resource: Resource,
        *,
        credential: str | None,
    ) -> CodeHostRepositoryFact:
        repository = _repository_name(resource)
        try:
            data = await self.client.get_json(
                binding.base_url,
                f"repos/{repository}",
                token=credential,
            )
        except Exception as exc:
            raise self._classify(exc) from exc
        if not isinstance(data, dict):
            raise CodeHostError("GitHub repository response is invalid")
        owner = data.get("owner") if isinstance(data.get("owner"), dict) else {}
        external_id = str(data.get("id") or repository)
        full_name = str(data.get("full_name") or repository)
        return CodeHostRepositoryFact(
            resource_id=resource.id,
            provider_type=self.provider_type,
            provider_instance=binding.provider_instance,
            external_id=external_id,
            name=str(data.get("name") or full_name.rsplit("/", 1)[-1]),
            full_name=full_name,
            default_branch=data.get("default_branch"),
            web_url=data.get("html_url"),
            archived=bool(data.get("archived")),
            visibility=data.get("visibility") or ("private" if data.get("private") else "public"),
        )

    async def refs(
        self,
        binding: CodeHostProviderBinding,
        resource: Resource,
        *,
        credential: str | None,
    ) -> tuple[CodeHostRefFact, ...]:
        repository = _repository_name(resource)
        try:
            data = await self.client.get_json(
                binding.base_url,
                f"repos/{repository}/git/matching-refs/",
                token=credential,
            )
        except Exception as exc:
            raise self._classify(exc) from exc
        if not isinstance(data, list):
            raise CodeHostError("GitHub refs response is invalid")
        facts = []
        for item in data:
            if not isinstance(item, dict):
                continue
            ref = str(item.get("ref") or "")
            obj = item.get("object") if isinstance(item.get("object"), dict) else {}
            if ref.startswith("refs/heads/"):
                kind = "branch"
                name = ref.removeprefix("refs/heads/")
            elif ref.startswith("refs/tags/"):
                kind = "tag"
                name = ref.removeprefix("refs/tags/")
            else:
                kind = "ref"
                name = ref
            revision = str(obj.get("sha") or "")
            if name and revision:
                facts.append(CodeHostRefFact(name=name, kind=kind, revision=revision))
        return tuple(facts)

    async def commit(
        self,
        binding: CodeHostProviderBinding,
        resource: Resource,
        revision: str,
        *,
        credential: str | None,
    ) -> CodeHostCommitFact:
        repository = _repository_name(resource)
        try:
            data = await self.client.get_json(
                binding.base_url,
                f"repos/{repository}/commits/{quote(revision, safe='')}",
                token=credential,
            )
        except Exception as exc:
            raise self._classify(exc) from exc
        if not isinstance(data, dict):
            raise CodeHostError("GitHub commit response is invalid")
        commit_data = data.get("commit") if isinstance(data.get("commit"), dict) else {}
        author = data.get("author") if isinstance(data.get("author"), dict) else {}
        author_data = commit_data.get("author") if isinstance(commit_data.get("author"), dict) else {}
        return CodeHostCommitFact(
            revision=str(data.get("sha") or revision),
            message=str(commit_data.get("message") or ""),
            author_external_id=(
                str(author.get("id")) if author.get("id") is not None else None
            ),
            authored_at=_timestamp(author_data.get("date")),
            web_url=data.get("html_url"),
        )

    async def pull_request(
        self,
        binding: CodeHostProviderBinding,
        resource: Resource,
        external_id: str,
        *,
        credential: str | None,
    ) -> CodeHostPullRequestFact:
        repository = _repository_name(resource)
        try:
            data = await self.client.get_json(
                binding.base_url,
                f"repos/{repository}/pulls/{quote(str(external_id), safe='')}",
                token=credential,
            )
        except Exception as exc:
            raise self._classify(exc) from exc
        if not isinstance(data, dict):
            raise CodeHostError("GitHub pull request response is invalid")
        head = data.get("head") if isinstance(data.get("head"), dict) else {}
        base = data.get("base") if isinstance(data.get("base"), dict) else {}
        user = data.get("user") if isinstance(data.get("user"), dict) else {}
        return CodeHostPullRequestFact(
            external_id=str(data.get("id") or external_id),
            number=data.get("number"),
            title=str(data.get("title") or ""),
            state=str(data.get("state") or "unknown"),
            source_ref=head.get("ref"),
            target_ref=base.get("ref"),
            author_external_id=str(user.get("id")) if user.get("id") is not None else None,
            web_url=data.get("html_url"),
            draft=bool(data.get("draft")),
            merge_revision=data.get("merge_commit_sha"),
        )

    async def reviews(
        self,
        binding: CodeHostProviderBinding,
        resource: Resource,
        external_id: str,
        *,
        credential: str | None,
    ) -> tuple[CodeHostReviewFact, ...]:
        repository = _repository_name(resource)
        try:
            data = await self.client.get_json(
                binding.base_url,
                f"repos/{repository}/pulls/{quote(str(external_id), safe='')}/reviews",
                token=credential,
            )
        except Exception as exc:
            raise self._classify(exc) from exc
        if not isinstance(data, list):
            raise CodeHostError("GitHub reviews response is invalid")
        facts = []
        for item in data:
            if not isinstance(item, dict) or item.get("id") is None:
                continue
            user = item.get("user") if isinstance(item.get("user"), dict) else {}
            facts.append(
                CodeHostReviewFact(
                    external_id=str(item["id"]),
                    state=str(item.get("state") or "unknown").casefold(),
                    author_external_id=(
                        str(user.get("id")) if user.get("id") is not None else None
                    ),
                    submitted_at=_timestamp(item.get("submitted_at")),
                    web_url=item.get("html_url"),
                )
            )
        return tuple(facts)

    async def checks(
        self,
        binding: CodeHostProviderBinding,
        resource: Resource,
        revision: str,
        *,
        credential: str | None,
    ) -> tuple[CodeHostCheckFact, ...]:
        repository = _repository_name(resource)
        try:
            data = await self.client.get_json(
                binding.base_url,
                f"repos/{repository}/commits/{quote(revision, safe='')}/check-runs",
                token=credential,
            )
        except Exception as exc:
            raise self._classify(exc) from exc
        if not isinstance(data, dict):
            raise CodeHostError("GitHub checks response is invalid")
        rows = data.get("check_runs")
        if not isinstance(rows, list):
            rows = []
        return tuple(
            CodeHostCheckFact(
                external_id=str(item.get("id")),
                name=str(item.get("name") or "check"),
                state=str(item.get("status") or "unknown"),
                conclusion=item.get("conclusion"),
                revision=str(item.get("head_sha") or revision),
                web_url=item.get("html_url"),
            )
            for item in rows
            if isinstance(item, dict) and item.get("id") is not None
        )

    async def releases(
        self,
        binding: CodeHostProviderBinding,
        resource: Resource,
        *,
        credential: str | None,
    ) -> tuple[CodeHostReleaseFact, ...]:
        repository = _repository_name(resource)
        try:
            data = await self.client.get_json(
                binding.base_url,
                f"repos/{repository}/releases",
                token=credential,
            )
        except Exception as exc:
            raise self._classify(exc) from exc
        if not isinstance(data, list):
            raise CodeHostError("GitHub releases response is invalid")
        return tuple(
            CodeHostReleaseFact(
                external_id=str(item.get("id")),
                tag=str(item.get("tag_name") or ""),
                name=str(item.get("name") or item.get("tag_name") or ""),
                state=("draft" if item.get("draft") else "prerelease" if item.get("prerelease") else "published"),
                web_url=item.get("html_url"),
            )
            for item in data
            if isinstance(item, dict) and item.get("id") is not None and item.get("tag_name")
        )

    async def compare(
        self,
        binding: CodeHostProviderBinding,
        resource: Resource,
        base_revision: str,
        head_revision: str,
        *,
        credential: str | None,
    ) -> CodeHostCompareFact:
        repository = _repository_name(resource)
        try:
            data = await self.client.get_json(
                binding.base_url,
                f"repos/{repository}/compare/{quote(base_revision, safe='')}...{quote(head_revision, safe='')}",
                token=credential,
            )
        except Exception as exc:
            raise self._classify(exc) from exc
        if not isinstance(data, dict):
            raise CodeHostError("GitHub compare response is invalid")
        files = data.get("files") if isinstance(data.get("files"), list) else []
        return CodeHostCompareFact(
            base_revision=base_revision,
            head_revision=head_revision,
            ahead_by=data.get("ahead_by"),
            behind_by=data.get("behind_by"),
            total_commits=data.get("total_commits"),
            changed_files=tuple(
                str(item.get("filename"))
                for item in files
                if isinstance(item, dict) and item.get("filename")
            ),
        )

    def normalize_webhook(
        self,
        binding: CodeHostProviderBinding,
        payload: dict,
        *,
        event_kind: str,
        event_id: str,
    ) -> CodeHostWebhookFact:
        kind = str(event_kind or "").strip().casefold()
        repository = payload.get("repository") if isinstance(payload.get("repository"), dict) else {}
        subject = None
        canonical = CanonicalEventType.TASK_SOURCE
        action = payload.get("action")
        state = None
        web_url = None
        if kind == "pull_request":
            subject = payload.get("pull_request") if isinstance(payload.get("pull_request"), dict) else {}
            canonical = CanonicalEventType.PULL_REQUEST
            state = subject.get("state")
            web_url = subject.get("html_url")
        elif kind in {"check_run", "check_suite", "workflow_run", "workflow_job"}:
            key = kind
            subject = payload.get(key) if isinstance(payload.get(key), dict) else {}
            canonical = CanonicalEventType.CI_PIPELINE
            state = subject.get("status") or subject.get("conclusion")
            web_url = subject.get("html_url")
        elif kind in {"deployment", "deployment_status"}:
            subject = payload.get(kind) if isinstance(payload.get(kind), dict) else {}
            canonical = CanonicalEventType.DEPLOYMENT
            state = subject.get("state") or subject.get("status")
            web_url = subject.get("target_url") or subject.get("environment_url")
        if not isinstance(subject, dict):
            subject = {}
        return CodeHostWebhookFact(
            provider_type=self.provider_type,
            provider_instance=binding.provider_instance,
            event_id=event_id,
            event_kind=kind or "unknown",
            canonical_event_type=canonical.value,
            repository_external_id=(
                str(repository.get("id"))
                if repository.get("id") is not None
                else repository.get("full_name")
            ),
            subject_external_id=(
                str(subject.get("id")) if subject.get("id") is not None else None
            ),
            action=str(action) if action is not None else None,
            state=str(state) if state is not None else None,
            web_url=str(web_url) if web_url is not None else None,
            provider_payload={
                "repository_full_name": repository.get("full_name"),
                "sender_id": (
                    payload.get("sender", {}).get("id")
                    if isinstance(payload.get("sender"), dict)
                    else None
                ),
            },
        )
