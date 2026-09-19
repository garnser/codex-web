from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from codex_web.compatibility import CanonicalEventEnvelope, ContractSpec, MigrationRegistry
from codex_web.storage.sqlite_state import SQLiteStateStore


CANONICAL_EVENT_STATE_CONTRACT = ContractSpec(
    "canonical-event-state",
    "1.0",
    ("1.0",),
)
CANONICAL_EVENT_STATE_MIGRATIONS = MigrationRegistry("canonical-event-state")
CANONICAL_EVENT_STATE_MIGRATIONS.register(
    "0.0",
    "1.0",
    lambda payload: {
        "schema_version": CANONICAL_EVENT_STATE_CONTRACT.current,
        "events": list(payload.get("events") or []),
        "idempotency": dict(payload.get("idempotency") or {}),
    },
)


class CanonicalEventState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = CANONICAL_EVENT_STATE_CONTRACT.current
    events: list[CanonicalEventEnvelope] = Field(default_factory=list)
    idempotency: dict[str, str] = Field(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        CANONICAL_EVENT_STATE_CONTRACT.require(self.schema_version)


class CanonicalEventConflictError(RuntimeError):
    pass


class CanonicalEventStore:
    namespace = "canonical_events"

    def __init__(self, store: SQLiteStateStore, *, max_events: int = 5000) -> None:
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
                if existing.model_dump(mode="json") != event.model_dump(mode="json"):
                    raise CanonicalEventConflictError(
                        "idempotency key was reused for a different canonical event"
                    )
                result["event"] = existing
                result["inserted"] = False
                return state.model_dump(mode="json")

            existing = by_id.get(event.event_id)
            if existing is not None:
                if existing.model_dump(mode="json") != event.model_dump(mode="json"):
                    raise CanonicalEventConflictError(
                        "canonical event id was reused with different content"
                    )
                state.idempotency[key] = event.event_id
                result["event"] = existing
                result["inserted"] = False
                return state.model_dump(mode="json")

            state.events.append(event)
            state.idempotency[key] = event.event_id
            if len(state.events) > self.max_events:
                state.events = state.events[-self.max_events :]
                retained = {item.event_id for item in state.events}
                state.idempotency = {
                    candidate: event_id
                    for candidate, event_id in state.idempotency.items()
                    if event_id in retained
                }
            result["event"] = event
            result["inserted"] = True
            return state.model_dump(mode="json")

        self.store.update(
            self.namespace,
            apply,
            default=CanonicalEventState().model_dump(mode="json"),
        )
        return result["event"], bool(result["inserted"])

    def recent(self, *, limit: int = 100) -> list[CanonicalEventEnvelope]:
        count = max(0, min(int(limit), self.max_events))
        if count == 0:
            return []
        return list(reversed(self.load().events[-count:]))
