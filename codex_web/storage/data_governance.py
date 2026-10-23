from __future__ import annotations

from typing import Any, Callable

from codex_web.compatibility import MigrationRegistry
from codex_web.data_governance import DATA_GOVERNANCE_CONTRACT, DataGovernanceState
from codex_web.storage.sqlite_state import SQLiteStateStore


DATA_GOVERNANCE_MIGRATIONS = MigrationRegistry("data-governance-state")
DATA_GOVERNANCE_MIGRATIONS.register(
    "0.0",
    "1.0",
    lambda payload: {
        "schema_version": "1.0",
        "records": list(payload.get("records", [])),
        "requests": list(payload.get("requests", [])),
        "events": list(payload.get("events", [])),
    },
)

DATA_GOVERNANCE_MIGRATIONS.register(
    "1.0", "1.1", lambda payload: {**payload, "schema_version": "1.1"})


class DataGovernanceStore:
    namespace = "data_governance"

    def __init__(self, store: SQLiteStateStore) -> None:
        self.store = store

    def _decode(self, payload: Any) -> DataGovernanceState:
        if payload is None:
            return DataGovernanceState()
        if not isinstance(payload, dict):
            raise ValueError("data governance state must be an object")
        version = str(payload.get("schema_version") or "0.0")
        if version != DATA_GOVERNANCE_CONTRACT.current:
            payload = DATA_GOVERNANCE_MIGRATIONS.migrate(
                payload,
                from_version=version,
                to_version=DATA_GOVERNANCE_CONTRACT.current,
            )
        DATA_GOVERNANCE_CONTRACT.require(payload.get("schema_version", ""))
        return DataGovernanceState.model_validate(payload)

    @staticmethod
    def _encode(state: DataGovernanceState) -> dict[str, Any]:
        return state.model_dump(mode="json")

    def load(self) -> DataGovernanceState:
        return self._decode(self.store.get(self.namespace))

    def context_state(self, record_ids: tuple[str, ...], *, organization_id: str,
                      workspace_id: str) -> DataGovernanceState:
        """Read complete selected records in one uncached canonical snapshot.

        Legacy backends/envelopes retain the ordinary full decoder. Mutation and
        audit callers continue to load the complete document.
        """
        reader = getattr(self.store, 'document_array_items_envelope', None)
        if not callable(reader):
            return self.load()
        return self._decode(reader(self.namespace, 'records', record_ids,
            organization_id=organization_id, workspace_id=workspace_id,
            collection_names=('records', 'requests', 'events')))

    def update(
        self,
        updater: Callable[[DataGovernanceState], DataGovernanceState],
    ) -> DataGovernanceState:
        def apply(raw: Any) -> dict[str, Any]:
            current = self._decode(raw)
            updated = updater(current)
            return self._encode(updated)

        payload = self.store.update(
            self.namespace,
            apply,
            default=DataGovernanceState().model_dump(mode="json"),
        )
        return self._decode(payload)
