from __future__ import annotations

from typing import Any, Callable

from codex_web.business_kpis import (
    BUSINESS_KPI_CONTRACT,
    BusinessKPIState,
)
from codex_web.compatibility import MigrationRegistry
from codex_web.storage.state_store import StateStore


BUSINESS_KPI_MIGRATIONS = MigrationRegistry("business-kpi-state")


class BusinessKPIStore:
    namespace = "business_kpis"

    def __init__(self, store: StateStore) -> None:
        self.store = store

    def _decode(self, payload: Any) -> BusinessKPIState:
        if payload is None:
            return BusinessKPIState()
        if not isinstance(payload, dict):
            raise ValueError("business KPI state must be an object")
        version = str(
            payload.get("schema_version") or BUSINESS_KPI_CONTRACT.current
        )
        if version != BUSINESS_KPI_CONTRACT.current:
            payload = BUSINESS_KPI_MIGRATIONS.migrate(
                payload,
                from_version=version,
                to_version=BUSINESS_KPI_CONTRACT.current,
            )
        BUSINESS_KPI_CONTRACT.require(payload.get("schema_version", ""))
        return BusinessKPIState.model_validate(payload)

    def load(self) -> BusinessKPIState:
        return self._decode(self.store.get(self.namespace))

    def update(
        self,
        updater: Callable[[BusinessKPIState], BusinessKPIState],
    ) -> BusinessKPIState:
        raw = self.store.update(
            self.namespace,
            lambda current: updater(self._decode(current)).model_dump(mode="json"),
            default=BusinessKPIState().model_dump(mode="json"),
        )
        return self._decode(raw)
