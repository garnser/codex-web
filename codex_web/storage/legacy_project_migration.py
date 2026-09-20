from __future__ import annotations

from typing import Any, Callable

from codex_web.legacy_project_migration import (
    LEGACY_PROJECT_MIGRATION_CONTRACT,
    LegacyProjectMigrationState,
)
from codex_web.storage.sqlite_state import SQLiteStateStore


class LegacyProjectMigrationStore:
    namespace = "legacy_project_migration"

    def __init__(self, store: SQLiteStateStore) -> None:
        self.store = store

    @staticmethod
    def _decode(payload: Any) -> LegacyProjectMigrationState:
        if payload is None:
            return LegacyProjectMigrationState()
        if not isinstance(payload, dict):
            raise ValueError("legacy migration state must be an object")
        LEGACY_PROJECT_MIGRATION_CONTRACT.require(
            str(payload.get("schema_version") or "")
        )
        return LegacyProjectMigrationState.model_validate(payload)

    def load(self) -> LegacyProjectMigrationState:
        return self._decode(self.store.get(self.namespace))

    def update(
        self,
        updater: Callable[
            [LegacyProjectMigrationState],
            LegacyProjectMigrationState,
        ],
    ) -> LegacyProjectMigrationState:
        payload = self.store.update(
            self.namespace,
            lambda raw: updater(self._decode(raw)).model_dump(mode="json"),
            default=LegacyProjectMigrationState().model_dump(mode="json"),
        )
        return self._decode(payload)
