from __future__ import annotations

import threading
from typing import Any, Callable

from codex_web.agent_runtime_usage import (
    AGENT_RUNTIME_USAGE_STATE_CONTRACT,
    AgentRuntimeUsage,
    AgentRuntimeUsageState,
)
from codex_web.compatibility import MigrationRegistry
from codex_web.storage.sqlite_state import SQLiteStateStore


AGENT_RUNTIME_USAGE_MIGRATIONS = MigrationRegistry("agent-runtime-usage-state")
AGENT_RUNTIME_USAGE_MIGRATIONS.register(
    "0.0",
    "1.0",
    lambda payload: {
        "schema_version": AGENT_RUNTIME_USAGE_STATE_CONTRACT.current,
        "records": list(payload.get("records", [])),
    },
)
AGENT_RUNTIME_USAGE_MIGRATIONS.register(
    "1.0",
    "1.1",
    lambda payload: {
        "schema_version": AGENT_RUNTIME_USAGE_STATE_CONTRACT.current,
        "records": [
            {**record, "resources": list(record.get("resources", []))}
            for record in payload.get("records", [])
        ],
    },
)


class AgentRuntimeUsageStore:
    namespace = "agent_runtime_usage"

    def __init__(self, store: SQLiteStateStore) -> None:
        self.store = store
        self._lock = threading.RLock()

    def _decode(self, payload: Any) -> AgentRuntimeUsageState:
        if payload is None:
            return AgentRuntimeUsageState()
        if not isinstance(payload, dict):
            raise ValueError("agent runtime usage state must be an object")
        version = str(payload.get("schema_version") or "0.0")
        if version != AGENT_RUNTIME_USAGE_STATE_CONTRACT.current:
            payload = AGENT_RUNTIME_USAGE_MIGRATIONS.migrate(
                payload,
                from_version=version,
                to_version=AGENT_RUNTIME_USAGE_STATE_CONTRACT.current,
            )
        AGENT_RUNTIME_USAGE_STATE_CONTRACT.require(
            payload.get("schema_version", "")
        )
        return AgentRuntimeUsageState.model_validate(payload)

    def _ensure_records(self) -> None:
        collection_exists = self.store.record_collection_exists(self.namespace)
        payload = self.store.get(self.namespace)
        if collection_exists and not (
            isinstance(payload, dict)
            and "schema_version" in payload
            and "records" in payload
        ):
            return
        state = self._decode(payload)
        records = {item.id: item.model_dump(mode="json") for item in state.records}
        if collection_exists:
            merged = self.store.record_items(self.namespace)
            merged.update(records)
            records = merged
        self.store.record_replace(
            self.namespace,
            records,
        )

    def _load_unlocked(self) -> AgentRuntimeUsageState:
        self._ensure_records()
        records = self.store.record_items(self.namespace).values()
        return self._decode(
            {
                "schema_version": AGENT_RUNTIME_USAGE_STATE_CONTRACT.current,
                "records": list(records),
            }
        )

    def load(self) -> AgentRuntimeUsageState:
        with self._lock:
            return self._load_unlocked()

    def update(
        self,
        updater: Callable[[AgentRuntimeUsageState], AgentRuntimeUsageState],
    ) -> AgentRuntimeUsageState:
        with self._lock:
            current = self._load_unlocked()
            before = {
                item.id: item.model_dump(mode="json") for item in current.records
            }
            updated = updater(current)
            after = {
                item.id: item.model_dump(mode="json") for item in updated.records
            }
            self.store.record_apply(
                self.namespace,
                upserts={
                    key: value
                    for key, value in after.items()
                    if before.get(key) != value
                },
                deletes=tuple(set(before) - set(after)),
            )
            return updated

    def list(self) -> list[AgentRuntimeUsage]:
        return sorted(
            self.load().records,
            key=lambda item: (
                item.organization_id,
                item.workspace_id,
                item.observed_at,
                item.id,
            ),
            reverse=True,
        )

    def upsert(self, record: AgentRuntimeUsage) -> AgentRuntimeUsage:
        with self._lock:
            self._ensure_records()
            existing_payload = self.store.record_get(self.namespace, record.id)
            if existing_payload is not None:
                existing = AgentRuntimeUsage.model_validate(existing_payload)
                if (
                    existing.organization_id != record.organization_id
                    or existing.workspace_id != record.workspace_id
                ):
                    raise RuntimeError(
                        "runtime usage id already exists in another tenant scope"
                    )
            self.store.record_apply(
                self.namespace,
                upserts={record.id: record.model_dump(mode="json")},
            )
        return record

    def get(self, record_id: str) -> AgentRuntimeUsage | None:
        with self._lock:
            self._ensure_records()
            payload = self.store.record_get(self.namespace, record_id)
        return AgentRuntimeUsage.model_validate(payload) if payload is not None else None
