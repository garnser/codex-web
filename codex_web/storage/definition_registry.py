from __future__ import annotations

from typing import Any, Callable

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

    def _decode(self, payload: Any) -> list[DefinitionRecord]:
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
        return [DefinitionRecord.model_validate(item) for item in records]

    @staticmethod
    def _encode(records: list[DefinitionRecord]) -> dict[str, Any]:
        return {
            "schema_version": DEFINITION_RECORD_CONTRACT.current,
            "records": [record.model_dump(mode="json") for record in records],
        }

    def load(self) -> list[DefinitionRecord]:
        return self._decode(self.store.get(self.namespace))

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
