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
    """StateStore-primary per-thread turn queues with rollback-safe JSON mirroring."""

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

    def _raw(self) -> dict[str, Any]:
        payload = self.store.get(self.namespace)
        if payload is None:
            payload = self._legacy_payload()
            self.store.put(self.namespace, payload)
        return payload if isinstance(payload, dict) else {}

    def load(self) -> dict[str, list[QueuedTurn]]:
        raw = self._raw()
        result: dict[str, list[QueuedTurn]] = {}
        for thread_id, values in raw.items():
            if not isinstance(thread_id, str) or not isinstance(values, list):
                continue
            result[thread_id] = [
                QueuedTurn.model_validate(item)
                for item in values
                if isinstance(item, dict)
            ]
        with self._lock:
            self._snapshots[id(result)] = copy.deepcopy(raw)
        return result

    def save(self, values: dict[str, list[QueuedTurn]]) -> None:
        payload = {
            str(thread_id): [item.model_dump(mode="json") for item in items]
            for thread_id, items in sorted(values.items())
        }
        with self._lock:
            base = self._snapshots.pop(id(values), None)
        if base is None:
            self.store.put(self.namespace, payload)
            merged = payload
        else:
            changed = {
                key: value
                for key, value in payload.items()
                if base.get(key) != value
            }
            deleted = set(base) - set(payload)

            def merge(current: Any) -> dict[str, Any]:
                latest = dict(current) if isinstance(current, dict) else {}
                for key in deleted:
                    latest.pop(key, None)
                latest.update(changed)
                return latest

            merged = self.store.update(
                self.namespace,
                merge,
                default={},
            )

        atomic_write_text(
            self.legacy_path,
            json.dumps(merged, indent=2, sort_keys=True) + "\n",
            private=True,
        )
