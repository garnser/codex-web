from __future__ import annotations

from datetime import datetime
from urllib.parse import quote

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
    CodeHostTransientError,
    CodeHostWebhookFact,
)
from codex_web.integrations.gitlab_client import GitLabClient
from codex_web.resources import Resource


def _timestamp(value: object) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _project_name(resource: Resource) -> str:
    for alias in resource.aliases:
        if (alias.provider or "").casefold() == "gitlab" and alias.namespace in {
            "provider",
            "repository",
            "gitlab-project",
        }:
            return alias.value
    provenance = resource.provenance
    if provenance and provenance.external_id:
        return provenance.external_id
    return resource.name


class GitLabCodeHostProvider:
    provider_type = "gitlab"

    def __init__(self, client: GitLabClient | None = None) -> None:
        self.client = client or GitLabClient()

    def capabilities(self) -> frozenset[CodeHostCapability]:
        return frozenset(CodeHostCapability)

    @staticmethod
    def _classify(exc: Exception) -> CodeHostError:
        if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
            return CodeHostTransientError(f"{type(exc).__name__}: {exc}")
        status = getattr(exc, "status_code", None)
        message = str(exc)
        if status is None and "HTTP " in message:
            try:
                status = int(message.split("HTTP ", 1)[1].split()[0])
            except (ValueError, IndexError):
                status = None
        if status in {408, 409, 429} or (isinstance(status, int) and status >= 500):
            return CodeHostTransientError(message)
        return CodeHostError(message)

    async def _get(
        self,
        binding: CodeHostProviderBinding,
        path: str,
        *,
        credential: str | None,
        params: dict | None = None,
    ):
        if not credential:
            raise CodeHostError("GitLab code-host credential is required")
        try:
            return await self.client.get_json(
                binding.base_url,
                path,
                token=credential,
                params=params,
            )
        except Exception as exc:
            raise self._classify(exc) from exc

    async def repository(
        self,
        binding: CodeHostProviderBinding,
        resource: Resource,
        *,
        credential: str | None,
    ) -> CodeHostRepositoryFact:
        project = _project_name(resource)
        data = await self._get(
            binding,
            f"projects/{quote(project, safe='')}",
            credential=credential,
        )
        if not isinstance(data, dict):
            raise CodeHostError("GitLab project response is invalid")
        return CodeHostRepositoryFact(
            resource_id=resource.id,
            provider_type=self.provider_type,
            provider_instance=binding.provider_instance,
            external_id=str(data.get("id") or project),
            name=str(data.get("name") or project.rsplit("/", 1)[-1]),
            full_name=str(data.get("path_with_namespace") or project),
            default_branch=data.get("default_branch"),
            web_url=data.get("web_url"),
            archived=bool(data.get("archived")),
            visibility=data.get("visibility"),
        )

    async def refs(
        self,
        binding: CodeHostProviderBinding,
        resource: Resource,
        *,
        credential: str | None,
    ) -> tuple[CodeHostRefFact, ...]:
        project = quote(_project_name(resource), safe="")
        branches = await self._get(
            binding,
            f"projects/{project}/repository/branches",
            credential=credential,
            params={"per_page": 100},
        )
        tags = await self._get(
            binding,
            f"projects/{project}/repository/tags",
            credential=credential,
            params={"per_page": 100},
        )
        facts: list[CodeHostRefFact] = []
        for kind, rows in (("branch", branches), ("tag", tags)):
            if not isinstance(rows, list):
                raise CodeHostError(f"GitLab {kind} response is invalid")
            for item in rows:
                if not isinstance(item, dict):
                    continue
                commit = item.get("commit") if isinstance(item.get("commit"), dict) else {}
                name = item.get("name")
                revision = commit.get("id")
                if name and revision:
                    facts.append(
                        CodeHostRefFact(
                            name=str(name),
                            kind=kind,
                            revision=str(revision),
                        )
                    )
        return tuple(facts)

    async def commit(
        self,
        binding: CodeHostProviderBinding,
        resource: Resource,
        revision: str,
        *,
        credential: str | None,
    ) -> CodeHostCommitFact:
        project = quote(_project_name(resource), safe="")
        data = await self._get(
            binding,
            f"projects/{project}/repository/commits/{quote(revision, safe='')}",
            credential=credential,
        )
        if not isinstance(data, dict):
            raise CodeHostError("GitLab commit response is invalid")
        return CodeHostCommitFact(
            revision=str(data.get("id") or revision),
            message=str(data.get("message") or data.get("title") or ""),
            author_external_id=(
                str(data.get("author_email")) if data.get("author_email") else None
            ),
            authored_at=_timestamp(data.get("authored_date")),
            web_url=data.get("web_url"),
        )

    async def pull_request(
        self,
        binding: CodeHostProviderBinding,
        resource: Resource,
        external_id: str,
        *,
        credential: str | None,
    ) -> CodeHostPullRequestFact:
        project = quote(_project_name(resource), safe="")
        data = await self._get(
            binding,
            f"projects/{project}/merge_requests/{quote(str(external_id), safe='')}",
            credential=credential,
        )
        if not isinstance(data, dict):
            raise CodeHostError("GitLab merge request response is invalid")
        author = data.get("author") if isinstance(data.get("author"), dict) else {}
        return CodeHostPullRequestFact(
            external_id=str(data.get("id") or external_id),
            number=data.get("iid"),
            title=str(data.get("title") or ""),
            state=str(data.get("state") or "unknown"),
            source_ref=data.get("source_branch"),
            target_ref=data.get("target_branch"),
            author_external_id=(
                str(author.get("id")) if author.get("id") is not None else None
            ),
            web_url=data.get("web_url"),
            draft=bool(data.get("draft") or data.get("work_in_progress")),
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
        project = quote(_project_name(resource), safe="")
        data = await self._get(
            binding,
            f"projects/{project}/merge_requests/{quote(str(external_id), safe='')}/approvals",
            credential=credential,
        )
        if not isinstance(data, dict):
            raise CodeHostError("GitLab approvals response is invalid")
        approved_by = data.get("approved_by") if isinstance(data.get("approved_by"), list) else []
        facts = []
        for entry in approved_by:
            if not isinstance(entry, dict):
                continue
            user = entry.get("user") if isinstance(entry.get("user"), dict) else {}
            if user.get("id") is None:
                continue
            facts.append(
                CodeHostReviewFact(
                    external_id=f"approval:{user['id']}",
                    state="approved",
                    author_external_id=str(user["id"]),
                    web_url=None,
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
        project = quote(_project_name(resource), safe="")
        data = await self._get(
            binding,
            f"projects/{project}/repository/commits/{quote(revision, safe='')}/statuses",
            credential=credential,
            params={"per_page": 100},
        )
        if not isinstance(data, list):
            raise CodeHostError("GitLab commit statuses response is invalid")
        return tuple(
            CodeHostCheckFact(
                external_id=str(item.get("id")),
                name=str(item.get("name") or item.get("context") or "status"),
                state=str(item.get("status") or "unknown"),
                conclusion=str(item.get("status")) if item.get("status") else None,
                revision=str(item.get("sha") or revision),
                web_url=item.get("target_url"),
            )
            for item in data
            if isinstance(item, dict) and item.get("id") is not None
        )

    async def releases(
        self,
        binding: CodeHostProviderBinding,
        resource: Resource,
        *,
        credential: str | None,
    ) -> tuple[CodeHostReleaseFact, ...]:
        project = quote(_project_name(resource), safe="")
        data = await self._get(
            binding,
            f"projects/{project}/releases",
            credential=credential,
            params={"per_page": 100},
        )
        if not isinstance(data, list):
            raise CodeHostError("GitLab releases response is invalid")
        return tuple(
            CodeHostReleaseFact(
                external_id=str(item.get("tag_name")),
                tag=str(item.get("tag_name")),
                name=str(item.get("name") or item.get("tag_name") or ""),
                state="published",
                web_url=(
                    item.get("_links", {}).get("self")
                    if isinstance(item.get("_links"), dict)
                    else None
                ),
            )
            for item in data
            if isinstance(item, dict) and item.get("tag_name")
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
        project = quote(_project_name(resource), safe="")
        data = await self._get(
            binding,
            f"projects/{project}/repository/compare",
            credential=credential,
            params={"from": base_revision, "to": head_revision},
        )
        if not isinstance(data, dict):
            raise CodeHostError("GitLab compare response is invalid")
        commits = data.get("commits") if isinstance(data.get("commits"), list) else []
        diffs = data.get("diffs") if isinstance(data.get("diffs"), list) else []
        return CodeHostCompareFact(
            base_revision=base_revision,
            head_revision=head_revision,
            total_commits=len(commits),
            changed_files=tuple(
                str(item.get("new_path") or item.get("old_path"))
                for item in diffs
                if isinstance(item, dict) and (item.get("new_path") or item.get("old_path"))
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
        kind = str(
            event_kind
            or payload.get("object_kind")
            or payload.get("event_name")
            or ""
        ).strip().casefold().replace(" ", "_")
        attrs = payload.get("object_attributes") if isinstance(payload.get("object_attributes"), dict) else {}
        project = payload.get("project") if isinstance(payload.get("project"), dict) else {}
        canonical = CanonicalEventType.TASK_SOURCE
        if kind == "merge_request":
            canonical = CanonicalEventType.PULL_REQUEST
        elif kind in {"pipeline", "build", "job"}:
            canonical = CanonicalEventType.CI_PIPELINE
        elif kind in {"deployment", "deployment_status"}:
            canonical = CanonicalEventType.DEPLOYMENT
        elif kind == "incident":
            canonical = CanonicalEventType.INCIDENT
        state = attrs.get("status") or attrs.get("state") or payload.get("status")
        if str(state or "").casefold() in {"failed", "failure", "error"}:
            canonical = CanonicalEventType.FAILURE
        return CodeHostWebhookFact(
            provider_type=self.provider_type,
            provider_instance=binding.provider_instance,
            event_id=event_id,
            event_kind=kind or "unknown",
            canonical_event_type=canonical.value,
            repository_external_id=(
                str(project.get("id"))
                if project.get("id") is not None
                else project.get("path_with_namespace")
            ),
            subject_external_id=(
                str(attrs.get("id"))
                if attrs.get("id") is not None
                else str(attrs.get("iid")) if attrs.get("iid") is not None else None
            ),
            action=(str(attrs.get("action")) if attrs.get("action") is not None else None),
            state=(str(state) if state is not None else None),
            web_url=attrs.get("url") or attrs.get("web_url"),
            provider_payload={
                "project_path": project.get("path_with_namespace"),
                "ref": attrs.get("ref"),
            },
        )
