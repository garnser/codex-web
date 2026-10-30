from __future__ import annotations

from typing import Any, Callable
from copy import deepcopy
import inspect
import threading

from codex_web.compatibility import MigrationRegistry
from codex_web.definitions import DEFINITION_RECORD_CONTRACT, DefinitionRecord
from codex_web.storage.sqlite_state import SQLiteStateStore


DEFINITION_STORE_MIGRATIONS = MigrationRegistry("definition-store")
DEFINITION_STORE_MIGRATIONS.register(
    "0.0",
    "1.0",
    lambda payload: {
        "schema_version": DEFINITION_RECORD_CONTRACT.current,
        "records": list(payload.get("records", [])),
    },
)


class DefinitionRegistryStore:
    """SQLite-backed canonical store for immutable definition revisions."""

    namespace = "definition_registry"

    def __init__(self, store: SQLiteStateStore) -> None:
        self.store = store

    def _decode(
        self, payload: Any, *, matches: dict[str, str] | None = None
    ) -> list[DefinitionRecord]:
        if payload is None:
            return []
        if isinstance(payload, list):
            payload = {"records": payload}
        if not isinstance(payload, dict):
            raise ValueError("definition registry store must be an object")
        version = str(payload.get("schema_version") or "0.0")
        if version != DEFINITION_RECORD_CONTRACT.current:
            payload = DEFINITION_STORE_MIGRATIONS.migrate(
                payload,
                from_version=version,
                to_version=DEFINITION_RECORD_CONTRACT.current,
            )
        DEFINITION_RECORD_CONTRACT.require(payload.get("schema_version", ""))
        records = payload.get("records", [])
        if not isinstance(records, list):
            raise ValueError("definition registry records must be a list")
        if matches:
            records = [
                item for item in records
                if isinstance(item, dict)
                and all(item.get(key) == value for key, value in matches.items())
            ]
        return [DefinitionRecord.model_validate(item) for item in records]

    @staticmethod
    def _encode(records: list[DefinitionRecord]) -> dict[str, Any]:
        return {
            "schema_version": DEFINITION_RECORD_CONTRACT.current,
            "records": [record.model_dump(mode="json") for record in records],
        }

    def load(self) -> list[DefinitionRecord]:
        return self._decode(self.store.get(self.namespace))

    def load_matching(self, **matches: str) -> list[DefinitionRecord]:
        """Validate only requested revisions, retaining canonical storage reads."""
        return self._decode(self.store.get(self.namespace), matches=matches)

    def can_observe_reads(self) -> bool:
        return (type(self) is DefinitionRegistryStore
                and getattr(self.load, "__self__", None) is self
                and getattr(self.load, "__func__", None) is _CANONICAL_LOAD
                and getattr(self.load_matching, "__self__", None) is self
                and getattr(self.load_matching, "__func__", None) is _CANONICAL_LOAD_MATCHING
                and getattr(self._decode, "__self__", None) is self
                and getattr(self._decode, "__func__", None) is _CANONICAL_DECODE)

    def observe_reads(self, callback):
        """One explicit synchronous raw read; custom/legacy owners use no view."""
        if not self.can_observe_reads():
            return callback(None)
        raw = self.store.get(self.namespace)
        if raw is not None and (not isinstance(raw, dict)
                or raw.get("schema_version") != DEFINITION_RECORD_CONTRACT.current):
            return callback(None)
        observation = _DefinitionRawObservation(self, raw)
        try:
            result = callback(observation)
            if inspect.isawaitable(result) or inspect.isgenerator(result) or inspect.isasyncgen(result):
                if inspect.iscoroutine(result) or inspect.isgenerator(result):
                    result.close()
                raise ValueError("Definition read observation callback must be synchronous")
            return result
        finally:
            observation.close()

    def replace(self, records: list[DefinitionRecord]) -> list[DefinitionRecord]:
        self.store.put(self.namespace, self._encode(records))
        return [record.model_copy(deep=True) for record in records]

    def update(
        self,
        updater: Callable[[list[DefinitionRecord]], list[DefinitionRecord]],
    ) -> list[DefinitionRecord]:
        def apply(raw: Any) -> dict[str, Any]:
            records = self._decode(raw)
            return self._encode(updater(records))

        updated = self.store.update(
            self.namespace,
            apply,
            default={
                "schema_version": DEFINITION_RECORD_CONTRACT.current,
                "records": [],
            },
        )
        return self._decode(updated)


class _DefinitionRawObservation:
    def __init__(self, owner, raw):
        self._owner = owner
        self._raw = raw
        self._thread = threading.get_ident()
        self._active = True

    def require_owner(self, owner):
        if not self._active or owner is not self._owner or threading.get_ident() != self._thread:
            raise ValueError("Definition read observation is foreign, closed or on another thread")

    def load(self, owner, *, matches=None):
        self.require_owner(owner)
        if self._raw is None:
            return owner._decode(None, matches=matches)
        records = self._raw.get("records", [])
        if not isinstance(records, list):
            raise ValueError("definition registry records must be a list")
        selected = (item for item in records if not matches or isinstance(item, dict)
                    and all(item.get(key) == value for key, value in matches.items()))
        # Schema/Pydantic callbacks receive only fresh owned members. A bad
        # selected duplicate is still decoded; unrelated malformed rows retain
        # ordinary load_matching semantics instead of becoming a global scan.
        payload = {"schema_version": self._raw["schema_version"],
                   "records": [deepcopy(item) for item in selected]}
        return owner._decode(payload, matches=matches)

    def close(self):
        self._active = False
        self._raw = None


_CANONICAL_LOAD = DefinitionRegistryStore.load
_CANONICAL_LOAD_MATCHING = DefinitionRegistryStore.load_matching
_CANONICAL_DECODE = DefinitionRegistryStore._decode
