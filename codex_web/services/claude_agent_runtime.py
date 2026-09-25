from __future__ import annotations

from collections.abc import Callable
from typing import Any

from codex_web.agent_providers import AgentProviderCapability
from codex_web.agent_runtime import (
    AgentRuntimeEvent,
    AgentRuntimeHealth,
    AgentRuntimeListRequest,
    AgentRuntimeObjectiveRequest,
    AgentRuntimeResult,
    AgentRuntimeSessionRequest,
    AgentRuntimeTurnRequest,
)


class ClaudeAgentRuntimeAdapter:
    """Translate the Claude execution runtime into the canonical AgentRuntime surface.

    Provider-native stream/control messages remain behind the transport. This
    adapter intentionally does not claim native context compaction or any
    provider-local permission authority.
    """

    provider_id = "anthropic"
    runtime_id = "claude-code"
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
        AgentProviderCapability.MCP_TOOL_SERVERS,
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

    @staticmethod
    def _native_session_id(message: dict[str, Any]) -> str | None:
        value = (
            message.get("session_id")
            or message.get("sessionId")
            or message.get("conversation_id")
        )
        return str(value) if value else None

    @staticmethod
    def _native_turn_id(message: dict[str, Any]) -> str | None:
        value = (
            message.get("turn_id")
            or message.get("turnId")
            or message.get("request_id")
        )
        if value:
            return str(value)
        result = message.get("result")
        if isinstance(result, dict):
            value = result.get("turn_id") or result.get("turnId")
            if value:
                return str(value)
        return None

    @classmethod
    def _event_type(cls, message: dict[str, Any]) -> str:
        message_type = str(message.get("type") or "claude.event")
        subtype = message.get("subtype")
        if subtype:
            return f"{message_type}/{subtype}"
        if message_type == "assistant":
            content = (message.get("message") or {}).get("content")
            if isinstance(content, list):
                block_types = {
                    str(block.get("type"))
                    for block in content
                    if isinstance(block, dict) and block.get("type")
                }
                if "tool_use" in block_types:
                    return "tool/requested"
                if "text" in block_types:
                    return "text/delta"
        return message_type

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
            message = event.get("message")
            if not isinstance(message, dict):
                return
            listener(
                AgentRuntimeEvent(
                    event_type=self._event_type(message),
                    provider_native_session_id=self._native_session_id(message),
                    provider_native_turn_id=self._native_turn_id(message),
                    payload=message,
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
    def _result(
        response: Any,
        *,
        fallback_session_id: str | None = None,
    ) -> AgentRuntimeResult:
        payload = response if isinstance(response, dict) else {}
        session_id = ClaudeAgentRuntimeAdapter._native_session_id(payload)
        turn_id = ClaudeAgentRuntimeAdapter._native_turn_id(payload)
        return AgentRuntimeResult(
            provider_native_session_id=session_id or fallback_session_id,
            provider_native_turn_id=turn_id,
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
        return AgentRuntimeResult(
            payload=response if isinstance(response, dict) else {},
        )

    @staticmethod
    def _session_params(request: AgentRuntimeSessionRequest) -> dict[str, Any]:
        return {
            "project_id": request.project_id,
            "cwd": request.workspace_cwd,
            "sandbox": request.sandbox,
            "sandbox_policy": request.sandbox_policy,
            "approval_policy": request.approval_policy,
            "approval_reviewer": request.approval_reviewer,
            "execution_id": request.execution_id,
            "assignment_id": request.assignment_id,
            "execution_workspace_id": request.execution_workspace_id,
            "worker_id": request.worker_id,
            "model": request.model,
            "model_class": request.model_class,
            "developer_instructions": request.developer_instructions,
            "resource_ids": list(request.resource_ids),
        }

    async def create_session(
        self,
        request: AgentRuntimeSessionRequest,
    ) -> AgentRuntimeResult:
        response = await self.transport.request(
            "session/create",
            self._session_params(request),
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
                **self._session_params(request),
            },
        )
        return self._result(
            response,
            fallback_session_id=provider_native_session_id,
        )

    async def read_session(
        self,
        provider_native_session_id: str,
    ) -> AgentRuntimeResult:
        response = await self.transport.request(
            "session/read",
            {"session_id": provider_native_session_id},
        )
        return self._result(
            response,
            fallback_session_id=provider_native_session_id,
        )

    async def close_session(
        self,
        provider_native_session_id: str,
    ) -> AgentRuntimeResult:
        response = await self.transport.request(
            "session/close",
            {"session_id": provider_native_session_id},
        )
        return self._result(
            response,
            fallback_session_id=provider_native_session_id,
        )

    async def restore_session(
        self,
        provider_native_session_id: str,
    ) -> AgentRuntimeResult:
        response = await self.transport.request(
            "session/restore",
            {"session_id": provider_native_session_id},
        )
        return self._result(
            response,
            fallback_session_id=provider_native_session_id,
        )

    async def compact_session(
        self,
        provider_native_session_id: str,
    ) -> AgentRuntimeResult:
        raise RuntimeError(
            "Claude runtime does not support native context compaction"
        )

    async def read_objective(
        self,
        provider_native_session_id: str,
    ) -> AgentRuntimeResult:
        del provider_native_session_id
        raise RuntimeError("Claude runtime does not support native execution objectives")

    async def set_objective(
        self,
        provider_native_session_id: str,
        request: AgentRuntimeObjectiveRequest,
    ) -> AgentRuntimeResult:
        del provider_native_session_id, request
        raise RuntimeError("Claude runtime does not support native execution objectives")

    async def clear_objective(
        self,
        provider_native_session_id: str,
    ) -> AgentRuntimeResult:
        del provider_native_session_id
        raise RuntimeError("Claude runtime does not support native execution objectives")

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
        return self._result(
            response,
            fallback_session_id=provider_native_session_id,
        )

    async def interrupt(
        self,
        provider_native_session_id: str,
    ) -> AgentRuntimeResult:
        response = await self.transport.request(
            "turn/interrupt",
            {"session_id": provider_native_session_id},
        )
        return self._result(
            response,
            fallback_session_id=provider_native_session_id,
        )
