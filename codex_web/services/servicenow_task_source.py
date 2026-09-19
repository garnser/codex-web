from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Any

from codex_web.compatibility import TASK_SOURCE_CONTRACT
from codex_web.integrations.servicenow_client import ServiceNowClient
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


@dataclass(frozen=True, slots=True)
class ServiceNowFieldMapping:
    number: str = "number"
    title: str = "short_description"
    body: str = "description"
    state: str = "state"
    assignee: str = "assigned_to"
    priority: str = "priority"
    category: str = "category"
    parent: str = "parent"
    updated: str = "sys_updated_on"
    sys_id: str = "sys_id"
    comment: str = "comments"

    def fields(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                value
                for value in (
                    self.sys_id,
                    self.number,
                    self.title,
                    self.body,
                    self.state,
                    self.assignee,
                    self.priority,
                    self.category,
                    self.parent,
                    self.updated,
                )
                if value
            )
        )


def _display(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("display_value") or value.get("value")
    text = str(value or "").strip()
    return text or None


def _raw(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("value") or value.get("display_value")
    text = str(value or "").strip()
    return text or None


class ServiceNowTaskSource:
    """Configurable ServiceNow Task-derived authoritative TaskSource adapter."""

    source_type = "servicenow"
    contract_version = TASK_SOURCE_CONTRACT.current

    def __init__(
        self,
        api_base: str,
        token: str,
        *,
        table: str = "task",
        fields: ServiceNowFieldMapping | None = None,
        canonical_state_values: dict[WorkItemStage, str] | None = None,
        client: ServiceNowClient | None = None,
    ) -> None:
        api_base = str(api_base or "").strip().rstrip("/")
        token = str(token or "").strip()
        table = str(table or "").strip()
        if not api_base:
            raise ValueError("ServiceNow task source api_base must not be empty")
        if not token:
            raise ValueError("ServiceNow task source credential must not be empty")
        if not table:
            raise ValueError("ServiceNow task source table must not be empty")
        self.source_instance = api_base
        self.api_base = api_base
        self.token = token
        self.table = table
        self.fields = fields or ServiceNowFieldMapping()
        self.canonical_state_values = dict(canonical_state_values or {})
        supported = {
            TaskSourceCapability.CREATE,
            TaskSourceCapability.DISCOVERY,
            TaskSourceCapability.PAGED_DISCOVERY,
            TaskSourceCapability.READ,
            TaskSourceCapability.EVENTS,
            TaskSourceCapability.OWNER_WRITE,
            TaskSourceCapability.COMMENTS,
            TaskSourceCapability.PROVIDER_IDENTITIES,
            TaskSourceCapability.INCREMENTAL_RECONCILIATION,
        }
        if self.canonical_state_values:
            supported.add(TaskSourceCapability.STATE_WRITE)
            supported.add(TaskSourceCapability.WORKFLOW_TRANSITIONS)
        self.capabilities = TaskSourceCapabilities(frozenset(supported))
        self.client = client or ServiceNowClient()

    def _identity(
        self,
        sys_id: str,
        *,
        number: str | None = None,
        revision: str | None = None,
    ) -> TaskSourceIdentity:
        return TaskSourceIdentity(
            source_type=self.source_type,
            source_instance=self.source_instance,
            external_id=sys_id,
            external_url=(
                f"{self.api_base}/nav_to.do?uri={self.table}.do?sys_id={sys_id}"
                if sys_id
                else None
            ),
            revision=revision,
            event_cursor=number,
        )

    def _validate_identity(self, identity: TaskSourceIdentity) -> str:
        if identity.source_type.casefold() != self.source_type:
            raise ValueError("Task-source identity does not belong to ServiceNow adapter")
        if identity.source_instance.rstrip("/") != self.source_instance:
            raise ValueError("Task-source identity belongs to another ServiceNow instance")
        sys_id = str(identity.external_id or "").strip()
        if not sys_id:
            raise ValueError("ServiceNow sys_id must not be empty")
        return sys_id

    def _snapshot_from_record(self, record: dict[str, Any]) -> TaskSourceSnapshot:
        sys_id = _raw(record.get(self.fields.sys_id))
        if not sys_id:
            raise ValueError("ServiceNow record is missing sys_id")
        number = _display(record.get(self.fields.number))
        updated = _raw(record.get(self.fields.updated))
        assignee_value = record.get(self.fields.assignee)
        assignee_id = _raw(assignee_value)
        assignee_name = _display(assignee_value)
        refs = (
            (
                TaskSourceUserReference(
                    provider_id=assignee_id,
                    display_name=assignee_name,
                ),
            )
            if assignee_id
            else ()
        )
        return TaskSourceSnapshot(
            identity=self._identity(
                sys_id,
                number=number,
                revision=updated,
            ),
            title=_display(record.get(self.fields.title)),
            body_text=_display(record.get(self.fields.body)),
            source_state=_display(record.get(self.fields.state)),
            owners=tuple(
                ref.display_name or ref.provider_id
                for ref in refs
            ),
            owner_references=refs,
            priority=_display(record.get(self.fields.priority)),
            category=_display(record.get(self.fields.category)),
            parent_external_id=_raw(record.get(self.fields.parent)),
        )

    async def discover(self, *, scope: str) -> list[TaskSourceSnapshot]:
        snapshots: list[TaskSourceSnapshot] = []
        cursor: str | None = None
        while True:
            page = await self.discover_page(scope=scope, cursor=cursor, limit=100)
            snapshots.extend(page.items)
            if page.exhausted:
                return snapshots
            cursor = page.next_cursor

    async def discover_page(
        self,
        *,
        scope: str,
        cursor: str | None = None,
        limit: int = 100,
    ) -> TaskSourcePage:
        self.capabilities.require(TaskSourceCapability.PAGED_DISCOVERY)
        query = str(scope or "").strip()
        bounded = max(1, min(int(limit), 100))
        offset = int(cursor or 0)
        records = await self.client.list_records(
            self.api_base,
            self.table,
            token=self.token,
            query=query,
            fields=self.fields.fields(),
            limit=bounded,
            offset=offset,
        )
        snapshots = tuple(
            self._snapshot_from_record(record)
            for record in records
        )
        exhausted = len(records) < bounded
        watermark = None
        if snapshots:
            last = snapshots[-1]
            watermark = f"{last.identity.revision or ''}|{last.identity.external_id}"
        return TaskSourcePage(
            items=snapshots,
            next_cursor=None if exhausted else str(offset + len(records)),
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
        query = str(scope or "").strip()
        if cursor is not None:
            clause = f"{self.fields.updated}>={cursor.watermark}"
            query = f"{query}^{clause}" if query else clause
        order = f"ORDERBY{self.fields.updated}^ORDERBY{self.fields.sys_id}"
        query = f"{query}^{order}" if query else order
        records = await self.client.list_records(
            self.api_base,
            self.table,
            token=self.token,
            query=query,
            fields=self.fields.fields(),
            limit=max(1, min(int(limit), 100)),
            offset=0,
        )
        snapshots = tuple(self._snapshot_from_record(record) for record in records)
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
        tie = cursor.tiebreaker if cursor is not None else None
        if snapshots:
            last = snapshots[-1]
            watermark = last.identity.revision or watermark
            tie = last.identity.external_id
        return TaskSourcePage(
            items=snapshots,
            watermark=f"{watermark}|{tie}" if watermark else None,
            exhausted=True,
        )

    async def read(self, identity: TaskSourceIdentity) -> TaskSourceSnapshot:
        self.capabilities.require(TaskSourceCapability.READ)
        sys_id = self._validate_identity(identity)
        record = await self.client.record(
            self.api_base,
            self.table,
            sys_id,
            token=self.token,
            fields=self.fields.fields(),
        )
        if not record:
            raise LookupError(sys_id)
        return self._snapshot_from_record(record)

    async def normalize_event(self, payload: object) -> TaskSourceEvent | None:
        self.capabilities.require(TaskSourceCapability.EVENTS)
        if not isinstance(payload, dict):
            return None
        record = payload.get("record") if isinstance(payload.get("record"), dict) else payload
        with contextlib.suppress(ValueError):
            snapshot = self._snapshot_from_record(record)
            event_type = str(payload.get("event_type") or "record.updated").strip()
            return TaskSourceEvent(
                identity=snapshot.identity,
                event_type=event_type,
                snapshot=snapshot,
            )
        return None

    def project(
        self,
        snapshot: TaskSourceSnapshot,
        *,
        current_stage: WorkItemStage | None = None,
    ) -> TaskSourceCanonicalProjection:
        self._validate_identity(snapshot.identity)
        source_state = (snapshot.source_state or "").strip().casefold()
        closed_values = {
            "closed",
            "complete",
            "completed",
            "resolved",
        }
        closed_values.update(
            str(value).strip().casefold()
            for stage, value in self.canonical_state_values.items()
            if stage == "closed"
        )
        stage: WorkItemStage
        if source_state and source_state in closed_values:
            stage = "closed"
        else:
            stage = current_stage or "implementation_active"
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
        payload: dict[str, Any] = {
            self.fields.title: request.title,
        }
        if request.body:
            payload[self.fields.body] = request.body
        if request.owners:
            payload[self.fields.assignee] = request.owners[0]
        if not str(scope or "").strip():
            raise ValueError("ServiceNow task creation scope must not be empty")
        record = await self.client.create_record(
            self.api_base,
            self.table,
            token=self.token,
            payload=payload,
        )
        if not record:
            raise RuntimeError("ServiceNow task creation returned no record")
        return self._snapshot_from_record(record)

    async def write_owner(
        self,
        identity: TaskSourceIdentity,
        owner: str | None,
    ) -> TaskSourceSnapshot:
        self.capabilities.require(TaskSourceCapability.OWNER_WRITE)
        sys_id = self._validate_identity(identity)
        await self.client.update_record(
            self.api_base,
            self.table,
            sys_id,
            token=self.token,
            payload={self.fields.assignee: owner or ""},
        )
        return await self.read(identity)

    async def write_state(
        self,
        identity: TaskSourceIdentity,
        state: str,
    ) -> TaskSourceSnapshot:
        self.capabilities.require(TaskSourceCapability.STATE_WRITE)
        if state not in self.canonical_state_values:
            raise ValueError(f"No ServiceNow state mapping for canonical stage: {state}")
        sys_id = self._validate_identity(identity)
        await self.client.update_record(
            self.api_base,
            self.table,
            sys_id,
            token=self.token,
            payload={self.fields.state: self.canonical_state_values[state]},
        )
        return await self.read(identity)

    async def add_comment(self, identity: TaskSourceIdentity, body: str) -> None:
        self.capabilities.require(TaskSourceCapability.COMMENTS)
        sys_id = self._validate_identity(identity)
        text = str(body or "").strip()
        if not text:
            raise ValueError("comment body must not be empty")
        await self.client.update_record(
            self.api_base,
            self.table,
            sys_id,
            token=self.token,
            payload={self.fields.comment: text},
        )

    async def attach_artifact(self, identity: TaskSourceIdentity, url: str) -> None:
        self.capabilities.require(TaskSourceCapability.ARTIFACT_LINKS)

    async def available_transitions(
        self,
        identity: TaskSourceIdentity,
    ) -> tuple[str, ...]:
        self.capabilities.require(TaskSourceCapability.WORKFLOW_TRANSITIONS)
        self._validate_identity(identity)
        return tuple(self.canonical_state_values)

    async def transition(
        self,
        identity: TaskSourceIdentity,
        transition: str,
    ) -> TaskSourceSnapshot:
        self.capabilities.require(TaskSourceCapability.WORKFLOW_TRANSITIONS)
        requested = str(transition or "").strip()
        if requested not in self.canonical_state_values:
            raise ValueError(f"ServiceNow workflow transition is not configured: {requested}")
        return await self.write_state(identity, requested)
