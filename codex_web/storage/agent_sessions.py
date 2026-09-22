from __future__ import annotations

from typing import Any, Callable

from codex_web.agent_runtime import (
    AGENT_SESSION_STATE_CONTRACT,
    AgentSession,
    AgentSessionState,
)
from codex_web.compatibility import MigrationRegistry
from codex_web.storage.sqlite_state import SQLiteStateStore


AGENT_SESSION_MIGRATIONS = MigrationRegistry("agent-session-state")
AGENT_SESSION_MIGRATIONS.register(
    "0.0",
    "1.0",
    lambda payload: {
        "schema_version": "1.0",
        "sessions": list(payload.get("sessions", [])),
    },
)
AGENT_SESSION_MIGRATIONS.register(
    "1.0",
    "1.1",
    lambda payload: {
        **payload,
        "schema_version": "1.1",
        "sessions": [
            {**dict(item), "failure": dict(item).get("failure")}
            for item in payload.get("sessions", [])
        ],
    },
)


class AgentSessionNotFoundError(KeyError):
    pass


class AgentSessionConflictError(RuntimeError):
    pass


class AgentSessionStore:
    namespace = "agent_sessions"

    def __init__(self, store: SQLiteStateStore) -> None:
        self.store = store

    def _decode(self, payload: Any) -> AgentSessionState:
        if payload is None:
            return AgentSessionState()
        if not isinstance(payload, dict):
            raise ValueError("agent session state must be an object")
        version = str(payload.get("schema_version") or "0.0")
        if version != AGENT_SESSION_STATE_CONTRACT.current:
            payload = AGENT_SESSION_MIGRATIONS.migrate(
                payload,
                from_version=version,
                to_version=AGENT_SESSION_STATE_CONTRACT.current,
            )
        AGENT_SESSION_STATE_CONTRACT.require(payload.get("schema_version", ""))
        return AgentSessionState.model_validate(payload)

    def load(self) -> AgentSessionState:
        return self._decode(self.store.get(self.namespace))

    def update(
        self,
        updater: Callable[[AgentSessionState], AgentSessionState],
    ) -> AgentSessionState:
        payload = self.store.update(
            self.namespace,
            lambda raw: updater(self._decode(raw)).model_dump(mode="json"),
            default=AgentSessionState().model_dump(mode="json"),
        )
        return self._decode(payload)

    def list(self) -> list[AgentSession]:
        return sorted(
            self.load().sessions,
            key=lambda item: (
                item.organization_id,
                item.workspace_id,
                item.created_at,
                item.id,
            ),
        )

    def get(
        self,
        session_id: str,
        *,
        organization_id: str,
        workspace_id: str,
    ) -> AgentSession:
        item = next(
            (
                session
                for session in self.load().sessions
                if session.id == session_id
                and session.organization_id == organization_id
                and session.workspace_id == workspace_id
            ),
            None,
        )
        if item is None:
            raise AgentSessionNotFoundError(session_id)
        return item

    def find_by_native_id(
        self,
        provider_native_session_id: str,
        *,
        organization_id: str,
        workspace_id: str,
    ) -> AgentSession | None:
        return next(
            (
                session
                for session in self.load().sessions
                if session.provider_native_session_id == provider_native_session_id
                and session.organization_id == organization_id
                and session.workspace_id == workspace_id
            ),
            None,
        )

    def upsert(self, session: AgentSession) -> AgentSession:
        result: dict[str, AgentSession] = {}

        def apply(state: AgentSessionState) -> AgentSessionState:
            collision = next(
                (
                    item
                    for item in state.sessions
                    if item.id == session.id
                    and (
                        item.organization_id != session.organization_id
                        or item.workspace_id != session.workspace_id
                    )
                ),
                None,
            )
            if collision is not None:
                raise AgentSessionConflictError(
                    "canonical agent session id already exists in another tenant scope"
                )
            state.sessions = [
                session
                if (
                    item.id == session.id
                    and item.organization_id == session.organization_id
                    and item.workspace_id == session.workspace_id
                )
                else item
                for item in state.sessions
            ]
            if not any(
                item.id == session.id
                and item.organization_id == session.organization_id
                and item.workspace_id == session.workspace_id
                for item in state.sessions
            ):
                state.sessions.append(session)
            result["value"] = session
            return state

        self.update(apply)
        return result["value"]
