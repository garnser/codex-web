from __future__ import annotations

from typing import Any, Callable

from codex_web.compatibility import ContractSpec, MigrationRegistry
from codex_web.resources import ResourceCatalogState
from codex_web.storage.sqlite_state import SQLiteStateStore


RESOURCE_CATALOG_CONTRACT = ContractSpec("resource-catalog", "1.0", ("1.0",))
RESOURCE_CATALOG_MIGRATIONS = MigrationRegistry("resource-catalog")
RESOURCE_CATALOG_MIGRATIONS.register(
    "0.0",
    "1.0",
    lambda payload: {
        "schema_version": RESOURCE_CATALOG_CONTRACT.current,
        **{key: value for key, value in payload.items() if key != "schema_version"},
    },
)


class ResourceCatalogStore:
    namespace = "resource_catalog"

    def __init__(self, store: SQLiteStateStore) -> None:
        self.store = store

    def _decode(self, payload: Any) -> ResourceCatalogState:
        if payload is None:
            return ResourceCatalogState()
        if not isinstance(payload, dict):
            raise ValueError("resource catalog state must be an object")
        version = str(payload.get("schema_version") or "0.0")
        if version != RESOURCE_CATALOG_CONTRACT.current:
            payload = RESOURCE_CATALOG_MIGRATIONS.migrate(
                payload,
                from_version=version,
                to_version=RESOURCE_CATALOG_CONTRACT.current,
            )
        RESOURCE_CATALOG_CONTRACT.require(payload.get("schema_version", ""))
        return ResourceCatalogState.model_validate(payload)

    def load(self) -> ResourceCatalogState:
        return self._decode(self.store.get(self.namespace))

    def update(
        self,
        updater: Callable[[ResourceCatalogState], ResourceCatalogState],
    ) -> ResourceCatalogState:
        def apply(raw: Any) -> dict[str, Any]:
            state = self._decode(raw)
            return updater(state).model_dump(mode="json")

        payload = self.store.update(
            self.namespace,
            apply,
            default=ResourceCatalogState().model_dump(mode="json"),
        )
        return self._decode(payload)
