from __future__ import annotations

from typing import Any, Callable

from codex_web.recovery import RecoveryState, RECOVERY_STATE_CONTRACT
from codex_web.compatibility import MigrationRegistry


RECOVERY_STATE_MIGRATIONS = MigrationRegistry("recovery-state")
RECOVERY_STATE_MIGRATIONS.register("1.0", "1.1", lambda payload: {**payload, "schema_version": "1.1"})
from codex_web.storage.state_store import StateStore


class RecoveryStore:
    namespace = "recovery"

    def __init__(self, store: StateStore) -> None:
        self.store = store

    @staticmethod
    def _decode(raw: Any) -> RecoveryState:
        if raw is None:
            return RecoveryState()
        if not isinstance(raw, dict):
            raise ValueError("recovery state must be an object")
        version = str(raw.get("schema_version") or "1.0")
        if version != RECOVERY_STATE_CONTRACT.current:
            raw = RECOVERY_STATE_MIGRATIONS.migrate(raw, from_version=version, to_version=RECOVERY_STATE_CONTRACT.current)
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
