from __future__ import annotations

from typing import Any, Callable

from codex_web.compatibility import ContractSpec, MigrationRegistry
from codex_web.security import SecurityState
from codex_web.storage.sqlite_state import SQLiteStateStore


SECURITY_STATE_CONTRACT = ContractSpec("security-state", "1.0", ("1.0",))
SECURITY_STATE_MIGRATIONS = MigrationRegistry("security-state")
SECURITY_STATE_MIGRATIONS.register(
    "0.0",
    "1.0",
    lambda payload: {
        "schema_version": SECURITY_STATE_CONTRACT.current,
        **{key: value for key, value in payload.items() if key != "schema_version"},
    },
)


class SecurityEventStore:
    namespace = "security_events"

    def __init__(self, store: SQLiteStateStore) -> None:
        self.store = store

    def _decode(self, payload: Any) -> SecurityState:
        if payload is None:
            return SecurityState()
        if not isinstance(payload, dict):
            raise ValueError("security state must be an object")
        version = str(payload.get("schema_version") or "0.0")
        if version != SECURITY_STATE_CONTRACT.current:
            payload = SECURITY_STATE_MIGRATIONS.migrate(
                payload,
                from_version=version,
                to_version=SECURITY_STATE_CONTRACT.current,
            )
        SECURITY_STATE_CONTRACT.require(payload.get("schema_version", ""))
        return SecurityState.model_validate(payload)

    def load(self) -> SecurityState:
        return self._decode(self.store.get(self.namespace))

    def update(self, updater: Callable[[SecurityState], SecurityState]) -> SecurityState:
        def apply(raw: Any) -> dict[str, Any]:
            state = self._decode(raw)
            return updater(state).model_dump(mode="json")

        payload = self.store.update(
            self.namespace,
            apply,
            default=SecurityState().model_dump(mode="json"),
        )
        return self._decode(payload)
