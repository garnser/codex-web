from __future__ import annotations

import logging
import time
from typing import Callable, Protocol

from codex_web.attention import (
    AttentionItem,
    AttentionItemCreate,
    AttentionSeverity,
    AttentionSource,
    AttentionStatus,
    EscalationPolicy,
    TERMINAL_ATTENTION_STATUSES,
)
from codex_web.canonical_events import CanonicalEventType
from codex_web.compatibility import CanonicalEventEnvelope
from codex_web.identity import AuthenticationActor, MembershipRole
from codex_web.scheduler import MisfirePolicy, ScheduleCreate
from codex_web.services.canonical_events import CanonicalEventBus, CanonicalEventIngestionService
from codex_web.services.scheduler import SchedulerService
from codex_web.storage.attention import AttentionItemNotFoundError, AttentionStore


Clock = Callable[[], float]
logger = logging.getLogger(__name__)


class AttentionNotificationAdapter(Protocol):
    """Provider-neutral best-effort delivery after canonical persistence."""

    async def deliver(self, item: AttentionItem) -> None: ...


class AttentionStateError(RuntimeError):
    pass


class AttentionService:
    ESCALATION_TRIGGER_TYPE = "attention.escalate"

    def __init__(
        self,
        store: AttentionStore,
        canonical_events: CanonicalEventIngestionService,
        *,
        scheduler: SchedulerService | None = None,
        clock: Clock = time.time,
    ) -> None:
        self.store = store
        self.canonical_events = canonical_events
        self.scheduler = scheduler
        self.clock = clock
        self.notification_adapters: list[AttentionNotificationAdapter] = []

    def register_notification_adapter(
        self,
        adapter: AttentionNotificationAdapter,
    ) -> None:
        self.notification_adapters.append(adapter)

    async def _notify(self, item: AttentionItem) -> None:
        for adapter in tuple(self.notification_adapters):
            try:
                await adapter.deliver(item)
            except Exception:
                # Delivery is deliberately downstream of canonical persistence:
                # provider failure must never lose or roll back an AttentionItem.
                logger.exception(
                    "attention notification delivery failed",
                    extra={"attention_item_id": item.id},
                )

    def list(self, actor: AuthenticationActor) -> list[AttentionItem]:
        return [
            item
            for item in self.store.list()
            if item.organization_id == actor.tenant.organization_id
            and item.workspace_id == actor.tenant.workspace_id
            and (
                (
                    not item.recipient_identity_ids
                    and not item.recipient_team_ids
                    and item.owner_identity_id is None
                )
                or actor.identity_id in item.recipient_identity_ids
                or bool(set(actor.team_ids) & set(item.recipient_team_ids))
                or actor.identity_id == item.owner_identity_id
                or actor.has_role(MembershipRole.OWNER, MembershipRole.ADMIN)
            )
        ]

    def list_page(
        self,
        actor: AuthenticationActor,
        *,
        limit: int = 50,
        cursor: int = 0,
        status: str | None = None,
        severity: str | None = None,
        project_id: str | None = None,
        item_type: str | None = None,
        assignee: str | None = None,
    ) -> tuple[list[AttentionItem], int | null, int]:
        bounded_limit = max(1, min(int(limit), 100))
        offset = max(0, int(cursor))
        visible = self.list(actor)
        if status:
            if status == "active":
                visible = [
                    item for item in visible
                    if item.status not in TERMINAL_ATTENTION_STATUSES
                ]
            else:
                visible = [item for item in visible if item.status.value == status]
        if severity:
            visible = [item for item in visible if item.severity.value == severity]
        if project_id:
            visible = [item for item in visible if item.project_id == project_id]
        if item_type:
            visible = [item for item in visible if item.type == item_type]
        if assignee:
            assignee_id = actor.identity_id if assignee == "me" else assignee
            visible = [
                item
                for item in visible
                if item.owner_identity_id == assignee_id
                or assignee_id in item.recipient_identity_ids
            ]
        total = len(visible)
        page = visible[offset:offset + bounded_limit]
        next_cursor = offset + len(page)
        if next_cursor >= total:
            next_cursor = None
        return page, next_cursor, total

    def get(self, item_id: str, *, actor: AuthenticationActor) -> AttentionItem:
        item = self.store.get(item_id)
        if item not in self.list(actor):
            raise AttentionItemNotFoundError(item_id)
        return item

    async def _emit(self, item: AttentionItem, *, transition: str, actor_id: str) -> None:
        await self.canonical_events.ingest(
            event_type=CanonicalEventType.ATTENTION,
            source=f"attention:{item.id}",
            idempotency_key=f"{item.id}:{item.revision}:{transition}",
            payload={
                "attention_item_id": item.id,
                "transition": transition,
                "status": item.status.value,
                "type": item.type,
                "severity": item.severity.value,
                "source_object_type": item.source.object_type,
                "source_object_id": item.source.object_id,
                "actor_id": actor_id,
                "due_at": item.due_at,
                "expires_at": item.expires_at,
            },
            occurred_at=item.updated_at,
            tenant_id=item.organization_id,
            workspace_id=item.workspace_id,
        )

    def _schedule_escalation(self, item: AttentionItem, *, actor_id: str) -> AttentionItem:
        policy = item.escalation
        if (
            self.scheduler is None
            or policy is None
            or policy.escalate_at is None
            or policy.escalate_at <= self.clock()
        ):
            return item
        schedule = self.scheduler.create(
            ScheduleCreate(
                name=f"Escalate attention {item.id}",
                tenant_id=item.organization_id,
                workspace_id=item.workspace_id,
                trigger_type=self.ESCALATION_TRIGGER_TYPE,
                payload={"attention_item_id": item.id},
                due_at=policy.escalate_at,
                misfire_policy=MisfirePolicy.FIRE_ONCE,
                misfire_grace_seconds=0.0,
            ),
            actor_id=actor_id,
        )
        return self.store.transition(
            item.id,
            actor_id=actor_id,
            transition=lambda current: current.model_copy(
                update={"escalation_schedule_id": schedule.id}
            ),
            now=self.clock(),
        )

    async def upsert(self, payload: AttentionItemCreate, *, actor_id: str) -> AttentionItem:
        now = float(self.clock())
        item = AttentionItem.from_create(payload, actor_id=actor_id, now=now)
        existing = self.store.get_by_dedupe_key(payload.dedupe_key)
        if existing is not None:
            item = item.model_copy(
                update={
                    "status": (
                        AttentionStatus.OPEN
                        if existing.status in TERMINAL_ATTENTION_STATUSES
                        else existing.status
                    ),
                    "escalation_schedule_id": existing.escalation_schedule_id,
                }
            )
        saved = self.store.upsert(item)
        if saved.escalation_schedule_id is None:
            saved = self._schedule_escalation(saved, actor_id=actor_id)
        await self._emit(saved, transition="upserted", actor_id=actor_id)
        await self._notify(saved)
        return saved

    async def acknowledge(self, item_id: str, *, actor: AuthenticationActor) -> AttentionItem:
        item = self.get(item_id, actor=actor)
        if item.status in TERMINAL_ATTENTION_STATUSES:
            raise AttentionStateError("terminal attention item cannot be acknowledged")
        updated = self.store.transition(
            item.id,
            actor_id=actor.identity_id,
            transition=lambda current: current.model_copy(
                update={
                    "status": AttentionStatus.ACKNOWLEDGED,
                    "acknowledged_by_identity_id": actor.identity_id,
                    "acknowledged_at": self.clock(),
                    "snoozed_until": None,
                }
            ),
            now=self.clock(),
        )
        await self._emit(updated, transition="acknowledged", actor_id=actor.identity_id)
        return updated

    async def resolve(
        self,
        item_id: str,
        *,
        actor: AuthenticationActor,
        reason: str | None = None,
    ) -> AttentionItem:
        item = self.get(item_id, actor=actor)
        if item.status == AttentionStatus.RESOLVED:
            return item
        updated = self.store.transition(
            item.id,
            actor_id=actor.identity_id,
            transition=lambda current: current.model_copy(
                update={
                    "status": AttentionStatus.RESOLVED,
                    "resolved_by_identity_id": actor.identity_id,
                    "resolved_at": self.clock(),
                    "resolution_reason": reason,
                    "snoozed_until": None,
                }
            ),
            now=self.clock(),
        )
        await self._emit(updated, transition="resolved", actor_id=actor.identity_id)
        return updated

    async def resolve_by_source(
        self,
        dedupe_key: str,
        *,
        actor_id: str,
        reason: str,
    ) -> AttentionItem | None:
        existing = self.store.get_by_dedupe_key(dedupe_key)
        if existing is None or existing.status == AttentionStatus.RESOLVED:
            return existing
        updated = self.store.transition(
            existing.id,
            actor_id=actor_id,
            transition=lambda current: current.model_copy(
                update={
                    "status": AttentionStatus.RESOLVED,
                    "resolved_by_identity_id": actor_id,
                    "resolved_at": self.clock(),
                    "resolution_reason": reason,
                    "snoozed_until": None,
                }
            ),
            now=self.clock(),
        )
        await self._emit(updated, transition="resolved", actor_id=actor_id)
        return updated

    async def snooze(
        self,
        item_id: str,
        *,
        actor: AuthenticationActor,
        until: float,
    ) -> AttentionItem:
        item = self.get(item_id, actor=actor)
        if item.status in TERMINAL_ATTENTION_STATUSES:
            raise AttentionStateError("terminal attention item cannot be snoozed")
        if until <= self.clock():
            raise AttentionStateError("snooze time must be in the future")
        updated = self.store.transition(
            item.id,
            actor_id=actor.identity_id,
            transition=lambda current: current.model_copy(
                update={
                    "status": AttentionStatus.SNOOZED,
                    "snoozed_until": until,
                }
            ),
            now=self.clock(),
        )
        await self._emit(updated, transition="snoozed", actor_id=actor.identity_id)
        return updated

    async def escalate(self, item_id: str, *, actor_id: str = "scheduler") -> AttentionItem:
        current = self.store.get(item_id)
        if current.status in TERMINAL_ATTENTION_STATUSES:
            return current
        policy = current.escalation
        recipients = current.recipient_identity_ids
        teams = current.recipient_team_ids
        if policy is not None:
            recipients = tuple(sorted(set(recipients) | set(policy.recipient_identity_ids)))
            teams = tuple(sorted(set(teams) | set(policy.recipient_team_ids)))
        updated = self.store.transition(
            item_id,
            actor_id=actor_id,
            transition=lambda item: item.model_copy(
                update={
                    "status": AttentionStatus.ESCALATED,
                    "recipient_identity_ids": recipients,
                    "recipient_team_ids": teams,
                    "escalation_count": item.escalation_count + 1,
                }
            ),
            now=self.clock(),
        )
        await self._emit(updated, transition="escalated", actor_id=actor_id)
        await self._notify(updated)
        return updated

    async def handle_approval_event(self, event: CanonicalEventEnvelope) -> None:
        payload = event.payload
        request_id = str(payload.get("approval_request_id") or "").strip()
        status = str(payload.get("status") or "").strip()
        if not request_id or not status or not event.tenant_id or not event.workspace_id:
            return
        dedupe_key = f"approval:{request_id}"

        if status in {"pending", "partially_approved"}:
            await self.upsert(
                AttentionItemCreate(
                    organization_id=event.tenant_id,
                    workspace_id=event.workspace_id,
                    project_id=payload.get("project_id"),
                    type="approval.required",
                    severity=AttentionSeverity.HIGH,
                    source=AttentionSource(
                        object_type="approval_request",
                        object_id=request_id,
                        event_id=event.event_id,
                    ),
                    reason="Approval requires human decision",
                    dedupe_key=dedupe_key,
                    due_at=payload.get("expires_at"),
                    expires_at=payload.get("expires_at"),
                    deep_link=f"/?approval={request_id}",
                    escalation=(
                        EscalationPolicy(
                            escalate_at=payload.get("expires_at"),
                            mandatory=True,
                        )
                        if payload.get("expires_at")
                        else None
                    ),
                ),
                actor_id="approval-bridge",
            )
            return

        if status in {"rejected", "expired", "invalidated"}:
            await self.upsert(
                AttentionItemCreate(
                    organization_id=event.tenant_id,
                    workspace_id=event.workspace_id,
                    project_id=payload.get("project_id"),
                    type=f"approval.{status}",
                    severity=(
                        AttentionSeverity.CRITICAL
                        if status == "invalidated"
                        else AttentionSeverity.HIGH
                    ),
                    source=AttentionSource(
                        object_type="approval_request",
                        object_id=request_id,
                        event_id=event.event_id,
                    ),
                    reason=f"Approval {status}; operator review required",
                    dedupe_key=dedupe_key,
                    deep_link=f"/?approval={request_id}",
                ),
                actor_id="approval-bridge",
            )
            return

        if status in {"approved", "consumed", "cancelled", "superseded"}:
            await self.resolve_by_source(
                dedupe_key,
                actor_id="approval-bridge",
                reason=f"Approval transitioned to {status}",
            )

    async def handle_schedule_event(self, event: CanonicalEventEnvelope) -> None:
        if event.event_type != CanonicalEventType.SCHEDULE.value:
            return
        payload = event.payload
        if payload.get("trigger_type") != self.ESCALATION_TRIGGER_TYPE:
            return
        nested = payload.get("payload") or {}
        item_id = str(nested.get("attention_item_id") or "").strip()
        if item_id:
            try:
                await self.escalate(item_id)
            except AttentionItemNotFoundError:
                return


def install_attention_event_bridges(
    bus: CanonicalEventBus,
    service: AttentionService,
):
    unsubscribe_approval = bus.subscribe(
        service.handle_approval_event,
        event_types=(CanonicalEventType.APPROVAL,),
    )
    unsubscribe_schedule = bus.subscribe(
        service.handle_schedule_event,
        event_types=(CanonicalEventType.SCHEDULE,),
    )

    def unsubscribe() -> None:
        unsubscribe_approval()
        unsubscribe_schedule()

    return unsubscribe
