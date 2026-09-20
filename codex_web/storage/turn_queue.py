from __future__ import annotations

import copy
import json
import threading
from pathlib import Path
from typing import Any

from codex_web.models import QueuedTurn
from codex_web.storage.json_files import atomic_write_text
from codex_web.storage.state_store import StateStore


class TurnQueueRepository:
    """Keyed per-thread turn queues with rollback-compatible JSON checkpoints."""

    namespace = "turn_queues"

    def __init__(self, store: StateStore, legacy_path: Path) -> None:
        self.store = store
        self.legacy_path = legacy_path
        self._snapshots: dict[int, dict[str, Any]] = {}
        self._lock = threading.RLock()

    def _legacy_payload(self) -> dict[str, Any]:
        if not self.legacy_path.exists():
            return {}
        try:
            payload = json.loads(self.legacy_path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _ensure_records(self) -> None:
        if self.store.record_collection_exists(self.namespace):
            return
        payload = self.store.get(self.namespace)
        if payload is None:
            payload = self._legacy_payload()
        self.store.record_replace(
            self.namespace,
            payload if isinstance(payload, dict) else {},
        )

    @staticmethod
    def _validate(values: Any) -> list[QueuedTurn]:
        if not isinstance(values, list):
            return []
        return [
            QueuedTurn.model_validate(item)
            for item in values
            if isinstance(item, dict)
        ]

    def load(self) -> dict[str, list[QueuedTurn]]:
        self._ensure_records()
        raw = self.store.record_items(self.namespace)
        result = {
            thread_id: self._validate(values)
            for thread_id, values in raw.items()
            if isinstance(thread_id, str)
        }
        with self._lock:
            self._snapshots[id(result)] = copy.deepcopy(raw)
        return result

    def get(self, thread_id: str) -> list[QueuedTurn]:
        self._ensure_records()
        return self._validate(
            self.store.record_get(self.namespace, str(thread_id))
        )

    def put(self, thread_id: str, items: list[QueuedTurn]) -> None:
        self._ensure_records()
        key = str(thread_id)
        if not items:
            self.store.record_apply(
                self.namespace,
                upserts={},
                deletes=(key,),
            )
            return
        self.store.record_apply(
            self.namespace,
            upserts={
                key: [item.model_dump(mode="json") for item in items]
            },
        )

    def delete(self, thread_id: str) -> bool:
        self._ensure_records()
        key = str(thread_id)
        existed = self.store.record_get(self.namespace, key) is not None
        if existed:
            self.store.record_apply(
                self.namespace,
                upserts={},
                deletes=(key,),
            )
        return existed

    def flush_legacy_mirror(self) -> None:
        self._ensure_records()
        atomic_write_text(
            self.legacy_path,
            json.dumps(
                self.store.record_items(self.namespace),
                indent=2,
                sort_keys=True,
            )
            + "\n",
            private=True,
        )

    def save(self, values: dict[str, list[QueuedTurn]]) -> None:
        payload = {
            str(thread_id): [
                item.model_dump(mode="json") for item in items
            ]
            for thread_id, items in sorted(values.items())
            if items
        }
        with self._lock:
            base = self._snapshots.pop(id(values), None)
        current = (
            base
            if base is not None
            else self.store.record_items(self.namespace)
        )
        upserts = {
            key: value
            for key, value in payload.items()
            if current.get(key) != value
        }
        deletes = tuple(set(current) - set(payload))
        self._ensure_records()
        if upserts or deletes:
            self.store.record_apply(
                self.namespace,
                upserts=upserts,
                deletes=deletes,
            )
        self.flush_legacy_mirror()

