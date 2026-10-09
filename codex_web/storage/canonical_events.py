from __future__ import annotations

import hashlib
import time
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
CANONICAL_EVENT_RECORDS_CONTRACT = ContractSpec(
    "canonical-event-records",
    "3.0",
    ("3.0",),
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
    records_namespace = "canonical_event_records"
    records_schema_version = CANONICAL_EVENT_RECORDS_CONTRACT.current
    _meta_key = "meta"

    @staticmethod
    def _semantic_payload(event: CanonicalEventEnvelope) -> dict[str, Any]:
        payload = event.model_dump(mode="json")
        payload.pop("occurred_at", None)
        payload.pop("correlation_id", None)
        return payload

    def __init__(self, store: StateStore, *, max_events: int = 5000) -> None:
        self.store = store
        self.max_events = max(1, int(max_events))

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @classmethod
    def _idempotency_record_key(cls, key: str) -> str:
        return f"idempotency:{cls._digest(key)}"

    @staticmethod
    def _event_alias_key(event_id: str) -> str:
        return f"event:{event_id}"

    @classmethod
    def _inbox_key(cls, event_id: str, backend_id: str) -> str:
        return f"inbox:{cls._digest(backend_id)}:{event_id}"

    @staticmethod
    def _order_key(sequence: int, event_id: str) -> str:
        return f"order:{sequence:020d}:{event_id}"

    @staticmethod
    def _pending_key(outbox: CanonicalEventOutboxRecord) -> str:
        ready_at = outbox.not_before if outbox.not_before is not None else 0.0
        return (
            f"outbox:pending:{ready_at:020.6f}:"
            f"{outbox.created_at:020.6f}:{outbox.event_id}"
        )

    @staticmethod
    def _entry(
        *,
        idempotency_key: str,
        event: CanonicalEventEnvelope,
        order_key: str,
        outbox: CanonicalEventOutboxRecord | None,
    ) -> dict[str, Any]:
        return {
            "schema_version": CANONICAL_EVENT_RECORDS_CONTRACT.current,
            "idempotency_key": idempotency_key,
            "event": event.model_dump(mode="json"),
            "order_key": order_key,
            "outbox": outbox.model_dump(mode="json") if outbox is not None else None,
            "outbox_index_key": (
                CanonicalEventStore._pending_key(outbox)
                if outbox is not None
                and outbox.status == CanonicalEventOutboxStatus.PENDING
                else None
            ),
        }

    def _records_from_legacy(self, state: CanonicalEventState) -> dict[str, Any]:
        by_event = {event_id: key for key, event_id in state.idempotency.items()}
        records: dict[str, Any] = {
            self._meta_key: {
                "schema_version": self.records_schema_version,
                "event_count": len(state.events),
                "inbox_count": len(state.inbox),
            }
        }
        for offset, event in enumerate(state.events):
            key = by_event.get(event.event_id, f"legacy-event:{event.event_id}")
            record_key = self._idempotency_record_key(key)
            order_key = self._order_key(offset, event.event_id)
            outbox = state.outbox.get(event.event_id)
            entry = self._entry(
                idempotency_key=key,
                event=event,
                order_key=order_key,
                outbox=outbox,
            )
            records[record_key] = entry
            records[self._event_alias_key(event.event_id)] = {
                "record_key": record_key
            }
            records[order_key] = {"record_key": record_key}
            if entry["outbox_index_key"] is not None:
                records[entry["outbox_index_key"]] = entry["outbox"]
        for receipt in state.inbox:
            records[self._inbox_key(receipt.event_id, receipt.transport_backend_id)] = (
                receipt.model_dump(mode="json")
            )
        return records

    def _ensure_records(self) -> None:
        meta = self.store.record_get(self.records_namespace, self._meta_key)
        if meta is not None:
            if not isinstance(meta, dict):
                raise ValueError("canonical event keyed metadata must be an object")
            CANONICAL_EVENT_RECORDS_CONTRACT.require(
                str(meta.get("schema_version") or "")
            )
            return
        legacy = self._decode(self.store.get(self.namespace))
        migrated = self._records_from_legacy(legacy)

        def initialize(current: dict[str, Any | None]) -> dict[str, Any | None]:
            if current[self._meta_key] is not None:
                current_meta = current[self._meta_key]
                if not isinstance(current_meta, dict):
                    raise ValueError("canonical event keyed metadata must be an object")
                CANONICAL_EVENT_RECORDS_CONTRACT.require(
                    str(current_meta.get("schema_version") or "")
                )
                return {}
            return migrated

        self.store.record_mutate(
            self.records_namespace,
            (self._meta_key,),
            initialize,
        )

    @staticmethod
    def _decode_entry(raw: Any) -> tuple[str, CanonicalEventEnvelope, CanonicalEventOutboxRecord | None]:
        if not isinstance(raw, dict):
            raise ValueError("invalid canonical event keyed record")
        CANONICAL_EVENT_RECORDS_CONTRACT.require(
            str(raw.get("schema_version") or "")
        )
        key = str(raw.get("idempotency_key") or "")
        event = CanonicalEventEnvelope.model_validate(raw.get("event"))
        outbox_raw = raw.get("outbox")
        outbox = (
            CanonicalEventOutboxRecord.model_validate(outbox_raw)
            if outbox_raw is not None
            else None
        )
        return key, event, outbox

    def _record_key_for_event(self, event_id: str) -> str | None:
        self._ensure_records()
        alias = self.store.record_get(
            self.records_namespace,
            self._event_alias_key(event_id),
        )
        if not isinstance(alias, dict):
            return None
        value = str(alias.get("record_key") or "")
        return value or None

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
        self._ensure_records()
        records = self.store.record_items(self.records_namespace)
        ordered: list[tuple[str, dict[str, Any]]] = sorted(
            (
                (str(raw.get("order_key") or ""), raw)
                for key, raw in records.items()
                if key.startswith("idempotency:") and isinstance(raw, dict)
            ),
            key=lambda item: item[0],
        )
        events: list[CanonicalEventEnvelope] = []
        idempotency: dict[str, str] = {}
        outbox: dict[str, CanonicalEventOutboxRecord] = {}
        for _, raw in ordered:
            key, event, outbox_record = self._decode_entry(raw)
            events.append(event)
            idempotency[key] = event.event_id
            if outbox_record is not None:
                outbox[event.event_id] = outbox_record
        inbox = [
            CanonicalEventInboxReceipt.model_validate(raw)
            for key, raw in records.items()
            if key.startswith("inbox:")
        ]
        inbox.sort(key=lambda item: (item.acknowledged_at, item.event_id))
        return CanonicalEventState(
            events=events,
            idempotency=idempotency,
            outbox=outbox,
            inbox=inbox,
        )

    def flush_legacy_mirror(self) -> None:
        """Write an explicit rollback checkpoint for pre-keyed releases."""
        self.store.put(self.namespace, self.load().model_dump(mode="json"))

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
        self._ensure_records()
        result: dict[str, Any] = {}
        record_key = self._idempotency_record_key(key)
        alias_key = self._event_alias_key(event.event_id)
        sequence = time.time_ns()

        def apply(current: dict[str, Any | None]) -> dict[str, Any | None]:
            existing_raw = current[record_key]
            alias_raw = current[alias_key]
            if existing_raw is not None:
                stored_key, existing, outbox = self._decode_entry(existing_raw)
                if stored_key != key or self._semantic_payload(existing) != self._semantic_payload(event):
                    raise CanonicalEventConflictError(
                        "idempotency key was reused for a different canonical event"
                    )
                changes: dict[str, Any | None] = {}
                if alias_raw is None:
                    changes[alias_key] = {"record_key": record_key}
                if enqueue_transport and outbox is None:
                    outbox = CanonicalEventOutboxRecord(event_id=existing.event_id)
                    updated = dict(existing_raw)
                    updated["outbox"] = outbox.model_dump(mode="json")
                    updated["outbox_index_key"] = self._pending_key(outbox)
                    changes[record_key] = updated
                    changes[updated["outbox_index_key"]] = updated["outbox"]
                result.update(event=existing, inserted=False)
                return changes
            if alias_raw is not None:
                raise CanonicalEventConflictError(
                    "canonical event id already exists under a different idempotency key"
                )
            meta = dict(current[self._meta_key] or {})
            order_key = self._order_key(sequence, event.event_id)
            outbox = CanonicalEventOutboxRecord(event_id=event.event_id) if enqueue_transport else None
            entry = self._entry(
                idempotency_key=key,
                event=event,
                order_key=order_key,
                outbox=outbox,
            )
            meta.update(
                schema_version=self.records_schema_version,
                event_count=int(meta.get("event_count") or 0) + 1,
            )
            changes = {
                self._meta_key: meta,
                record_key: entry,
                alias_key: {"record_key": record_key},
                order_key: {"record_key": record_key},
            }
            if entry["outbox_index_key"] is not None:
                changes[entry["outbox_index_key"]] = entry["outbox"]
            result.update(event=event, inserted=True)
            return changes

        self.store.record_mutate(
            self.records_namespace,
            (self._meta_key, record_key, alias_key),
            apply,
        )
        if result["inserted"]:
            self._prune_events()
        return result["event"], bool(result["inserted"])

    def record_with_document_mutation(
        self,
        namespace: str,
        default: Any,
        updater,
        event: CanonicalEventEnvelope,
        *,
        idempotency_key: str,
        enqueue_transport: bool = False,
    ) -> tuple[Any, CanonicalEventEnvelope, bool]:
        """Atomically mutate one canonical document and append event/outbox.

        Replaying the same event idempotency key returns the persisted event and
        leaves the guarded domain document unchanged.
        """

        key = str(idempotency_key or "").strip()
        if not key:
            raise ValueError("canonical event idempotency key must not be empty")
        if namespace in {self.namespace, self.records_namespace}:
            raise ValueError("guarded namespace must differ from canonical events")
        self._ensure_records()
        result: dict[str, Any] = {}
        record_key = self._idempotency_record_key(key)
        alias_key = self._event_alias_key(event.event_id)
        sequence = time.time_ns()

        def apply(documents: dict[str, Any]) -> dict[str, Any]:
            records = dict(documents[self.records_namespace])
            existing_raw = records.get(record_key)
            alias_raw = records.get(alias_key)
            if existing_raw is not None:
                stored_key, existing, outbox = self._decode_entry(existing_raw)
                if stored_key != key or self._semantic_payload(existing) != self._semantic_payload(event):
                    raise CanonicalEventConflictError(
                        "idempotency key was reused for a different canonical event"
                    )
                if alias_raw is None:
                    records[alias_key] = {"record_key": record_key}
                if enqueue_transport and outbox is None:
                    outbox = CanonicalEventOutboxRecord(event_id=existing.event_id)
                    updated = dict(existing_raw)
                    updated["outbox"] = outbox.model_dump(mode="json")
                    updated["outbox_index_key"] = self._pending_key(outbox)
                    records[record_key] = updated
                    records[updated["outbox_index_key"]] = updated["outbox"]
                result["domain"] = documents[namespace]
                result["event"] = existing
                result["inserted"] = False
                return {
                    self.records_namespace: records,
                    namespace: documents[namespace],
                }
            if alias_raw is not None:
                raise CanonicalEventConflictError(
                    "canonical event id already exists under a different idempotency key"
                )
            updated_domain = updater(documents[namespace])
            meta = dict(records.get(self._meta_key) or {})
            order_key = self._order_key(sequence, event.event_id)
            outbox = CanonicalEventOutboxRecord(event_id=event.event_id) if enqueue_transport else None
            entry = self._entry(
                idempotency_key=key,
                event=event,
                order_key=order_key,
                outbox=outbox,
            )
            meta.update(
                schema_version=self.records_schema_version,
                event_count=int(meta.get("event_count") or 0) + 1,
            )
            records[self._meta_key] = meta
            records[record_key] = entry
            records[alias_key] = {"record_key": record_key}
            records[order_key] = {"record_key": record_key}
            if entry["outbox_index_key"] is not None:
                records[entry["outbox_index_key"]] = entry["outbox"]
            result["domain"] = updated_domain
            result["event"] = event
            result["inserted"] = True
            return {
                self.records_namespace: records,
                namespace: updated_domain,
            }

        self.store.update_many(
            {
                self.records_namespace: {},
                namespace: default,
            },
            apply,
        )
        if result["inserted"]:
            self._prune_events()
        return result["domain"], result["event"], bool(result["inserted"])

    def event(self, event_id: str) -> CanonicalEventEnvelope | None:
        record_key = self._record_key_for_event(event_id)
        if record_key is None:
            return None
        raw = self.store.record_get(self.records_namespace, record_key)
        if raw is None:
            raise CanonicalEventConflictError(
                "canonical event alias points to missing event"
            )
        return self._decode_entry(raw)[1]

    def pending_outbox(
        self,
        *,
        now: float,
        limit: int = 100,
    ) -> list[CanonicalEventOutboxRecord]:
        self._ensure_records()
        count = max(0, min(int(limit), 1000))
        if count == 0:
            return []
        page, _ = self.store.record_page(
            self.records_namespace,
            key_prefix="outbox:pending:",
            limit=count,
        )
        rows: list[CanonicalEventOutboxRecord] = []
        for raw in page.values():
            item = CanonicalEventOutboxRecord.model_validate(raw)
            if item.not_before is not None and item.not_before > now:
                break
            rows.append(item)
        rows.sort(key=lambda item: (item.created_at, item.event_id))
        return rows

    def _update_outbox(
        self,
        event_id: str,
        transform,
    ) -> CanonicalEventOutboxRecord:
        record_key = self._record_key_for_event(event_id)
        if record_key is None:
            raise CanonicalEventConflictError("canonical event outbox entry not found")
        result: list[CanonicalEventOutboxRecord] = []

        def apply(current: dict[str, Any | None]) -> dict[str, Any | None]:
            raw = current[record_key]
            if not isinstance(raw, dict):
                raise CanonicalEventConflictError("canonical event outbox entry not found")
            _, _, outbox = self._decode_entry(raw)
            if outbox is None:
                raise CanonicalEventConflictError("canonical event outbox entry not found")
            updated_outbox = transform(outbox)
            updated = dict(raw)
            old_index = str(updated.get("outbox_index_key") or "") or None
            new_index = (
                self._pending_key(updated_outbox)
                if updated_outbox.status == CanonicalEventOutboxStatus.PENDING
                else None
            )
            updated["outbox"] = updated_outbox.model_dump(mode="json")
            updated["outbox_index_key"] = new_index
            changes: dict[str, Any | None] = {record_key: updated}
            if old_index is not None and old_index != new_index:
                changes[old_index] = None
            if new_index is not None:
                changes[new_index] = updated["outbox"]
            result.append(updated_outbox)
            return changes

        self.store.record_mutate(self.records_namespace, (record_key,), apply)
        return result[0]

    def mark_outbox_published(
        self,
        event_id: str,
        *,
        backend_id: str,
        delivery_id: str | None,
        now: float,
    ) -> CanonicalEventOutboxRecord:
        def transform(current: CanonicalEventOutboxRecord) -> CanonicalEventOutboxRecord:
            return current.model_copy(
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
        return self._update_outbox(event_id, transform)

    def mark_outbox_failed(
        self,
        event_id: str,
        *,
        error_code: str,
        now: float,
        max_attempts: int,
        backoff_seconds: float,
    ) -> CanonicalEventOutboxRecord:
        def transform(current: CanonicalEventOutboxRecord) -> CanonicalEventOutboxRecord:
            attempts = current.attempts + 1
            dead = attempts >= max(1, int(max_attempts))
            return current.model_copy(
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
        return self._update_outbox(event_id, transform)

    def inbox_seen(
        self,
        *,
        event_id: str,
        backend_id: str,
    ) -> bool:
        # backend_id identifies the transport consumer-group boundary. Dedupe
        # by canonical event identity rather than broker delivery ID so replay,
        # reclaim, or a newly published transport message cannot re-run an
        # already completed canonical event on another replica.
        self._ensure_records()
        return self.store.record_get(
            self.records_namespace,
            self._inbox_key(event_id, backend_id),
        ) is not None

    def record_inbox_receipt(
        self,
        receipt: CanonicalEventInboxReceipt,
        *,
        max_receipts: int = 10000,
    ) -> CanonicalEventInboxReceipt:
        self._ensure_records()
        inbox_key = self._inbox_key(receipt.event_id, receipt.transport_backend_id)

        def apply(current: dict[str, Any | None]) -> dict[str, Any | None]:
            if current[inbox_key] is not None:
                return {}
            meta = dict(current[self._meta_key] or {})
            meta["inbox_count"] = int(meta.get("inbox_count") or 0) + 1
            return {
                self._meta_key: meta,
                inbox_key: receipt.model_dump(mode="json"),
            }

        self.store.record_mutate(
            self.records_namespace,
            (self._meta_key, inbox_key),
            apply,
        )
        self._prune_inbox(max(1, int(max_receipts)))
        return receipt

    def _prune_events(self) -> None:
        while True:
            meta = self.store.record_get(self.records_namespace, self._meta_key) or {}
            if int(meta.get("event_count") or 0) <= self.max_events:
                return
            records = self.store.record_items(self.records_namespace)
            deleted = False
            for order_key in sorted(
                key for key in records if key.startswith("order:")
            ):
                alias = records.get(order_key)
                record_key = str(alias.get("record_key") or "") if isinstance(alias, dict) else ""
                raw = records.get(record_key)
                if not record_key or not isinstance(raw, dict):
                    continue
                _, event, outbox = self._decode_entry(raw)
                if outbox is not None and outbox.status != CanonicalEventOutboxStatus.PUBLISHED:
                    continue
                event_alias = self._event_alias_key(event.event_id)
                did_delete: list[bool] = []

                def prune(current: dict[str, Any | None]) -> dict[str, Any | None]:
                    current_raw = current[record_key]
                    if not isinstance(current_raw, dict):
                        return {}
                    _, current_event, current_outbox = self._decode_entry(current_raw)
                    if (
                        current_event.event_id != event.event_id
                        or current_outbox is not None
                        and current_outbox.status != CanonicalEventOutboxStatus.PUBLISHED
                    ):
                        return {}
                    current_meta = dict(current[self._meta_key] or {})
                    current_meta["event_count"] = max(
                        0, int(current_meta.get("event_count") or 0) - 1
                    )
                    changes: dict[str, Any | None] = {
                        self._meta_key: current_meta,
                        record_key: None,
                        order_key: None,
                        event_alias: None,
                    }
                    current_pending = str(current_raw.get("outbox_index_key") or "")
                    if current_pending:
                        changes[current_pending] = None
                    did_delete.append(True)
                    return changes

                keys = (self._meta_key, record_key, order_key, event_alias)
                self.store.record_mutate(self.records_namespace, keys, prune)
                if did_delete:
                    deleted = True
                    break
            if not deleted:
                return

    def _prune_inbox(self, max_receipts: int) -> None:
        meta = self.store.record_get(self.records_namespace, self._meta_key) or {}
        overflow = int(meta.get("inbox_count") or 0) - max_receipts
        if overflow <= 0:
            return
        records = self.store.record_items(self.records_namespace)
        candidates = sorted(
            (
                CanonicalEventInboxReceipt.model_validate(raw).acknowledged_at,
                key,
            )
            for key, raw in records.items()
            if key.startswith("inbox:")
        )[:overflow]
        delete_keys = tuple(key for _, key in candidates)
        if not delete_keys:
            return

        def prune(current: dict[str, Any | None]) -> dict[str, Any | None]:
            existing = [key for key in delete_keys if current.get(key) is not None]
            current_meta = dict(current[self._meta_key] or {})
            current_meta["inbox_count"] = max(
                0, int(current_meta.get("inbox_count") or 0) - len(existing)
            )
            return {
                self._meta_key: current_meta,
                **{key: None for key in existing},
            }

        self.store.record_mutate(
            self.records_namespace,
            (self._meta_key, *delete_keys),
            prune,
        )

    def outbox_status(self) -> dict[str, int]:
        self._ensure_records()
        counts = {
            status.value: 0
            for status in CanonicalEventOutboxStatus
        }
        for key, raw in self.store.record_items(self.records_namespace).items():
            if not key.startswith("idempotency:"):
                continue
            _, _, item = self._decode_entry(raw)
            if item is not None:
                counts[item.status.value] += 1
        return counts

    def recent(self, *, limit: int = 100) -> list[CanonicalEventEnvelope]:
        count = max(0, min(int(limit), self.max_events))
        if count == 0:
            return []
        return list(reversed(self.load().events[-count:]))
