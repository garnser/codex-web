from __future__ import annotations

import time

from codex_web.agent_runtime import (
    AgentRuntimeAdapter,
    AgentRuntimeHealth,
    AgentRuntimeRegistration,
    AgentRuntimeSessionRequest,
    AgentRuntimeTurnRequest,
    AgentRuntimeUnsupportedCapability,
    AgentSession,
    AgentSessionStatus,
)
from codex_web.agent_providers import AgentProviderCapability
from codex_web.failures import (
    FailureReason,
    failure_from_exception,
)
from codex_web.identity import AuthenticationActor
from codex_web.observability import current_correlation
from codex_web.storage.agent_sessions import AgentSessionStore


class AgentRuntimeError(RuntimeError):
    reason_code = FailureReason.CONFIGURATION_MISSING_OR_INVALID


class AgentRuntimeNotFoundError(AgentRuntimeError):
    reason_code = FailureReason.RUNTIME_OFFLINE


class AgentRuntimeRegistry:
    def __init__(self) -> None:
        self._adapters: dict[tuple[str, str], AgentRuntimeAdapter] = {}
        self._registrations: dict[tuple[str, str], AgentRuntimeRegistration] = {}

    def register(
        self,
        adapter: AgentRuntimeAdapter,
        *,
        capability_revision: int = 1,
        sandbox_profiles: tuple[str, ...] = (),
        network_profiles: tuple[str, ...] = (),
        residency_tags: tuple[str, ...] = (),
        compliance_tags: tuple[str, ...] = (),
        max_session_cost_usd: float | None = None,
    ) -> None:
        key = (adapter.provider_id, adapter.runtime_id)
        registration = AgentRuntimeRegistration(
            provider_id=adapter.provider_id,
            runtime_id=adapter.runtime_id,
            runtime_type=adapter.runtime_type,
            capabilities=tuple(dict.fromkeys(adapter.capabilities)),
            capability_revision=capability_revision,
            sandbox_profiles=tuple(dict.fromkeys(item for item in sandbox_profiles if item)),
            network_profiles=tuple(dict.fromkeys(item for item in network_profiles if item)),
            residency_tags=tuple(dict.fromkeys(item for item in residency_tags if item)),
            compliance_tags=tuple(dict.fromkeys(item for item in compliance_tags if item)),
            max_session_cost_usd=max_session_cost_usd,
        )
        existing = self._adapters.get(key)
        if existing is not None and existing is not adapter:
            raise AgentRuntimeError(
                f"agent runtime already registered: {adapter.provider_id}/{adapter.runtime_id}"
            )
        existing_registration = self._registrations.get(key)
        if existing_registration is not None and existing_registration != registration:
            raise AgentRuntimeError(
                f"agent runtime registration changed without replacement: "
                f"{adapter.provider_id}/{adapter.runtime_id}"
            )
        self._adapters[key] = adapter
        self._registrations[key] = registration

    def list_registrations(self) -> tuple[AgentRuntimeRegistration, ...]:
        return tuple(
            self._registrations[key]
            for key in sorted(self._registrations)
        )

    def registration(
        self,
        provider_id: str,
        runtime_id: str,
    ) -> AgentRuntimeRegistration:
        registration = self._registrations.get((provider_id, runtime_id))
        if registration is None:
            raise AgentRuntimeNotFoundError(
                f"agent runtime not registered: {provider_id}/{runtime_id}"
            )
        return registration

    def get(self, provider_id: str, runtime_id: str) -> AgentRuntimeAdapter:
        adapter = self._adapters.get((provider_id, runtime_id))
        if adapter is None:
            raise AgentRuntimeNotFoundError(
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

    def mark_status(
        self,
        session_id: str,
        status: AgentSessionStatus,
        *,
        actor: AuthenticationActor,
        failure_reason: str | None = None,
    ) -> AgentSession:
        session = self.get(session_id, actor)
        updated = session.model_copy(
            update={
                "status": status,
                "failure_reason": failure_reason,
                "failure": None if status != AgentSessionStatus.FAILED else session.failure,
                "updated_at": time.time(),
            }
        )
        return self.store.upsert(updated)

    def find_by_native_id(
        self,
        provider_native_session_id: str,
        actor: AuthenticationActor,
        *,
        provider_id: str | None = None,
        runtime_id: str | None = None,
    ) -> AgentSession | None:
        return next(
            (
                item
                for item in self.list(actor)
                if item.provider_native_session_id == provider_native_session_id
                and (provider_id is None or item.provider_id == provider_id)
                and (runtime_id is None or item.runtime_id == runtime_id)
            ),
            None,
        )

    def adopt(
        self,
        *,
        provider_id: str,
        runtime_id: str,
        runtime_type: str,
        provider_native_session_id: str,
        request: AgentRuntimeSessionRequest,
        actor: AuthenticationActor,
        capability_snapshot: tuple[AgentProviderCapability, ...],
        capability_revision: int | None = None,
    ) -> AgentSession:
        existing = self.find_by_native_id(
            provider_native_session_id,
            actor,
            provider_id=provider_id,
            runtime_id=runtime_id,
        )
        if existing is not None:
            return existing
        now = time.time()
        if capability_revision is None:
            try:
                capability_revision = self.registry.registration(
                    provider_id,
                    runtime_id,
                ).capability_revision
            except AgentRuntimeError:
                capability_revision = 1
        return self.store.upsert(
            AgentSession(
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                provider_id=provider_id,
                runtime_id=runtime_id,
                runtime_type=runtime_type,
                provider_native_session_id=provider_native_session_id,
                project_id=request.project_id,
                resource_ids=request.resource_ids,
                execution_id=request.execution_id,
                assignment_id=request.assignment_id,
                execution_workspace_id=request.execution_workspace_id,
                worker_id=request.worker_id,
                model=request.model,
                model_class=request.model_class,
                capability_snapshot=capability_snapshot,
                capability_revision=capability_revision,
                status=AgentSessionStatus.READY,
                created_at=now,
                updated_at=now,
            )
        )

    async def create(
        self,
        *,
        provider_id: str,
        runtime_id: str,
        request: AgentRuntimeSessionRequest,
        actor: AuthenticationActor,
        capability_revision: int | None = None,
    ) -> AgentSession:
        adapter = self.registry.get(provider_id, runtime_id)
        self._require_capability(adapter, AgentProviderCapability.AGENT_EXECUTION)
        result = await adapter.create_session(request)
        if not result.provider_native_session_id:
            raise AgentRuntimeError(
                "runtime create_session returned no provider-native session id"
            )
        now = time.time()
        if capability_revision is None:
            capability_revision = self.registry.registration(
                provider_id,
                runtime_id,
            ).capability_revision
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
                "failure": None,
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
            context = current_correlation()
            failure = failure_from_exception(
                exc,
                source_subsystem="agent_runtime",
                default_reason=FailureReason.PROCESS_FAILURE,
                provider_id=session.provider_id,
                runtime_id=session.runtime_id,
                worker_id=session.worker_id,
                assignment_id=session.assignment_id,
                execution_id=session.execution_id,
                correlation_id=(
                    context.correlation_id if context is not None else None
                ),
                causation_id=(
                    context.causation_id if context is not None else None
                ),
                details={
                    "runtime_type": session.runtime_type,
                    "project_id": session.project_id,
                },
            )
            self.store.upsert(
                session.model_copy(
                    update={
                        "status": AgentSessionStatus.FAILED,
                        "failure_reason": failure.summary,
                        "failure": failure,
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

    async def compact(
        self,
        session_id: str,
        *,
        actor: AuthenticationActor,
    ):
        session = self.get(session_id, actor)
        adapter = self.registry.get(session.provider_id, session.runtime_id)
        self._require_capability(
            adapter,
            AgentProviderCapability.NATIVE_CONTEXT_COMPACTION,
        )
        native_id = session.provider_native_session_id
        if not native_id:
            raise AgentRuntimeError("agent session has no provider-native session id")
        return await adapter.compact_session(native_id)

    async def read_objective(
        self,
        session_id: str,
        *,
        actor: AuthenticationActor,
    ):
        session = self.get(session_id, actor)
        adapter = self.registry.get(session.provider_id, session.runtime_id)
        self._require_capability(
            adapter,
            AgentProviderCapability.NATIVE_EXECUTION_OBJECTIVES,
        )
        native_id = session.provider_native_session_id
        if not native_id:
            raise AgentRuntimeError("agent session has no provider-native session id")
        return await adapter.read_objective(native_id)

    async def set_objective(
        self,
        session_id: str,
        request: AgentRuntimeObjectiveRequest,
        *,
        actor: AuthenticationActor,
    ):
        session = self.get(session_id, actor)
        adapter = self.registry.get(session.provider_id, session.runtime_id)
        self._require_capability(
            adapter,
            AgentProviderCapability.NATIVE_EXECUTION_OBJECTIVES,
        )
        native_id = session.provider_native_session_id
        if not native_id:
            raise AgentRuntimeError("agent session has no provider-native session id")
        return await adapter.set_objective(native_id, request)

    async def clear_objective(
        self,
        session_id: str,
        *,
        actor: AuthenticationActor,
    ):
        session = self.get(session_id, actor)
        adapter = self.registry.get(session.provider_id, session.runtime_id)
        self._require_capability(
            adapter,
            AgentProviderCapability.NATIVE_EXECUTION_OBJECTIVES,
        )
        native_id = session.provider_native_session_id
        if not native_id:
            raise AgentRuntimeError("agent session has no provider-native session id")
        return await adapter.clear_objective(native_id)

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
