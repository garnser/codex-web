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


class IndexedBotBindingRepository:
    """Revision-aware read indexes over canonical BotBinding list state.

    Mutating callers keep the existing load()/save() snapshot semantics. Routing
    callers use indexed methods that rebuild only when the canonical namespace
    revision changes, including writes made by another process.
    """

    def __init__(self, repository: ModelListRepository[BotBinding]) -> None:
        self.repository = repository
        self._lock = threading.RLock()
        self._revision: float | None = None
        self._loaded = False
        self._all: tuple[BotBinding, ...] = ()
        self._by_id: dict[str, BotBinding] = {}
        self._by_thread: dict[str, tuple[BotBinding, ...]] = {}
        self._by_project_provider: dict[
            tuple[str, str],
            tuple[BotBinding, ...],
        ] = {}
        self._by_project: dict[str, tuple[BotBinding, ...]] = {}
        self._by_connection: dict[
            tuple[str, str],
            tuple[BotBinding, ...],
        ] = {}
        self._masters_by_project: dict[str, tuple[BotBinding, ...]] = {}

    @staticmethod
    def _copies(values: tuple[BotBinding, ...]) -> list[BotBinding]:
        return [value.model_copy(deep=True) for value in values]

    def _canonical_revision(self) -> float | None:
        return self.repository.store.namespace_revision(
            self.repository.namespace
        )

    def _build(self) -> None:
        values: list[BotBinding] = []
        stable_revision: float | None = None
        for _attempt in range(3):
            before = self._canonical_revision()
            raw = self.repository._raw()
            values = [
                BotBinding.model_validate(item)
                for item in raw
                if isinstance(item, dict)
            ]
            after = self._canonical_revision()
            if before == after:
                stable_revision = after
                break

        by_thread: dict[str, list[BotBinding]] = {}
        by_project_provider: dict[
            tuple[str, str],
            list[BotBinding],
        ] = {}
        by_project: dict[str, list[BotBinding]] = {}
        by_connection: dict[
            tuple[str, str],
            list[BotBinding],
        ] = {}
        masters: dict[str, list[BotBinding]] = {}
        for binding in values:
            provider = binding.provider.lower()
            by_thread.setdefault(binding.thread_id, []).append(binding)
            by_project_provider.setdefault(
                (provider, binding.project_id),
                [],
            ).append(binding)
            by_project.setdefault(
                binding.project_id,
                [],
            ).append(binding)
            by_connection.setdefault(
                (provider, binding.external_conversation_id),
                [],
            ).append(binding)
            if binding.is_master:
                masters.setdefault(binding.project_id, []).append(binding)

        self._all = tuple(values)
        self._by_id = {binding.id: binding for binding in values}
        self._by_thread = {
            key: tuple(items) for key, items in by_thread.items()
        }
        self._by_project_provider = {
            key: tuple(items)
            for key, items in by_project_provider.items()
        }
        self._by_project = {
            key: tuple(items)
            for key, items in by_project.items()
        }
        self._by_connection = {
            key: tuple(items) for key, items in by_connection.items()
        }
        self._masters_by_project = {
            key: tuple(items) for key, items in masters.items()
        }
        # If state changed continuously during the rebuild, deliberately leave
        # the revision unset so the next lookup rebuilds rather than treating a
        # potentially stale snapshot as current.
        self._revision = stable_revision
        self._loaded = True

    def _ensure(self) -> None:
        revision = self._canonical_revision()
        with self._lock:
            if (
                self._loaded
                and self._revision is not None
                and revision == self._revision
            ):
                return
            self._build()

    def invalidate(self) -> None:
        with self._lock:
            self._revision = None
            self._loaded = False

    def load(self) -> list[BotBinding]:
        return self.repository.load()

    def save(self, values: list[BotBinding]) -> None:
        with self._lock:
            self.repository.save(values)
            self._revision = None
            self._loaded = False

    def all(self) -> list[BotBinding]:
        self._ensure()
        with self._lock:
            return self._copies(self._all)

    def by_id(self, binding_id: str) -> BotBinding | None:
        self._ensure()
        with self._lock:
            binding = self._by_id.get(str(binding_id))
            return binding.model_copy(deep=True) if binding else None

    def for_thread(self, thread_id: str) -> list[BotBinding]:
        self._ensure()
        with self._lock:
            return self._copies(
                self._by_thread.get(str(thread_id), ())
            )

    def for_project(
        self,
        provider: str,
        project_id: str,
    ) -> list[BotBinding]:
        self._ensure()
        key = (provider.lower(), str(project_id))
        with self._lock:
            return self._copies(
                self._by_project_provider.get(key, ())
            )

    def for_project_all(
        self,
        project_id: str,
    ) -> list[BotBinding]:
        self._ensure()
        with self._lock:
            return self._copies(
                self._by_project.get(str(project_id), ())
            )

    def for_connection(
        self,
        provider: str,
        external_conversation_id: str,
    ) -> list[BotBinding]:
        self._ensure()
        key = (
            provider.lower(),
            str(external_conversation_id),
        )
        with self._lock:
            return self._copies(self._by_connection.get(key, ()))

    def masters(self, project_id: str) -> list[BotBinding]:
        self._ensure()
        with self._lock:
            return self._copies(
                self._masters_by_project.get(str(project_id), ())
            )

    def index_status(self) -> dict[str, Any]:
        self._ensure()
        with self._lock:
            return {
                "revision": self._revision,
                "bindings": len(self._all),
                "byId": len(self._by_id),
                "threads": len(self._by_thread),
                "projects": len(self._by_project),
                "projectProviders": len(self._by_project_provider),
                "connections": len(self._by_connection),
                "masterProjects": len(self._masters_by_project),
            }


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
