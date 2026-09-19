from __future__ import annotations

from typing import Any

from codex_web.agent_runtime import (
    AgentRuntimeHealth,
    AgentRuntimeListRequest,
    AgentRuntimeResult,
    AgentRuntimeSessionRequest,
    AgentRuntimeTurnRequest,
)
from codex_web.agent_providers import AgentProviderCapability


class CodexAgentRuntimeAdapter:
    """Translate canonical agent-runtime operations to Codex app-server RPC."""

    provider_id = "openai"
    runtime_id = "codex"
    runtime_type = "codex-app-server"
    capabilities = (
        AgentProviderCapability.AGENT_EXECUTION,
        AgentProviderCapability.PERSISTENT_SESSIONS,
        AgentProviderCapability.STREAMING,
        AgentProviderCapability.INTERRUPT_CANCEL,
        AgentProviderCapability.FILESYSTEM_EDITING,
        AgentProviderCapability.SHELL_TOOLS,
        AgentProviderCapability.GIT_OPERATIONS,
        AgentProviderCapability.INTERACTIVE_APPROVALS,
        AgentProviderCapability.NATIVE_CONTEXT_COMPACTION,
        AgentProviderCapability.MCP_TOOL_SERVERS,
        AgentProviderCapability.USAGE_PARTIAL,
    )

    def __init__(self, transport: Any) -> None:
        self.transport = transport

    async def health(self) -> AgentRuntimeHealth:
        proc = getattr(self.transport, "proc", None)
        if proc is not None and getattr(proc, "poll", lambda: None)() is not None:
            return AgentRuntimeHealth.UNAVAILABLE
        return AgentRuntimeHealth.HEALTHY

    @staticmethod
    def _thread(result: Any) -> dict[str, Any]:
        if not isinstance(result, dict):
            return {}
        thread = result.get("thread")
        return thread if isinstance(thread, dict) else result

    @classmethod
    def _result(cls, response: Any) -> AgentRuntimeResult:
        payload = response if isinstance(response, dict) else {}
        thread = cls._thread(payload)
        native_session_id = thread.get("id") or thread.get("threadId")
        turn = payload.get("turn") if isinstance(payload.get("turn"), dict) else {}
        return AgentRuntimeResult(
            provider_native_session_id=(
                str(native_session_id) if native_session_id else None
            ),
            provider_native_turn_id=(
                str(turn.get("id")) if turn.get("id") else None
            ),
            payload=payload,
        )

    async def list_sessions(
        self,
        request: AgentRuntimeListRequest,
    ) -> AgentRuntimeResult:
        params: dict[str, Any] = {
            "limit": request.limit,
            "archived": request.archived,
            "sortKey": "updated_at",
            "sortDirection": "desc",
            "sourceKinds": ["appServer", "cli", "vscode", "exec"],
        }
        if request.workspace_cwd:
            params["cwd"] = request.workspace_cwd
        if request.search:
            params["searchTerm"] = request.search
        response = await self.transport.request("thread/list", params)
        return AgentRuntimeResult(
            payload=response if isinstance(response, dict) else {},
        )

    async def create_session(
        self,
        request: AgentRuntimeSessionRequest,
    ) -> AgentRuntimeResult:
        params: dict[str, Any] = {"sessionStartSource": "startup"}
        if request.workspace_cwd:
            params["cwd"] = request.workspace_cwd
        if request.sandbox:
            params["sandbox"] = request.sandbox
        if request.sandbox_policy is not None:
            params["sandboxPolicy"] = request.sandbox_policy
        if request.approval_policy:
            params["approvalPolicy"] = request.approval_policy
        if request.model:
            params["model"] = request.model
        response = await self.transport.request("thread/start", params)
        return self._result(response)

    async def resume_session(
        self,
        provider_native_session_id: str,
        request: AgentRuntimeSessionRequest,
    ) -> AgentRuntimeResult:
        params: dict[str, Any] = {"threadId": provider_native_session_id}
        if request.workspace_cwd:
            params["cwd"] = request.workspace_cwd
        if request.sandbox:
            params["sandbox"] = request.sandbox
        if request.sandbox_policy is not None:
            params["sandboxPolicy"] = request.sandbox_policy
        if request.approval_policy:
            params["approvalPolicy"] = request.approval_policy
        if request.model:
            params["model"] = request.model
        response = await self.transport.request("thread/resume", params)
        result = self._result(response)
        if result.provider_native_session_id is None:
            result = result.model_copy(
                update={"provider_native_session_id": provider_native_session_id}
            )
        return result

    async def read_session(
        self,
        provider_native_session_id: str,
    ) -> AgentRuntimeResult:
        response = await self.transport.request(
            "thread/read",
            {"threadId": provider_native_session_id, "includeTurns": True},
        )
        result = self._result(response)
        if result.provider_native_session_id is None:
            result = result.model_copy(
                update={"provider_native_session_id": provider_native_session_id}
            )
        return result

    async def close_session(
        self,
        provider_native_session_id: str,
    ) -> AgentRuntimeResult:
        response = await self.transport.request(
            "thread/archive",
            {"threadId": provider_native_session_id},
        )
        return AgentRuntimeResult(
            provider_native_session_id=provider_native_session_id,
            payload=response if isinstance(response, dict) else {},
        )

    async def restore_session(
        self,
        provider_native_session_id: str,
    ) -> AgentRuntimeResult:
        response = await self.transport.request(
            "thread/unarchive",
            {"threadId": provider_native_session_id},
        )
        return AgentRuntimeResult(
            provider_native_session_id=provider_native_session_id,
            payload=response if isinstance(response, dict) else {},
        )

    async def start_turn(
        self,
        provider_native_session_id: str,
        request: AgentRuntimeTurnRequest,
    ) -> AgentRuntimeResult:
        params: dict[str, Any] = {
            "threadId": provider_native_session_id,
            "input": [{"type": "text", "text": request.message}],
        }
        if request.model:
            params["model"] = request.model
        if request.reasoning_effort:
            params["effort"] = request.reasoning_effort
        response = await self.transport.request("turn/start", params)
        result = self._result(response)
        if result.provider_native_session_id is None:
            result = result.model_copy(
                update={"provider_native_session_id": provider_native_session_id}
            )
        return result

    async def interrupt(
        self,
        provider_native_session_id: str,
    ) -> AgentRuntimeResult:
        response = await self.transport.request(
            "turn/interrupt",
            {"threadId": provider_native_session_id},
        )
        return AgentRuntimeResult(
            provider_native_session_id=provider_native_session_id,
            payload=response if isinstance(response, dict) else {},
        )
