from __future__ import annotations

import copy
import json
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Callable, Generic, TypeVar

from pydantic import BaseModel

from codex_web.models import ActiveThreadTurn, ThreadRunSettings, WorkItemState
from codex_web.storage.json_files import atomic_write_text
from codex_web.storage.state_store import StateStore


T = TypeVar("T", bound=BaseModel)


class ModelMapRepository(Generic[T]):
    """Keyed StateStore repository with rollback-compatible JSON snapshots.

    Canonical mutations use per-record StateStore operations. The historical
    JSON file is a compatibility checkpoint: bulk save() keeps the old
    synchronous behavior, while put()/delete() deliberately avoid rewriting
    the whole mirror on the mutation hot path.
    """

    def __init__(
        self,
        store: StateStore,
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

    def _ensure_records(self) -> None:
        if self.store.record_collection_exists(self.namespace):
            return
        payload = self.store.get(self.namespace)
        if payload is None:
            payload = self._legacy_payload()
        if not isinstance(payload, dict):
            payload = {}
        self.store.record_replace(self.namespace, payload)

    def _raw(self) -> dict[str, Any]:
        self._ensure_records()
        return self.store.record_items(self.namespace)

    def _included(self, key: str) -> bool:
        return self.key_filter is None or self.key_filter(key)

    def load(self) -> dict[str, T]:
        raw = {
            key: value
            for key, value in self._raw().items()
            if isinstance(key, str) and self._included(key)
        }
        self._snapshot.set(copy.deepcopy(raw))
        return {
            key: self.model.model_validate(value)
            for key, value in raw.items()
        }

    def get(self, key: str) -> T | None:
        key = str(key)
        if not self._included(key):
            return None
        self._ensure_records()
        raw = self.store.record_get(self.namespace, key)
        return self.model.model_validate(raw) if raw is not None else None

    def put(self, key: str, value: T) -> T:
        key = str(key)
        if not self._included(key):
            raise ValueError(f"key is outside repository filter: {key}")
        self._ensure_records()
        self.store.record_apply(
            self.namespace,
            upserts={key: value.model_dump(mode="json")},
        )
        self._snapshot.set(None)
        return value

    def delete(self, key: str) -> bool:
        key = str(key)
        if not self._included(key):
            return False
        self._ensure_records()
        existed = self.store.record_get(self.namespace, key) is not None
        if existed:
            self.store.record_apply(
                self.namespace,
                upserts={},
                deletes=(key,),
            )
        self._snapshot.set(None)
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
            private=self.private,
        )

    def save(self, values: dict[str, T]) -> None:
        payload = {
            key: value.model_dump(mode="json")
            for key, value in sorted(values.items())
            if self._included(key)
        }
        base = self._snapshot.get()
        if base is None:
            current = self.store.record_items(self.namespace)
            upserts = {
                key: value
                for key, value in payload.items()
                if current.get(key) != value
            }
            deletes = tuple(
                key
                for key in current
                if self._included(key) and key not in payload
            )
        else:
            upserts = {
                key: value
                for key, value in payload.items()
                if base.get(key) != value
            }
            deletes = tuple(set(base) - set(payload))

        self._ensure_records()
        if upserts or deletes:
            self.store.record_apply(
                self.namespace,
                upserts=upserts,
                deletes=deletes,
            )
        self._snapshot.set(copy.deepcopy(payload))
        self.flush_legacy_mirror()


class RuntimeStateRepositories:
    def __init__(
        self,
        store: StateStore,
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
