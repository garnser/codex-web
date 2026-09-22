from __future__ import annotations

from typing import Any, Callable

from codex_web.compatibility import ContractSpec, MigrationRegistry
from codex_web.execution_workspaces import ExecutionWorkspaceState
from codex_web.storage.sqlite_state import SQLiteStateStore


EXECUTION_WORKSPACE_STATE_CONTRACT = ContractSpec(
    "execution-workspace-state",
    "1.4",
    ("1.0", "1.1", "1.2", "1.3", "1.4"),
)
EXECUTION_WORKSPACE_STATE_MIGRATIONS = MigrationRegistry("execution-workspace-state")
EXECUTION_WORKSPACE_STATE_MIGRATIONS.register(
    "0.0",
    "1.0",
    lambda payload: {
        "schema_version": "1.0",
        **{key: value for key, value in payload.items() if key != "schema_version"},
    },
)


def _subject_from_legacy(item: dict[str, Any]) -> dict[str, Any]:
    migrated = dict(item)
    if migrated.get("subject") is None and migrated.get("work_item_ref"):
        migrated["subject"] = {
            "kind": "work_item",
            "ref": migrated["work_item_ref"],
        }
    return migrated


def _migrate_1_0_to_1_1(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        **payload,
        "schema_version": "1.1",
        "workspaces": [
            _subject_from_legacy(item)
            for item in payload.get("workspaces", [])
        ],
        "leases": [
            _subject_from_legacy(item)
            for item in payload.get("leases", [])
        ],
    }


EXECUTION_WORKSPACE_STATE_MIGRATIONS.register(
    "1.0",
    "1.1",
    _migrate_1_0_to_1_1,
)
EXECUTION_WORKSPACE_STATE_MIGRATIONS.register(
    "1.1",
    "1.2",
    lambda payload: {
        **payload,
        "schema_version": "1.2",
    },
)
EXECUTION_WORKSPACE_STATE_MIGRATIONS.register(
    "1.2",
    "1.3",
    lambda payload: {
        **payload,
        "schema_version": "1.3",
        "leases": [
            {
                **dict(item),
                "resource_modes": (
                    dict(item).get("resource_modes")
                    or {
                        resource_id: dict(item).get("mode", "write")
                        for resource_id in dict(item).get("resource_ids", [])
                    }
                ),
            }
            for item in payload.get("leases", [])
        ],
        "workspaces": [
            {
                **dict(item),
                "repository_members": dict(item).get("repository_members") or [],
            }
            for item in payload.get("workspaces", [])
        ],
    },
)


def _repository_outcome_status(item: dict[str, Any]) -> str:
    writable = list(item.get("writable_repository_ids") or [])
    primary = item.get("repository_resource_id")
    if not writable and primary:
        writable = [primary]

    integrations = dict(item.get("repository_integrations") or {})
    legacy = item.get("integration")
    if (
        primary
        and primary not in integrations
        and isinstance(legacy, dict)
        and legacy.get("recorded_at") is not None
    ):
        integrations[primary] = legacy

    recorded = [
        integrations.get(repository_id)
        for repository_id in writable
        if integrations.get(repository_id) is not None
    ]
    outcomes = [
        str(value.get("outcome") or "pending")
        for value in recorded
        if isinstance(value, dict)
    ]
    if "conflict" in outcomes:
        return "blocked"
    if writable and len(recorded) == len(writable):
        successful = {"merged", "rebased", "fast_forwarded"}
        if outcomes and all(value in successful for value in outcomes):
            return "complete"
        if outcomes and all(value == "discarded" for value in outcomes):
            return "discarded"
        return "partial"
    if recorded:
        return "partial"
    return "pending"


def _migrate_1_3_to_1_4(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        **payload,
        "schema_version": "1.4",
        "workspaces": [
            {
                **dict(item),
                "repository_outcome_status": (
                    dict(item).get("repository_outcome_status")
                    or _repository_outcome_status(dict(item))
                ),
            }
            for item in payload.get("workspaces", [])
        ],
    }


EXECUTION_WORKSPACE_STATE_MIGRATIONS.register(
    "1.3",
    "1.4",
    _migrate_1_3_to_1_4,
)


class ExecutionWorkspaceStateStore:
    namespace = "execution_workspaces"

    def __init__(self, store: SQLiteStateStore) -> None:
        self.store = store

    def _decode(self, payload: Any) -> ExecutionWorkspaceState:
        if payload is None:
            return ExecutionWorkspaceState()
        if not isinstance(payload, dict):
            raise ValueError("execution workspace state must be an object")
        version = str(payload.get("schema_version") or "0.0")
        if version != EXECUTION_WORKSPACE_STATE_CONTRACT.current:
            payload = EXECUTION_WORKSPACE_STATE_MIGRATIONS.migrate(
                payload,
                from_version=version,
                to_version=EXECUTION_WORKSPACE_STATE_CONTRACT.current,
            )
        EXECUTION_WORKSPACE_STATE_CONTRACT.require(payload.get("schema_version", ""))
        return ExecutionWorkspaceState.model_validate(payload)

    def load(self) -> ExecutionWorkspaceState:
        return self._decode(self.store.get(self.namespace))

    def update(
        self,
        updater: Callable[[ExecutionWorkspaceState], ExecutionWorkspaceState],
    ) -> ExecutionWorkspaceState:
        def apply(raw: Any) -> dict[str, Any]:
            state = self._decode(raw)
            return updater(state).model_dump(mode="json")

        payload = self.store.update(
            self.namespace,
            apply,
            default=ExecutionWorkspaceState().model_dump(mode="json"),
        )
        return self._decode(payload)
