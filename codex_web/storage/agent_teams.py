from __future__ import annotations

from typing import Any, Callable

from codex_web.agent_teams import (
    AGENT_TEAM_STATE_CONTRACT,
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
        "schema_version": AGENT_TEAM_STATE_CONTRACT.current,
        "revisions": list(payload.get("revisions", [])),
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
