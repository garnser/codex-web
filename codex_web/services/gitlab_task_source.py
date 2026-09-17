from __future__ import annotations

import contextlib
import re
from datetime import datetime, timezone
from typing import Any, get_args

from codex_web.integrations.gitlab_client import GitLabClient
from codex_web.models import TaskSourceIdentity, WorkItemStage
from codex_web.services.task_sources import (
    TaskSourceCanonicalProjection,
    TaskSourceCapabilities,
    TaskSourceCapability,
    TaskSourceEvent,
    TaskSourceSnapshot,
)


class GitLabTaskSource:
    """Provider-neutral authoritative-task adapter backed by GitLab issues.

    This adapter owns GitLab-native discovery/read/event/label semantics. It
    returns only normalized TaskSource objects to callers and performs
    deterministic canonical projection without mutating codex-web work state.
    """

    source_type = "gitlab"
    capabilities = TaskSourceCapabilities(
        frozenset(
            {
                TaskSourceCapability.DISCOVERY,
                TaskSourceCapability.READ,
                TaskSourceCapability.EVENTS,
                TaskSourceCapability.OWNER_WRITE,
                TaskSourceCapability.STATE_WRITE,
                TaskSourceCapability.COMMENTS,
            }
        )
    )

    def __init__(
        self,
        api_base: str,
        token: str,
        *,
        client: GitLabClient | None = None,
    ) -> None:
        api_base = str(api_base or "").strip().rstrip("/")
        token = str(token or "").strip()
        if not api_base:
            raise ValueError("GitLab task source api_base must not be empty")
        if not token:
            raise ValueError("GitLab task source token must not be empty")
        self.source_instance = api_base
        self.api_base = api_base
        self.token = token
        self.client = client or GitLabClient()

    @staticmethod
    def _normalize_labels(values: Any) -> tuple[str, ...]:
        labels: list[str] = []
        for item in values if isinstance(values, list) else []:
            if isinstance(item, str):
                value = item.strip()
            elif isinstance(item, dict):
                value = str(item.get("title") or item.get("name") or "").strip()
            else:
                value = ""
            if value:
                labels.append(value)
        return tuple(sorted(dict.fromkeys(labels)))

    @classmethod
    def _event_labels(cls, payload: dict[str, Any]) -> tuple[str, ...]:
        attrs = payload.get("object_attributes") or {}
        labels: list[str] = []
        sources = (
            payload.get("labels"),
            attrs.get("labels"),
            (payload.get("changes") or {}).get("labels", {}).get("current"),
            attrs.get("label_names"),
        )
        for source in sources:
            labels.extend(cls._normalize_labels(source))
        return tuple(sorted(dict.fromkeys(labels)))

    @staticmethod
    def _normalize_assignees(values: Any) -> tuple[str, ...]:
        owners: list[str] = []
        for item in values if isinstance(values, list) else []:
            if isinstance(item, str):
                value = item.strip()
            elif isinstance(item, dict):
                value = str(item.get("username") or item.get("name") or item.get("id") or "").strip()
            else:
                value = ""
            if value:
                owners.append(value)
        return tuple(sorted(dict.fromkeys(owners)))

    @classmethod
    def _event_assignees(cls, payload: dict[str, Any]) -> tuple[str, ...]:
        attrs = payload.get("object_attributes") or {}
        values: list[Any] = []
        for source in (payload.get("assignees"), attrs.get("assignees")):
            if isinstance(source, list):
                values.extend(source)
        assignee = payload.get("assignee") or attrs.get("assignee")
        if assignee:
            values.append(assignee)
        return cls._normalize_assignees(values)

    @staticmethod
    def _parse_timestamp(value: Any) -> float | None:
        if isinstance(value, (int, float)):
            return float(value)
        if not isinstance(value, str) or not value.strip():
            return None
        text = value.strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        with contextlib.suppress(ValueError):
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        return None

    @classmethod
    def _latest_timestamp(cls, *values: Any) -> float | None:
        timestamps = [
            parsed
            for parsed in (cls._parse_timestamp(value) for value in values)
            if parsed is not None
        ]
        return max(timestamps) if timestamps else None

    def _identity(
        self,
        external_id: str,
        *,
        external_url: str | None = None,
        revision: str | None = None,
        event_cursor: str | None = None,
    ) -> TaskSourceIdentity:
        return TaskSourceIdentity(
            source_type=self.source_type,
            source_instance=self.source_instance,
            external_id=external_id,
            external_url=external_url,
            revision=revision,
            event_cursor=event_cursor,
        )

    def _validate_identity(self, identity: TaskSourceIdentity) -> None:
        if identity.source_type.strip().casefold() != self.source_type:
            raise ValueError("Task-source identity does not belong to GitLab adapter")
        if identity.source_instance.strip().rstrip("/") != self.source_instance:
            raise ValueError("Task-source identity belongs to another GitLab instance")

    @staticmethod
    def _split_external_id(external_id: str) -> tuple[str, int]:
        project_path, separator, iid_text = str(external_id or "").partition("#")
        project_path = project_path.strip().strip("/")
        iid_text = iid_text.strip()
        if not separator or not project_path or not iid_text.isdigit():
            raise ValueError("GitLab external_id must use <project-path>#<issue-iid>")
        return project_path, int(iid_text)

    @staticmethod
    def _issue_external_id(issue: dict[str, Any], *, project_path: str | None = None) -> str:
        full_ref = str((issue.get("references") or {}).get("full") or "").strip()
        if full_ref and "#" in full_ref:
            return full_ref
        iid = issue.get("iid")
        if project_path and iid is not None:
            return f"{project_path.strip().strip('/')}#{iid}"
        raise ValueError("GitLab issue is missing a routable full reference")

    def _snapshot_from_issue(
        self,
        issue: dict[str, Any],
        *,
        project_path: str | None = None,
    ) -> TaskSourceSnapshot:
        external_id = self._issue_external_id(issue, project_path=project_path)
        revision_value = issue.get("updated_at") or issue.get("closed_at") or issue.get("created_at")
        revision = str(revision_value).strip() if revision_value else None
        identity = self._identity(
            external_id,
            external_url=str(issue.get("web_url") or "").strip() or None,
            revision=revision,
        )
        return TaskSourceSnapshot(
            identity=identity,
            title=str(issue.get("title") or "").strip() or None,
            source_state=str(issue.get("state") or "").strip().lower() or None,
            owners=self._normalize_assignees(issue.get("assignees")),
            labels=self._normalize_labels(issue.get("labels")),
        )

    async def discover(self, *, scope: str) -> list[TaskSourceSnapshot]:
        self.capabilities.require(TaskSourceCapability.DISCOVERY)
        scope = str(scope or "").strip().strip("/")
        if not scope:
            raise ValueError("GitLab discovery scope must not be empty")
        issues = await self.client.group_issues(
            self.api_base,
            scope,
            token=self.token,
            state="opened",
        )
        snapshots: list[TaskSourceSnapshot] = []
        for issue in issues:
            with contextlib.suppress(ValueError):
                snapshots.append(self._snapshot_from_issue(issue))
        return snapshots

    async def read(self, identity: TaskSourceIdentity) -> TaskSourceSnapshot:
        self.capabilities.require(TaskSourceCapability.READ)
        self._validate_identity(identity)
        project_path, iid = self._split_external_id(identity.external_id)
        issue = await self.client.project_issue(
            self.api_base,
            project_path,
            iid,
            token=self.token,
        )
        return self._snapshot_from_issue(issue, project_path=project_path)

    async def normalize_event(self, payload: object) -> TaskSourceEvent | None:
        self.capabilities.require(TaskSourceCapability.EVENTS)
        if not isinstance(payload, dict):
            return None
        kind = str(payload.get("object_kind") or payload.get("event_name") or "").strip().lower()
        if kind != "issue":
            return None

        attrs = payload.get("object_attributes") or {}
        project = payload.get("project") or {}
        project_path = str(project.get("path_with_namespace") or "").strip().strip("/")
        iid = attrs.get("iid")
        if not project_path or iid is None:
            return None
        external_id = f"{project_path}#{iid}"
        revision_value = attrs.get("updated_at") or attrs.get("closed_at") or attrs.get("created_at")
        revision = str(revision_value).strip() if revision_value else None
        external_url = str(attrs.get("url") or attrs.get("web_url") or "").strip() or None
        identity = self._identity(
            external_id,
            external_url=external_url,
            revision=revision,
        )
        snapshot = TaskSourceSnapshot(
            identity=identity,
            title=str(attrs.get("title") or "").strip() or None,
            source_state=str(attrs.get("state") or payload.get("state") or "").strip().lower() or None,
            owners=self._event_assignees(payload),
            labels=self._event_labels(payload),
        )
        occurred_at = self._latest_timestamp(
            attrs.get("updated_at"),
            attrs.get("closed_at"),
            attrs.get("last_edited_at"),
            attrs.get("created_at"),
        )
        action = str(attrs.get("action") or "updated").strip().lower() or "updated"
        return TaskSourceEvent(
            identity=identity,
            event_type=f"issue.{action}",
            occurred_at=occurred_at,
            snapshot=snapshot,
        )

    @staticmethod
    def _status_label(labels: tuple[str, ...]) -> str | None:
        for label in labels:
            if label.casefold().startswith("status::"):
                return label.casefold()
        return None

    @staticmethod
    def _owner_from_labels(labels: tuple[str, ...]) -> str | None:
        for label in labels:
            match = re.match(r"owner::(.+)", label.strip(), re.IGNORECASE)
            if match:
                owner = match.group(1).strip().lower()
                if owner:
                    return owner
        return None

    @classmethod
    def _canonical_stage(
        cls,
        snapshot: TaskSourceSnapshot,
        current_stage: WorkItemStage | None,
    ) -> WorkItemStage:
        source_state = (snapshot.source_state or "").strip().lower()
        if source_state in {"closed", "merged"}:
            return "closed"
        status_label = cls._status_label(snapshot.labels)
        if status_label == "status::awaiting confirmation":
            return "ready_for_validation"
        if status_label == "status::blocked":
            return "failed_with_action_owner"
        if status_label == "status::in progress":
            if current_stage in {
                "implementation_active",
                "validation_running",
                "ready_to_close",
            }:
                return current_stage
            return "implementation_active"
        return current_stage or "implementation_active"

    def project(
        self,
        snapshot: TaskSourceSnapshot,
        *,
        current_stage: WorkItemStage | None = None,
    ) -> TaskSourceCanonicalProjection:
        self._validate_identity(snapshot.identity)
        stage = self._canonical_stage(snapshot, current_stage)
        label_owner = self._owner_from_labels(snapshot.labels)
        return TaskSourceCanonicalProjection(
            identity=snapshot.identity,
            stage=stage,
            owner=None if stage == "closed" else label_owner,
            owner_known=bool(label_owner),
            source_state=snapshot.source_state,
        )

    @staticmethod
    def _replace_prefixed_label(
        labels: tuple[str, ...],
        prefix: str,
        replacement: str | None,
    ) -> list[str]:
        kept = [label for label in labels if not label.casefold().startswith(prefix.casefold())]
        if replacement:
            kept.append(replacement)
        return list(dict.fromkeys(kept))

    async def write_owner(
        self,
        identity: TaskSourceIdentity,
        owner: str | None,
    ) -> TaskSourceSnapshot:
        self.capabilities.require(TaskSourceCapability.OWNER_WRITE)
        current = await self.read(identity)
        project_path, iid = self._split_external_id(identity.external_id)
        owner = str(owner or "").strip().lower() or None
        labels = self._replace_prefixed_label(
            current.labels,
            "owner::",
            f"owner::{owner}" if owner else None,
        )
        issue = await self.client.update_project_issue(
            self.api_base,
            project_path,
            iid,
            token=self.token,
            payload={"labels": ",".join(labels)},
        )
        return self._snapshot_from_issue(issue, project_path=project_path)

    async def write_state(
        self,
        identity: TaskSourceIdentity,
        state: str,
    ) -> TaskSourceSnapshot:
        self.capabilities.require(TaskSourceCapability.STATE_WRITE)
        if state not in get_args(WorkItemStage):
            raise ValueError(f"Unsupported canonical work-item stage: {state!r}")

        current = await self.read(identity)
        project_path, iid = self._split_external_id(identity.external_id)
        status_by_stage = {
            "implementation_active": "status::in progress",
            "ready_for_validation": "status::awaiting confirmation",
            "validation_running": "status::in progress",
            "failed_with_action_owner": "status::blocked",
            "ready_to_close": "status::in progress",
            "closed": None,
        }
        labels = self._replace_prefixed_label(
            current.labels,
            "status::",
            status_by_stage[state],
        )
        update_payload: dict[str, Any] = {"labels": ",".join(labels)}
        if state == "closed":
            update_payload["state_event"] = "close"
        elif (current.source_state or "").lower() == "closed":
            update_payload["state_event"] = "reopen"

        issue = await self.client.update_project_issue(
            self.api_base,
            project_path,
            iid,
            token=self.token,
            payload=update_payload,
        )
        return self._snapshot_from_issue(issue, project_path=project_path)

    async def add_comment(self, identity: TaskSourceIdentity, body: str) -> None:
        self.capabilities.require(TaskSourceCapability.COMMENTS)
        self._validate_identity(identity)
        text = str(body or "").strip()
        if not text:
            raise ValueError("comment body must not be empty")
        project_path, iid = self._split_external_id(identity.external_id)
        await self.client.create_project_issue_note(
            self.api_base,
            project_path,
            iid,
            token=self.token,
            body=text,
        )

    async def attach_artifact(self, identity: TaskSourceIdentity, url: str) -> None:
        self.capabilities.require(TaskSourceCapability.ARTIFACT_LINKS)
