from __future__ import annotations

import copy
import json
import threading
from pathlib import Path
from typing import Any

from codex_web.models import QueuedTurn
from codex_web.storage.json_files import atomic_write_text
from codex_web.storage.state_store import OperationTimingMetrics, StateStore


class TurnQueueRepository:
    """Keyed per-thread turn queues with rollback-compatible JSON checkpoints."""

    namespace = "turn_queues"

    def __init__(self, store: StateStore, legacy_path: Path) -> None:
        self.store = store
        self.legacy_path = legacy_path
        self._snapshots: dict[int, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._mirror_metrics = OperationTimingMetrics()

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

    def update(
        self,
        thread_id: str,
        updater,
    ) -> list[QueuedTurn]:
        self._ensure_records()
        key = str(thread_id)

        def apply(raw: Any):
            current = self._validate(raw)
            updated = list(updater(current))
            if not updated:
                return None
            return [
                item.model_dump(mode="json")
                for item in updated
            ]

        raw = self.store.record_update(
            self.namespace,
            key,
            apply,
            default=[],
        )
        return self._validate(raw)

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
        started = __import__("time").perf_counter()
        success = False
        try:
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
            success = True
        finally:
            self._mirror_metrics.observe(
                __import__("time").perf_counter() - started,
                success=success,
            )

    def compatibility_metrics(self) -> dict[str, Any]:
        return {
            "namespace": self.namespace,
            "legacyPath": str(self.legacy_path),
            "checkpoint": self._mirror_metrics.snapshot(),
        }

    @staticmethod
    def _merge_queue(
        base_items: list[Any],
        desired_items: list[Any],
        latest_items: list[Any],
    ) -> list[Any]:
        base_by_id = {
            str(item.get("id")): item
            for item in base_items
            if isinstance(item, dict) and item.get("id")
        }
        desired_by_id = {
            str(item.get("id")): item
            for item in desired_items
            if isinstance(item, dict) and item.get("id")
        }
        changed_by_id = {
            item_id: item
            for item_id, item in desired_by_id.items()
            if base_by_id.get(item_id) != item
        }
        deleted_ids = set(base_by_id) - set(desired_by_id)
        result: list[Any] = []
        seen_ids: set[str] = set()
        for item in latest_items:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or "")
            if not item_id or item_id in deleted_ids:
                continue
            result.append(changed_by_id.get(item_id, item))
            seen_ids.add(item_id)
        for item in desired_items:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or "")
            if (
                item_id
                and item_id in changed_by_id
                and item_id not in seen_ids
            ):
                result.append(changed_by_id[item_id])
                seen_ids.add(item_id)

        deduped: list[Any] = []
        seen_semantics: set[tuple[str, str]] = set()
        for item in result:
            semantic = (
                str(item.get("source") or ""),
                str(item.get("message") or ""),
            )
            if semantic in seen_semantics:
                continue
            seen_semantics.add(semantic)
            deduped.append(item)
        return deduped

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
        self._ensure_records()
        if base is None:
            current = self.store.record_items(self.namespace)
            base = copy.deepcopy(current)

        changed_keys = {
            key
            for key in set(base) | set(payload)
            if base.get(key) != payload.get(key)
        }
        for key in sorted(changed_keys):
            base_items = base.get(key)
            desired_items = payload.get(key)
            if not isinstance(base_items, list):
                base_items = []
            if not isinstance(desired_items, list):
                desired_items = []

            def merge(latest: Any, *, _base=base_items, _desired=desired_items):
                latest_items = latest if isinstance(latest, list) else []
                merged = self._merge_queue(
                    _base,
                    _desired,
                    latest_items,
                )
                return merged or None

            self.store.record_update(
                self.namespace,
                key,
                merge,
                default=[],
            )
        self.flush_legacy_mirror()

