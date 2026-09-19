from __future__ import annotations

from collections.abc import Iterable

from codex_web.compatibility import TASK_SOURCE_CONTRACT
from codex_web.models import TaskSourceIdentity, WorkItemStage
from codex_web.services.task_sources import (
    TaskSourceCapabilities,
    TaskSourceCapability,
    TaskSourceCanonicalProjection,
    TaskSourceEvent,
    TaskSourceSnapshot,
    UnsupportedTaskSourceCapability,
)


class ExampleTicketSource:
    """Synthetic read-only task system adapter."""

    source_type = "example-ticket"
    contract_version = TASK_SOURCE_CONTRACT.current
    capabilities = TaskSourceCapabilities(
        frozenset(
            {
                TaskSourceCapability.DISCOVERY,
                TaskSourceCapability.READ,
                TaskSourceCapability.EVENTS,
            }
        )
    )

    def __init__(
        self,
        source_instance: str = "demo",
        snapshots: Iterable[TaskSourceSnapshot] = (),
    ) -> None:
        self.source_instance = source_instance
        self._items = {
            item.identity.external_id: item
            for item in snapshots
        }

    async def discover(self, *, scope: str) -> list[TaskSourceSnapshot]:
        # A real adapter would use scope to bound the provider query.
        del scope
        return [self._items[key] for key in sorted(self._items)]

    async def read(self, identity: TaskSourceIdentity) -> TaskSourceSnapshot:
        self._validate_identity(identity)
        return self._items[identity.external_id]

    async def normalize_event(self, payload: object) -> TaskSourceEvent | None:
        if not isinstance(payload, dict):
            return None
        external_id = str(payload.get("external_id") or "").strip()
        event_type = str(payload.get("event_type") or "").strip()
        if not external_id or not event_type:
            return None

        owner = str(payload.get("owner") or "").strip()
        snapshot = TaskSourceSnapshot(
            identity=TaskSourceIdentity(
                source_type=self.source_type,
                source_instance=self.source_instance,
                external_id=external_id,
                external_url=(
                    str(payload["external_url"])
                    if payload.get("external_url")
                    else None
                ),
                revision=(
                    str(payload["revision"])
                    if payload.get("revision")
                    else None
                ),
            ),
            title=str(payload.get("title") or "").strip() or None,
            body_text=str(payload.get("body") or "").strip() or None,
            source_state=str(payload.get("state") or "").strip() or None,
            owners=(owner,) if owner else (),
            labels=tuple(
                str(value).strip()
                for value in payload.get("labels", ())
                if str(value).strip()
            ),
        )
        self._items[external_id] = snapshot
        return TaskSourceEvent(
            identity=snapshot.identity,
            event_type=event_type,
            snapshot=snapshot,
            occurred_at=(
                float(payload["occurred_at"])
                if payload.get("occurred_at") is not None
                else None
            ),
        )

    def project(
        self,
        snapshot: TaskSourceSnapshot,
        *,
        current_stage: WorkItemStage | None = None,
    ) -> TaskSourceCanonicalProjection:
        state = (snapshot.source_state or "").strip().casefold()
        stage_by_state: dict[str, WorkItemStage] = {
            "open": "implementation_active",
            "in progress": "implementation_active",
            "review": "ready_for_validation",
            "validation": "validation_running",
            "blocked": "failed_with_action_owner",
            "done": "closed",
            "closed": "closed",
        }
        stage = stage_by_state.get(state, current_stage)
        return TaskSourceCanonicalProjection(
            identity=snapshot.identity,
            stage=stage,
            owner=snapshot.owners[0] if snapshot.owners else None,
            owner_known=bool(snapshot.owners),
            source_state=snapshot.source_state,
        )

    async def write_owner(
        self,
        identity: TaskSourceIdentity,
        owner: str | None,
    ) -> TaskSourceSnapshot:
        del identity, owner
        raise UnsupportedTaskSourceCapability(TaskSourceCapability.OWNER_WRITE)

    async def write_state(
        self,
        identity: TaskSourceIdentity,
        state: str,
    ) -> TaskSourceSnapshot:
        del identity, state
        raise UnsupportedTaskSourceCapability(TaskSourceCapability.STATE_WRITE)

    async def add_comment(self, identity: TaskSourceIdentity, body: str) -> None:
        del identity, body
        raise UnsupportedTaskSourceCapability(TaskSourceCapability.COMMENTS)

    async def attach_artifact(self, identity: TaskSourceIdentity, url: str) -> None:
        del identity, url
        raise UnsupportedTaskSourceCapability(TaskSourceCapability.ARTIFACT_LINKS)

    def _validate_identity(self, identity: TaskSourceIdentity) -> None:
        if (
            identity.source_type != self.source_type
            or identity.source_instance != self.source_instance
        ):
            raise KeyError("task identity belongs to a different source")
