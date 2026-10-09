from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from codex_web.compatibility import MigrationRegistry
from codex_web.execution_workers import (
    EXECUTION_WORKER_CONTRACT,
    AssignmentStatus,
    ExecutionAssignment,
    ExecutionWorker,
    ExecutionWorkerState,
)
from codex_web.storage.sqlite_state import SQLiteStateStore

EXECUTION_WORKER_MIGRATIONS = MigrationRegistry("execution-worker-state")
EXECUTION_WORKER_MIGRATIONS.register(
    "0.0",
    "1.0",
    lambda payload: {
        "schema_version": "1.0",
        "workers": list(payload.get("workers", [])),
        "assignments": list(payload.get("assignments", [])),
        "events": list(payload.get("events", [])),
    },
)


def _migrate_1_0_to_1_1(payload: dict[str, Any]) -> dict[str, Any]:
    assignments = []
    for raw in payload.get("assignments", []):
        item = dict(raw)
        if item.get("subject") is None and item.get("work_item_ref"):
            item["subject"] = {
                "kind": "work_item",
                "ref": item["work_item_ref"],
            }
        assignments.append(item)
    return {
        **payload,
        "schema_version": "1.1",
        "assignments": assignments,
    }


EXECUTION_WORKER_MIGRATIONS.register("1.0", "1.1", _migrate_1_0_to_1_1)
EXECUTION_WORKER_MIGRATIONS.register(
    "1.1",
    "1.2",
    lambda payload: {
        **payload,
        "schema_version": "1.2",
    },
)


def _migrate_1_2_to_1_3(payload: dict[str, Any]) -> dict[str, Any]:
    workers = []
    for raw in payload.get("workers", []):
        item = dict(raw)
        item.setdefault("supported_execution_contract_versions", ["1.0"])
        workers.append(item)
    return {
        **payload,
        "schema_version": "1.3",
        "workers": workers,
    }


EXECUTION_WORKER_MIGRATIONS.register("1.2", "1.3", _migrate_1_2_to_1_3)
EXECUTION_WORKER_MIGRATIONS.register(
    "1.3",
    "1.4",
    lambda payload: {
        **payload,
        "schema_version": "1.4",
        "assignments": [
            {**dict(item), "repository_target": dict(item).get("repository_target")}
            for item in payload.get("assignments", [])
        ],
    },
)
EXECUTION_WORKER_MIGRATIONS.register(
    "1.4",
    "1.5",
    lambda payload: {
        **payload,
        "schema_version": "1.5",
        "assignments": [
            {
                **dict(item),
                "execution_profile_id": dict(item).get("execution_profile_id"),
                "execution_profile_definition": dict(item).get(
                    "execution_profile_definition"
                ),
            }
            for item in payload.get("assignments", [])
        ],
    },
)

EXECUTION_WORKER_MIGRATIONS.register(
    "1.5",
    "1.6",
    lambda payload: {
        **payload,
        "schema_version": "1.6",
        "assignments": [
            {**dict(item), "failure": dict(item).get("failure")}
            for item in payload.get("assignments", [])
        ],
    },
)


EXECUTION_WORKER_MIGRATIONS.register(
    "1.6",
    "1.7",
    lambda payload: {
        **payload,
        "schema_version": "1.7",
        "enrollments": list(payload.get("enrollments", [])),
    },
)


def _migrate_1_7_to_1_8(payload: dict[str, Any]) -> dict[str, Any]:
    workers = []
    for raw in payload.get("workers", []):
        item = dict(raw)
        item.setdefault(
            "supported_sandbox_profiles",
            ["read-only", "workspace-write", "danger-full-access"],
        )
        workers.append(item)
    return {
        **payload,
        "schema_version": "1.8",
        "workers": workers,
    }


EXECUTION_WORKER_MIGRATIONS.register("1.7", "1.8", _migrate_1_7_to_1_8)
EXECUTION_WORKER_MIGRATIONS.register(
    "1.8",
    "1.9",
    lambda payload: {
        **payload,
        "schema_version": "1.9",
        "assignments": [
            {
                **dict(item),
                "skill_refs": list(dict(item).get("skill_refs") or []),
                "skill_assignment_sources": dict(
                    dict(item).get("skill_assignment_sources") or {}
                ),
            }
            for item in payload.get("assignments", [])
        ],
    },
)


class ExecutionWorkerStore:
    namespace = "execution_workers"
    worker_namespace = "execution_workers.workers"
    assignment_namespace = "execution_workers.assignments"
    event_namespace = "execution_workers.events"
    enrollment_namespace = "execution_workers.enrollments"
    max_retained_terminal_assignments = 128
    max_retained_events = 1000

    def __init__(self, store: SQLiteStateStore) -> None:
        self.store = store
        self._lock = threading.RLock()

    def _decode(self, payload: Any) -> ExecutionWorkerState:
        if payload is None:
            return ExecutionWorkerState()
        if not isinstance(payload, dict):
            raise ValueError("execution worker state must be an object")
        version = str(payload.get("schema_version") or "0.0")
        if version != EXECUTION_WORKER_CONTRACT.current:
            payload = EXECUTION_WORKER_MIGRATIONS.migrate(
                payload,
                from_version=version,
                to_version=EXECUTION_WORKER_CONTRACT.current,
            )
        EXECUTION_WORKER_CONTRACT.require(payload.get("schema_version", ""))
        return ExecutionWorkerState.model_validate(payload)

    def _ensure_records(self) -> None:
        namespaces = (
            self.worker_namespace,
            self.assignment_namespace,
            self.event_namespace,
            self.enrollment_namespace,
        )
        collection_exists = {
            item: self.store.record_collection_exists(item) for item in namespaces
        }
        legacy_payload = self.store.get(self.namespace)
        if all(collection_exists.values()) and legacy_payload is None:
            return
        state = self._decode(legacy_payload)
        records = {
            self.worker_namespace: {
                item.id: item.model_dump(mode="json") for item in state.workers
            },
            self.assignment_namespace: {
                item.id: item.model_dump(mode="json") for item in state.assignments
            },
            self.event_namespace: {
                item.id: item.model_dump(mode="json") for item in state.events
            },
            self.enrollment_namespace: {
                item.id: item.model_dump(mode="json") for item in state.enrollments
            },
        }
        for namespace, items in records.items():
            if collection_exists[namespace]:
                self.store.record_apply(namespace, upserts=items)
            else:
                self.store.record_replace(namespace, items)
        self.store.delete(self.namespace)

    def _load_unlocked(self) -> ExecutionWorkerState:
        self._ensure_records()
        return self._decode(
            {
                "schema_version": EXECUTION_WORKER_CONTRACT.current,
                "workers": sorted(
                    self.store.record_items(self.worker_namespace).values(),
                    key=lambda item: (item.get("registered_at", 0), item.get("id", "")),
                ),
                "assignments": sorted(
                    self.store.record_items(self.assignment_namespace).values(),
                    key=lambda item: (item.get("created_at", 0), item.get("id", "")),
                ),
                "events": sorted(
                    self.store.record_items(self.event_namespace).values(),
                    key=lambda item: (item.get("occurred_at", 0), item.get("id", "")),
                ),
                "enrollments": sorted(
                    self.store.record_items(self.enrollment_namespace).values(),
                    key=lambda item: (item.get("created_at", 0), item.get("id", "")),
                ),
            }
        )

    def load(self) -> ExecutionWorkerState:
        with self._lock:
            return self._load_unlocked()

    def assignment(self, assignment_id: str) -> ExecutionAssignment | None:
        self._ensure_records()
        payload = self.store.record_get(self.assignment_namespace, assignment_id)
        return (
            ExecutionAssignment.model_validate(payload)
            if payload is not None
            else None
        )

    def assignment_page(
        self, *, after: str | None = None, limit: int = 100,
    ) -> tuple[list[ExecutionAssignment], str | None]:
        """Read bounded assignments without loading worker/event history."""
        self._ensure_records()
        records, cursor = self.store.record_page(
            self.assignment_namespace, after=after, limit=limit,
        )
        return [ExecutionAssignment.model_validate(raw) for raw in records.values()], cursor

    def worker(self, worker_id: str) -> ExecutionWorker | None:
        self._ensure_records()
        payload = self.store.record_get(self.worker_namespace, worker_id)
        return (
            ExecutionWorker.model_validate(payload)
            if payload is not None
            else None
        )

    def update_worker(
        self,
        worker_id: str,
        updater: Callable[[ExecutionWorkerState], ExecutionWorkerState],
    ) -> ExecutionWorkerState:
        """Mutate one current worker without scanning retained runtime history."""
        with self._lock:
            self._ensure_records()
            result: list[ExecutionWorkerState] = []

            def apply(raw: Any) -> Any:
                current = ExecutionWorker.model_validate(raw) if raw is not None else None
                state = ExecutionWorkerState(workers=[current] if current else [])
                state = updater(state)
                if (len(state.workers) != 1 or state.workers[0].id != worker_id
                        or state.assignments or state.events or state.enrollments):
                    raise ValueError("worker mutation must only update its target record")
                result.append(state)
                return state.workers[0].model_dump(mode="json")

            self.store.record_update(self.worker_namespace, worker_id, apply, default=None)
            return result[0]

    def update_assignment(
        self,
        worker_id: str,
        assignment_id: str,
        updater: Callable[[ExecutionWorkerState], ExecutionWorkerState],
    ) -> ExecutionWorkerState:
        """Mutate a current assignment with its owning worker's trust context."""
        with self._lock:
            self._ensure_records()
            raw_worker = self.store.record_get(self.worker_namespace, worker_id)
            worker = ExecutionWorker.model_validate(raw_worker) if raw_worker is not None else None
            result: list[ExecutionWorkerState] = []

            def apply(raw: Any) -> Any:
                assignment = ExecutionAssignment.model_validate(raw) if raw is not None else None
                workers = [worker] if worker else []
                state = ExecutionWorkerState(workers=workers, assignments=[assignment] if assignment else [])
                state = updater(state)
                if (len(state.assignments) != 1 or state.assignments[0].id != assignment_id
                        or state.workers != workers or state.events or state.enrollments):
                    raise ValueError("assignment mutation must only update its target record")
                result.append(state)
                return state.assignments[0].model_dump(mode="json")

            self.store.record_update(self.assignment_namespace, assignment_id, apply, default=None)
            return result[0]

    def update(
        self,
        updater: Callable[[ExecutionWorkerState], ExecutionWorkerState],
    ) -> ExecutionWorkerState:
        with self._lock:
            current = self._load_unlocked()
            before = {
                self.worker_namespace: {
                    item.id: item.model_dump(mode="json") for item in current.workers
                },
                self.assignment_namespace: {
                    item.id: item.model_dump(mode="json") for item in current.assignments
                },
                self.event_namespace: {
                    item.id: item.model_dump(mode="json") for item in current.events
                },
                self.enrollment_namespace: {
                    item.id: item.model_dump(mode="json") for item in current.enrollments
                },
            }
            state = updater(current)
            active_statuses = {
                AssignmentStatus.PENDING,
                AssignmentStatus.CLAIMED,
                AssignmentStatus.RUNNING,
            }
            terminal = [
                item
                for item in state.assignments
                if item.status not in active_statuses
            ]
            retained_terminal_ids = {
                item.id
                for item in terminal[
                    -self.max_retained_terminal_assignments:
                ]
            }
            state.assignments = [
                item
                for item in state.assignments
                if item.status in active_statuses
                or item.id in retained_terminal_ids
            ]
            state.events = state.events[-self.max_retained_events :]
            after = {
                self.worker_namespace: {
                    item.id: item.model_dump(mode="json") for item in state.workers
                },
                self.assignment_namespace: {
                    item.id: item.model_dump(mode="json") for item in state.assignments
                },
                self.event_namespace: {
                    item.id: item.model_dump(mode="json") for item in state.events
                },
                self.enrollment_namespace: {
                    item.id: item.model_dump(mode="json") for item in state.enrollments
                },
            }
            for namespace, items in after.items():
                previous = before[namespace]
                self.store.record_apply(
                    namespace,
                    upserts={
                        key: value
                        for key, value in items.items()
                        if previous.get(key) != value
                    },
                    deletes=tuple(set(previous) - set(items)),
                )
            return state
