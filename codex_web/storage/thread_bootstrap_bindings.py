from __future__ import annotations

from typing import Any, Callable

from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.thread_bootstrap import (
    THREAD_BOOTSTRAP_BINDING_CONTRACT,
    ThreadBootstrapBindingState,
)


class ThreadBootstrapBindingStore:
    namespace = "thread_bootstrap_bindings"

    def __init__(self, store: SQLiteStateStore) -> None:
        self.store = store

    def _decode(self, payload: Any) -> ThreadBootstrapBindingState:
        if payload is None:
            return ThreadBootstrapBindingState()
        if not isinstance(payload, dict):
            raise ValueError("thread bootstrap binding state must be an object")
        THREAD_BOOTSTRAP_BINDING_CONTRACT.require(
            payload.get("schema_version", "")
        )
        return ThreadBootstrapBindingState.model_validate(payload)

    def load(self) -> ThreadBootstrapBindingState:
        return self._decode(self.store.get(self.namespace))

    def update(
        self,
        updater: Callable[
            [ThreadBootstrapBindingState],
            ThreadBootstrapBindingState,
        ],
    ) -> ThreadBootstrapBindingState:
        def apply(raw: Any) -> dict[str, Any]:
            return updater(self._decode(raw)).model_dump(mode="json")

        payload = self.store.update(
            self.namespace,
            apply,
            default=ThreadBootstrapBindingState().model_dump(mode="json"),
        )
        return self._decode(payload)
