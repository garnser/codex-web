from __future__ import annotations

from typing import Any, Callable

from codex_web.recovery import RecoveryState, RECOVERY_STATE_CONTRACT
from codex_web.compatibility import MigrationRegistry


RECOVERY_STATE_MIGRATIONS = MigrationRegistry("recovery-state")
RECOVERY_STATE_MIGRATIONS.register("1.0", "1.1", lambda payload: {**payload, "schema_version": "1.1"})
RECOVERY_STATE_MIGRATIONS.register("1.1", "1.2", lambda payload: {**payload, "schema_version": "1.2"})
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
        if version in ("1.0", "1.1"):
            raw = RECOVERY_STATE_MIGRATIONS.migrate(raw, from_version=version, to_version="1.2")
        RECOVERY_STATE_CONTRACT.require(raw.get("schema_version", version))
        return RecoveryState.model_validate(raw)

    @staticmethod
    def _encode(state: RecoveryState) -> dict:
        # Revalidate model_copy/update too; never emit new fields under 1.2.
        state = RecoveryState.model_validate(state.model_dump(mode="json"))
        raw = state.model_dump(mode="json")
        if state.schema_version == "1.2":
            raw.pop("reconciliations")
        return raw

    def load(self) -> RecoveryState:
        return self._decode(self.store.get(self.namespace))

    def update(
        self,
        updater: Callable[[RecoveryState], RecoveryState],
    ) -> RecoveryState:
        def apply(raw):
            current = self._decode(raw)
            previous_version = current.schema_version
            updated = updater(current)
            if previous_version == "1.2" and updated.schema_version != "1.2":
                raise ValueError("First1.3 write requires the proof-bound reconciliation transaction")
            return self._encode(updated)
        raw = self.store.update(
            self.namespace,
            apply,
            default=self._encode(RecoveryState()),
        )
        return self._decode(raw)
