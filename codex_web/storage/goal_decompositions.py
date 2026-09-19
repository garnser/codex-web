from __future__ import annotations

from typing import Any, Callable

from codex_web.goal_decomposition import (
    GOAL_DECOMPOSITION_CONTRACT,
    GoalDecompositionState,
)
from codex_web.storage.sqlite_state import SQLiteStateStore


class GoalDecompositionStore:
    namespace = "goal-decompositions"

    def __init__(self, store: SQLiteStateStore) -> None:
        self.store = store

    def _decode(self, payload: Any) -> GoalDecompositionState:
        if payload is None:
            return GoalDecompositionState()
        if not isinstance(payload, dict):
            raise ValueError("goal decomposition state must be an object")
        GOAL_DECOMPOSITION_CONTRACT.require(
            payload.get("schema_version", "")
        )
        state = GoalDecompositionState.model_validate(payload)
        if state.schema_version != GOAL_DECOMPOSITION_CONTRACT.current:
            state = state.model_copy(
                update={"schema_version": GOAL_DECOMPOSITION_CONTRACT.current}
            )
        return state

    def load(self) -> GoalDecompositionState:
        return self._decode(self.store.get(self.namespace))

    def update(
        self,
        updater: Callable[[GoalDecompositionState], GoalDecompositionState],
    ) -> GoalDecompositionState:
        def apply(raw: Any) -> dict[str, Any]:
            return updater(self._decode(raw)).model_dump(mode="json")

        payload = self.store.update(
            self.namespace,
            apply,
            default=GoalDecompositionState().model_dump(mode="json"),
        )
        return self._decode(payload)
