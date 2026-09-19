from __future__ import annotations

from typing import Any, Callable

from codex_web.compatibility import MigrationRegistry
from codex_web.metrics import METRIC_CONTRACT, MetricState
from codex_web.storage.sqlite_state import SQLiteStateStore


METRIC_MIGRATIONS = MigrationRegistry("metric-state")


class MetricStore:
    namespace = "metrics"

    def __init__(self, store: SQLiteStateStore) -> None:
        self.store = store

    def _decode(self, payload: Any) -> MetricState:
        if payload is None:
            return MetricState()
        if not isinstance(payload, dict):
            raise ValueError("metric state must be an object")
        version = str(payload.get("schema_version") or METRIC_CONTRACT.current)
        if version != METRIC_CONTRACT.current:
            payload = METRIC_MIGRATIONS.migrate(
                payload,
                from_version=version,
                to_version=METRIC_CONTRACT.current,
            )
        METRIC_CONTRACT.require(payload.get("schema_version", ""))
        return MetricState.model_validate(payload)

    def load(self) -> MetricState:
        return self._decode(self.store.get(self.namespace))

    def update(self, updater: Callable[[MetricState], MetricState]) -> MetricState:
        def apply(raw: Any) -> dict[str, Any]:
            return updater(self._decode(raw)).model_dump(mode="json")

        payload = self.store.update(
            self.namespace,
            apply,
            default=MetricState().model_dump(mode="json"),
        )
        return self._decode(payload)
