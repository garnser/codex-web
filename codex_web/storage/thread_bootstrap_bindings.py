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


    def update_with_assignment_fence(self, updater, assignments, assignment_id, fence):
        """One canonical transaction pins the old binding AND retired fence."""
        from codex_web.execution_workers import ExecutionAssignment
        if assignments.store is not self.store:
            raise ValueError('Bootstrap fence CAS requires the same canonical store')
        assignments.assignment(assignment_id)  # initialize existing keyed projection
        selected={self.namespace:(None,),assignments.assignment_namespace:(assignment_id,)}
        result=[]
        def apply(rows):
            original=rows[assignments.assignment_namespace][assignment_id]
            if original is None:
                raise ValueError('Retired bootstrap assignment unavailable')
            assignment=ExecutionAssignment.model_validate(original)
            if assignment.fence!=fence or assignment.status not in ('cancelled','failed','succeeded') or assignment.lease is not None:
                raise ValueError('Retired bootstrap fence/status changed')
            state=updater(self._decode(rows[self.namespace][None]))
            result.append(state)
            return {self.namespace:{None:state.model_dump(mode='json')}}
        self.store.record_mutate_many(selected,apply)
        return result[0]
