from __future__ import annotations

from typing import Any, Callable

from codex_web.recovery import RecoveryState
from codex_web.storage.state_store import StateStore


class RecoveryStore:
    namespace = "recovery"

    def __init__(self, store: StateStore) -> None:
        self.store = store

    @staticmethod
    def _decode(raw: Any) -> RecoveryState:
        if raw is None:
            return RecoveryState()
        return RecoveryState.model_validate(raw)

    def load(self) -> RecoveryState:
        return self._decode(self.store.get(self.namespace))

    def update(
        self,
        updater: Callable[[RecoveryState], RecoveryState],
    ) -> RecoveryState:
        raw = self.store.update(
            self.namespace,
            lambda current: updater(self._decode(current)).model_dump(mode="json"),
            default=RecoveryState().model_dump(mode="json"),
        )
        return self._decode(raw)
