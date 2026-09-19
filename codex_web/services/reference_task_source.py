from __future__ import annotations

from dataclasses import replace
import uuid
from typing import Any

from codex_web.compatibility import TASK_SOURCE_CONTRACT
from codex_web.models import TaskSourceIdentity, WorkItemStage
from codex_web.services.task_sources import (
    TaskSourceCanonicalProjection,
    TaskSourceCapabilities,
    TaskSourceCapability,
    TaskSourceCreateRequest,
    TaskSourceEvent,
    TaskSourceSnapshot,
)


class ReferenceTaskSource:
    """Small in-memory TaskSource used to prove provider portability.

    This is intentionally provider-agnostic and useful for tests/demo fixtures.
    It implements the complete mutation surface so shared runtime services can
    be exercised without relying on GitLab semantics or transport objects.
    """

    source_type = "reference"
    contract_version = TASK_SOURCE_CONTRACT.current
    capabilities = TaskSourceCapabilities(
        frozenset(
            {
                TaskSourceCapability.CREATE,
                TaskSourceCapability.DISCOVERY,
                TaskSourceCapability.READ,
                TaskSourceCapability.EVENTS,
                TaskSourceCapability.OWNER_WRITE,
                TaskSourceCapability.STATE_WRITE,
                TaskSourceCapability.COMMENTS,
                TaskSourceCapability.ARTIFACT_LINKS,
            }
        )
    )

    def __init__(
        self,
        source_instance: str = "local-reference",
        *,
        snapshots: list[TaskSourceSnapshot] | None = None,
    ) -> None:
        instance = str(source_instance or "").strip()
        if not instance:
            raise ValueError("reference task source instance must not be empty")
        self.source_instance = instance
        self._snapshots: dict[str, TaskSourceSnapshot] = {}
        self.comments: dict[str, list[str]] = {}
        for snapshot in snapshots or []:
            self._validate_identity(snapshot.identity)
            self._snapshots[snapshot.identity.external_id] = snapshot

    def _validate_identity(self, identity: TaskSourceIdentity) -> None:
        if identity.source_type != self.source_type:
            raise ValueError("Task-source identity does not belong to reference adapter")
        if identity.source_instance != self.source_instance:
            raise ValueError("Task-source identity belongs to another reference instance")

    async def create(
        self,
        request: TaskSourceCreateRequest,
        *,
        scope: str,
    ) -> TaskSourceSnapshot:
        self.capabilities.require(TaskSourceCapability.CREATE)
        if not str(scope or "").strip():
            raise ValueError("task-source create scope must not be empty")
        external_id = f"TASK-{uuid.uuid4().hex[:12]}"
        snapshot = TaskSourceSnapshot(
            identity=TaskSourceIdentity(
                source_type=self.source_type,
                source_instance=self.source_instance,
                external_id=external_id,
                revision="1",
            ),
            title=request.title,
            source_state="open",
            owners=request.owners,
            labels=request.labels,
        )
        self._snapshots[external_id] = snapshot
        if request.body:
            self.comments.setdefault(external_id, []).append(request.body)
        return snapshot

    async def discover(self, *, scope: str) -> list[TaskSourceSnapshot]:
        self.capabilities.require(TaskSourceCapability.DISCOVERY)
        return list(self._snapshots.values())

    async def read(self, identity: TaskSourceIdentity) -> TaskSourceSnapshot:
        self.capabilities.require(TaskSourceCapability.READ)
        self._validate_identity(identity)
        try:
            return self._snapshots[identity.external_id]
        except KeyError as exc:
            raise LookupError(identity.external_id) from exc

    async def normalize_event(self, payload: object) -> TaskSourceEvent | None:
        self.capabilities.require(TaskSourceCapability.EVENTS)
        if isinstance(payload, TaskSourceEvent):
            self._validate_identity(payload.identity)
            return payload
        if not isinstance(payload, dict):
            return None
        external_id = str(payload.get("external_id") or "").strip()
        if not external_id:
            return None
        snapshot = self._snapshots.get(external_id)
        if snapshot is None:
            identity = TaskSourceIdentity(
                source_type=self.source_type,
                source_instance=self.source_instance,
                external_id=external_id,
                revision=str(payload.get("revision") or "").strip() or None,
            )
            snapshot = TaskSourceSnapshot(
                identity=identity,
                title=str(payload.get("title") or "").strip() or None,
                source_state=str(payload.get("state") or "open").strip().lower(),
                owners=tuple(str(value).strip() for value in payload.get("owners", ()) if str(value).strip()),
                labels=tuple(str(value).strip() for value in payload.get("labels", ()) if str(value).strip()),
            )
            self._snapshots[external_id] = snapshot
        return TaskSourceEvent(
            identity=snapshot.identity,
            event_type=str(payload.get("event_type") or "updated"),
            occurred_at=float(payload["occurred_at"]) if payload.get("occurred_at") is not None else None,
            snapshot=snapshot,
        )

    def project(
        self,
        snapshot: TaskSourceSnapshot,
        *,
        current_stage: WorkItemStage | None = None,
    ) -> TaskSourceCanonicalProjection:
        self._validate_identity(snapshot.identity)
        state = (snapshot.source_state or "").strip().lower()
        stage: WorkItemStage = "closed" if state in {"closed", "done"} else (current_stage or "implementation_active")
        owner = snapshot.owners[0] if snapshot.owners and stage != "closed" else None
        return TaskSourceCanonicalProjection(
            identity=snapshot.identity,
            stage=stage,
            owner=owner,
            owner_known=bool(snapshot.owners),
            source_state=snapshot.source_state,
        )

    async def write_owner(
        self,
        identity: TaskSourceIdentity,
        owner: str | None,
    ) -> TaskSourceSnapshot:
        self.capabilities.require(TaskSourceCapability.OWNER_WRITE)
        current = await self.read(identity)
        owners = (str(owner).strip(),) if owner and str(owner).strip() else ()
        updated = replace(current, owners=owners)
        self._snapshots[identity.external_id] = updated
        return updated

    async def write_state(
        self,
        identity: TaskSourceIdentity,
        state: str,
    ) -> TaskSourceSnapshot:
        self.capabilities.require(TaskSourceCapability.STATE_WRITE)
        current = await self.read(identity)
        source_state = "closed" if state == "closed" else "open"
        updated = replace(current, source_state=source_state)
        self._snapshots[identity.external_id] = updated
        return updated

    async def add_comment(self, identity: TaskSourceIdentity, body: str) -> None:
        self.capabilities.require(TaskSourceCapability.COMMENTS)
        self._validate_identity(identity)
        text = str(body or "").strip()
        if not text:
            raise ValueError("comment body must not be empty")
        self.comments.setdefault(identity.external_id, []).append(text)

    async def attach_artifact(self, identity: TaskSourceIdentity, url: str) -> None:
        self.capabilities.require(TaskSourceCapability.ARTIFACT_LINKS)
        current = await self.read(identity)
        value = str(url or "").strip()
        if not value:
            raise ValueError("artifact URL must not be empty")
        links = tuple(dict.fromkeys((*current.artifact_links, value)))
        self._snapshots[identity.external_id] = replace(current, artifact_links=links)
