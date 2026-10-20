from __future__ import annotations

import contextlib
import re
import httpx
from datetime import datetime, timezone
from typing import Any, get_args

from codex_web.compatibility import TASK_SOURCE_CONTRACT
from codex_web.integrations.github_client import GitHubAPIError, GitHubClient
from codex_web.models import GitHubTaskSourceSettings, TaskSourceIdentity, WorkItemStage
from codex_web.services.task_source_projection import TaskSourceProjectionError
from urllib.parse import quote
import hashlib
import json
from codex_web.services.task_sources import (
    TaskSourceCanonicalProjection, TaskSourceCapabilities, TaskSourceCapability,
    TaskSourceCreateRequest, TaskSourceEvent, TaskSourceSnapshot, TaskSourceWritebackResult,
    TaskSourceRecordUnavailable,
)


class GitHubTaskSource:
    """Authoritative TaskSource backed by GitHub Issues."""

    source_type = "github"
    contract_version = TASK_SOURCE_CONTRACT.current
    capabilities = TaskSourceCapabilities(frozenset({
        TaskSourceCapability.CREATE, TaskSourceCapability.DISCOVERY,
        TaskSourceCapability.READ, TaskSourceCapability.SCOPED_READ, TaskSourceCapability.EVENTS,
        TaskSourceCapability.OWNER_WRITE, TaskSourceCapability.STATE_WRITE,
        TaskSourceCapability.COMMENTS, TaskSourceCapability.DISCUSSIONS,
    }))

    def __init__(self, api_base: str, token: str, *, client: GitHubClient | None = None, settings: GitHubTaskSourceSettings | None = None) -> None:
        self.source_instance = str(api_base or "").strip().rstrip("/")
        self.api_base = self.source_instance
        self.token = str(token or "").strip()
        if not self.source_instance or not self.token:
            raise ValueError("GitHub task source api_base and token are required")
        self.client = client or GitHubClient()
        self.settings = settings or GitHubTaskSourceSettings()
        self.mapping_revision = hashlib.sha256(json.dumps(self.settings.model_dump(), sort_keys=True).encode()).hexdigest()

    supports_guarded_projection = True

    def _mapped_owner(self, owner):
        if owner is None:
            return None
        login = self.settings.assignee_mapping.get(owner)
        if login is None:
            raise TaskSourceProjectionError("github_assignee_mapping_missing")
        return login

    def mapped_assignee(self, owner):
        return self._mapped_owner(owner)

    async def write_guarded_projection(self, identity, *, owner, stage, guard,
                                       expected_revision, observe, protected_state=None):
        login = self._mapped_owner(owner)
        current = await self.read(identity)
        guard()
        baseline = protected_state or current.source_state
        observe(current, baseline)
        desired_state = "closed" if stage == "closed" else "open"

        def check_state(snapshot):
            observe(snapshot, baseline)
            if snapshot.source_state != baseline:
                raise TaskSourceProjectionError("provider_state_changed", conflict=True)

        check_state(current)
        matches = (login is None or login.casefold() in {v.casefold() for v in current.owners}) and current.source_state == desired_state
        if matches:
            return TaskSourceWritebackResult(snapshot=current, mutated=False)
        if expected_revision is not None and current.identity.revision != expected_revision:
            raise TaskSourceProjectionError("provider_revision_changed", conflict=True)
        # GitHub's issue PATCH has no documented atomic revision precondition.
        # A read followed by PATCH cannot protect a human close/reopen during
        # the PATCH await. Fail visibly instead of claiming compare-and-swap.
        if current.source_state != desired_state:
            raise TaskSourceProjectionError("github_state_transition_requires_reconciliation", conflict=True)
        repo, number = self._split(identity.external_id)
        if login is not None:
            await self.client.request_empty("GET", self.api_base,
                f"repos/{quote(repo, safe='/')}/assignees/{quote(login, safe='')}", token=self.token)
            guard()
            current = await self.read(identity)
            guard()
            check_state(current)
        mutated = False
        if login and login.casefold() not in {v.casefold() for v in current.owners}:
            # Additive provider semantics preserve concurrent manual assignees.
            # No noncommutative state mutation follows this await.
            await self.client.request_json("POST", self.api_base,
                f"repos/{quote(repo, safe='/')}/issues/{number}/assignees", token=self.token,
                json_body={"assignees": [login]})
            mutated = True
        verified = await self.read(identity)
        guard()
        check_state(verified)
        if login and login.casefold() not in {v.casefold() for v in verified.owners}:
            raise TaskSourceProjectionError("provider_projection_not_verified", conflict=True)
        return TaskSourceWritebackResult(snapshot=verified, mutated=mutated)

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
        if request.owners:
            payload["assignees"] = [self._mapped_owner(owner) for owner in request.owners]
            for login in payload["assignees"]:
                await self.client.request_empty("GET", self.api_base,
                    f"repos/{quote(self._repo(scope), safe='/')}/assignees/{quote(login, safe='')}", token=self.token)
        return self._snapshot(await self.client.create_issue(self.api_base, self._repo(scope), token=self.token, payload=payload), repo=self._repo(scope))

    async def discover(self, *, scope: str) -> list[TaskSourceSnapshot]:
        repo = self._repo(scope)
        return [self._snapshot(item, repo=repo) for item in await self.client.list_issues(self.api_base, repo, token=self.token)]

    async def read_scoped(self, identity: TaskSourceIdentity, *, scope: str) -> TaskSourceSnapshot:
        self.capabilities.require(TaskSourceCapability.SCOPED_READ)
        repo, _ = self._split(identity.external_id)
        if any(not re.fullmatch(r"[\w.-]+", part) or part in {".", ".."} for part in repo.split("/")):
            raise ValueError("Invalid GitHub repository reference")
        if repo != self._repo(scope):
            raise ValueError("Reference is outside the configured GitHub repository")
        try:
            return await self.read(identity)
        except GitHubAPIError as exc:
            raise TaskSourceRecordUnavailable(exc.status_code) from exc
        except httpx.RequestError as exc:
            raise TaskSourceRecordUnavailable() from exc

    async def read(self, identity: TaskSourceIdentity) -> TaskSourceSnapshot:
        repo, number = self._split(identity.external_id)
        if identity.source_type.casefold() != self.source_type or identity.source_instance.rstrip("/") != self.source_instance:
            raise ValueError("Task-source identity does not belong to GitHub adapter")
        snapshot = self._snapshot(await self.client.issue(self.api_base, repo, number, token=self.token), repo=repo)
        if snapshot.source_state not in {"open", "closed"}:
            raise TaskSourceProjectionError("provider_response_invalid", conflict=True)
        if snapshot.identity.external_id != identity.external_id:
            raise TaskSourceProjectionError("provider_identity_mismatch", conflict=True)
        return snapshot

    async def discussion(self, identity: TaskSourceIdentity) -> list[dict[str, Any]]:
        """Return a bounded, provider-neutral issue discussion projection."""
        repo, number = self._split(identity.external_id)
        if identity.source_type.casefold() != self.source_type or identity.source_instance.rstrip("/") != self.source_instance:
            raise ValueError("Task-source identity does not belong to GitHub adapter")
        comments = await self.client.list_issue_comments(
            self.api_base, repo, number, token=self.token
        )
        return [
            {
                "external_id": str(item.get("id") or ""),
                "body_text": str(item.get("body") or ""),
                "author": str((item.get("user") or {}).get("login") or ""),
                "created_at": item.get("created_at"),
                "updated_at": item.get("updated_at"),
                "url": item.get("html_url"),
            }
            for item in comments
            if item.get("id") is not None
        ]

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
        if snapshot.source_state == "closed":
            return "closed"
        if snapshot.source_state == "open" and current == "closed":
            return "implementation_active"
        return current or "implementation_active"

    def project(self, snapshot: TaskSourceSnapshot, *, current_stage: WorkItemStage | None = None) -> TaskSourceCanonicalProjection:
        stage = self._stage(snapshot, current_stage)
        roles = [role for role, login in self.settings.assignee_mapping.items()
                 if login.casefold() in {value.casefold() for value in snapshot.owners}]
        return TaskSourceCanonicalProjection(identity=snapshot.identity, stage=stage,
            owner=roles[0] if stage != "closed" and len(roles) == 1 else None,
            owner_known=len(roles) == 1, source_state=snapshot.source_state)

    async def write_owner(self, identity: TaskSourceIdentity, owner: str | None) -> TaskSourceSnapshot:
        current = await self.read(identity)
        login = self._mapped_owner(owner)
        if login is None or login.casefold() in {v.casefold() for v in current.owners}:
            return current
        repo, number = self._split(identity.external_id)
        await self.client.request_empty("GET", self.api_base,
            f"repos/{quote(repo, safe='/')}/assignees/{quote(login, safe='')}", token=self.token)
        await self.client.request_json("POST", self.api_base,
            f"repos/{quote(repo, safe='/')}/issues/{number}/assignees", token=self.token,
            json_body={"assignees": [login]})
        return await self.read(identity)

    async def write_state(self, identity: TaskSourceIdentity, state: str) -> TaskSourceSnapshot:
        if state not in get_args(WorkItemStage): raise ValueError(f"Unsupported canonical work-item stage: {state!r}")
        current = await self.read(identity)
        if current.source_state != ("closed" if state == "closed" else "open"):
            raise TaskSourceProjectionError("github_state_transition_requires_reconciliation", conflict=True)
        return current

    async def add_comment(self, identity: TaskSourceIdentity, body: str) -> None:
        repo, number = self._split(identity.external_id)
        text = str(body or "").strip()
        if not text: raise ValueError("comment body must not be empty")
        await self.client.create_comment(self.api_base, repo, number, token=self.token, body=text)

    async def attach_artifact(self, identity: TaskSourceIdentity, url: str) -> None:
        raise NotImplementedError("GitHub issue artifacts are represented by comments")
