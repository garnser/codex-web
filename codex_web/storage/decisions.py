from __future__ import annotations

from collections.abc import Callable
from typing import Any

from codex_web.compatibility import MigrationRegistry
from codex_web.decisions import DECISION_CONTRACT, Decision, DecisionState
from codex_web.storage.sqlite_state import SQLiteStateStore


DECISION_MIGRATIONS = MigrationRegistry("decision-state")


class DecisionNotFoundError(KeyError):
    pass


class DecisionConflictError(RuntimeError):
    pass


class DecisionStore:
    namespace = "decisions"

    def __init__(self, store: SQLiteStateStore) -> None:
        self.store = store

    def _decode(self, payload: Any) -> DecisionState:
        if payload is None:
            return DecisionState()
        if not isinstance(payload, dict):
            raise ValueError("decision state must be an object")
        version = str(payload.get("schema_version") or DECISION_CONTRACT.current)
        if version != DECISION_CONTRACT.current:
            payload = DECISION_MIGRATIONS.migrate(
                payload,
                from_version=version,
                to_version=DECISION_CONTRACT.current,
            )
        DECISION_CONTRACT.require(payload.get("schema_version", ""))
        return DecisionState.model_validate(payload)

    def default_document(self) -> dict[str, Any]:
        return DecisionState().model_dump(mode="json")

    def load(self) -> DecisionState:
        return self._decode(self.store.get(self.namespace))

    def list(
        self,
        *,
        organization_id: str,
        workspace_id: str,
    ) -> tuple[Decision, ...]:
        rows = [
            item
            for item in self.load().decisions
            if item.organization_id == organization_id
            and item.workspace_id == workspace_id
        ]
        rows.sort(key=lambda item: (item.updated_at, item.id), reverse=True)
        return tuple(rows)

    def get(self, decision_id: str) -> Decision:
        item = next(
            (row for row in self.load().decisions if row.id == decision_id),
            None,
        )
        if item is None:
            raise DecisionNotFoundError(decision_id)
        return item

    def create(self, decision: Decision) -> Decision:
        def apply(raw: Any) -> dict[str, Any]:
            state = self._decode(raw)
            if any(item.id == decision.id for item in state.decisions):
                raise DecisionConflictError(f"decision already exists: {decision.id}")
            state.decisions.append(decision)
            return state.model_dump(mode="json")

        self.store.update(
            self.namespace,
            apply,
            default=self.default_document(),
        )
        return decision

    def update(
        self,
        decision_id: str,
        updater: Callable[[DecisionState, Decision], tuple[DecisionState, Decision]],
    ) -> Decision:
        result: dict[str, Decision] = {}

        def apply(raw: Any) -> dict[str, Any]:
            state = self._decode(raw)
            current = next(
                (item for item in state.decisions if item.id == decision_id),
                None,
            )
            if current is None:
                raise DecisionNotFoundError(decision_id)
            updated_state, updated = updater(state, current)
            if updated.id != current.id:
                raise ValueError("decision updater cannot change decision id")
            result["value"] = updated
            return updated_state.model_dump(mode="json")

        self.store.update(
            self.namespace,
            apply,
            default=self.default_document(),
        )
        return result["value"]

    def mutate_document(
        self,
        raw: Any,
        decision_id: str,
        updater: Callable[[DecisionState, Decision], tuple[DecisionState, Decision]],
    ) -> tuple[dict[str, Any], Decision]:
        state = self._decode(raw)
        current = next(
            (item for item in state.decisions if item.id == decision_id),
            None,
        )
        if current is None:
            raise DecisionNotFoundError(decision_id)
        updated_state, updated = updater(state, current)
        if updated.id != current.id:
            raise ValueError("decision updater cannot change decision id")
        return updated_state.model_dump(mode="json"), updated
