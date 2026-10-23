from __future__ import annotations

import threading
import json
from contextlib import closing
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
        document_get = getattr(self.store, "document_get", None)
        payload = (
            document_get(self.namespace)
            if callable(document_get)
            else self.store.get(self.namespace)
        )
        if collection_exists and payload is None and callable(document_get):
            # A compatible generic put can install a legacy envelope as keyed
            # entries. Probe only those reserved keys, never the usage catalog.
            version = self.store.record_get(self.namespace, "schema_version")
            if version is not None:
                records = self.store.record_get(self.namespace, "records")
                if records is not None:
                    payload = {"schema_version": version, "records": records}
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
            merged.pop("schema_version", None)
            merged.pop("records", None)
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

    def revision(self) -> float | None:
        with self._lock:
            self._ensure_records()
            return self.store.namespace_revision(self.namespace)

    def inspect_for_history(self, *, max_pages=10):
        """Bounded projection, never accounting truth or a read-triggered migration.

        Unknown-scope corruption produces ONLY a generic completeness flag. No
        foreign row IDs, digests, parser messages or global counts leave here.
        """
        from codex_web.storage.state_store import parse_state_record_storage_key
        from codex_web.storage.storage_preservation import StoragePreservationRefused
        if not 1 <= max_pages <= 10:
            raise ValueError("invalid usage inspection page bound")
        reader = getattr(self.store, "inspect_storage_pages", None)
        if not callable(reader):
            return [], {"schema_version":"1.0", "complete":False,
                        "reason":"usage_unavailable", "continuation_available":False}
        records = {}
        legacy = {}
        cursor = None
        complete = True
        continuation = False
        try:
            with closing(reader(self.namespace, max_pages=max_pages)) as pages:
                for rows, cursor in pages:
                    for key, payload in rows:
                        try:
                            parsed = parse_state_record_storage_key(key)
                            value = json.loads(payload)
                            if parsed is None:
                                state = self._decode(value)
                                if len(state.records) > 1000:
                                    raise ValueError("legacy inspection bound")
                                legacy.update({r.id:r for r in state.records})
                            elif parsed[1] is None:
                                continue
                            elif parsed[1] in {"schema_version", "records"}:
                                complete = False
                            else:
                                record = AgentRuntimeUsage.model_validate(value)
                                if record.id != parsed[1]:
                                    raise ValueError("usage record key mismatch")
                                records[record.id] = record
                        except (ValueError, TypeError):
                            complete = False
                if cursor is not None:
                    continuation = True
                    complete = False
        except StoragePreservationRefused:
            complete = False
        # Match strict migration's legacy precedence without performing it.
        records.update(legacy)
        return list(records.values()), {
            "schema_version":"1.0", "complete":complete,
            "reason":"complete" if complete else "usage_unavailable",
            "continuation_available":continuation,
        }

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

    def reconcile_observed_call_count(self, record_id: str, fingerprint: str, *, event_has_usage: bool) -> tuple[AgentRuntimeUsage, bool]:
        """Idempotently settle one retained observation without stale overwrite."""
        with self._lock:
            self._ensure_records()
            changed=[]
            def reconcile(raw):
                record=AgentRuntimeUsage.model_validate(raw)
                if fingerprint not in record.event_fingerprints:
                    raise ValueError('Original usage observation changed')
                if (record.model_call_count is None and record.terminal_outcome!='unknown'
                        and (event_has_usage or any(getattr(record,key) is not None for key in (
                            'input_tokens','output_tokens','total_tokens','cost_usd')))):
                    changed.append(True)
                    return record.model_copy(update={'model_call_count':1}).model_dump(mode='json')
                return raw
            payload=self.store.record_update(self.namespace,record_id,reconcile,default=None)
        return AgentRuntimeUsage.model_validate(payload), bool(changed)

    def get(self, record_id: str) -> AgentRuntimeUsage | None:
        with self._lock:
            self._ensure_records()
            payload = self.store.record_get(self.namespace, record_id)
        return AgentRuntimeUsage.model_validate(payload) if payload is not None else None
