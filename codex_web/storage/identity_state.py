from __future__ import annotations

from typing import Any, Callable

from codex_web.compatibility import ContractSpec, MigrationRegistry
from codex_web.identity import IdentityState
from codex_web.storage.sqlite_state import SQLiteStateStore


IDENTITY_STATE_CONTRACT = ContractSpec("identity-state", "1.1", ("1.0", "1.1"))
IDENTITY_STATE_MIGRATIONS = MigrationRegistry("identity-state")
IDENTITY_STATE_MIGRATIONS.register(
    "0.0",
    "1.0",
    lambda payload: {
        "schema_version": "1.0",
        **{key: value for key, value in payload.items() if key != "schema_version"},
    },
)
IDENTITY_STATE_MIGRATIONS.register(
    "1.0",
    "1.1",
    lambda payload: {
        **payload,
        "schema_version": "1.1",
        "memberships": [
            {
                **dict(item),
                "updated_at": dict(item).get("updated_at"),
                "updated_by": dict(item).get("updated_by"),
                "revoked_by": dict(item).get("revoked_by"),
            }
            for item in payload.get("memberships", [])
        ],
    },
)


class IdentityStateStore:
    namespace = "identity_state"

    def __init__(self, store: SQLiteStateStore) -> None:
        self.store = store

    @staticmethod
    def _empty() -> dict[str, Any]:
        return IdentityState().model_dump(mode="json")

    def _decode(self, payload: Any) -> IdentityState:
        if payload is None:
            return IdentityState()
        if not isinstance(payload, dict):
            raise ValueError("identity state must be an object")
        version = str(payload.get("schema_version") or "0.0")
        if version != IDENTITY_STATE_CONTRACT.current:
            payload = IDENTITY_STATE_MIGRATIONS.migrate(
                payload,
                from_version=version,
                to_version=IDENTITY_STATE_CONTRACT.current,
            )
        IDENTITY_STATE_CONTRACT.require(payload.get("schema_version", ""))
        return IdentityState.model_validate(payload)

    def load(self) -> IdentityState:
        return self._decode(self.store.get(self.namespace))

    def save(self, state: IdentityState) -> IdentityState:
        payload = state.model_dump(mode="json")
        self.store.put(self.namespace, payload)
        return state.model_copy(deep=True)

    def update(self, updater: Callable[[IdentityState], IdentityState]) -> IdentityState:
        def apply(raw: Any) -> dict[str, Any]:
            current = self._decode(raw)
            updated = updater(current)
            return updated.model_dump(mode="json")

        payload = self.store.update(
            self.namespace,
            apply,
            default=self._empty(),
        )
        return self._decode(payload)
