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
        AGENT_TEAM_STATE_CONTRACT.require(
            payload.get("schema_version", "")
        )
        return AgentTeamState.model_validate(payload)

    def load(self) -> AgentTeamState:
        return self._decode(self.store.get(self.namespace))

    def update(
        self,
        updater: Callable[[AgentTeamState], AgentTeamState],
    ) -> AgentTeamState:
        payload = self.store.update(
            self.namespace,
            lambda raw: updater(
                self._decode(raw)
            ).model_dump(mode="json"),
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
        return sorted(
            values,
            key=lambda item: (item.team_id, item.revision),
        )

    def latest(
        self,
        team_id: str,
        *,
        organization_id: str,
        workspace_id: str,
    ) -> AgentTeamRevision | None:
        values = self.list_revisions(
            organization_id=organization_id,
            workspace_id=workspace_id,
            team_id=team_id,
        )
        return values[-1] if values else None

    def revision(
        self,
        team_id: str,
        revision: int,
        *,
        organization_id: str,
        workspace_id: str,
    ) -> AgentTeamRevision | None:
        return next(
            (
                item
                for item in self.list_revisions(
                    organization_id=organization_id,
                    workspace_id=workspace_id,
                    team_id=team_id,
                )
                if item.revision == revision
            ),
            None,
        )

    def append_revision(
        self,
        record: AgentTeamRevision,
    ) -> AgentTeamRevision:
        def apply(state: AgentTeamState) -> AgentTeamState:
            same_scope = [
                item
                for item in state.revisions
                if item.organization_id == record.organization_id
                and item.workspace_id == record.workspace_id
                and item.team_id == record.team_id
            ]
            expected = (
                max(
                    (item.revision for item in same_scope),
                    default=0,
                )
                + 1
            )
            if record.revision != expected:
                raise ValueError("agent team revision conflict")
            state.revisions.append(record)
            return state

        self.update(apply)
        return record

    def append_delegation(
        self,
        record: AgentTeamDelegationRecord,
    ) -> AgentTeamDelegationRecord:
        def apply(state: AgentTeamState) -> AgentTeamState:
            if any(
                item.delegation_id == record.delegation_id
                for item in state.delegations
            ):
                raise ValueError("agent team delegation already exists")
            state.delegations.append(record)
            state.delegations = state.delegations[-10000:]
            return state

        self.update(apply)
        return record

    def get_delegation(
        self,
        delegation_id: str,
        *,
        organization_id: str,
        workspace_id: str,
    ) -> AgentTeamDelegationRecord | None:
        return next(
            (
                item
                for item in self.load().delegations
                if item.delegation_id == delegation_id
                and item.organization_id == organization_id
                and item.workspace_id == workspace_id
            ),
            None,
        )

    def update_delegation(
        self,
        record: AgentTeamDelegationRecord,
    ) -> AgentTeamDelegationRecord:
        replaced: list[AgentTeamDelegationRecord] = []

        def apply(state: AgentTeamState) -> AgentTeamState:
            found = False
            values: list[AgentTeamDelegationRecord] = []
            for item in state.delegations:
                if (
                    item.delegation_id == record.delegation_id
                    and item.organization_id == record.organization_id
                    and item.workspace_id == record.workspace_id
                ):
                    values.append(record)
                    found = True
                else:
                    values.append(item)
            if not found:
                raise ValueError("agent team delegation not found")
            state.delegations = values[-10000:]
            replaced.append(record)
            return state

        self.update(apply)
        return replaced[0]

    def list_delegations(
        self,
        *,
        organization_id: str,
        workspace_id: str,
        team_id: str | None = None,
        work_item_ref: str | None = None,
        limit: int = 50,
    ) -> list[AgentTeamDelegationRecord]:
        values = [
            item
            for item in self.load().delegations
            if item.organization_id == organization_id
            and item.workspace_id == workspace_id
            and (team_id is None or item.team_id == team_id)
            and (
                work_item_ref is None
                or item.work_item_ref == work_item_ref
            )
        ]
        values.sort(
            key=lambda item: (item.updated_at, item.delegation_id),
            reverse=True,
        )
        return values[: max(1, min(int(limit), 200))]
