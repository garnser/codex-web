from __future__ import annotations

import copy
import json
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Callable, Generic, TypeVar

from pydantic import BaseModel

from codex_web.models import ActiveThreadTurn, ThreadRunSettings, WorkItemState
from codex_web.storage.json_files import atomic_write_text
from codex_web.storage.sqlite_state import SQLiteStateStore


T = TypeVar("T", bound=BaseModel)


class ModelMapRepository(Generic[T]):
    """SQLite-primary map storage with rollback-safe legacy JSON mirroring.

    A load/save pair keeps a context-local snapshot. save() computes only the
    keys changed by that caller and atomically merges the delta into the latest
    SQLite document, preventing unrelated concurrent updates from being lost.
    """

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
        self._snapshot: ContextVar[dict[str, Any] | None] = ContextVar(
            f"codex_web_{namespace}_snapshot",
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

    def load(self) -> dict[str, T]:
        raw = self._raw()
        self._snapshot.set(copy.deepcopy(raw))
        result: dict[str, T] = {}
        for key, value in raw.items():
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

            def merge(current: Any) -> dict[str, Any]:
                latest = dict(current) if isinstance(current, dict) else {}
                for key in deleted:
                    latest.pop(key, None)
                latest.update(changed)
                return latest

            merged = self.store.update(self.namespace, merge, default={})

        self._snapshot.set(copy.deepcopy(merged))
        atomic_write_text(
            self.legacy_path,
            json.dumps(merged, indent=2) + "\n",
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
