from __future__ import annotations

import contextlib
import re
from datetime import datetime, timezone
from typing import Any, get_args

from codex_web.compatibility import TASK_SOURCE_CONTRACT
from codex_web.integrations.github_client import GitHubClient
from codex_web.models import TaskSourceIdentity, WorkItemStage
from codex_web.services.task_sources import (
    TaskSourceCanonicalProjection, TaskSourceCapabilities, TaskSourceCapability,
    TaskSourceCreateRequest, TaskSourceEvent, TaskSourceSnapshot, TaskSourceWritebackResult,
)


class GitHubTaskSource:
    """Authoritative TaskSource backed by GitHub Issues."""

    source_type = "github"
    contract_version = TASK_SOURCE_CONTRACT.current
    capabilities = TaskSourceCapabilities(frozenset({
        TaskSourceCapability.CREATE, TaskSourceCapability.DISCOVERY,
        TaskSourceCapability.READ, TaskSourceCapability.EVENTS,
        TaskSourceCapability.OWNER_WRITE, TaskSourceCapability.STATE_WRITE,
        TaskSourceCapability.COMMENTS,
    }))

    def __init__(self, api_base: str, token: str, *, client: GitHubClient | None = None) -> None:
        self.source_instance = str(api_base or "").strip().rstrip("/")
        self.api_base = self.source_instance
        self.token = str(token or "").strip()
        if not self.source_instance or not self.token:
            raise ValueError("GitHub task source api_base and token are required")
        self.client = client or GitHubClient()

    def _identity(self, external_id: str, *, url: str | None = None, revision: str | None = None) -> TaskSourceIdentity:
        return TaskSourceIdentity(source_type=self.source_type, source_instance=self.source_instance,
                                  external_id=external_id, external_url=url, revision=revision)

    @staticmethod
    def _repo(scope: str) -> str:
        value = str(scope or "").strip().strip("/")
        if not re.fullmatch(r"[^/]+/[^/]+", value):
            raise ValueError("GitHub task source scope must use owner/repository")
        return value

    @staticmethod
    def _labels(values: Any) -> tuple[str, ...]:
        return tuple(sorted(dict.fromkeys(str(item.get("name") if isinstance(item, dict) else item).strip()
                                    for item in (values if isinstance(values, list) else [])
                                    if str(item.get("name") if isinstance(item, dict) else item).strip())))

    @staticmethod
    def _owners(values: Any) -> tuple[str, ...]:
        return tuple(sorted(dict.fromkeys(str(item.get("login") if isinstance(item, dict) else item).strip()
                                    for item in (values if isinstance(values, list) else [])
                                    if str(item.get("login") if isinstance(item, dict) else item).strip())))

    @staticmethod
    def _timestamp(value: Any) -> float | None:
        if not isinstance(value, str) or not value.strip(): return None
        with contextlib.suppress(ValueError):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()
        return None

    @staticmethod
    def _split(external_id: str) -> tuple[str, int]:
        repo, sep, number = str(external_id).partition("#")
        if not sep or not re.fullmatch(r"[^/]+/[^/]+", repo) or not number.isdigit():
            raise ValueError("GitHub external_id must use owner/repository#issue-number")
        return repo, int(number)

    def _snapshot(self, issue: dict[str, Any], *, repo: str | None = None) -> TaskSourceSnapshot:
        repo_name = repo or str((issue.get("repository") or {}).get("full_name") or "").strip()
        number = issue.get("number")
        if not repo_name or number is None: raise ValueError("GitHub issue is missing repository/number")
        revision = str(issue.get("updated_at") or issue.get("created_at") or "").strip() or None
        identity = self._identity(f"{repo_name}#{number}", url=str(issue.get("html_url") or "").strip() or None, revision=revision)
        return TaskSourceSnapshot(identity=identity, title=str(issue.get("title") or "").strip() or None,
            body_text=str(issue.get("body") or "").strip() or None, source_state=str(issue.get("state") or "").strip().lower() or None,
            owners=self._owners(issue.get("assignees")), labels=self._labels(issue.get("labels")))

    async def create(self, request: TaskSourceCreateRequest, *, scope: str) -> TaskSourceSnapshot:
        payload: dict[str, Any] = {"title": request.title}
        if request.body: payload["body"] = request.body
        if request.labels: payload["labels"] = list(request.labels)
        if request.owners: payload["assignees"] = list(request.owners)
        return self._snapshot(await self.client.create_issue(self.api_base, self._repo(scope), token=self.token, payload=payload), repo=self._repo(scope))

    async def discover(self, *, scope: str) -> list[TaskSourceSnapshot]:
        repo = self._repo(scope)
        return [self._snapshot(item, repo=repo) for item in await self.client.list_issues(self.api_base, repo, token=self.token)]

    async def read(self, identity: TaskSourceIdentity) -> TaskSourceSnapshot:
        repo, number = self._split(identity.external_id)
        if identity.source_type.casefold() != self.source_type or identity.source_instance.rstrip("/") != self.source_instance:
            raise ValueError("Task-source identity does not belong to GitHub adapter")
        return self._snapshot(await self.client.issue(self.api_base, repo, number, token=self.token), repo=repo)

    async def normalize_event(self, payload: object) -> TaskSourceEvent | None:
        if not isinstance(payload, dict): return None
        issue = payload.get("issue") if isinstance(payload.get("issue"), dict) else None
        repo = payload.get("repository") if isinstance(payload.get("repository"), dict) else {}
        if issue is None or not repo.get("full_name") or issue.get("number") is None: return None
        snapshot = self._snapshot({**issue, "repository": repo}, repo=str(repo["full_name"]))
        return TaskSourceEvent(identity=snapshot.identity, event_type=f"issue.{str(payload.get('action') or 'updated').strip()}",
                               occurred_at=self._timestamp(issue.get("updated_at")), snapshot=snapshot)

    @staticmethod
    def _stage(snapshot: TaskSourceSnapshot, current: WorkItemStage | None) -> WorkItemStage:
        return "closed" if snapshot.source_state == "closed" else (current or "implementation_active")

    def project(self, snapshot: TaskSourceSnapshot, *, current_stage: WorkItemStage | None = None) -> TaskSourceCanonicalProjection:
        stage = self._stage(snapshot, current_stage)
        return TaskSourceCanonicalProjection(identity=snapshot.identity, stage=stage,
            owner=None if stage == "closed" else (snapshot.owners[0] if snapshot.owners else None),
            owner_known=bool(snapshot.owners), source_state=snapshot.source_state)

    async def write_owner(self, identity: TaskSourceIdentity, owner: str | None) -> TaskSourceSnapshot:
        repo, number = self._split(identity.external_id)
        return self._snapshot(await self.client.update_issue(self.api_base, repo, number, token=self.token,
            payload={"assignees": [str(owner).strip()] if str(owner or "").strip() else []}), repo=repo)

    async def write_state(self, identity: TaskSourceIdentity, state: str) -> TaskSourceSnapshot:
        if state not in get_args(WorkItemStage): raise ValueError(f"Unsupported canonical work-item stage: {state!r}")
        repo, number = self._split(identity.external_id)
        return self._snapshot(await self.client.update_issue(self.api_base, repo, number, token=self.token,
            payload={"state": "closed" if state == "closed" else "open"}), repo=repo)

    async def add_comment(self, identity: TaskSourceIdentity, body: str) -> None:
        repo, number = self._split(identity.external_id)
        text = str(body or "").strip()
        if not text: raise ValueError("comment body must not be empty")
        await self.client.create_comment(self.api_base, repo, number, token=self.token, body=text)

    async def attach_artifact(self, identity: TaskSourceIdentity, url: str) -> None:
        raise NotImplementedError("GitHub issue artifacts are represented by comments")
