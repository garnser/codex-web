from __future__ import annotations

from typing import Any, Callable

from codex_web.compatibility import MigrationRegistry
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.work_graph import WORK_GRAPH_CONTRACT, WorkGraphState


WORK_GRAPH_MIGRATIONS = MigrationRegistry("work-graph-state")
WORK_GRAPH_MIGRATIONS.register(
    "0.0",
    "1.0",
    lambda payload: {
        "schema_version": WORK_GRAPH_CONTRACT.current,
        "edges": list(payload.get("edges", [])),
        "events": list(payload.get("events", [])),
    },
)


class WorkGraphStore:
    namespace = "work_graph"

    def __init__(self, store: SQLiteStateStore) -> None:
        self.store = store

    def _decode(self, payload: Any) -> WorkGraphState:
        if payload is None:
            return WorkGraphState()
        if not isinstance(payload, dict):
            raise ValueError("work graph state must be an object")
        version = str(payload.get("schema_version") or "0.0")
        if version != WORK_GRAPH_CONTRACT.current:
            payload = WORK_GRAPH_MIGRATIONS.migrate(
                payload,
                from_version=version,
                to_version=WORK_GRAPH_CONTRACT.current,
            )
        WORK_GRAPH_CONTRACT.require(payload.get("schema_version", ""))
        return WorkGraphState.model_validate(payload)

    def load(self) -> WorkGraphState:
        return self._decode(self.store.get(self.namespace))

    def update(
        self,
        updater: Callable[[WorkGraphState], WorkGraphState],
    ) -> WorkGraphState:
        def apply(raw: Any) -> dict[str, Any]:
            return updater(self._decode(raw)).model_dump(mode="json")

        payload = self.store.update(
            self.namespace,
            apply,
            default=WorkGraphState().model_dump(mode="json"),
        )
        return self._decode(payload)
