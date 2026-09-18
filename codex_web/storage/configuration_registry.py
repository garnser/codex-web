from __future__ import annotations

from typing import Any, Callable

from codex_web.compatibility import MigrationRegistry
from codex_web.configuration import CONFIGURATION_CONTRACT, ConfigurationRecord
from codex_web.storage.sqlite_state import SQLiteStateStore


CONFIGURATION_MIGRATIONS = MigrationRegistry("configuration-record")
CONFIGURATION_MIGRATIONS.register(
    "0.0",
    "1.0",
    lambda payload: {
        "schema_version": CONFIGURATION_CONTRACT.current,
        "records": list(payload.get("records", [])),
    },
)


class ConfigurationRegistryStore:
    """SQLite-backed immutable-revision configuration record store."""

    namespace = "canonical_configuration_registry"

    def __init__(self, store: SQLiteStateStore) -> None:
        self.store = store

    def _decode(self, payload: Any) -> list[ConfigurationRecord]:
        if payload is None:
            return []
        if isinstance(payload, list):
            payload = {"records": payload}
        if not isinstance(payload, dict):
            raise ValueError("canonical configuration store must be an object")
        version = str(payload.get("schema_version") or "0.0")
        if version != CONFIGURATION_CONTRACT.current:
            payload = CONFIGURATION_MIGRATIONS.migrate(
                payload,
                from_version=version,
                to_version=CONFIGURATION_CONTRACT.current,
            )
        CONFIGURATION_CONTRACT.require(payload.get("schema_version", ""))
        values = payload.get("records", [])
        if not isinstance(values, list):
            raise ValueError("canonical configuration records must be a list")
        return [ConfigurationRecord.model_validate(item) for item in values]

    @staticmethod
    def _encode(records: list[ConfigurationRecord]) -> dict[str, Any]:
        return {
            "schema_version": CONFIGURATION_CONTRACT.current,
            "records": [record.model_dump(mode="json") for record in records],
        }

    def load(self) -> list[ConfigurationRecord]:
        return self._decode(self.store.get(self.namespace))

    def replace(self, records: list[ConfigurationRecord]) -> list[ConfigurationRecord]:
        payload = self._encode(records)
        self.store.put(self.namespace, payload)
        return [record.model_copy(deep=True) for record in records]

    def update(
        self,
        updater: Callable[[list[ConfigurationRecord]], list[ConfigurationRecord]],
    ) -> list[ConfigurationRecord]:
        def apply(raw: Any) -> dict[str, Any]:
            records = self._decode(raw)
            updated = updater(records)
            return self._encode(updated)

        payload = self.store.update(
            self.namespace,
            apply,
            default={
                "schema_version": CONFIGURATION_CONTRACT.current,
                "records": [],
            },
        )
        return self._decode(payload)
