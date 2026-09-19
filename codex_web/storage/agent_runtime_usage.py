from __future__ import annotations

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


class AgentRuntimeUsageStore:
    namespace = "agent_runtime_usage"

    def __init__(self, store: SQLiteStateStore) -> None:
        self.store = store

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

    def load(self) -> AgentRuntimeUsageState:
        return self._decode(self.store.get(self.namespace))

    def update(
        self,
        updater: Callable[[AgentRuntimeUsageState], AgentRuntimeUsageState],
    ) -> AgentRuntimeUsageState:
        payload = self.store.update(
            self.namespace,
            lambda raw: updater(self._decode(raw)).model_dump(mode="json"),
            default=AgentRuntimeUsageState().model_dump(mode="json"),
        )
        return self._decode(payload)

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
        def apply(state: AgentRuntimeUsageState) -> AgentRuntimeUsageState:
            for existing in state.records:
                if (
                    existing.id == record.id
                    and (
                        existing.organization_id != record.organization_id
                        or existing.workspace_id != record.workspace_id
                    )
                ):
                    raise RuntimeError(
                        "runtime usage id already exists in another tenant scope"
                    )
            replaced = False
            records: list[AgentRuntimeUsage] = []
            for existing in state.records:
                if (
                    existing.id == record.id
                    and existing.organization_id == record.organization_id
                    and existing.workspace_id == record.workspace_id
                ):
                    records.append(record)
                    replaced = True
                else:
                    records.append(existing)
            if not replaced:
                records.append(record)
            state.records = records
            return state

        self.update(apply)
        return record
