from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Generic, TypeVar

from pydantic import BaseModel

from codex_web.models import ActiveThreadTurn, ThreadRunSettings, WorkItemState
from codex_web.storage.json_files import atomic_write_text
from codex_web.storage.sqlite_state import SQLiteStateStore


T = TypeVar("T", bound=BaseModel)


class ModelMapRepository(Generic[T]):
    """SQLite-primary map storage with rollback-safe legacy JSON mirroring."""

    def __init__(
        self,
        store: SQLiteStateStore,
        *,
        namespace: str,
        legacy_path: Path,
        model: type[T],
        private: bool = True,
        key_filter: Callable[[Any], bool] | None = None,
    ) -> None:
        self.store = store
        self.namespace = namespace
        self.legacy_path = legacy_path
        self.model = model
        self.private = private
        self.key_filter = key_filter

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

    def load(self) -> dict[str, T]:
        result: dict[str, T] = {}
        for key, value in self._raw().items():
            if not isinstance(key, str):
                continue
            if self.key_filter is not None and not self.key_filter(key):
                continue
            result[key] = self.model.model_validate(value)
        return result

    def save(self, values: dict[str, T]) -> None:
        payload = {
            key: value.model_dump()
            for key, value in sorted(values.items())
            if self.key_filter is None or self.key_filter(key)
        }
        # SQLite is the source of truth. Keep the old file synchronized during
        # the migration window so downgrading to the previous release is safe.
        self.store.put(self.namespace, payload)
        atomic_write_text(
            self.legacy_path,
            json.dumps(payload, indent=2) + "\n",
            private=self.private,
        )


class RuntimeStateRepositories:
    def __init__(
        self,
        store: SQLiteStateStore,
        *,
        thread_settings_file: Path,
        active_turns_file: Path,
        work_item_states_file: Path,
    ) -> None:
        self.thread_settings = ModelMapRepository(
            store,
            namespace="thread_settings",
            legacy_path=thread_settings_file,
            model=ThreadRunSettings,
        )
        self.active_turns = ModelMapRepository(
            store,
            namespace="active_turns",
            legacy_path=active_turns_file,
            model=ActiveThreadTurn,
        )
        self.work_item_states = ModelMapRepository(
            store,
            namespace="work_item_states",
            legacy_path=work_item_states_file,
            model=WorkItemState,
        )
