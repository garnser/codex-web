from __future__ import annotations

import threading
from typing import Any, Callable
from urllib.parse import quote

from codex_web.agent_runtime import (
    AGENT_SESSION_STATE_CONTRACT,
    AgentSession,
    AgentSessionState,
)
from codex_web.compatibility import MigrationRegistry
from codex_web.storage.state_store import StateStore


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
    records_namespace = "agent_sessions.records.v1"
    _meta_key = "meta"

    def __init__(self, store: StateStore) -> None:
        self.store = store
        self._records_ready = False
        self._migration_lock = threading.RLock()

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

    @staticmethod
    def _part(value: str) -> str:
        return quote(str(value), safe="")

    @classmethod
    def _session_key(cls, session_id: str) -> str:
        return f"sessions:{cls._part(session_id)}"

    @classmethod
    def _native_prefix(
        cls,
        provider_native_session_id: str,
        *,
        provider_id: str | None = None,
        runtime_id: str | None = None,
    ) -> str:
        prefix = f"native:{cls._part(provider_native_session_id)}:"
        if provider_id is not None and runtime_id is not None:
            prefix += f"{cls._part(provider_id)}:{cls._part(runtime_id)}:"
        return prefix

    @classmethod
    def _native_key(cls, session: AgentSession) -> str | None:
        if not session.provider_native_session_id:
            return None
        return (
            cls._native_prefix(
                session.provider_native_session_id,
                provider_id=session.provider_id,
                runtime_id=session.runtime_id,
            )
            + f"{cls._part(session.organization_id)}:"
            + f"{cls._part(session.workspace_id)}:"
            + cls._part(session.id)
        )

    @classmethod
    def _records(cls, state: AgentSessionState) -> dict[str, Any]:
        records: dict[str, Any] = {}
        for session in state.sessions:
            session_key = cls._session_key(session.id)
            if session_key in records:
                raise AgentSessionConflictError(
                    "duplicate canonical agent session id in state"
                )
            records[session_key] = session.model_dump(mode="json")
            native_key = cls._native_key(session)
            if native_key is not None:
                records[native_key] = {"session_id": session.id}
        return records

    def _ensure_records(self) -> None:
        if self._records_ready:
            return
        with self._migration_lock:
            if self._records_ready:
                return
            if not self.store.record_collection_exists(self.records_namespace):
                self.store.record_mutate(
                    self.records_namespace,
                    (),
                    lambda _current: {},
                )
            meta = self.store.record_get(self.records_namespace, self._meta_key)
            if meta is None:
                # Upgrade both namespaces under the shared state-store lock.
                # The old document remains a rollback checkpoint, not a second
                # active authority.
                def migrate(documents: dict[str, Any]) -> dict[str, Any]:
                    records = documents[self.records_namespace]
                    if self._meta_key not in records:
                        records = self._records(
                            self._decode(documents[self.namespace])
                        )
                        records[self._meta_key] = {"schema_version": "1.0"}
                    return {
                        self.records_namespace: records,
                        self.namespace: documents[self.namespace],
                    }

                self.store.update_many(
                    {
                        self.namespace: AgentSessionState().model_dump(mode="json"),
                        self.records_namespace: {},
                    },
                    migrate,
                )
                meta = self.store.record_get(
                    self.records_namespace,
                    self._meta_key,
                )
            if meta != {"schema_version": "1.0"}:
                raise ValueError("unsupported agent session records schema")
            self._records_ready = True

    def _state(self, records: dict[str, Any]) -> AgentSessionState:
        return self._decode(
            {
                "schema_version": AGENT_SESSION_STATE_CONTRACT.current,
                "sessions": [
                    raw
                    for key, raw in sorted(records.items())
                    if key.startswith("sessions:") and raw is not None
                ],
            }
        )

    def load(self) -> AgentSessionState:
        self._ensure_records()
        return self._state(self.store.record_items(self.records_namespace))

    def update(
        self,
        updater: Callable[[AgentSessionState], AgentSessionState],
    ) -> AgentSessionState:
        self._ensure_records()
        keys = tuple(self.store.record_items(self.records_namespace))
        result: list[AgentSessionState] = []

        def apply(current: dict[str, Any | None]) -> dict[str, Any | None]:
            current_state = self._state(current)
            before = self._records(current_state)
            updated = updater(current_state)
            after = self._records(updated)
            changes: dict[str, Any | None] = {
                key: raw for key, raw in after.items() if before.get(key) != raw
            }
            changes.update(
                {key: None for key in before.keys() - after.keys()}
            )
            result.append(updated)
            return changes

        self.store.record_mutate(self.records_namespace, keys, apply)
        return result[0]

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
        self._ensure_records()
        raw = self.store.record_get(
            self.records_namespace,
            self._session_key(session_id),
        )
        item = AgentSession.model_validate(raw) if raw is not None else None
        if (
            item is None
            or item.organization_id != organization_id
            or item.workspace_id != workspace_id
        ):
            raise AgentSessionNotFoundError(session_id)
        return item

    def _native_matches(
        self,
        provider_native_session_id: str,
        *,
        provider_id: str | None = None,
        runtime_id: str | None = None,
        limit: int | None = None,
    ) -> list[AgentSession]:
        self._ensure_records()
        exact_runtime = provider_id is not None and runtime_id is not None
        prefix = self._native_prefix(
            provider_native_session_id,
            provider_id=provider_id if exact_runtime else None,
            runtime_id=runtime_id if exact_runtime else None,
        )
        matches: list[AgentSession] = []
        after: str | None = None
        while limit is None or len(matches) < limit:
            page_limit = min(100, (limit - len(matches)) if limit else 100)
            page, after = self.store.record_page(
                self.records_namespace,
                key_prefix=prefix,
                after=after,
                limit=page_limit,
            )
            for alias_key, alias in page.items():
                if not isinstance(alias, dict) or not alias.get("session_id"):
                    raise ValueError("invalid agent session native index")
                raw = self.store.record_get(
                    self.records_namespace,
                    self._session_key(str(alias["session_id"])),
                )
                if raw is None:
                    raise ValueError("agent session native index target is missing")
                session = AgentSession.model_validate(raw)
                if (
                    session.provider_native_session_id
                    != provider_native_session_id
                    or self._native_key(session) != alias_key
                ):
                    raise ValueError("agent session native index target mismatch")
                if (
                    (provider_id is not None and session.provider_id != provider_id)
                    or (runtime_id is not None and session.runtime_id != runtime_id)
                ):
                    continue
                matches.append(session)
                if limit is not None and len(matches) >= limit:
                    break
            if after is None:
                break
        return matches

    def find_by_native_id(
        self,
        provider_native_session_id: str,
        *,
        organization_id: str,
        workspace_id: str,
        provider_id: str | None = None,
        runtime_id: str | None = None,
    ) -> AgentSession | None:
        matches = sorted(
            (
                session
                for session in self._native_matches(
                    provider_native_session_id,
                    provider_id=provider_id,
                    runtime_id=runtime_id,
                )
                if session.organization_id == organization_id
                and session.workspace_id == workspace_id
            ),
            key=lambda session: (session.created_at, session.id),
        )
        return matches[0] if matches else None

    def find_unique_by_native_id(
        self,
        provider_native_session_id: str,
        *,
        provider_id: str,
        runtime_id: str,
    ) -> AgentSession | None:
        matches = self._native_matches(
            provider_native_session_id,
            provider_id=provider_id,
            runtime_id=runtime_id,
            limit=2,
        )
        # Provider-native IDs are compatibility metadata. Never guess when
        # the same identity is present in more than one canonical scope.
        return matches[0] if len(matches) == 1 else None

    def upsert(self, session: AgentSession) -> AgentSession:
        self._ensure_records()
        session_key = self._session_key(session.id)
        new_native_key = self._native_key(session)

        def apply(current: dict[str, Any | None]) -> dict[str, Any | None]:
            old_raw = current[session_key]
            old = AgentSession.model_validate(old_raw) if old_raw is not None else None
            if old is not None and (
                old.organization_id != session.organization_id
                or old.workspace_id != session.workspace_id
            ):
                raise AgentSessionConflictError(
                    "canonical agent session id already exists in another tenant scope"
                )
            changes: dict[str, Any | None] = {
                session_key: session.model_dump(mode="json")
            }
            old_native_key = self._native_key(old) if old is not None else None
            if old_native_key is not None and old_native_key != new_native_key:
                changes[old_native_key] = None
            if new_native_key is not None:
                changes[new_native_key] = {"session_id": session.id}
            return changes

        selected = (session_key,) + (
            (new_native_key,) if new_native_key is not None else ()
        )
        self.store.record_mutate(self.records_namespace, selected, apply)
        return session

    def flush_legacy_mirror(self) -> None:
        self.store.put(self.namespace, self.load().model_dump(mode="json"))
