from __future__ import annotations

from typing import Any, Callable

from codex_web.agent_teams import (
    AGENT_TEAM_STATE_CONTRACT,
    AgentTeamDelegationRecord,
    AgentTeamRevision,
    AgentTeamState,
)
from codex_web.compatibility import MigrationRegistry
from codex_web.storage.sqlite_state import SQLiteStateStore


AGENT_TEAM_MIGRATIONS = MigrationRegistry("agent-team-state")
AGENT_TEAM_MIGRATIONS.register(
    "0.0",
    "1.0",
    lambda payload: {
        "schema_version": "1.0",
        "revisions": list(payload.get("revisions", [])),
    },
)
AGENT_TEAM_MIGRATIONS.register(
    "1.0",
    "1.1",
    lambda payload: {
        "schema_version": AGENT_TEAM_STATE_CONTRACT.current,
        "revisions": list(payload.get("revisions", [])),
        "delegations": list(payload.get("delegations", [])),
    },
)


class AgentTeamStore:
    namespace = "agent_teams"

    def __init__(self, store: SQLiteStateStore) -> None:
        self.store = store

    def _decode(self, payload: Any) -> AgentTeamState:
        if payload is None:
            return AgentTeamState()
        if not isinstance(payload, dict):
            raise ValueError("agent team state must be an object")
        version = str(payload.get("schema_version") or "0.0")
        if version != AGENT_TEAM_STATE_CONTRACT.current:
            payload = AGENT_TEAM_MIGRATIONS.migrate(
                payload,
                from_version=version,
                to_version=AGENT_TEAM_STATE_CONTRACT.current,
            )
        AGENT_TEAM_STATE_CONTRACT.require(payload.get("schema_version", ""))
        return AgentTeamState.model_validate(payload)

    def load(self) -> AgentTeamState:
        return self._decode(self.store.get(self.namespace))

    def update(
        self,
        updater: Callable[[AgentTeamState], AgentTeamState],
    ) -> AgentTeamState:
        payload = self.store.update(
            self.namespace,
            lambda raw: updater(self._decode(raw)).model_dump(mode="json"),
            default=AgentTeamState().model_dump(mode="json"),
        )
        return self._decode(payload)

    def list_revisions(
        self,
        *,
        organization_id: str,
        workspace_id: str,
        team_id: str | None = None,
    ) -> list[AgentTeamRevision]:
        values = [
            item
            for item in self.load().revisions
            if item.organization_id == organization_id
            and item.workspace_id == workspace_id
            and (team_id is None or item.team_id == team_id)
        ]
        return sorted(values, key=lambda item: (item.team_id, item.revision))

    def latest(
        self,
        team_id: str,
        *,
        organization_id: str,
        workspace_id: str,
    ) -> AgentTeamRevision | None:
        items = self.list_revisions(
            organization_id=organization_id,
            workspace_id=workspace_id,
            team_id=team_id,
        )
        return items[-1] if items else None

    def append(self, record: AgentTeamRevision) -> AgentTeamRevision:
        def apply(state: AgentTeamState) -> AgentTeamState:
            same = [
                item
                for item in state.revisions
                if item.organization_id == record.organization_id
                and item.workspace_id == record.workspace_id
                and item.team_id == record.team_id
            ]
            expected = max((item.revision for item in same), default=0) + 1
            if record.revision != expected:
                raise ValueError("agent team revision conflict")
            state.revisions.append(record)
            return state

        self.update(apply)
        return record


    def list_delegations(
        self,
        *,
        organization_id: str,
        workspace_id: str,
        work_item_id: str | None = None,
        team_id: str | None = None,
    ) -> list[AgentTeamDelegationRecord]:
        values = [
            item
            for item in self.load().delegations
            if item.organization_id == organization_id
            and item.workspace_id == workspace_id
            and (work_item_id is None or item.work_item_id == work_item_id)
            and (team_id is None or item.team_id == team_id)
        ]
        return sorted(values, key=lambda item: (item.created_at, item.id))

    def delegation_by_dedupe_key(
        self,
        dedupe_key: str,
        *,
        organization_id: str,
        workspace_id: str,
    ) -> AgentTeamDelegationRecord | None:
        return next(
            (
                item
                for item in self.load().delegations
                if item.organization_id == organization_id
                and item.workspace_id == workspace_id
                and item.dedupe_key == dedupe_key
            ),
            None,
        )

    def append_delegation(
        self,
        record: AgentTeamDelegationRecord,
    ) -> AgentTeamDelegationRecord:
        result: dict[str, AgentTeamDelegationRecord] = {}

        def apply(state: AgentTeamState) -> AgentTeamState:
            existing = next(
                (
                    item
                    for item in state.delegations
                    if item.organization_id == record.organization_id
                    and item.workspace_id == record.workspace_id
                    and item.dedupe_key == record.dedupe_key
                ),
                None,
            )
            if existing is not None:
                result["value"] = existing
                return state
            state.delegations.append(record)
            result["value"] = record
            return state

        self.update(apply)
        return result["value"]

    def update_delegation(
        self,
        record_id: str,
        *,
        organization_id: str,
        workspace_id: str,
        updater: Callable[[AgentTeamDelegationRecord], AgentTeamDelegationRecord],
    ) -> AgentTeamDelegationRecord:
        result: dict[str, AgentTeamDelegationRecord] = {}

        def apply(state: AgentTeamState) -> AgentTeamState:
            for index, item in enumerate(state.delegations):
                if (
                    item.id == record_id
                    and item.organization_id == organization_id
                    and item.workspace_id == workspace_id
                ):
                    updated = updater(item)
                    state.delegations[index] = updated
                    result["value"] = updated
                    return state
            raise KeyError(record_id)

        self.update(apply)
        return result["value"]
