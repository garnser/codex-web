from __future__ import annotations

from typing import Any, Callable

from codex_web.compatibility import MigrationRegistry
from codex_web.goals import GOAL_CONTRACT, GoalState
from codex_web.storage.sqlite_state import SQLiteStateStore


GOAL_MIGRATIONS = MigrationRegistry("goal-state")
GOAL_MIGRATIONS.register(
    "0.0",
    "1.0",
    lambda payload: {
        "schema_version": "1.0",
        "goals": list(payload.get("goals", [])),
        "revisions": list(payload.get("revisions", [])),
        "events": list(payload.get("events", [])),
    },
)
GOAL_MIGRATIONS.register(
    "1.0",
    "1.1",
    lambda payload: {
        **payload,
        "schema_version": GOAL_CONTRACT.current,
        "goals": [
            {
                **item,
                "completion_evaluation_id": item.get("completion_evaluation_id"),
                "completed_at": item.get("completed_at"),
            }
            for item in payload.get("goals", [])
        ],
        "completion_evaluations": list(
            payload.get("completion_evaluations", [])
        ),
    },
)


GOAL_MIGRATIONS.register(
    "1.1",
    "1.2",
    lambda payload: {
        **payload,
        "schema_version": GOAL_CONTRACT.current,
        "goals": [
            {
                **item,
                "success_criteria": [
                    {
                        **criterion,
                        "metric_id": criterion.get("metric_id"),
                        "metric_snapshot_id": criterion.get("metric_snapshot_id"),
                        "metric_window_seconds": criterion.get("metric_window_seconds"),
                    }
                    for criterion in item.get("success_criteria", [])
                ],
            }
            for item in payload.get("goals", [])
        ],
    },
)


class GoalStore:
    namespace = "goals"

    def __init__(self, store: SQLiteStateStore) -> None:
        self.store = store

    def _decode(self, payload: Any) -> GoalState:
        if payload is None:
            return GoalState()
        if not isinstance(payload, dict):
            raise ValueError("goal state must be an object")
        version = str(payload.get("schema_version") or "0.0")
        if version != GOAL_CONTRACT.current:
            payload = GOAL_MIGRATIONS.migrate(
                payload,
                from_version=version,
                to_version=GOAL_CONTRACT.current,
            )
        GOAL_CONTRACT.require(payload.get("schema_version", ""))
        return GoalState.model_validate(payload)

    def load(self) -> GoalState:
        return self._decode(self.store.get(self.namespace))

    def update(self, updater: Callable[[GoalState], GoalState]) -> GoalState:
        def apply(raw: Any) -> dict[str, Any]:
            return updater(self._decode(raw)).model_dump(mode="json")

        payload = self.store.update(
            self.namespace,
            apply,
            default=GoalState().model_dump(mode="json"),
        )
        return self._decode(payload)
