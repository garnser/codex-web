from __future__ import annotations

from typing import Any, Callable

from codex_web.compatibility import MigrationRegistry
from codex_web.execution_workers import EXECUTION_WORKER_CONTRACT, ExecutionWorkerState
from codex_web.storage.sqlite_state import SQLiteStateStore


EXECUTION_WORKER_MIGRATIONS = MigrationRegistry("execution-worker-state")
EXECUTION_WORKER_MIGRATIONS.register(
    "0.0",
    "1.0",
    lambda payload: {
        "schema_version": "1.0",
        "workers": list(payload.get("workers", [])),
        "assignments": list(payload.get("assignments", [])),
        "events": list(payload.get("events", [])),
    },
)


def _migrate_1_0_to_1_1(payload: dict[str, Any]) -> dict[str, Any]:
    assignments = []
    for raw in payload.get("assignments", []):
        item = dict(raw)
        if item.get("subject") is None and item.get("work_item_ref"):
            item["subject"] = {
                "kind": "work_item",
                "ref": item["work_item_ref"],
            }
        assignments.append(item)
    return {
        **payload,
        "schema_version": "1.1",
        "assignments": assignments,
    }


EXECUTION_WORKER_MIGRATIONS.register("1.0", "1.1", _migrate_1_0_to_1_1)
EXECUTION_WORKER_MIGRATIONS.register(
    "1.1",
    "1.2",
    lambda payload: {
        **payload,
        "schema_version": "1.2",
    },
)


class ExecutionWorkerStore:
    namespace = "execution_workers"

    def __init__(self, store: SQLiteStateStore) -> None:
        self.store = store

    def _decode(self, payload: Any) -> ExecutionWorkerState:
        if payload is None:
            return ExecutionWorkerState()
        if not isinstance(payload, dict):
            raise ValueError("execution worker state must be an object")
        version = str(payload.get("schema_version") or "0.0")
        if version != EXECUTION_WORKER_CONTRACT.current:
            payload = EXECUTION_WORKER_MIGRATIONS.migrate(
                payload,
                from_version=version,
                to_version=EXECUTION_WORKER_CONTRACT.current,
            )
        EXECUTION_WORKER_CONTRACT.require(payload.get("schema_version", ""))
        return ExecutionWorkerState.model_validate(payload)

    def load(self) -> ExecutionWorkerState:
        return self._decode(self.store.get(self.namespace))

    def update(
        self,
        updater: Callable[[ExecutionWorkerState], ExecutionWorkerState],
    ) -> ExecutionWorkerState:
        def apply(raw: Any) -> dict[str, Any]:
            return updater(self._decode(raw)).model_dump(mode="json")

        payload = self.store.update(
            self.namespace,
            apply,
            default=ExecutionWorkerState().model_dump(mode="json"),
        )
        return self._decode(payload)
