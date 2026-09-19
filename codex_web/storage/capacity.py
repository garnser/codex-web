from __future__ import annotations

from typing import Any, Callable

from codex_web.capacity import CapacityState
from codex_web.storage.state_store import StateStore


class CapacityStore:
    namespace = "capacity"

    def __init__(self, store: StateStore) -> None:
        self.store = store

    @staticmethod
    def _decode(raw: Any) -> CapacityState:
        if raw is None:
            return CapacityState()
        return CapacityState.model_validate(raw)

    def load(self) -> CapacityState:
        return self._decode(self.store.get(self.namespace))

    def update(
        self,
        updater: Callable[[CapacityState], CapacityState],
    ) -> CapacityState:
        raw = self.store.update(
            self.namespace,
            lambda current: updater(self._decode(current)).model_dump(mode="json"),
            default=CapacityState().model_dump(mode="json"),
        )
        return self._decode(raw)
