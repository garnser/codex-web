from __future__ import annotations

import time
from typing import Any, Callable

from codex_web.autonomy import (
    AUTONOMY_STATE_CONTRACT,
    AutonomyControl,
    AutonomyCycleRecord,
    AutonomyDeadLetter,
    AutonomyState,
)
from codex_web.compatibility import MigrationRegistry
from codex_web.storage.sqlite_state import SQLiteStateStore


AUTONOMY_STATE_MIGRATIONS = MigrationRegistry("autonomy-state")
AUTONOMY_STATE_MIGRATIONS.register(
    "0.0",
    "1.0",
    lambda payload: {
        "schema_version": AUTONOMY_STATE_CONTRACT.current,
        "control": payload.get("control") or {},
        "cycles": list(payload.get("cycles") or []),
        "dead_letters": list(payload.get("dead_letters") or []),
        "updated_at": float(payload.get("updated_at") or time.time()),
        "updated_by": str(payload.get("updated_by") or "migration"),
    },
)


class AutonomyStateStore:
    namespace = "autonomy"
    max_cycles = 5000
    max_dead_letters = 1000

    def __init__(self, store: SQLiteStateStore) -> None:
        self.store = store

    def _decode(self, payload: Any) -> AutonomyState:
        if payload is None:
            return AutonomyState()
        if not isinstance(payload, dict):
            raise ValueError("autonomy state must be an object")
        version = str(payload.get("schema_version") or "0.0")
        if version != AUTONOMY_STATE_CONTRACT.current:
            payload = AUTONOMY_STATE_MIGRATIONS.migrate(
                payload,
                from_version=version,
                to_version=AUTONOMY_STATE_CONTRACT.current,
            )
        AUTONOMY_STATE_CONTRACT.require(payload.get("schema_version", ""))
        return AutonomyState.model_validate(payload)

    def load(self) -> AutonomyState:
        return self._decode(self.store.get(self.namespace))

    def update(self, updater: Callable[[AutonomyState], AutonomyState]) -> AutonomyState:
        def apply(raw: Any) -> dict[str, Any]:
            state = updater(self._decode(raw))
            state.cycles = state.cycles[-self.max_cycles :]
            state.dead_letters = state.dead_letters[-self.max_dead_letters :]
            state.updated_at = time.time()
            return state.model_dump(mode="json")

        payload = self.store.update(
            self.namespace,
            apply,
            default=AutonomyState().model_dump(mode="json"),
        )
        return self._decode(payload)

    def set_control(self, control: AutonomyControl, *, actor_id: str) -> AutonomyState:
        return self.update(
            lambda state: state.model_copy(
                update={"control": control, "updated_by": actor_id}
            )
        )

    def append_cycle(self, cycle: AutonomyCycleRecord) -> AutonomyState:
        def apply(state: AutonomyState) -> AutonomyState:
            state.cycles.append(cycle)
            return state

        return self.update(apply)

    def append_dead_letter(self, item: AutonomyDeadLetter) -> AutonomyState:
        def apply(state: AutonomyState) -> AutonomyState:
            state.dead_letters.append(item)
            return state

        return self.update(apply)
