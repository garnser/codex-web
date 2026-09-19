from __future__ import annotations

import hashlib
import inspect
import json
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from codex_web.canonical_events import (
    CanonicalEventInboxReceipt,
    CanonicalEventType,
    task_source_event_type,
)
from codex_web.compatibility import CANONICAL_EVENT_CONTRACT, CanonicalEventEnvelope
from codex_web.event_transport import EventTransport, EventTransportHealth
from codex_web.services.task_sources import TaskSourceEvent
from codex_web.storage.canonical_events import CanonicalEventStore


EventHandler = Callable[[CanonicalEventEnvelope], Awaitable[None] | None]
EventPredicate = Callable[[CanonicalEventEnvelope], bool]


@dataclass(frozen=True, slots=True)
class CanonicalEventDelivery:
    event: CanonicalEventEnvelope
    inserted: bool
    dispatched: int
    transport_delivery_id: str | None = None
    transport_pending: bool = False


@dataclass(frozen=True, slots=True)
class _Subscription:
    handler: EventHandler
    event_types: frozenset[str] | None
    predicate: EventPredicate | None


class CanonicalEventBus:
    """Durable canonical ingress plus deterministic in-process dispatch."""

    def __init__(
        self,
        store: CanonicalEventStore,
        *,
        transport: EventTransport | None = None,
        instance_id: str | None = None,
        outbox_max_attempts: int = 10,
        outbox_backoff_seconds: float = 1.0,
    ) -> None:
        self.store = store
        self.transport = transport
        self.instance_id = instance_id or f"control-plane-{uuid.uuid4().hex}"
        self.outbox_max_attempts = max(1, int(outbox_max_attempts))
        self.outbox_backoff_seconds = max(0.0, float(outbox_backoff_seconds))
        self._subscriptions: list[_Subscription] = []

    def subscribe(
        self,
        handler: EventHandler,
        *,
        event_types: Iterable[str | CanonicalEventType] | None = None,
        predicate: EventPredicate | None = None,
    ) -> Callable[[], None]:
        normalized = (
            frozenset(
                item.value if isinstance(item, CanonicalEventType) else str(item)
                for item in event_types
            )
            if event_types is not None
            else None
        )
        subscription = _Subscription(handler, normalized, predicate)
        self._subscriptions.append(subscription)

        def unsubscribe() -> None:
            try:
                self._subscriptions.remove(subscription)
            except ValueError:
                return

        return unsubscribe

    async def _dispatch_local(
        self,
        event: CanonicalEventEnvelope,
    ) -> int:
        dispatched = 0
        for subscription in tuple(self._subscriptions):
            if (
                subscription.event_types is not None
                and event.event_type not in subscription.event_types
            ):
                continue
            if subscription.predicate is not None and not subscription.predicate(event):
                continue
            outcome = subscription.handler(event)
            if inspect.isawaitable(outcome):
                await outcome
            dispatched += 1
        return dispatched

    async def _publish_transport(
        self,
        event: CanonicalEventEnvelope,
        *,
        attempt: int,
    ) -> tuple[str | None, bool]:
        if self.transport is None:
            return None, False
        now = time.time()
        try:
            delivery = await self.transport.publish(
                event,
                attempt=max(1, attempt),
            )
        except Exception as exc:
            self.store.mark_outbox_failed(
                event.event_id,
                error_code=f"{type(exc).__name__}",
                now=now,
                max_attempts=self.outbox_max_attempts,
                backoff_seconds=(
                    self.outbox_backoff_seconds
                    * (2 ** max(0, attempt - 1))
                ),
            )
            return None, True
        self.store.mark_outbox_published(
            event.event_id,
            backend_id=delivery.backend_id,
            delivery_id=delivery.transport_message_id or delivery.id,
            now=now,
        )
        return delivery.transport_message_id or delivery.id, False

    async def dispatch_committed(
        self,
        event: CanonicalEventEnvelope,
        *,
        inserted: bool = True,
    ) -> CanonicalEventDelivery:
        if not inserted:
            return CanonicalEventDelivery(event, False, 0)
        distributed_transport = bool(
            self.transport is not None
            and self.transport.capabilities.durable
            and self.transport.capabilities.consumer_groups
        )
        # Shared consumer-group transport is the wakeup/fan-out path in
        # replicated mode. Do not also run process-local subscribers here or
        # one canonical event could execute handlers twice.
        dispatched = (
            0
            if distributed_transport
            else await self._dispatch_local(event)
        )
        transport_delivery_id = None
        transport_pending = False
        if self.transport is not None:
            outbox = self.store.load().outbox.get(event.event_id)
            attempt = (outbox.attempts + 1) if outbox is not None else 1
            transport_delivery_id, transport_pending = await self._publish_transport(
                event,
                attempt=attempt,
            )
        return CanonicalEventDelivery(
            event,
            True,
            dispatched,
            transport_delivery_id=transport_delivery_id,
            transport_pending=transport_pending,
        )

    async def publish(
        self,
        event: CanonicalEventEnvelope,
        *,
        idempotency_key: str,
    ) -> CanonicalEventDelivery:
        CANONICAL_EVENT_CONTRACT.require(event.schema_version)
        persisted, inserted = self.store.record_if_new(
            event,
            idempotency_key=idempotency_key,
            enqueue_transport=self.transport is not None,
        )
        if not inserted:
            return CanonicalEventDelivery(persisted, False, 0)

        # Canonical state is already committed before either local handlers or
        # transport publication run.
        return await self.dispatch_committed(persisted, inserted=True)

    async def dispatch_outbox_once(
        self,
        *,
        limit: int = 100,
        now: float | None = None,
    ) -> dict[str, int]:
        if self.transport is None:
            return {"attempted": 0, "published": 0, "pending": 0}
        current = time.time() if now is None else float(now)
        rows = self.store.pending_outbox(now=current, limit=limit)
        published = 0
        for row in rows:
            event = self.store.event(row.event_id)
            if event is None:
                self.store.mark_outbox_failed(
                    row.event_id,
                    error_code="canonical_event_missing",
                    now=current,
                    max_attempts=1,
                    backoff_seconds=0,
                )
                continue
            _delivery_id, pending = await self._publish_transport(
                event,
                attempt=row.attempts + 1,
            )
            if not pending:
                published += 1
        return {
            "attempted": len(rows),
            "published": published,
            "pending": self.store.outbox_status().get("pending", 0),
        }

    async def consume_transport_once(
        self,
        consumer_id: str,
        *,
        limit: int = 100,
        timeout_seconds: float = 1.0,
    ) -> dict[str, int]:
        """Dispatch shared transport messages only after canonical-state lookup.

        A broker message can wake a replica, but it cannot create canonical
        truth. If the referenced event is absent from shared state the message
        is rejected/dead-lettered.
        """

        if self.transport is None:
            return {"received": 0, "dispatched": 0, "acknowledged": 0}
        deliveries = await self.transport.consume(
            consumer_id,
            limit=limit,
            timeout_seconds=timeout_seconds,
        )
        dispatched = 0
        acknowledged = 0
        for delivery in deliveries:
            canonical = self.store.event(delivery.canonical_event_id)
            if canonical is None:
                await self.transport.negative_acknowledge(
                    delivery,
                    consumer_id=consumer_id,
                    reason="canonical_event_missing",
                    retry=False,
                )
                continue
            if self.store.inbox_seen(
                backend_id=delivery.backend_id,
                delivery_id=delivery.transport_message_id or delivery.id,
                consumer_id=consumer_id,
            ):
                await self.transport.acknowledge(
                    delivery,
                    consumer_id=consumer_id,
                )
                acknowledged += 1
                continue
            if delivery.backend_id != "in-process":
                dispatched += await self._dispatch_local(canonical)
            acknowledged_delivery = await self.transport.acknowledge(
                delivery,
                consumer_id=consumer_id,
            )
            self.store.record_inbox_receipt(
                CanonicalEventInboxReceipt(
                    event_id=canonical.event_id,
                    transport_backend_id=delivery.backend_id,
                    transport_delivery_id=(
                        delivery.transport_message_id
                        or acknowledged_delivery.transport_message_id
                        or delivery.id
                    ),
                    consumer_id=consumer_id,
                )
            )
            acknowledged += 1
        return {
            "received": len(deliveries),
            "dispatched": dispatched,
            "acknowledged": acknowledged,
        }

    async def transport_health(self) -> EventTransportHealth | None:
        if self.transport is None:
            return None
        return await self.transport.health()



class CanonicalEventIngestionService:
    """Normalize deterministic source facts into canonical event envelopes."""

    def __init__(self, bus: CanonicalEventBus) -> None:
        self.bus = bus

    @staticmethod
    def _event_id(source: str, idempotency_key: str) -> str:
        material = f"{source}\n{idempotency_key}".encode("utf-8")
        return f"evt-{hashlib.sha256(material).hexdigest()[:32]}"

    async def ingest(
        self,
        *,
        event_type: str | CanonicalEventType,
        source: str,
        idempotency_key: str,
        payload: dict[str, Any],
        occurred_at: float | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        tenant_id: str | None = None,
        workspace_id: str | None = None,
    ) -> CanonicalEventDelivery:
        normalized_source = str(source or "").strip()
        normalized_key = str(idempotency_key or "").strip()
        normalized_type = (
            event_type.value
            if isinstance(event_type, CanonicalEventType)
            else str(event_type or "").strip()
        )
        if not normalized_source:
            raise ValueError("canonical event source must not be empty")
        if not normalized_key:
            raise ValueError("canonical event idempotency key must not be empty")
        if not normalized_type:
            raise ValueError("canonical event type must not be empty")

        event_id = self._event_id(normalized_source, normalized_key)
        event = CanonicalEventEnvelope(
            event_id=event_id,
            event_type=normalized_type,
            occurred_at=time.time() if occurred_at is None else float(occurred_at),
            source=normalized_source,
            correlation_id=correlation_id or event_id,
            causation_id=causation_id,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            payload=dict(payload),
        )
        return await self.bus.publish(event, idempotency_key=normalized_key)

    async def ingest_with_mutation(
        self,
        *,
        namespace: str,
        default: Any,
        updater: Callable[[Any], Any],
        event_type: str | CanonicalEventType,
        source: str,
        idempotency_key: str,
        payload: dict[str, Any],
        occurred_at: float | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        tenant_id: str | None = None,
        workspace_id: str | None = None,
    ) -> tuple[Any, CanonicalEventDelivery]:
        normalized_source = str(source or "").strip()
        normalized_key = str(idempotency_key or "").strip()
        normalized_type = (
            event_type.value
            if isinstance(event_type, CanonicalEventType)
            else str(event_type or "").strip()
        )
        if not normalized_source or not normalized_key or not normalized_type:
            raise ValueError("canonical mutation event source/key/type must not be empty")
        event_id = self._event_id(normalized_source, normalized_key)
        event = CanonicalEventEnvelope(
            event_id=event_id,
            event_type=normalized_type,
            occurred_at=time.time() if occurred_at is None else float(occurred_at),
            source=normalized_source,
            correlation_id=correlation_id or event_id,
            causation_id=causation_id,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            payload=dict(payload),
        )
        domain, persisted, inserted = self.bus.store.record_with_document_mutation(
            namespace,
            default,
            updater,
            event,
            idempotency_key=normalized_key,
            enqueue_transport=self.bus.transport is not None,
        )
        delivery = await self.bus.dispatch_committed(
            persisted,
            inserted=inserted,
        )
        return domain, delivery

    async def ingest_task_source(
        self,
        event: TaskSourceEvent,
        *,
        project_id: str,
        tenant_id: str | None = None,
        workspace_id: str | None = None,
        event_cursor: str | None = None,
    ) -> CanonicalEventDelivery:
        identity = event.identity
        snapshot = event.snapshot
        provider_key = {
            "source_type": identity.source_type,
            "source_instance": identity.source_instance,
            "external_id": identity.external_id,
            "revision": identity.revision,
            "event_type": event.event_type,
            "occurred_at": event.occurred_at,
            "event_cursor": event_cursor,
        }
        idempotency_key = json.dumps(
            provider_key,
            sort_keys=True,
            separators=(",", ":"),
        )
        payload: dict[str, Any] = {
            "project_id": project_id,
            "provider_event_type": event.event_type,
            "identity": {
                "source_type": identity.source_type,
                "source_instance": identity.source_instance,
                "external_id": identity.external_id,
                "revision": identity.revision,
            },
        }
        if snapshot is not None:
            payload["snapshot"] = {
                "title": snapshot.title,
                "source_state": snapshot.source_state,
                "owners": list(snapshot.owners),
                "labels": list(snapshot.labels),
                "artifact_links": list(snapshot.artifact_links),
            }
        return await self.ingest(
            event_type=task_source_event_type(event.event_type),
            source=f"task-source:{identity.source_type}:{identity.source_instance}",
            idempotency_key=idempotency_key,
            payload=payload,
            occurred_at=event.occurred_at,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
        )
