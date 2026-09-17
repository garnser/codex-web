from __future__ import annotations

from typing import Any

from codex_web.integrations.gitlab_client import GitLabClient
from codex_web.services.gitlab_task_source import GitLabTaskSource
from codex_web.services.task_sources import (
    TaskSourceCapabilities,
    TaskSourceCapability,
    TaskSourceEvent,
    TaskSourceSnapshot,
)


class GitLabWebhookTaskSource(GitLabTaskSource):
    """GitLab adapter instance limited to signed webhook normalization.

    Webhook normalization and canonical projection need no GitLab API token.
    API-backed discovery/read/write use the normal ``GitLabTaskSource`` and
    therefore continue to require credentials.
    """

    capabilities = TaskSourceCapabilities(frozenset({TaskSourceCapability.EVENTS}))

    def __init__(
        self,
        api_base: str,
        *,
        client: GitLabClient | None = None,
    ) -> None:
        api_base = str(api_base or "").strip().rstrip("/")
        if not api_base:
            raise ValueError("GitLab task source api_base must not be empty")
        self.source_instance = api_base
        self.api_base = api_base
        self.token = ""
        self.client = client or GitLabClient()

    def normalize_event_sync(self, payload: object) -> TaskSourceEvent | None:
        """Normalize an already-authenticated webhook payload without I/O."""

        self.capabilities.require(TaskSourceCapability.EVENTS)
        if not isinstance(payload, dict):
            return None
        kind = str(payload.get("object_kind") or payload.get("event_name") or "").strip().lower()
        if kind != "issue":
            return None

        attrs: dict[str, Any] = payload.get("object_attributes") or {}
        project: dict[str, Any] = payload.get("project") or {}
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

    async def normalize_event(self, payload: object) -> TaskSourceEvent | None:
        return self.normalize_event_sync(payload)
