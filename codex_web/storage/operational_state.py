from __future__ import annotations

import copy
import json
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Generic, TypeVar

from pydantic import BaseModel

from codex_web.models import BotBinding, BotConnection, BotReplyTarget, QueuedTurn
from codex_web.storage.json_files import atomic_write_text
from codex_web.storage.runtime_state import ModelMapRepository
from codex_web.storage.state_store import StateStore


T = TypeVar("T", bound=BaseModel)


class ModelListRepository(Generic[T]):
    """StateStore-primary list storage with rollback-safe JSON mirroring.

    Lists of models with stable `id` fields are delta-merged against the latest
    SQLite value so concurrent connection/binding updates do not overwrite
    unrelated objects.
    """

    def __init__(
        self,
        store: StateStore,
        *,
        namespace: str,
        legacy_path: Path,
        model: type[T],
        private: bool = True,
    ) -> None:
        self.store = store
        self.namespace = namespace
        self.legacy_path = legacy_path
        self.model = model
        self.private = private
        self._snapshot: ContextVar[list[Any] | None] = ContextVar(
            f"codex_web_{namespace}_snapshot",
            default=None,
        )

    def _legacy_payload(self) -> list[Any]:
        if not self.legacy_path.exists():
            return []
        try:
            payload = json.loads(self.legacy_path.read_text())
        except (OSError, json.JSONDecodeError):
            return []
        return payload if isinstance(payload, list) else []

    def _raw(self) -> list[Any]:
        payload = self.store.get(self.namespace)
        if payload is None:
            payload = self._legacy_payload()
            self.store.put(self.namespace, payload)
        return payload if isinstance(payload, list) else []

    @staticmethod
    def _index(values: list[Any]) -> dict[str, dict[str, Any]] | None:
        result: dict[str, dict[str, Any]] = {}
        for item in values:
            if not isinstance(item, dict):
                return None
            item_id = str(item.get("id") or "").strip()
            if not item_id or item_id in result:
                return None
            result[item_id] = item
        return result

    def load(self) -> list[T]:
        raw = self._raw()
        self._snapshot.set(copy.deepcopy(raw))
        return [self.model.model_validate(item) for item in raw]

    def save(self, values: list[T]) -> None:
        payload = [value.model_dump() for value in values]
        base = self._snapshot.get()
        base_index = self._index(base) if base is not None else None
        payload_index = self._index(payload)

        if base is None or base_index is None or payload_index is None:
            self.store.put(self.namespace, payload)
            merged = payload
        else:
            changed = {
                item_id: item
                for item_id, item in payload_index.items()
                if item_id not in base_index or base_index[item_id] != item
            }
            deleted = set(base_index) - set(payload_index)
            payload_order = [str(item["id"]) for item in payload]

            def merge(current: Any) -> list[Any]:
                latest = list(current) if isinstance(current, list) else []
                latest_index = self._index(latest)
                if latest_index is None:
                    return payload

                result: list[dict[str, Any]] = []
                seen: set[str] = set()
                for item in latest:
                    item_id = str(item["id"])
                    if item_id in deleted:
                        continue
                    result.append(changed.get(item_id, item))
                    seen.add(item_id)
                for item_id in payload_order:
                    if item_id in changed and item_id not in seen:
                        result.append(changed[item_id])
                        seen.add(item_id)
                return result

            merged = self.store.update(self.namespace, merge, default=[])

        self._snapshot.set(copy.deepcopy(merged))
        atomic_write_text(
            self.legacy_path,
            json.dumps(merged, indent=2) + "\n",
            private=self.private,
        )


class QueuedTurnRepository:
    """Persist per-thread turn queues as one transactional shared-store document."""

    def __init__(self, store: StateStore, legacy_path: Path) -> None:
        self.store = store
        self.legacy_path = legacy_path
        self.namespace = "turn_queues"
        self._snapshot: ContextVar[dict[str, Any] | None] = ContextVar(
            "codex_web_turn_queues_snapshot",
            default=None,
        )

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
        self._snapshot.set(copy.deepcopy(raw))
        result: dict[str, list[QueuedTurn]] = {}
        for thread_id, values in raw.items():
            if not isinstance(thread_id, str) or not isinstance(values, list):
                continue
            result[thread_id] = [QueuedTurn.model_validate(item) for item in values]
        return result

    def save(self, queues: dict[str, list[QueuedTurn]]) -> None:
        payload = {
            thread_id: [queued.model_dump() for queued in values]
            for thread_id, values in sorted(queues.items())
        }
        base = self._snapshot.get()
        if base is None:
            self.store.put(self.namespace, payload)
            merged = payload
        else:
            changed = {
                key: value
                for key, value in payload.items()
                if key not in base or base.get(key) != value
            }
            deleted = set(base) - set(payload)

            def merge_queue(
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
                    if item_id and item_id in changed_by_id and item_id not in seen_ids:
                        result.append(changed_by_id[item_id])
                        seen_ids.add(item_id)

                # Preserve the queue's existing duplicate contract across
                # replicas: one pending entry per source/message pair.
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

            def merge(current: Any) -> dict[str, Any]:
                latest = dict(current) if isinstance(current, dict) else {}
                for key in deleted:
                    latest.pop(key, None)
                for key, desired_items in changed.items():
                    base_items = base.get(key)
                    latest_items = latest.get(key)
                    latest[key] = merge_queue(
                        base_items if isinstance(base_items, list) else [],
                        desired_items if isinstance(desired_items, list) else [],
                        latest_items if isinstance(latest_items, list) else [],
                    )
                    if not latest[key]:
                        latest.pop(key, None)
                return latest

            merged = self.store.update(self.namespace, merge, default={})

        self._snapshot.set(copy.deepcopy(merged))
        atomic_write_text(
            self.legacy_path,
            json.dumps(merged, indent=2) + "\n",
            private=True,
        )


class OperationalStateRepositories:
    def __init__(
        self,
        store: StateStore,
        *,
        turn_queue_file: Path,
        bot_connections_file: Path,
        bot_bindings_file: Path,
        bot_reply_targets_file: Path,
        bot_delivery_targets_file: Path,
    ) -> None:
        self.turn_queues = QueuedTurnRepository(store, turn_queue_file)
        self.bot_connections = ModelListRepository(
            store,
            namespace="bot_connections",
            legacy_path=bot_connections_file,
            model=BotConnection,
            private=True,
        )
        self.bot_bindings = ModelListRepository(
            store,
            namespace="bot_bindings",
            legacy_path=bot_bindings_file,
            model=BotBinding,
            private=True,
        )
        self.bot_reply_targets = ModelMapRepository(
            store,
            namespace="bot_reply_targets",
            legacy_path=bot_reply_targets_file,
            model=BotReplyTarget,
            private=True,
        )
        self.bot_delivery_targets = ModelMapRepository(
            store,
            namespace="bot_delivery_targets",
            legacy_path=bot_delivery_targets_file,
            model=BotReplyTarget,
            private=True,
        )


def install_operational_state(app: Any, host: Any) -> OperationalStateRepositories:
    """Wire high-churn queue/bot state to the configured canonical StateStore."""

    from codex_web.paths import (
        BOT_DELIVERY_TARGETS_FILE,
        BOT_REPLY_TARGETS_FILE,
        BOTS_BINDINGS_FILE,
        BOTS_CONNECTIONS_FILE,
        TURN_QUEUE_FILE,
    )

    existing = getattr(app.state, "operational_state_repositories", None)
    if existing is not None:
        repositories = existing
    else:
        repositories = OperationalStateRepositories(
            getattr(app.state, "state_store", app.state.sqlite_state_store),
            turn_queue_file=TURN_QUEUE_FILE,
            bot_connections_file=BOTS_CONNECTIONS_FILE,
            bot_bindings_file=BOTS_BINDINGS_FILE,
            bot_reply_targets_file=BOT_REPLY_TARGETS_FILE,
            bot_delivery_targets_file=BOT_DELIVERY_TARGETS_FILE,
        )
        app.state.operational_state_repositories = repositories

    host._load_turn_queues = repositories.turn_queues.load
    host._save_turn_queues = repositories.turn_queues.save
    host._load_bot_connections = repositories.bot_connections.load
    host._save_bot_connections = repositories.bot_connections.save
    host._load_bot_bindings = repositories.bot_bindings.load
    host._save_bot_bindings = repositories.bot_bindings.save
    host._load_bot_reply_targets = repositories.bot_reply_targets.load
    host._save_bot_reply_targets = repositories.bot_reply_targets.save
    host._load_bot_delivery_targets = repositories.bot_delivery_targets.load
    host._save_bot_delivery_targets = repositories.bot_delivery_targets.save
    return repositories
