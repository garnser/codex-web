from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from codex_web.canonical_events import (
    CanonicalEventInboxReceipt,
    CanonicalEventOutboxRecord,
    CanonicalEventOutboxStatus,
)
from codex_web.compatibility import CanonicalEventEnvelope, ContractSpec, MigrationRegistry
from codex_web.storage.state_store import StateStore


CANONICAL_EVENT_STATE_CONTRACT = ContractSpec(
    "canonical-event-state",
    "2.0",
    ("1.0", "2.0"),
)
CANONICAL_EVENT_STATE_MIGRATIONS = MigrationRegistry("canonical-event-state")
CANONICAL_EVENT_STATE_MIGRATIONS.register(
    "0.0",
    "1.0",
    lambda payload: {
        "schema_version": "1.0",
        "events": list(payload.get("events") or []),
        "idempotency": dict(payload.get("idempotency") or {}),
    },
)
CANONICAL_EVENT_STATE_MIGRATIONS.register(
    "1.0",
    "2.0",
    lambda payload: {
        **payload,
        "schema_version": "2.0",
        "outbox": dict(payload.get("outbox") or {}),
        "inbox": list(payload.get("inbox") or []),
    },
)


class CanonicalEventState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = CANONICAL_EVENT_STATE_CONTRACT.current
    events: list[CanonicalEventEnvelope] = Field(default_factory=list)
    idempotency: dict[str, str] = Field(default_factory=dict)
    outbox: dict[str, CanonicalEventOutboxRecord] = Field(default_factory=dict)
    inbox: list[CanonicalEventInboxReceipt] = Field(default_factory=list)

    def model_post_init(self, __context: Any) -> None:
        CANONICAL_EVENT_STATE_CONTRACT.require(self.schema_version)


class CanonicalEventConflictError(RuntimeError):
    pass


class CanonicalEventStore:
    namespace = "canonical_events"

    @staticmethod
    def _semantic_payload(event: CanonicalEventEnvelope) -> dict[str, Any]:
        payload = event.model_dump(mode="json")
        payload.pop("occurred_at", None)
        payload.pop("correlation_id", None)
        return payload

    def __init__(self, store: StateStore, *, max_events: int = 5000) -> None:
        self.store = store
        self.max_events = max(1, int(max_events))

    def _decode(self, payload: Any) -> CanonicalEventState:
        if payload is None:
            return CanonicalEventState()
        if not isinstance(payload, dict):
            raise ValueError("canonical event state must be an object")
        version = str(payload.get("schema_version") or "0.0")
        if version != CANONICAL_EVENT_STATE_CONTRACT.current:
            payload = CANONICAL_EVENT_STATE_MIGRATIONS.migrate(
                payload,
                from_version=version,
                to_version=CANONICAL_EVENT_STATE_CONTRACT.current,
            )
        CANONICAL_EVENT_STATE_CONTRACT.require(payload.get("schema_version", ""))
        return CanonicalEventState.model_validate(payload)

    def load(self) -> CanonicalEventState:
        return self._decode(self.store.get(self.namespace))

    def record_if_new(
        self,
        event: CanonicalEventEnvelope,
        *,
        idempotency_key: str,
        enqueue_transport: bool = False,
    ) -> tuple[CanonicalEventEnvelope, bool]:
        key = str(idempotency_key or "").strip()
        if not key:
            raise ValueError("canonical event idempotency key must not be empty")
        result: dict[str, Any] = {}

        def apply(raw: Any) -> dict[str, Any]:
            state = self._decode(raw)
            by_id = {item.event_id: item for item in state.events}
            existing_id = state.idempotency.get(key)
            if existing_id is not None:
                existing = by_id.get(existing_id)
                if existing is None:
                    raise CanonicalEventConflictError(
                        "canonical event idempotency index points to missing event"
                    )
                if self._semantic_payload(existing) != self._semantic_payload(event):
                    raise CanonicalEventConflictError(
                        "idempotency key was reused for a different canonical event"
                    )
                if enqueue_transport and existing.event_id not in state.outbox:
                    state.outbox[existing.event_id] = CanonicalEventOutboxRecord(
                        event_id=existing.event_id
                    )
                result["event"] = existing
                result["inserted"] = False
                return state.model_dump(mode="json")

            existing = by_id.get(event.event_id)
            if existing is not None:
                if self._semantic_payload(existing) != self._semantic_payload(event):
                    raise CanonicalEventConflictError(
                        "canonical event id was reused with different content"
                    )
                state.idempotency[key] = event.event_id
                if enqueue_transport and existing.event_id not in state.outbox:
                    state.outbox[existing.event_id] = CanonicalEventOutboxRecord(
                        event_id=existing.event_id
                    )
                result["event"] = existing
                result["inserted"] = False
                return state.model_dump(mode="json")

            state.events.append(event)
            state.idempotency[key] = event.event_id
            if enqueue_transport:
                state.outbox[event.event_id] = CanonicalEventOutboxRecord(
                    event_id=event.event_id
                )
            if len(state.events) > self.max_events:
                overflow = len(state.events) - self.max_events
                removable: set[str] = set()
                for candidate in state.events:
                    outbox = state.outbox.get(candidate.event_id)
                    if (
                        outbox is None
                        or outbox.status == CanonicalEventOutboxStatus.PUBLISHED
                    ):
                        removable.add(candidate.event_id)
                        overflow -= 1
                        if overflow <= 0:
                            break
                if removable:
                    state.events = [
                        item
                        for item in state.events
                        if item.event_id not in removable
                    ]
                    state.idempotency = {
                        candidate: candidate_event_id
                        for candidate, candidate_event_id in state.idempotency.items()
                        if candidate_event_id not in removable
                    }
                    for event_id in removable:
                        state.outbox.pop(event_id, None)
            result["event"] = event
            result["inserted"] = True
            return state.model_dump(mode="json")

        self.store.update(
            self.namespace,
            apply,
            default=CanonicalEventState().model_dump(mode="json"),
        )
        return result["event"], bool(result["inserted"])

    def event(self, event_id: str) -> CanonicalEventEnvelope | None:
        return next(
            (item for item in self.load().events if item.event_id == event_id),
            None,
        )

    def pending_outbox(
        self,
        *,
        now: float,
        limit: int = 100,
    ) -> list[CanonicalEventOutboxRecord]:
        rows = [
            item
            for item in self.load().outbox.values()
            if item.status == CanonicalEventOutboxStatus.PENDING
            and (item.not_before is None or item.not_before <= now)
        ]
        rows.sort(key=lambda item: (item.created_at, item.event_id))
        return rows[: max(0, int(limit))]

    def mark_outbox_published(
        self,
        event_id: str,
        *,
        backend_id: str,
        delivery_id: str | None,
        now: float,
    ) -> CanonicalEventOutboxRecord:
        result: list[CanonicalEventOutboxRecord] = []

        def apply(raw: Any) -> dict[str, Any]:
            state = self._decode(raw)
            current = state.outbox.get(event_id)
            if current is None:
                raise CanonicalEventConflictError("canonical event outbox entry not found")
            updated = current.model_copy(
                update={
                    "status": CanonicalEventOutboxStatus.PUBLISHED,
                    "attempts": current.attempts + 1,
                    "transport_backend_id": backend_id,
                    "transport_delivery_id": delivery_id,
                    "last_error_code": None,
                    "published_at": now,
                    "updated_at": now,
                }
            )
            state.outbox[event_id] = updated
            result.append(updated)
            return state.model_dump(mode="json")

        self.store.update(
            self.namespace,
            apply,
            default=CanonicalEventState().model_dump(mode="json"),
        )
        return result[0]

    def mark_outbox_failed(
        self,
        event_id: str,
        *,
        error_code: str,
        now: float,
        max_attempts: int,
        backoff_seconds: float,
    ) -> CanonicalEventOutboxRecord:
        result: list[CanonicalEventOutboxRecord] = []

        def apply(raw: Any) -> dict[str, Any]:
            state = self._decode(raw)
            current = state.outbox.get(event_id)
            if current is None:
                raise CanonicalEventConflictError("canonical event outbox entry not found")
            attempts = current.attempts + 1
            dead = attempts >= max(1, int(max_attempts))
            updated = current.model_copy(
                update={
                    "status": (
                        CanonicalEventOutboxStatus.DEAD_LETTER
                        if dead
                        else CanonicalEventOutboxStatus.PENDING
                    ),
                    "attempts": attempts,
                    "last_error_code": error_code[:200],
                    "not_before": (
                        None
                        if dead
                        else now + max(0.0, float(backoff_seconds))
                    ),
                    "updated_at": now,
                }
            )
            state.outbox[event_id] = updated
            result.append(updated)
            return state.model_dump(mode="json")

        self.store.update(
            self.namespace,
            apply,
            default=CanonicalEventState().model_dump(mode="json"),
        )
        return result[0]

    def inbox_seen(
        self,
        *,
        backend_id: str,
        delivery_id: str,
        consumer_id: str,
    ) -> bool:
        return any(
            item.transport_backend_id == backend_id
            and item.transport_delivery_id == delivery_id
            and item.consumer_id == consumer_id
            for item in self.load().inbox
        )

    def record_inbox_receipt(
        self,
        receipt: CanonicalEventInboxReceipt,
        *,
        max_receipts: int = 10000,
    ) -> CanonicalEventInboxReceipt:
        def apply(raw: Any) -> dict[str, Any]:
            state = self._decode(raw)
            duplicate = next(
                (
                    item
                    for item in state.inbox
                    if item.transport_backend_id == receipt.transport_backend_id
                    and item.transport_delivery_id == receipt.transport_delivery_id
                    and item.consumer_id == receipt.consumer_id
                ),
                None,
            )
            if duplicate is not None:
                return state.model_dump(mode="json")
            state.inbox.append(receipt)
            if len(state.inbox) > max_receipts:
                state.inbox = state.inbox[-max_receipts:]
            return state.model_dump(mode="json")

        self.store.update(
            self.namespace,
            apply,
            default=CanonicalEventState().model_dump(mode="json"),
        )
        return receipt

    def outbox_status(self) -> dict[str, int]:
        state = self.load()
        counts = {
            status.value: 0
            for status in CanonicalEventOutboxStatus
        }
        for item in state.outbox.values():
            counts[item.status.value] += 1
        return counts

    def recent(self, *, limit: int = 100) -> list[CanonicalEventEnvelope]:
        count = max(0, min(int(limit), self.max_events))
        if count == 0:
            return []
        return list(reversed(self.load().events[-count:]))
