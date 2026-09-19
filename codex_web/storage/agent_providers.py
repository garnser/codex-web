from __future__ import annotations

from typing import Any, Callable

from codex_web.agent_providers import (
    AGENT_PROVIDER_STATE_CONTRACT,
    AgentProviderRecord,
    AgentProviderState,
)
from codex_web.compatibility import MigrationRegistry
from codex_web.storage.sqlite_state import SQLiteStateStore


AGENT_PROVIDER_MIGRATIONS = MigrationRegistry("agent-provider-state")
AGENT_PROVIDER_MIGRATIONS.register(
    "0.0",
    "1.0",
    lambda payload: {
        "schema_version": AGENT_PROVIDER_STATE_CONTRACT.current,
        "providers": list(payload.get("providers", [])),
    },
)


class AgentProviderNotFoundError(KeyError):
    pass


class AgentProviderStore:
    namespace = "agent_providers"

    def __init__(self, store: SQLiteStateStore) -> None:
        self.store = store

    def _decode(self, payload: Any) -> AgentProviderState:
        if payload is None:
            return AgentProviderState()
        if not isinstance(payload, dict):
            raise ValueError("agent provider state must be an object")
        version = str(payload.get("schema_version") or "0.0")
        if version != AGENT_PROVIDER_STATE_CONTRACT.current:
            payload = AGENT_PROVIDER_MIGRATIONS.migrate(
                payload,
                from_version=version,
                to_version=AGENT_PROVIDER_STATE_CONTRACT.current,
            )
        AGENT_PROVIDER_STATE_CONTRACT.require(payload.get("schema_version", ""))
        return AgentProviderState.model_validate(payload)

    def load(self) -> AgentProviderState:
        return self._decode(self.store.get(self.namespace))

    def update(
        self,
        updater: Callable[[AgentProviderState], AgentProviderState],
    ) -> AgentProviderState:
        payload = self.store.update(
            self.namespace,
            lambda raw: updater(self._decode(raw)).model_dump(mode="json"),
            default=AgentProviderState().model_dump(mode="json"),
        )
        return self._decode(payload)

    def list(self) -> list[AgentProviderRecord]:
        return sorted(
            self.load().providers,
            key=lambda item: (item.organization_id, item.workspace_id, item.id),
        )

    def upsert(self, record: AgentProviderRecord) -> AgentProviderRecord:
        result: dict[str, AgentProviderRecord] = {}

        def apply(state: AgentProviderState) -> AgentProviderState:
            existing = next(
                (
                    item
                    for item in state.providers
                    if item.organization_id == record.organization_id
                    and item.workspace_id == record.workspace_id
                    and item.id == record.id
                ),
                None,
            )
            if existing is None:
                state.providers.append(record)
                result["value"] = record
                return state
            updated = record.model_copy(
                update={
                    "created_at": existing.created_at,
                    "created_by": existing.created_by,
                    "revision": existing.revision + 1,
                }
            )
            state.providers = [
                updated
                if (
                    item.organization_id == updated.organization_id
                    and item.workspace_id == updated.workspace_id
                    and item.id == updated.id
                )
                else item
                for item in state.providers
            ]
            result["value"] = updated
            return state

        self.update(apply)
        return result["value"]
