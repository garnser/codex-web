from __future__ import annotations

import copy
import json
import threading
from pathlib import Path
from typing import Any, Generic, TypeVar

from pydantic import BaseModel

from codex_web.models import BotBinding, BotConnection, BotReplyTarget
from codex_web.storage.runtime_state import ModelMapRepository
from codex_web.storage.json_files import atomic_write_text
from codex_web.storage.state_store import StateStore


T = TypeVar("T", bound=BaseModel)


class ModelListRepository(Generic[T]):
    """StateStore-primary model list with ID-aware concurrent merge and JSON mirror."""

    def __init__(
        self,
        store: StateStore,
        *,
        namespace: str,
        legacy_path: Path,
        model: type[T],
    ) -> None:
        self.store = store
        self.namespace = namespace
        self.legacy_path = legacy_path
        self.model = model
        self._snapshots: dict[int, list[dict[str, Any]]] = {}
        self._lock = threading.RLock()

    def _legacy_payload(self) -> list[dict[str, Any]]:
        if not self.legacy_path.exists():
            return []
        try:
            payload = json.loads(self.legacy_path.read_text())
        except (OSError, json.JSONDecodeError):
            return []
        return payload if isinstance(payload, list) else []

    def _raw(self) -> list[dict[str, Any]]:
        payload = self.store.get(self.namespace)
        if payload is None:
            payload = self._legacy_payload()
            self.store.put(self.namespace, payload)
        return payload if isinstance(payload, list) else []

    def load(self) -> list[T]:
        raw = self._raw()
        result = [
            self.model.model_validate(item)
            for item in raw
            if isinstance(item, dict)
        ]
        with self._lock:
            self._snapshots[id(result)] = copy.deepcopy(raw)
        return result

    @staticmethod
    def _by_id(values: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for value in values:
            item_id = value.get("id") if isinstance(value, dict) else None
            if isinstance(item_id, str) and item_id:
                result[item_id] = value
        return result

    def save(self, values: list[T]) -> None:
        payload = [item.model_dump(mode="json") for item in values]
        with self._lock:
            base = self._snapshots.pop(id(values), None)
        if base is None:
            self.store.put(self.namespace, payload)
            merged = payload
        else:
            base_by_id = self._by_id(base)
            payload_by_id = self._by_id(payload)
            changed = {
                key: value
                for key, value in payload_by_id.items()
                if base_by_id.get(key) != value
            }
            deleted = set(base_by_id) - set(payload_by_id)

            def merge(current: Any) -> list[dict[str, Any]]:
                latest = (
                    [item for item in current if isinstance(item, dict)]
                    if isinstance(current, list)
                    else []
                )
                latest_by_id = self._by_id(latest)
                for key in deleted:
                    latest_by_id.pop(key, None)
                latest_by_id.update(changed)

                requested_order = [
                    item["id"]
                    for item in payload
                    if isinstance(item.get("id"), str)
                ]
                ordered: list[dict[str, Any]] = []
                used: set[str] = set()
                for key in requested_order:
                    value = latest_by_id.get(key)
                    if value is not None:
                        ordered.append(value)
                        used.add(key)
                for item in latest:
                    key = item.get("id")
                    if (
                        isinstance(key, str)
                        and key not in used
                        and key in latest_by_id
                    ):
                        ordered.append(latest_by_id[key])
                        used.add(key)
                for key, value in latest_by_id.items():
                    if key not in used:
                        ordered.append(value)
                return ordered

            merged = self.store.update(
                self.namespace,
                merge,
                default=[],
            )

        atomic_write_text(
            self.legacy_path,
            json.dumps(merged, indent=2) + "\n",
            private=True,
        )


class BotStateRepositories:
    def __init__(
        self,
        store: StateStore,
        *,
        connections_file: Path,
        bindings_file: Path,
        reply_targets_file: Path | None = None,
        delivery_targets_file: Path | None = None,
    ) -> None:
        self.connections = ModelListRepository(
            store,
            namespace="bot_connections",
            legacy_path=connections_file,
            model=BotConnection,
        )
        self.bindings = ModelListRepository(
            store,
            namespace="bot_bindings",
            legacy_path=bindings_file,
            model=BotBinding,
        )
        self.reply_targets = (
            ModelMapRepository(
                store,
                namespace="bot_reply_targets",
                legacy_path=reply_targets_file,
                model=BotReplyTarget,
            )
            if reply_targets_file is not None
            else None
        )
        self.delivery_targets = (
            ModelMapRepository(
                store,
                namespace="bot_delivery_targets",
                legacy_path=delivery_targets_file,
                model=BotReplyTarget,
            )
            if delivery_targets_file is not None
            else None
        )
