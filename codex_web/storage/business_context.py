from __future__ import annotations

from typing import Any, Callable

from codex_web.business_context import (
    BUSINESS_CONTEXT_CONTRACT,
    BusinessContextState,
)
from codex_web.compatibility import MigrationRegistry
from codex_web.storage.state_store import StateStore


BUSINESS_CONTEXT_MIGRATIONS = MigrationRegistry("business-context-state")


class BusinessContextStore:
    namespace = "business_context"

    def __init__(self, store: StateStore) -> None:
        self.store = store

    def _decode(self, payload: Any) -> BusinessContextState:
        if payload is None:
            return BusinessContextState()
        if not isinstance(payload, dict):
            raise ValueError("business context state must be an object")
        version = str(
            payload.get("schema_version") or BUSINESS_CONTEXT_CONTRACT.current
        )
        if version != BUSINESS_CONTEXT_CONTRACT.current:
            payload = BUSINESS_CONTEXT_MIGRATIONS.migrate(
                payload,
                from_version=version,
                to_version=BUSINESS_CONTEXT_CONTRACT.current,
            )
        BUSINESS_CONTEXT_CONTRACT.require(payload.get("schema_version", ""))
        return BusinessContextState.model_validate(payload)

    def load(self) -> BusinessContextState:
        return self._decode(self.store.get(self.namespace))

    def update(
        self,
        updater: Callable[[BusinessContextState], BusinessContextState],
    ) -> BusinessContextState:
        def apply(raw: Any) -> dict[str, Any]:
            return updater(self._decode(raw)).model_dump(mode="json")

        payload = self.store.update(
            self.namespace,
            apply,
            default=BusinessContextState().model_dump(mode="json"),
        )
        return self._decode(payload)
