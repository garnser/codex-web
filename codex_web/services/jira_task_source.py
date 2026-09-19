from __future__ import annotations

import contextlib
from typing import Any

from codex_web.compatibility import TASK_SOURCE_CONTRACT
from codex_web.integrations.jira_client import JiraClient
from codex_web.models import TaskSourceIdentity, WorkItemStage
from codex_web.services.task_sources import (
    TaskSourceCanonicalProjection,
    TaskSourceCapabilities,
    TaskSourceCapability,
    TaskSourceCreateRequest,
    TaskSourceEvent,
    TaskSourcePage,
    TaskSourceReconciliationCursor,
    TaskSourceSnapshot,
    TaskSourceUserReference,
)


def _adf_text(value: Any) -> str | None:
    """Deterministically flatten Jira ADF into plain text at the adapter boundary."""

    parts: list[str] = []

    def visit(node: Any) -> None:
        if isinstance(node, str):
            text = node.strip()
            if text:
                parts.append(text)
            return
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if not isinstance(node, dict):
            return
        if node.get("type") == "text":
            text = str(node.get("text") or "").strip()
            if text:
                parts.append(text)
            return
        for child in node.get("content", []) if isinstance(node.get("content"), list) else []:
            visit(child)

    visit(value)
    text = "\n".join(parts).strip()
    return text or None


def _adf_document(text: str) -> dict[str, Any]:
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": text}],
            }
        ],
    }


class JiraTaskSource:
    """Authoritative Jira issue adapter behind the canonical TaskSource contract."""

    source_type = "jira"
    contract_version = TASK_SOURCE_CONTRACT.current
    capabilities = TaskSourceCapabilities(
        frozenset(
            {
                TaskSourceCapability.CREATE,
                TaskSourceCapability.DISCOVERY,
                TaskSourceCapability.PAGED_DISCOVERY,
                TaskSourceCapability.READ,
                TaskSourceCapability.EVENTS,
                TaskSourceCapability.OWNER_WRITE,
                TaskSourceCapability.COMMENTS,
                TaskSourceCapability.WORKFLOW_TRANSITIONS,
                TaskSourceCapability.RICH_TEXT,
                TaskSourceCapability.PROVIDER_IDENTITIES,
                TaskSourceCapability.INCREMENTAL_RECONCILIATION,
            }
        )
    )

    def __init__(
        self,
        api_base: str,
        token: str,
        *,
        username: str | None = None,
        client: JiraClient | None = None,
    ) -> None:
        api_base = str(api_base or "").strip().rstrip("/")
        token = str(token or "").strip()
        username = str(username or "").strip() or None
        if not api_base:
            raise ValueError("Jira task source api_base must not be empty")
        if not token:
            raise ValueError("Jira task source credential must not be empty")
        self.source_instance = api_base
        self.api_base = api_base
        self.token = token
        self.username = username
        self.client = client or JiraClient()

    def _identity(
        self,
        key: str,
        *,
        revision: str | None = None,
        external_url: str | None = None,
    ) -> TaskSourceIdentity:
        return TaskSourceIdentity(
            source_type=self.source_type,
            source_instance=self.source_instance,
            external_id=key,
            external_url=external_url or f"{self.api_base}/browse/{key}",
            revision=revision,
        )

    def _validate_identity(self, identity: TaskSourceIdentity) -> str:
        if identity.source_type.casefold() != self.source_type:
            raise ValueError("Task-source identity does not belong to Jira adapter")
        if identity.source_instance.rstrip("/") != self.source_instance:
            raise ValueError("Task-source identity belongs to another Jira instance")
        key = str(identity.external_id or "").strip()
        if not key:
            raise ValueError("Jira issue key must not be empty")
        return key

    @staticmethod
    def _owner_reference(value: Any) -> tuple[TaskSourceUserReference, ...]:
        if not isinstance(value, dict):
            return ()
        provider_id = str(
            value.get("accountId")
            or value.get("key")
            or value.get("name")
            or ""
        ).strip()
        if not provider_id:
            return ()
        return (
            TaskSourceUserReference(
                provider_id=provider_id,
                display_name=str(value.get("displayName") or "").strip() or None,
                username=str(value.get("name") or "").strip() or None,
                email_hint=str(value.get("emailAddress") or "").strip() or None,
            ),
        )

    @staticmethod
    def _owner_names(refs: tuple[TaskSourceUserReference, ...]) -> tuple[str, ...]:
        return tuple(
            ref.username or ref.display_name or ref.provider_id
            for ref in refs
        )

    def _snapshot_from_issue(self, issue: dict[str, Any]) -> TaskSourceSnapshot:
        key = str(issue.get("key") or "").strip()
        if not key:
            raise ValueError("Jira issue is missing key")
        fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}
        status = fields.get("status") if isinstance(fields.get("status"), dict) else {}
        priority = fields.get("priority") if isinstance(fields.get("priority"), dict) else {}
        issue_type = fields.get("issuetype") if isinstance(fields.get("issuetype"), dict) else {}
        parent = fields.get("parent") if isinstance(fields.get("parent"), dict) else {}
        owner_refs = self._owner_reference(fields.get("assignee"))
        revision = str(fields.get("updated") or "").strip() or None
        return TaskSourceSnapshot(
            identity=self._identity(key, revision=revision),
            title=str(fields.get("summary") or "").strip() or None,
            body_text=_adf_text(fields.get("description")),
            source_state=str(status.get("name") or "").strip() or None,
            owners=self._owner_names(owner_refs),
            owner_references=owner_refs,
            labels=tuple(
                dict.fromkeys(
                    str(value).strip()
                    for value in fields.get("labels", [])
                    if str(value).strip()
                )
            ),
            priority=str(priority.get("name") or "").strip() or None,
            category=str(issue_type.get("name") or "").strip() or None,
            parent_external_id=str(parent.get("key") or "").strip() or None,
        )

    async def discover(self, *, scope: str) -> list[TaskSourceSnapshot]:
        page = await self.discover_page(scope=scope, limit=100)
        snapshots = list(page.items)
        cursor = page.next_cursor
        while cursor is not None:
            page = await self.discover_page(scope=scope, cursor=cursor, limit=100)
            snapshots.extend(page.items)
            cursor = page.next_cursor
        return snapshots

    async def discover_page(
        self,
        *,
        scope: str,
        cursor: str | None = None,
        limit: int = 100,
    ) -> TaskSourcePage:
        self.capabilities.require(TaskSourceCapability.PAGED_DISCOVERY)
        project = str(scope or "").strip()
        if not project:
            raise ValueError("Jira discovery scope must not be empty")
        bounded = max(1, min(int(limit), 100))
        start_at = int(cursor or 0)
        payload = await self.client.search_issues(
            self.api_base,
            token=self.token,
            username=self.username,
            jql=f'project = "{project}" ORDER BY updated ASC, key ASC',
            start_at=start_at,
            max_results=bounded,
        )
        issues = payload.get("issues", []) if isinstance(payload, dict) else []
        snapshots: list[TaskSourceSnapshot] = []
        for issue in issues if isinstance(issues, list) else []:
            if isinstance(issue, dict):
                with contextlib.suppress(ValueError):
                    snapshots.append(self._snapshot_from_issue(issue))
        total = int(payload.get("total") or len(snapshots))
        next_start = start_at + len(issues)
        exhausted = next_start >= total or not issues
        watermark = None
        if snapshots:
            last = snapshots[-1]
            watermark = f"{last.identity.revision or ''}|{last.identity.external_id}"
        return TaskSourcePage(
            items=tuple(snapshots),
            next_cursor=None if exhausted else str(next_start),
            watermark=watermark,
            exhausted=exhausted,
        )

    async def reconcile_since(
        self,
        *,
        scope: str,
        cursor: TaskSourceReconciliationCursor | None = None,
        limit: int = 100,
    ) -> TaskSourcePage:
        self.capabilities.require(TaskSourceCapability.INCREMENTAL_RECONCILIATION)
        project = str(scope or "").strip()
        if not project:
            raise ValueError("Jira reconciliation scope must not be empty")
        bounded = max(1, min(int(limit), 100))
        jql = f'project = "{project}"'
        if cursor is not None:
            escaped = cursor.watermark.replace('"', '\\"')
            jql += f' AND updated >= "{escaped}"'
        jql += " ORDER BY updated ASC, key ASC"
        payload = await self.client.search_issues(
            self.api_base,
            token=self.token,
            username=self.username,
            jql=jql,
            start_at=0,
            max_results=bounded,
        )
        issues = payload.get("issues", []) if isinstance(payload, dict) else []
        snapshots = tuple(
            self._snapshot_from_issue(issue)
            for issue in issues
            if isinstance(issue, dict)
        )
        if cursor is not None and cursor.tiebreaker:
            snapshots = tuple(
                snapshot
                for snapshot in snapshots
                if (
                    (snapshot.identity.revision or "") > cursor.watermark
                    or (
                        (snapshot.identity.revision or "") == cursor.watermark
                        and snapshot.identity.external_id > cursor.tiebreaker
                    )
                )
            )
        watermark = cursor.watermark if cursor is not None else None
        tiebreaker = cursor.tiebreaker if cursor is not None else None
        if snapshots:
            last = snapshots[-1]
            watermark = last.identity.revision or watermark
            tiebreaker = last.identity.external_id
        compound = f"{watermark}|{tiebreaker}" if watermark else None
        return TaskSourcePage(items=snapshots, watermark=compound, exhausted=True)

    async def read(self, identity: TaskSourceIdentity) -> TaskSourceSnapshot:
        self.capabilities.require(TaskSourceCapability.READ)
        key = self._validate_identity(identity)
        issue = await self.client.issue(
            self.api_base,
            key,
            token=self.token,
            username=self.username,
        )
        return self._snapshot_from_issue(issue)

    async def normalize_event(self, payload: object) -> TaskSourceEvent | None:
        self.capabilities.require(TaskSourceCapability.EVENTS)
        if not isinstance(payload, dict):
            return None
        event_type = str(payload.get("webhookEvent") or "").strip()
        issue = payload.get("issue")
        if not event_type or not isinstance(issue, dict):
            return None
        with contextlib.suppress(ValueError):
            snapshot = self._snapshot_from_issue(issue)
            return TaskSourceEvent(
                identity=snapshot.identity,
                event_type=event_type,
                snapshot=snapshot,
            )
        return None

    @staticmethod
    def _canonical_stage(
        snapshot: TaskSourceSnapshot,
        current_stage: WorkItemStage | None,
    ) -> WorkItemStage:
        state = (snapshot.source_state or "").strip().casefold()
        if state in {"done", "closed", "resolved", "complete", "completed"}:
            return "closed"
        return current_stage or "implementation_active"

    def project(
        self,
        snapshot: TaskSourceSnapshot,
        *,
        current_stage: WorkItemStage | None = None,
    ) -> TaskSourceCanonicalProjection:
        self._validate_identity(snapshot.identity)
        stage = self._canonical_stage(snapshot, current_stage)
        owner = snapshot.owners[0] if snapshot.owners and stage != "closed" else None
        return TaskSourceCanonicalProjection(
            identity=snapshot.identity,
            stage=stage,
            owner=owner,
            owner_known=bool(snapshot.owner_references),
            source_state=snapshot.source_state,
        )

    async def create(
        self,
        request: TaskSourceCreateRequest,
        *,
        scope: str,
    ) -> TaskSourceSnapshot:
        self.capabilities.require(TaskSourceCapability.CREATE)
        project = str(scope or "").strip()
        if not project:
            raise ValueError("Jira task creation requires project-key scope")
        fields: dict[str, Any] = {
            "project": {"key": project},
            "summary": request.title,
            "issuetype": {"name": "Task"},
        }
        if request.body:
            fields["description"] = _adf_document(request.body)
        if request.labels:
            fields["labels"] = list(request.labels)
        if request.owners:
            fields["assignee"] = {"accountId": request.owners[0]}
        created = await self.client.create_issue(
            self.api_base,
            token=self.token,
            username=self.username,
            payload={"fields": fields},
        )
        key = str(created.get("key") or "").strip()
        if not key:
            raise RuntimeError("Jira task creation returned no issue key")
        return await self.read(self._identity(key))

    async def write_owner(
        self,
        identity: TaskSourceIdentity,
        owner: str | None,
    ) -> TaskSourceSnapshot:
        self.capabilities.require(TaskSourceCapability.OWNER_WRITE)
        key = self._validate_identity(identity)
        await self.client.update_issue(
            self.api_base,
            key,
            token=self.token,
            username=self.username,
            payload={"fields": {"assignee": {"accountId": owner} if owner else None}},
        )
        return await self.read(identity)

    async def write_state(
        self,
        identity: TaskSourceIdentity,
        state: str,
    ) -> TaskSourceSnapshot:
        self.capabilities.require(TaskSourceCapability.STATE_WRITE)
        raise AssertionError("unreachable")

    async def add_comment(self, identity: TaskSourceIdentity, body: str) -> None:
        self.capabilities.require(TaskSourceCapability.COMMENTS)
        key = self._validate_identity(identity)
        text = str(body or "").strip()
        if not text:
            raise ValueError("comment body must not be empty")
        await self.client.add_comment(
            self.api_base,
            key,
            token=self.token,
            username=self.username,
            body=_adf_document(text),
        )

    async def attach_artifact(self, identity: TaskSourceIdentity, url: str) -> None:
        self.capabilities.require(TaskSourceCapability.ARTIFACT_LINKS)

    async def available_transitions(
        self,
        identity: TaskSourceIdentity,
    ) -> tuple[str, ...]:
        self.capabilities.require(TaskSourceCapability.WORKFLOW_TRANSITIONS)
        key = self._validate_identity(identity)
        transitions = await self.client.transitions(
            self.api_base,
            key,
            token=self.token,
            username=self.username,
        )
        return tuple(
            str(item.get("name") or item.get("id") or "").strip()
            for item in transitions
            if str(item.get("name") or item.get("id") or "").strip()
        )

    async def transition(
        self,
        identity: TaskSourceIdentity,
        transition: str,
    ) -> TaskSourceSnapshot:
        self.capabilities.require(TaskSourceCapability.WORKFLOW_TRANSITIONS)
        key = self._validate_identity(identity)
        requested = str(transition or "").strip()
        if not requested:
            raise ValueError("Jira workflow transition must not be empty")
        transitions = await self.client.transitions(
            self.api_base,
            key,
            token=self.token,
            username=self.username,
        )
        selected = next(
            (
                item
                for item in transitions
                if requested.casefold()
                in {
                    str(item.get("id") or "").strip().casefold(),
                    str(item.get("name") or "").strip().casefold(),
                }
            ),
            None,
        )
        if selected is None:
            raise ValueError(f"Jira workflow transition is not available: {requested}")
        transition_id = str(selected.get("id") or "").strip()
        if not transition_id:
            raise ValueError("Jira workflow transition is missing id")
        await self.client.transition_issue(
            self.api_base,
            key,
            transition_id,
            token=self.token,
            username=self.username,
        )
        return await self.read(identity)
