from __future__ import annotations

import hashlib
import json
import threading
from typing import Any, Callable

from codex_web.compatibility import ContractSpec, MigrationRegistry
from codex_web.execution_workspaces import ExecutionWorkspace, ExecutionWorkspaceState
from codex_web.storage.sqlite_state import SQLiteStateStore


EXECUTION_WORKSPACE_STATE_CONTRACT = ContractSpec(
    "execution-workspace-state",
    "1.5",
    ("1.0", "1.1", "1.2", "1.3", "1.4", "1.5"),
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
EXECUTION_WORKSPACE_STATE_MIGRATIONS.register(
    "1.4",
    "1.5",
    lambda payload: {
        **payload,
        "schema_version": "1.5",
        "workspaces": [
            {
                **dict(item),
                "repository_checkpoints": (
                    dict(item).get("repository_checkpoints") or {}
                ),
            }
            for item in payload.get("workspaces", [])
        ],
    },
)


class ExecutionWorkspaceStateStore:
    namespace = "execution_workspaces"
    workspace_namespace = "execution_workspaces.workspaces"
    lease_namespace = "execution_workspaces.leases"
    event_namespace = "execution_workspaces.events"

    def __init__(self, store: SQLiteStateStore) -> None:
        self.store = store
        self._lock = threading.RLock()

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

    @staticmethod
    def _event_records(events) -> dict[str, Any]:
        records: dict[str, Any] = {}
        occurrences: dict[str, int] = {}
        for event in events:
            payload = event.model_dump(mode="json")
            encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
            digest = hashlib.sha256(encoded.encode()).hexdigest()[:16]
            base = f"{event.occurred_at:020.6f}:{event.workspace_id}:{digest}"
            occurrence = occurrences.get(base, 0)
            occurrences[base] = occurrence + 1
            records[f"{base}:{occurrence:04d}"] = payload
        return records

    def _ensure_records(self) -> None:
        namespaces = (
            self.workspace_namespace,
            self.lease_namespace,
            self.event_namespace,
        )
        collection_exists = {
            item: self.store.record_collection_exists(item) for item in namespaces
        }
        legacy_payload = self.store.get(self.namespace)
        if all(collection_exists.values()) and legacy_payload is None:
            return
        state = self._decode(legacy_payload)
        records = {
            self.workspace_namespace: {
                item.id: item.model_dump(mode="json") for item in state.workspaces
            },
            self.lease_namespace: {
                item.id: item.model_dump(mode="json") for item in state.leases
            },
            self.event_namespace: self._event_records(state.events),
        }
        for namespace, items in records.items():
            if collection_exists[namespace]:
                self.store.record_apply(namespace, upserts=items)
            else:
                self.store.record_replace(namespace, items)
        self.store.delete(self.namespace)

    def _load_unlocked(self) -> ExecutionWorkspaceState:
        self._ensure_records()
        workspaces = self.store.record_items(self.workspace_namespace).values()
        leases = self.store.record_items(self.lease_namespace).values()
        events = self.store.record_items(self.event_namespace).values()
        return self._decode(
            {
                "schema_version": EXECUTION_WORKSPACE_STATE_CONTRACT.current,
                "workspaces": sorted(
                    workspaces,
                    key=lambda item: (item.get("created_at", 0), item.get("id", "")),
                ),
                "leases": sorted(
                    leases,
                    key=lambda item: (item.get("acquired_at", 0), item.get("id", "")),
                ),
                "events": sorted(
                    events,
                    key=lambda item: (
                        item.get("occurred_at", 0),
                        item.get("workspace_id", ""),
                        item.get("event_type", ""),
                    ),
                ),
            }
        )

    def load(self) -> ExecutionWorkspaceState:
        with self._lock:
            return self._load_unlocked()

    def workspace(self, workspace_id: str) -> ExecutionWorkspace | None:
        """Read one canonical workspace without decoding lease or event history."""
        with self._lock:
            self._ensure_records()
            raw = self.store.record_get(self.workspace_namespace, workspace_id)
            if raw is None:
                return None
            workspace = ExecutionWorkspace.model_validate(raw)
            if workspace.id != workspace_id:
                raise ValueError("execution workspace record key does not match its id")
            return workspace

    def _apply_records(
        self,
        namespace: str,
        before: dict[str, Any],
        after: dict[str, Any],
    ) -> None:
        self.store.record_apply(
            namespace,
            upserts={
                key: value
                for key, value in after.items()
                if before.get(key) != value
            },
            deletes=tuple(set(before) - set(after)),
        )

    def update(
        self,
        updater: Callable[[ExecutionWorkspaceState], ExecutionWorkspaceState],
    ) -> ExecutionWorkspaceState:
        with self._lock:
            current = self._load_unlocked()
            before_workspaces = {
                item.id: item.model_dump(mode="json") for item in current.workspaces
            }
            before_leases = {
                item.id: item.model_dump(mode="json") for item in current.leases
            }
            before_events = self._event_records(current.events)
            updated = updater(current)
            after_workspaces = {
                item.id: item.model_dump(mode="json") for item in updated.workspaces
            }
            after_leases = {
                item.id: item.model_dump(mode="json") for item in updated.leases
            }
            self._apply_records(
                self.workspace_namespace,
                before_workspaces,
                after_workspaces,
            )
            self._apply_records(
                self.lease_namespace,
                before_leases,
                after_leases,
            )
            self._apply_records(
                self.event_namespace,
                before_events,
                self._event_records(updated.events),
            )
            return updated
