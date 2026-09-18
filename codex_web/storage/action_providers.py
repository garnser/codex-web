from __future__ import annotations

from typing import Any, Callable

from codex_web.action_providers import ActionProviderState
from codex_web.compatibility import ContractSpec, MigrationRegistry
from codex_web.storage.sqlite_state import SQLiteStateStore


ACTION_PROVIDER_STATE_CONTRACT = ContractSpec("action-provider-state", "1.0", ("1.0",))
ACTION_PROVIDER_STATE_MIGRATIONS = MigrationRegistry("action-provider-state")
ACTION_PROVIDER_STATE_MIGRATIONS.register(
    "0.0",
    "1.0",
    lambda payload: {
        "schema_version": ACTION_PROVIDER_STATE_CONTRACT.current,
        **{key: value for key, value in payload.items() if key != "schema_version"},
    },
)


class ActionProviderStateStore:
    namespace = "action_provider_bindings"

    def __init__(self, store: SQLiteStateStore) -> None:
        self.store = store

    def _decode(self, payload: Any) -> ActionProviderState:
        if payload is None:
            return ActionProviderState()
        if not isinstance(payload, dict):
            raise ValueError("action provider state must be an object")
        version = str(payload.get("schema_version") or "0.0")
        if version != ACTION_PROVIDER_STATE_CONTRACT.current:
            payload = ACTION_PROVIDER_STATE_MIGRATIONS.migrate(
                payload,
                from_version=version,
                to_version=ACTION_PROVIDER_STATE_CONTRACT.current,
            )
        ACTION_PROVIDER_STATE_CONTRACT.require(payload.get("schema_version", ""))
        return ActionProviderState.model_validate(payload)

    def load(self) -> ActionProviderState:
        return self._decode(self.store.get(self.namespace))

    def update(self, updater: Callable[[ActionProviderState], ActionProviderState]) -> ActionProviderState:
        def apply(raw: Any) -> dict[str, Any]:
            state = self._decode(raw)
            return updater(state).model_dump(mode="json")

        payload = self.store.update(
            self.namespace,
            apply,
            default=ActionProviderState().model_dump(mode="json"),
        )
        return self._decode(payload)
