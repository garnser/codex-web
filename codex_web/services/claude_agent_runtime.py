from __future__ import annotations

from collections.abc import Callable
from typing import Any

from codex_web.agent_providers import AgentProviderCapability
from codex_web.agent_runtime import (
    AgentRuntimeEvent,
    AgentRuntimeHealth,
    AgentRuntimeListRequest,
    AgentRuntimeResult,
    AgentRuntimeSessionRequest,
    AgentRuntimeTurnRequest,
    AgentRuntimeUnsupportedCapability,
)


class ClaudeAgentRuntimeAdapter:
    """Translate canonical AgentRuntime operations to the Claude Agent SDK bridge.

    Provider-native protocol remains behind the injected transport. The adapter
    deliberately does not expose Claude-native permission state as canonical
    authority; approval requests are projected into the shared runtime event /
    server-request path for codex-web to authorize.
    """

    provider_id = "anthropic"
    runtime_id = "claude-agent-sdk"
    runtime_type = "claude-agent-sdk"
    capabilities = (
        AgentProviderCapability.AGENT_EXECUTION,
        AgentProviderCapability.PERSISTENT_SESSIONS,
        AgentProviderCapability.STREAMING,
        AgentProviderCapability.INTERRUPT_CANCEL,
        AgentProviderCapability.FILESYSTEM_EDITING,
        AgentProviderCapability.SHELL_TOOLS,
        AgentProviderCapability.GIT_OPERATIONS,
        AgentProviderCapability.INTERACTIVE_APPROVALS,
        AgentProviderCapability.USAGE_PARTIAL,
    )

    def __init__(self, transport: Any) -> None:
        self.transport = transport

    async def health(self) -> AgentRuntimeHealth:
        status = getattr(self.transport, "status", None)
        if callable(status):
            value = status()
            if getattr(value, "last_error", None):
                return AgentRuntimeHealth.DEGRADED
            if getattr(value, "ready", True) is False:
                return AgentRuntimeHealth.UNAVAILABLE
        proc = getattr(self.transport, "proc", None)
        if proc is not None and getattr(proc, "poll", lambda: None)() is not None:
            return AgentRuntimeHealth.UNAVAILABLE
        return AgentRuntimeHealth.HEALTHY

    def _event_hub(self):
        host = getattr(self.transport, "host", None)
        if host is None:
            runtime = getattr(self.transport, "runtime", None)
            host = getattr(runtime, "host", None)
        return getattr(host, "hub", None)

    def subscribe_events(
        self,
        listener: Callable[[AgentRuntimeEvent], None],
    ) -> Callable[[], None]:
        hub = self._event_hub()
        if hub is None:
            raise RuntimeError("Claude runtime event hub is unavailable")

        def project(event: dict[str, Any]) -> None:
            if event.get("type") != "claude.event":
                return
            message = event.get("message") or {}
            if not isinstance(message, dict):
                return
            params = message.get("params") or {}
            if not isinstance(params, dict):
                params = {}
            payload = params.get("payload")
            if not isinstance(payload, dict):
                payload = params
            listener(
                AgentRuntimeEvent(
                    event_type=str(
                        params.get("event_type")
                        or message.get("method")
                        or "claude.event"
                    ),
                    provider_native_session_id=(
                        str(params["session_id"])
                        if params.get("session_id")
                        else None
                    ),
                    provider_native_turn_id=(
                        str(params["turn_id"])
                        if params.get("turn_id")
                        else None
                    ),
                    payload=payload,
                )
            )

        hub.subscribe(project)

        def unsubscribe() -> None:
            hub.unsubscribe(project)

        return unsubscribe

    async def recover(self) -> AgentRuntimeHealth:
        ensure_started = getattr(self.transport, "ensure_started", None)
        if callable(ensure_started):
            await ensure_started()
        return await self.health()

    async def shutdown(self) -> None:
        stop = getattr(self.transport, "stop", None)
        if not callable(stop):
            raise RuntimeError("Claude runtime shutdown is unavailable")
        await stop()

    @staticmethod
    def _result(response: Any, *, session_id: str | None = None) -> AgentRuntimeResult:
        payload = response if isinstance(response, dict) else {}
        native_session_id = payload.get("session_id") or session_id
        native_turn_id = payload.get("turn_id")
        return AgentRuntimeResult(
            provider_native_session_id=(
                str(native_session_id) if native_session_id else None
            ),
            provider_native_turn_id=(
                str(native_turn_id) if native_turn_id else None
            ),
            payload=payload,
        )

    async def list_sessions(
        self,
        request: AgentRuntimeListRequest,
    ) -> AgentRuntimeResult:
        response = await self.transport.request(
            "session/list",
            {
                "cwd": request.workspace_cwd,
                "archived": request.archived,
                "search": request.search,
                "limit": request.limit,
            },
        )
        return self._result(response)

    async def create_session(
        self,
        request: AgentRuntimeSessionRequest,
    ) -> AgentRuntimeResult:
        response = await self.transport.request(
            "session/create",
            {
                "project_id": request.project_id,
                "cwd": request.workspace_cwd,
                "sandbox": request.sandbox,
                "approval_policy": request.approval_policy,
                "approval_reviewer": request.approval_reviewer,
                "sandbox_policy": request.sandbox_policy,
                "execution_id": request.execution_id,
                "assignment_id": request.assignment_id,
                "execution_workspace_id": request.execution_workspace_id,
                "worker_id": request.worker_id,
                "model": request.model,
                "model_class": request.model_class,
                "developer_instructions": request.developer_instructions,
                "resource_ids": list(request.resource_ids),
            },
        )
        return self._result(response)

    async def resume_session(
        self,
        provider_native_session_id: str,
        request: AgentRuntimeSessionRequest,
    ) -> AgentRuntimeResult:
        response = await self.transport.request(
            "session/resume",
            {
                "session_id": provider_native_session_id,
                "project_id": request.project_id,
                "cwd": request.workspace_cwd,
                "sandbox": request.sandbox,
                "approval_policy": request.approval_policy,
                "approval_reviewer": request.approval_reviewer,
                "sandbox_policy": request.sandbox_policy,
                "execution_id": request.execution_id,
                "assignment_id": request.assignment_id,
                "execution_workspace_id": request.execution_workspace_id,
                "worker_id": request.worker_id,
                "model": request.model,
                "model_class": request.model_class,
                "developer_instructions": request.developer_instructions,
                "resource_ids": list(request.resource_ids),
            },
        )
        return self._result(response, session_id=provider_native_session_id)

    async def read_session(
        self,
        provider_native_session_id: str,
    ) -> AgentRuntimeResult:
        response = await self.transport.request(
            "session/read",
            {"session_id": provider_native_session_id},
        )
        return self._result(response, session_id=provider_native_session_id)

    async def close_session(
        self,
        provider_native_session_id: str,
    ) -> AgentRuntimeResult:
        response = await self.transport.request(
            "session/close",
            {"session_id": provider_native_session_id},
        )
        return self._result(response, session_id=provider_native_session_id)

    async def restore_session(
        self,
        provider_native_session_id: str,
    ) -> AgentRuntimeResult:
        response = await self.transport.request(
            "session/restore",
            {"session_id": provider_native_session_id},
        )
        return self._result(response, session_id=provider_native_session_id)

    async def compact_session(
        self,
        provider_native_session_id: str,
    ) -> AgentRuntimeResult:
        del provider_native_session_id
        raise AgentRuntimeUnsupportedCapability(
            AgentProviderCapability.NATIVE_CONTEXT_COMPACTION
        )

    async def respond_approval(
        self,
        request_id: int | str,
        result: dict[str, Any],
    ) -> None:
        respond = getattr(self.transport, "respond_to_server_request", None)
        if not callable(respond):
            raise RuntimeError("Claude runtime approval response is unavailable")
        await respond(request_id, result)

    async def start_turn(
        self,
        provider_native_session_id: str,
        request: AgentRuntimeTurnRequest,
    ) -> AgentRuntimeResult:
        response = await self.transport.request(
            "turn/start",
            {
                "session_id": provider_native_session_id,
                "message": request.message,
                "model": request.model,
                "reasoning_effort": request.reasoning_effort,
                "cwd": request.workspace_cwd,
                "approval_policy": request.approval_policy,
                "approval_reviewer": request.approval_reviewer,
                "sandbox_policy": request.sandbox_policy,
                "developer_instructions": request.developer_instructions,
            },
        )
        return self._result(response, session_id=provider_native_session_id)

    async def interrupt(
        self,
        provider_native_session_id: str,
    ) -> AgentRuntimeResult:
        response = await self.transport.request(
            "turn/interrupt",
            {"session_id": provider_native_session_id},
        )
        return self._result(response, session_id=provider_native_session_id)
