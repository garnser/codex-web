from __future__ import annotations

from typing import Any, Callable

from codex_web.goal_execution_bindings import GoalExecutionBindingState
from codex_web.storage.sqlite_state import SQLiteStateStore


class GoalExecutionBindingStore:
    namespace = "goal_execution_bindings"

    def __init__(self, store: SQLiteStateStore) -> None:
        self.store = store

    @staticmethod
    def _decode(payload: Any) -> GoalExecutionBindingState:
        if payload is None:
            return GoalExecutionBindingState()
        if not isinstance(payload, dict):
            raise ValueError("goal execution binding state must be an object")
        return GoalExecutionBindingState.model_validate(payload)

    def load(self) -> GoalExecutionBindingState:
        return self._decode(self.store.get(self.namespace))

    def update(
        self,
        updater: Callable[[GoalExecutionBindingState], GoalExecutionBindingState],
    ) -> GoalExecutionBindingState:
        def apply(raw: Any) -> dict[str, Any]:
            return updater(self._decode(raw)).model_dump(mode="json")

        payload = self.store.update(
            self.namespace,
            apply,
            default=GoalExecutionBindingState().model_dump(mode="json"),
        )
        return self._decode(payload)
