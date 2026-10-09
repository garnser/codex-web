from __future__ import annotations

from typing import Any

from codex_web.models import WorkItemEvent
from codex_web.storage.state_store import StateStore


class WorkItemEventStore:
    """Ordered, bounded canonical audit window with a rollback checkpoint."""

    legacy_namespace = "work_item_events"
    namespace = "work_item_events.records.v1"

    def __init__(self, store: StateStore, *, max_events: int = 10000) -> None:
        if max_events < 1:
            raise ValueError("work-item event retention must be positive")
        self.store = store
        self.max_events = max_events

    @staticmethod
    def _key(sequence: int) -> str:
        return f"event:{sequence:020d}"

    def _ensure_records(self) -> None:
        if not self.store.record_collection_exists(self.namespace):
            # Materialize the collection before update_many so migration writes
            # actual rows, not another large mapping decoded by record_get.
            self.store.record_mutate(self.namespace, (), lambda _current: {})
        meta = self.store.record_get(self.namespace, "meta")
        if meta is not None:
            if not isinstance(meta, dict) or meta.get("schema_version") != "1.0":
                raise ValueError("unsupported work-item event records schema")
            if meta.get("max_events") != self.max_events:
                raise ValueError("work-item event retention change requires migration")
            return

        def migrate(documents):
            records = documents[self.namespace]
            if "meta" not in records:
                legacy = documents[self.legacy_namespace]
                if not isinstance(legacy, list):
                    raise ValueError("work-item event checkpoint must be a list")
                retained = legacy[-self.max_events:]
                records = {
                    self._key(i): WorkItemEvent.model_validate(raw).model_dump(mode="json")
                    for i, raw in enumerate(retained)
                }
                records["meta"] = {"schema_version": "1.0", "next_sequence": len(retained),
                                   "max_events": self.max_events}
            return {self.namespace: records, self.legacy_namespace: documents[self.legacy_namespace]}

        self.store.update_many({self.legacy_namespace: [], self.namespace: {}}, migrate)

    def append(self, event: WorkItemEvent) -> None:
        self._ensure_records()
        payload = event.model_dump(mode="json")

        def apply(current: dict[str, Any | None]) -> dict[str, Any | None]:
            meta = dict(current["meta"])
            sequence = int(meta["next_sequence"])
            meta["next_sequence"] = sequence + 1
            changes = {"meta": meta, self._key(sequence): payload}
            if sequence >= self.max_events:
                changes[self._key(sequence - self.max_events)] = None
            return changes

        # One namespace transaction assigns the sequence, appends the row and
        # prunes the expired row; concurrent appenders cannot overwrite events.
        self.store.record_mutate(self.namespace, ("meta",), apply)

    def load(self) -> list[WorkItemEvent]:
        self._ensure_records()
        events = []
        after = None
        while True:
            records, after = self.store.record_page(
                self.namespace, key_prefix="event:", after=after, limit=100,
            )
            events.extend(WorkItemEvent.model_validate(raw) for raw in records.values())
            if after is None:
                return events

    def flush_legacy_mirror(self) -> None:
        self.store.put(self.legacy_namespace, [event.model_dump(mode="json") for event in self.load()])
