from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Generic, TypeVar

from pydantic import BaseModel

from codex_web.models import BotBinding, BotConnection, BotReplyTarget, QueuedTurn
from codex_web.storage.json_files import atomic_write_text
from codex_web.storage.runtime_state import ModelMapRepository
from codex_web.storage.sqlite_state import SQLiteStateStore


T = TypeVar("T", bound=BaseModel)


class ModelListRepository(Generic[T]):
    """SQLite-primary list storage with rollback-safe JSON mirroring."""

    def __init__(
        self,
        store: SQLiteStateStore,
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

    def load(self) -> list[T]:
        return [self.model.model_validate(item) for item in self._raw()]

    def save(self, values: list[T]) -> None:
        payload = [value.model_dump() for value in values]
        self.store.put(self.namespace, payload)
        atomic_write_text(
            self.legacy_path,
            json.dumps(payload, indent=2) + "\n",
            private=self.private,
        )


class QueuedTurnRepository:
    """Persist per-thread turn queues as one transactional SQLite document."""

    def __init__(self, store: SQLiteStateStore, legacy_path: Path) -> None:
        self.store = store
        self.legacy_path = legacy_path
        self.namespace = "turn_queues"

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
        result: dict[str, list[QueuedTurn]] = {}
        for thread_id, values in self._raw().items():
            if not isinstance(thread_id, str) or not isinstance(values, list):
                continue
            result[thread_id] = [QueuedTurn.model_validate(item) for item in values]
        return result

    def save(self, queues: dict[str, list[QueuedTurn]]) -> None:
        payload = {
            thread_id: [queued.model_dump() for queued in values]
            for thread_id, values in sorted(queues.items())
        }
        self.store.put(self.namespace, payload)
        atomic_write_text(
            self.legacy_path,
            json.dumps(payload, indent=2) + "\n",
            private=True,
        )


class OperationalStateRepositories:
    def __init__(
        self,
        store: SQLiteStateStore,
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
    """Wire high-churn queue/bot state to the existing SQLite store."""

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
            app.state.sqlite_state_store,
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
