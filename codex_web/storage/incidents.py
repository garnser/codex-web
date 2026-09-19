from __future__ import annotations

from typing import Any, Callable

from codex_web.incidents import IncidentRecord, IncidentState
from codex_web.storage.state_store import StateStore


class IncidentNotFoundError(KeyError):
    pass


class IncidentStore:
    namespace = "incidents"

    def __init__(self, store: StateStore) -> None:
        self.store = store

    @staticmethod
    def _decode(raw: Any) -> IncidentState:
        if raw is None:
            return IncidentState()
        return IncidentState.model_validate(raw)

    def load(self) -> IncidentState:
        return self._decode(self.store.get(self.namespace))

    def update(
        self,
        updater: Callable[[IncidentState], IncidentState],
    ) -> IncidentState:
        raw = self.store.update(
            self.namespace,
            lambda current: updater(self._decode(current)).model_dump(mode="json"),
            default=IncidentState().model_dump(mode="json"),
        )
        return self._decode(raw)

    def get(self, incident_id: str) -> IncidentRecord:
        item = self.load().incidents.get(incident_id)
        if item is None:
            raise IncidentNotFoundError(incident_id)
        return item

    def list(
        self,
        *,
        organization_id: str,
        workspace_id: str,
    ) -> list[IncidentRecord]:
        rows = [
            item
            for item in self.load().incidents.values()
            if item.organization_id == organization_id
            and item.workspace_id == workspace_id
        ]
        return sorted(rows, key=lambda item: (item.detected_at, item.id), reverse=True)
