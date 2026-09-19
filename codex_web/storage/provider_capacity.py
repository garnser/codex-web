from __future__ import annotations

from typing import Any, Callable

from codex_web.provider_capacity import ProviderCapacityState
from codex_web.storage.sqlite_state import SQLiteStateStore


class ProviderCapacityStore:
    namespace = "provider_capacity"

    def __init__(self, store: SQLiteStateStore) -> None:
        self.store = store

    @staticmethod
    def _decode(payload: Any) -> ProviderCapacityState:
        if payload is None:
            return ProviderCapacityState()
        if not isinstance(payload, dict):
            raise ValueError("provider capacity state must be an object")
        return ProviderCapacityState.model_validate(payload)

    @staticmethod
    def _encode(state: ProviderCapacityState) -> dict[str, Any]:
        return state.model_dump(mode="json")

    def load(self) -> ProviderCapacityState:
        return self._decode(self.store.get(self.namespace))

    def update(
        self,
        updater: Callable[[ProviderCapacityState], ProviderCapacityState],
    ) -> ProviderCapacityState:
        def apply(raw: Any) -> dict[str, Any]:
            current = self._decode(raw)
            return self._encode(updater(current))

        payload = self.store.update(
            self.namespace,
            apply,
            default=ProviderCapacityState().model_dump(mode="json"),
        )
        return self._decode(payload)
