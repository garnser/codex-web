from __future__ import annotations

from typing import Any, Callable

from codex_web.compatibility import ContractSpec, MigrationRegistry
from codex_web.secrets import SecretState
from codex_web.storage.sqlite_state import SQLiteStateStore


SECRET_STATE_CONTRACT = ContractSpec("secret-state", "1.0", ("1.0",))
SECRET_STATE_MIGRATIONS = MigrationRegistry("secret-state")
SECRET_STATE_MIGRATIONS.register(
    "0.0",
    "1.0",
    lambda payload: {
        "schema_version": SECRET_STATE_CONTRACT.current,
        **{key: value for key, value in payload.items() if key != "schema_version"},
    },
)


class SecretStateStore:
    namespace = "secret_metadata"

    def __init__(self, store: SQLiteStateStore) -> None:
        self.store = store
        enable = getattr(store, 'enable_document_array_projection', None)
        if callable(enable):
            enable(self.namespace, array_names=('references',))

    def _decode(self, payload: Any) -> SecretState:
        if payload is None:
            return SecretState()
        if not isinstance(payload, dict):
            raise ValueError("secret metadata state must be an object")
        version = str(payload.get("schema_version") or "0.0")
        if version != SECRET_STATE_CONTRACT.current:
            payload = SECRET_STATE_MIGRATIONS.migrate(
                payload,
                from_version=version,
                to_version=SECRET_STATE_CONTRACT.current,
            )
        SECRET_STATE_CONTRACT.require(payload.get("schema_version", ""))
        return SecretState.model_validate(payload)

    def load(self) -> SecretState:
        return self._decode(self.store.get(self.namespace))

    def reference_state(self, secret_id: str, *, organization_id: str, workspace_id: str) -> SecretState:
        """Read a current reference through the existing document envelope.

        First-ID selection precedes scope filtering, matching the broker's
        original global lookup. Missing/foreign selections retain the full
        owner so not-found, tenant denial and legacy decoder behavior survive.
        This is metadata only; it never resolves material or caches authority.
        """
        load = self.load
        if (type(self) is not SecretStateStore
                or getattr(load, '__self__', None) is not self
                or getattr(load, '__func__', None) is not _CANONICAL_SECRET_STATE_LOAD):
            return load()
        project = getattr(self.store, 'document_array_items_envelope', None)
        if (callable(project)
                and getattr(self.store, 'supports_document_array_scope_after_first', False) is True):
            state = self._decode(project(self.namespace, 'references', (secret_id,),
                organization_id=organization_id, workspace_id=workspace_id,
                collection_names=('references', 'audit'), first_per_id=True, scope_after_first=True))
            if any(reference.id == secret_id for reference in state.references):
                return state
        return load()

    def update(self, updater: Callable[[SecretState], SecretState]) -> SecretState:
        def apply(raw: Any) -> dict[str, Any]:
            state = self._decode(raw)
            return updater(state).model_dump(mode="json")

        payload = self.store.update(
            self.namespace,
            apply,
            default=SecretState().model_dump(mode="json"),
        )
        return self._decode(payload)


_CANONICAL_SECRET_STATE_LOAD = SecretStateStore.load
