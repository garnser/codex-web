from __future__ import annotations

from typing import Any, Callable

from codex_web.agent_profiles import (
    AGENT_PROFILE_STATE_CONTRACT,
    AgentProfileRevision,
    AgentProfileState,
)
from codex_web.compatibility import MigrationRegistry
from codex_web.storage.sqlite_state import SQLiteStateStore


AGENT_PROFILE_MIGRATIONS = MigrationRegistry("agent-profile-state")
AGENT_PROFILE_MIGRATIONS.register(
    "0.0",
    "1.0",
    lambda payload: {
        "schema_version": AGENT_PROFILE_STATE_CONTRACT.current,
        "revisions": list(payload.get("revisions", [])),
    },
)


class AgentProfileStore:
    namespace = "agent_profiles"

    def __init__(self, store: SQLiteStateStore) -> None:
        self.store = store

    def _decode(self, payload: Any) -> AgentProfileState:
        if payload is None:
            return AgentProfileState()
        if not isinstance(payload, dict):
            raise ValueError("agent profile state must be an object")
        version = str(payload.get("schema_version") or "0.0")
        if version != AGENT_PROFILE_STATE_CONTRACT.current:
            payload = AGENT_PROFILE_MIGRATIONS.migrate(
                payload,
                from_version=version,
                to_version=AGENT_PROFILE_STATE_CONTRACT.current,
            )
        AGENT_PROFILE_STATE_CONTRACT.require(
            payload.get("schema_version", "")
        )
        return AgentProfileState.model_validate(payload)

    def load(self) -> AgentProfileState:
        return self._decode(self.store.get(self.namespace))

    def update(
        self,
        updater: Callable[[AgentProfileState], AgentProfileState],
    ) -> AgentProfileState:
        payload = self.store.update(
            self.namespace,
            lambda raw: updater(
                self._decode(raw)
            ).model_dump(mode="json"),
            default=AgentProfileState().model_dump(mode="json"),
        )
        return self._decode(payload)

    def list_revisions(
        self,
        *,
        organization_id: str,
        workspace_id: str,
        profile_id: str | None = None,
    ) -> list[AgentProfileRevision]:
        values = [
            item
            for item in self.load().revisions
            if item.organization_id == organization_id
            and item.workspace_id == workspace_id
            and (
                profile_id is None
                or item.profile_id == profile_id
            )
        ]
        return sorted(
            values,
            key=lambda item: (
                item.profile_id,
                item.revision,
            ),
        )

    def latest(
        self,
        profile_id: str,
        *,
        organization_id: str,
        workspace_id: str,
    ) -> AgentProfileRevision | None:
        values = self.list_revisions(
            organization_id=organization_id,
            workspace_id=workspace_id,
            profile_id=profile_id,
        )
        return values[-1] if values else None

    def revision(
        self,
        profile_id: str,
        revision: int,
        *,
        organization_id: str,
        workspace_id: str,
    ) -> AgentProfileRevision | None:
        return next(
            (
                item
                for item in self.list_revisions(
                    organization_id=organization_id,
                    workspace_id=workspace_id,
                    profile_id=profile_id,
                )
                if item.revision == revision
            ),
            None,
        )

    def append(
        self,
        record: AgentProfileRevision,
    ) -> AgentProfileRevision:
        def apply(state: AgentProfileState) -> AgentProfileState:
            same_scope = [
                item
                for item in state.revisions
                if item.organization_id == record.organization_id
                and item.workspace_id == record.workspace_id
                and item.profile_id == record.profile_id
            ]
            expected = (
                max(
                    (item.revision for item in same_scope),
                    default=0,
                )
                + 1
            )
            if record.revision != expected:
                raise ValueError(
                    "agent profile revision conflict"
                )
            state.revisions.append(record)
            return state

        self.update(apply)
        return record
