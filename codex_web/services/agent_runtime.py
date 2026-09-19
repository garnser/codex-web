from __future__ import annotations

import time

from codex_web.agent_runtime import (
    AgentRuntimeAdapter,
    AgentRuntimeHealth,
    AgentRuntimeSessionRequest,
    AgentRuntimeTurnRequest,
    AgentRuntimeUnsupportedCapability,
    AgentSession,
    AgentSessionStatus,
)
from codex_web.agent_providers import AgentProviderCapability
from codex_web.identity import AuthenticationActor
from codex_web.storage.agent_sessions import AgentSessionStore


class AgentRuntimeError(RuntimeError):
    pass


class AgentRuntimeRegistry:
    def __init__(self) -> None:
        self._adapters: dict[tuple[str, str], AgentRuntimeAdapter] = {}

    def register(self, adapter: AgentRuntimeAdapter) -> None:
        key = (adapter.provider_id, adapter.runtime_id)
        existing = self._adapters.get(key)
        if existing is not None and existing is not adapter:
            raise AgentRuntimeError(
                f"agent runtime already registered: {adapter.provider_id}/{adapter.runtime_id}"
            )
        self._adapters[key] = adapter

    def get(self, provider_id: str, runtime_id: str) -> AgentRuntimeAdapter:
        adapter = self._adapters.get((provider_id, runtime_id))
        if adapter is None:
            raise AgentRuntimeError(
                f"agent runtime not registered: {provider_id}/{runtime_id}"
            )
        return adapter


class AgentSessionService:
    """Durable provider-neutral session lifecycle and provider-ID mapping."""

    def __init__(self, store: AgentSessionStore, registry: AgentRuntimeRegistry) -> None:
        self.store = store
        self.registry = registry

    @staticmethod
    def _require_capability(
        adapter: AgentRuntimeAdapter,
        capability: AgentProviderCapability,
    ) -> None:
        if capability not in set(adapter.capabilities):
            raise AgentRuntimeUnsupportedCapability(capability)

    def list(self, actor: AuthenticationActor) -> list[AgentSession]:
        return [
            item
            for item in self.store.list()
            if item.organization_id == actor.organization_id
            and item.workspace_id == actor.workspace_id
        ]

    def get(self, session_id: str, actor: AuthenticationActor) -> AgentSession:
        return self.store.get(
            session_id,
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
        )

    async def create(
        self,
        *,
        provider_id: str,
        runtime_id: str,
        request: AgentRuntimeSessionRequest,
        actor: AuthenticationActor,
        capability_revision: int = 1,
    ) -> AgentSession:
        adapter = self.registry.get(provider_id, runtime_id)
        self._require_capability(adapter, AgentProviderCapability.AGENT_EXECUTION)
        result = await adapter.create_session(request)
        if not result.provider_native_session_id:
            raise AgentRuntimeError(
                "runtime create_session returned no provider-native session id"
            )
        now = time.time()
        session = AgentSession(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            provider_id=provider_id,
            runtime_id=runtime_id,
            runtime_type=adapter.runtime_type,
            provider_native_session_id=result.provider_native_session_id,
            project_id=request.project_id,
            resource_ids=request.resource_ids,
            execution_id=request.execution_id,
            assignment_id=request.assignment_id,
            execution_workspace_id=request.execution_workspace_id,
            worker_id=request.worker_id,
            model=request.model,
            model_class=request.model_class,
            capability_snapshot=adapter.capabilities,
            capability_revision=capability_revision,
            status=AgentSessionStatus.READY,
            created_at=now,
            updated_at=now,
        )
        return self.store.upsert(session)

    async def resume(
        self,
        session_id: str,
        *,
        request: AgentRuntimeSessionRequest,
        actor: AuthenticationActor,
    ) -> AgentSession:
        session = self.get(session_id, actor)
        adapter = self.registry.get(session.provider_id, session.runtime_id)
        self._require_capability(adapter, AgentProviderCapability.PERSISTENT_SESSIONS)
        native_id = session.provider_native_session_id
        if not native_id:
            raise AgentRuntimeError("agent session has no provider-native session id")
        result = await adapter.resume_session(native_id, request)
        now = time.time()
        updated = session.model_copy(
            update={
                "provider_native_session_id": (
                    result.provider_native_session_id or native_id
                ),
                "status": AgentSessionStatus.READY,
                "recovery_attempts": session.recovery_attempts + 1,
                "last_recovered_at": now,
                "failure_reason": None,
                "updated_at": now,
            }
        )
        return self.store.upsert(updated)

    async def start_turn(
        self,
        session_id: str,
        request: AgentRuntimeTurnRequest,
        *,
        actor: AuthenticationActor,
    ):
        session = self.get(session_id, actor)
        adapter = self.registry.get(session.provider_id, session.runtime_id)
        native_id = session.provider_native_session_id
        if not native_id:
            raise AgentRuntimeError("agent session has no provider-native session id")
        self.store.upsert(
            session.model_copy(
                update={
                    "status": AgentSessionStatus.RUNNING,
                    "updated_at": time.time(),
                }
            )
        )
        try:
            return await adapter.start_turn(native_id, request)
        except Exception as exc:
            self.store.upsert(
                session.model_copy(
                    update={
                        "status": AgentSessionStatus.FAILED,
                        "failure_reason": str(exc)[:500],
                        "updated_at": time.time(),
                    }
                )
            )
            raise

    async def interrupt(
        self,
        session_id: str,
        *,
        actor: AuthenticationActor,
    ):
        session = self.get(session_id, actor)
        adapter = self.registry.get(session.provider_id, session.runtime_id)
        self._require_capability(adapter, AgentProviderCapability.INTERRUPT_CANCEL)
        native_id = session.provider_native_session_id
        if not native_id:
            raise AgentRuntimeError("agent session has no provider-native session id")
        result = await adapter.interrupt(native_id)
        self.store.upsert(
            session.model_copy(
                update={
                    "status": AgentSessionStatus.INTERRUPTED,
                    "updated_at": time.time(),
                }
            )
        )
        return result

    async def close(
        self,
        session_id: str,
        *,
        actor: AuthenticationActor,
    ) -> AgentSession:
        session = self.get(session_id, actor)
        adapter = self.registry.get(session.provider_id, session.runtime_id)
        native_id = session.provider_native_session_id
        if native_id:
            await adapter.close_session(native_id)
        updated = session.model_copy(
            update={
                "status": AgentSessionStatus.CLOSED,
                "updated_at": time.time(),
            }
        )
        return self.store.upsert(updated)

    async def health(
        self,
        provider_id: str,
        runtime_id: str,
    ) -> AgentRuntimeHealth:
        return await self.registry.get(provider_id, runtime_id).health()
