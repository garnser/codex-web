from __future__ import annotations

from typing import Any, Callable

from codex_web.business_kpis import (
    BUSINESS_KPI_CONTRACT,
    BusinessKpiState,
)
from codex_web.compatibility import MigrationRegistry
from codex_web.storage.state_store import StateStore


BUSINESS_KPI_MIGRATIONS = MigrationRegistry("business-kpi-state")


class BusinessKpiStore:
    namespace = "business_kpis"

    def __init__(self, store: StateStore) -> None:
        self.store = store

    def _decode(self, payload: Any) -> BusinessKpiState:
        if payload is None:
            return BusinessKpiState()
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
        return BusinessKpiState.model_validate(payload)

    def load(self) -> BusinessKpiState:
        return self._decode(self.store.get(self.namespace))

    def update(
        self,
        updater: Callable[[BusinessKpiState], BusinessKpiState],
    ) -> BusinessKpiState:
        payload = self.store.update(
            self.namespace,
            lambda raw: updater(self._decode(raw)).model_dump(mode="json"),
            default=BusinessKpiState().model_dump(mode="json"),
        )
        return self._decode(payload)
