from __future__ import annotations

from typing import Any, Callable

from codex_web.goal_execution_bindings import GoalExecutionBindingState
from codex_web.compatibility import ContractSpec, MigrationRegistry


GOAL_EXECUTION_BINDING_CONTRACT = ContractSpec("goal-execution-binding-state", "1.1", ("1.0", "1.1"))
GOAL_EXECUTION_BINDING_MIGRATIONS = MigrationRegistry("goal-execution-binding-state")
GOAL_EXECUTION_BINDING_MIGRATIONS.register("1.0", "1.1",
    lambda payload: {**payload, "schema_version": "1.1"})
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
        version = payload.get("schema_version", "1.0")
        GOAL_EXECUTION_BINDING_CONTRACT.require(version)
        if version != GOAL_EXECUTION_BINDING_CONTRACT.current:
            payload = GOAL_EXECUTION_BINDING_MIGRATIONS.migrate(payload,
                from_version=version, to_version=GOAL_EXECUTION_BINDING_CONTRACT.current)
        # Legacy records acquire no delegation grant. Preserve statuses/events.
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
