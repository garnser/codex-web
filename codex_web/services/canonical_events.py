from __future__ import annotations

import hashlib
import inspect
import json
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from codex_web.canonical_events import CanonicalEventType, task_source_event_type
from codex_web.compatibility import CANONICAL_EVENT_CONTRACT, CanonicalEventEnvelope
from codex_web.services.task_sources import TaskSourceEvent
from codex_web.storage.canonical_events import CanonicalEventStore


EventHandler = Callable[[CanonicalEventEnvelope], Awaitable[None] | None]
EventPredicate = Callable[[CanonicalEventEnvelope], bool]


@dataclass(frozen=True, slots=True)
class CanonicalEventDelivery:
    event: CanonicalEventEnvelope
    inserted: bool
    dispatched: int


@dataclass(frozen=True, slots=True)
class _Subscription:
    handler: EventHandler
    event_types: frozenset[str] | None
    predicate: EventPredicate | None


class CanonicalEventBus:
    """Durable canonical ingress plus deterministic in-process dispatch."""

    def __init__(self, store: CanonicalEventStore) -> None:
        self.store = store
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
        )
        if not inserted:
            return CanonicalEventDelivery(persisted, False, 0)

        dispatched = 0
        for subscription in tuple(self._subscriptions):
            if (
                subscription.event_types is not None
                and persisted.event_type not in subscription.event_types
            ):
                continue
            if subscription.predicate is not None and not subscription.predicate(persisted):
                continue
            outcome = subscription.handler(persisted)
            if inspect.isawaitable(outcome):
                await outcome
            dispatched += 1
        return CanonicalEventDelivery(persisted, True, dispatched)


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
