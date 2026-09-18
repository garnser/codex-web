from __future__ import annotations

from typing import Any, Callable

from codex_web.compatibility import ContractSpec, MigrationRegistry
from codex_web.extensions import ExtensionState
from codex_web.storage.sqlite_state import SQLiteStateStore


EXTENSION_STATE_CONTRACT = ContractSpec(
    "extension-state",
    "1.0",
    ("1.0",),
)
EXTENSION_STATE_MIGRATIONS = MigrationRegistry("extension-state")
EXTENSION_STATE_MIGRATIONS.register(
    "0.0",
    "1.0",
    lambda payload: {
        "schema_version": EXTENSION_STATE_CONTRACT.current,
        "installations": list(payload.get("installations", [])),
        "grants": list(payload.get("grants", [])),
        "events": list(payload.get("events", [])),
    },
)


class ExtensionStateStore:
    namespace = "extensions"

    def __init__(self, store: SQLiteStateStore) -> None:
        self.store = store

    def _decode(self, payload: Any) -> ExtensionState:
        if payload is None:
            return ExtensionState()
        if not isinstance(payload, dict):
            raise ValueError("extension state must be an object")
        version = str(payload.get("schema_version") or "0.0")
        if version != EXTENSION_STATE_CONTRACT.current:
            payload = EXTENSION_STATE_MIGRATIONS.migrate(
                payload,
                from_version=version,
                to_version=EXTENSION_STATE_CONTRACT.current,
            )
        EXTENSION_STATE_CONTRACT.require(payload.get("schema_version", ""))
        return ExtensionState.model_validate(payload)

    def load(self) -> ExtensionState:
        return self._decode(self.store.get(self.namespace))

    def update(
        self,
        updater: Callable[[ExtensionState], ExtensionState],
    ) -> ExtensionState:
        def apply(raw: Any) -> dict[str, Any]:
            current = self._decode(raw)
            return updater(current).model_dump(mode="json")

        payload = self.store.update(
            self.namespace,
            apply,
            default=ExtensionState().model_dump(mode="json"),
        )
        return self._decode(payload)
