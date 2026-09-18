from __future__ import annotations

from typing import Any, Callable

from codex_web.action_intents import ActionIntentState
from codex_web.compatibility import ContractSpec, MigrationRegistry
from codex_web.storage.sqlite_state import SQLiteStateStore


ACTION_INTENT_STATE_CONTRACT = ContractSpec("action-intent-state", "1.0", ("1.0",))
ACTION_INTENT_STATE_MIGRATIONS = MigrationRegistry("action-intent-state")
ACTION_INTENT_STATE_MIGRATIONS.register(
    "0.0",
    "1.0",
    lambda payload: {
        "schema_version": ACTION_INTENT_STATE_CONTRACT.current,
        **{key: value for key, value in payload.items() if key != "schema_version"},
    },
)


class ActionIntentStore:
    namespace = "action_intents"

    def __init__(self, store: SQLiteStateStore) -> None:
        self.store = store

    def _decode(self, payload: Any) -> ActionIntentState:
        if payload is None:
            return ActionIntentState()
        if not isinstance(payload, dict):
            raise ValueError("action intent state must be an object")
        version = str(payload.get("schema_version") or "0.0")
        if version != ACTION_INTENT_STATE_CONTRACT.current:
            payload = ACTION_INTENT_STATE_MIGRATIONS.migrate(
                payload,
                from_version=version,
                to_version=ACTION_INTENT_STATE_CONTRACT.current,
            )
        ACTION_INTENT_STATE_CONTRACT.require(payload.get("schema_version", ""))
        return ActionIntentState.model_validate(payload)

    def load(self) -> ActionIntentState:
        return self._decode(self.store.get(self.namespace))

    def update(self, updater: Callable[[ActionIntentState], ActionIntentState]) -> ActionIntentState:
        def apply(raw: Any) -> dict[str, Any]:
            state = self._decode(raw)
            return updater(state).model_dump(mode="json")

        payload = self.store.update(
            self.namespace,
            apply,
            default=ActionIntentState().model_dump(mode="json"),
        )
        return self._decode(payload)
