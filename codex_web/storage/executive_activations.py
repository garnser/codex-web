from __future__ import annotations

from collections.abc import Callable
from typing import Any

from codex_web.compatibility import MigrationRegistry
from codex_web.executive_roles import (
    EXECUTIVE_ACTIVATION_CONTRACT,
    ExecutiveActivation,
    ExecutiveActivationState,
)
from codex_web.storage.sqlite_state import SQLiteStateStore


EXECUTIVE_ACTIVATION_MIGRATIONS = MigrationRegistry("executive-activation-state")


class ExecutiveActivationNotFoundError(KeyError):
    pass


class ExecutiveActivationConflictError(RuntimeError):
    pass


class ExecutiveActivationStore:
    namespace = "executive_activations"

    def __init__(self, store: SQLiteStateStore) -> None:
        self.store = store

    def _decode(self, payload: Any) -> ExecutiveActivationState:
        if payload is None:
            return ExecutiveActivationState()
        if not isinstance(payload, dict):
            raise ValueError("Executive activation state must be an object")
        version = str(
            payload.get("schema_version")
            or EXECUTIVE_ACTIVATION_CONTRACT.current
        )
        if version != EXECUTIVE_ACTIVATION_CONTRACT.current:
            payload = EXECUTIVE_ACTIVATION_MIGRATIONS.migrate(
                payload,
                from_version=version,
                to_version=EXECUTIVE_ACTIVATION_CONTRACT.current,
            )
        EXECUTIVE_ACTIVATION_CONTRACT.require(
            payload.get("schema_version", "")
        )
        return ExecutiveActivationState.model_validate(payload)

    def default_document(self) -> dict[str, Any]:
        return ExecutiveActivationState().model_dump(mode="json")

    def load(self) -> ExecutiveActivationState:
        return self._decode(self.store.get(self.namespace))

    def list(
        self,
        *,
        organization_id: str,
        workspace_id: str,
    ) -> tuple[ExecutiveActivation, ...]:
        rows = [
            item
            for item in self.load().activations
            if item.organization_id == organization_id
            and item.workspace_id == workspace_id
        ]
        rows.sort(key=lambda item: (item.updated_at, item.id), reverse=True)
        return tuple(rows)

    def get(self, activation_id: str) -> ExecutiveActivation:
        item = next(
            (
                row
                for row in self.load().activations
                if row.id == activation_id
            ),
            None,
        )
        if item is None:
            raise ExecutiveActivationNotFoundError(activation_id)
        return item

    def create(self, activation: ExecutiveActivation) -> ExecutiveActivation:
        def apply(raw: Any) -> dict[str, Any]:
            state = self._decode(raw)
            if any(item.id == activation.id for item in state.activations):
                raise ExecutiveActivationConflictError(
                    f"Executive activation already exists: {activation.id}"
                )
            state.activations.append(activation)
            return state.model_dump(mode="json")

        self.store.update(
            self.namespace,
            apply,
            default=self.default_document(),
        )
        return activation

    def update(
        self,
        activation_id: str,
        updater: Callable[
            [ExecutiveActivationState, ExecutiveActivation],
            tuple[ExecutiveActivationState, ExecutiveActivation],
        ],
    ) -> ExecutiveActivation:
        result: dict[str, ExecutiveActivation] = {}

        def apply(raw: Any) -> dict[str, Any]:
            state = self._decode(raw)
            current = next(
                (
                    item
                    for item in state.activations
                    if item.id == activation_id
                ),
                None,
            )
            if current is None:
                raise ExecutiveActivationNotFoundError(activation_id)
            updated_state, updated = updater(state, current)
            if updated.id != current.id:
                raise ValueError(
                    "Executive activation updater cannot change activation id"
                )
            result["value"] = updated
            return updated_state.model_dump(mode="json")

        self.store.update(
            self.namespace,
            apply,
            default=self.default_document(),
        )
        return result["value"]
