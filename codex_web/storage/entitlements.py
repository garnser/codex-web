from __future__ import annotations

from typing import Any, Callable

from codex_web.compatibility import MigrationRegistry
from codex_web.entitlements import ENTITLEMENT_CONTRACT, EntitlementState
from codex_web.storage.sqlite_state import SQLiteStateStore


ENTITLEMENT_MIGRATIONS = MigrationRegistry("entitlement-state")
ENTITLEMENT_MIGRATIONS.register(
    "0.0",
    "1.0",
    lambda payload: {
        "schema_version": ENTITLEMENT_CONTRACT.current,
        "settings": list(payload.get("settings", [])),
        "capabilities": list(payload.get("capabilities", [])),
        "quotas": list(payload.get("quotas", [])),
        "usage": list(payload.get("usage", [])),
    },
)


class EntitlementStore:
    namespace = "entitlements"

    def __init__(self, store: SQLiteStateStore) -> None:
        self.store = store

    def _decode(self, payload: Any) -> EntitlementState:
        if payload is None:
            return EntitlementState()
        if not isinstance(payload, dict):
            raise ValueError("entitlement state must be an object")
        version = str(payload.get("schema_version") or "0.0")
        if version != ENTITLEMENT_CONTRACT.current:
            payload = ENTITLEMENT_MIGRATIONS.migrate(
                payload,
                from_version=version,
                to_version=ENTITLEMENT_CONTRACT.current,
            )
        ENTITLEMENT_CONTRACT.require(payload.get("schema_version", ""))
        return EntitlementState.model_validate(payload)

    @staticmethod
    def _encode(state: EntitlementState) -> dict[str, Any]:
        return state.model_dump(mode="json")

    def load(self) -> EntitlementState:
        return self._decode(self.store.get(self.namespace))

    def update(
        self,
        updater: Callable[[EntitlementState], EntitlementState],
    ) -> EntitlementState:
        def apply(raw: Any) -> dict[str, Any]:
            current = self._decode(raw)
            return self._encode(updater(current))

        payload = self.store.update(
            self.namespace,
            apply,
            default=EntitlementState().model_dump(mode="json"),
        )
        return self._decode(payload)
