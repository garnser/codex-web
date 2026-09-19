from __future__ import annotations

from typing import Any, Callable

from codex_web.business_data_sources import BusinessDataSourceState
from codex_web.storage.state_store import StateStore


class BusinessDataSourceStore:
    namespace = "business_data_sources"

    def __init__(self, store: StateStore) -> None:
        self.store = store

    @staticmethod
    def _decode(payload: Any) -> BusinessDataSourceState:
        if payload is None:
            return BusinessDataSourceState()
        return BusinessDataSourceState.model_validate(payload)

    def load(self) -> BusinessDataSourceState:
        return self._decode(self.store.get(self.namespace))

    def update(
        self,
        updater: Callable[[BusinessDataSourceState], BusinessDataSourceState],
    ) -> BusinessDataSourceState:
        raw = self.store.update(
            self.namespace,
            lambda current: updater(self._decode(current)).model_dump(mode="json"),
            default=BusinessDataSourceState().model_dump(mode="json"),
        )
        return self._decode(raw)
