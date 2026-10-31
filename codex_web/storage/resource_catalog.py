from __future__ import annotations

import inspect
import threading
from typing import Any, Callable

from codex_web.compatibility import ContractSpec, MigrationRegistry
from codex_web.resources import ResourceCatalogState
from codex_web.storage.sqlite_state import SQLiteStateStore


RESOURCE_CATALOG_CONTRACT = ContractSpec("resource-catalog", "1.1", ("1.1",))
RESOURCE_CATALOG_MIGRATIONS = MigrationRegistry("resource-catalog")
RESOURCE_CATALOG_MIGRATIONS.register(
    "0.0",
    "1.0",
    lambda payload: {
        "schema_version": "1.0",
        **{key: value for key, value in payload.items() if key != "schema_version"},
    },
)

RESOURCE_CATALOG_MIGRATIONS.register(
    "1.0", "1.1", lambda payload: {**payload, "schema_version": "1.1"},
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

    def can_observe_reads(self) -> bool:
        return (type(self) is ResourceCatalogStore
                and all(getattr(getattr(self, name, None), "__self__", None) is self
                        and getattr(getattr(self, name, None), "__func__", None) is method
                        for name, method in _CANONICAL_RESOURCE_STORE_READS.items()))

    def observe_reads(self, callback):
        """One lazy, full canonical load for an explicitly synchronous owner."""
        if not self.can_observe_reads():
            return callback(None)
        observation = _ResourceCatalogReadObservation(self)
        try:
            result = callback(observation)
            if inspect.isawaitable(result) or inspect.isgenerator(result) or inspect.isasyncgen(result):
                if inspect.iscoroutine(result) or inspect.isgenerator(result):
                    result.close()
                raise ValueError("Resource read observation callback must be synchronous")
            return result
        finally:
            observation.close()

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


class _ResourceCatalogReadObservation:
    def __init__(self, owner):
        self._owner = owner
        self._backend = owner.store
        self._thread = threading.get_ident()
        self._active = True
        self._state = None

    def require_owner(self, owner):
        if (not self._active or owner is not self._owner
                or owner.store is not self._backend
                or threading.get_ident() != self._thread
                or not _CANONICAL_RESOURCE_STORE_CAN_OBSERVE(owner)):
            raise ValueError("Resource store read observation is foreign, closed or on another thread")

    def load(self, owner):
        self.require_owner(owner)
        if self._state is None:
            # Keep the ordinary full decoder and migrations, including failure
            # for malformed unrelated resources, relationships and bindings.
            self._state = owner.load().model_copy(deep=True)
        return self._state

    def close(self):
        self._active = False
        self._state = None


_CANONICAL_RESOURCE_STORE_READS = {
    name: getattr(ResourceCatalogStore, name) for name in ("load", "_decode")
}
_CANONICAL_RESOURCE_STORE_CAN_OBSERVE = ResourceCatalogStore.can_observe_reads
