from __future__ import annotations

from collections.abc import Callable
from typing import Any

from codex_web.agent_runtime import (
    AgentRuntimeEvent,
    AgentRuntimeHealth,
    AgentRuntimeListRequest,
    AgentRuntimeObjectiveRequest,
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
        AgentProviderCapability.NATIVE_EXECUTION_OBJECTIVES,
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

    def subscribe_events(
        self,
        listener: Callable[[AgentRuntimeEvent], None],
    ) -> Callable[[], None]:
        hub = self._event_hub()
        if hub is None:
            raise RuntimeError("Codex runtime event hub is unavailable")

        def project(event: dict[str, Any]) -> None:
            if event.get("type") != "codex.event":
                return
            message = event.get("message") or {}
            params = message.get("params") or {} if isinstance(message, dict) else {}
            turn = params.get("turn") or {} if isinstance(params, dict) else {}
            listener(
                AgentRuntimeEvent(
                    event_type=str(message.get("method") or "codex.event"),
                    provider_native_session_id=(
                        params.get("threadId")
                        or (turn.get("threadId") if isinstance(turn, dict) else None)
                    ),
                    provider_native_turn_id=(
                        params.get("turnId")
                        or (turn.get("id") if isinstance(turn, dict) else None)
                    ),
                    payload=message if isinstance(message, dict) else {},
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

    async def capacity_snapshot(self) -> dict[str, Any]:
        """Read structured Codex account quota without consuming model tokens."""

        response = await self.transport.request("account/rateLimits/read", {})
        if not isinstance(response, dict):
            raise RuntimeError("Codex rate-limit read returned an invalid payload")
        return response

    async def shutdown(self) -> None:
        stop = getattr(self.transport, "stop", None)
        if not callable(stop):
            raise RuntimeError("Codex runtime shutdown is unavailable")
        await stop()

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
        if request.approval_reviewer:
            params["approvalsReviewer"] = request.approval_reviewer
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
        if request.approval_reviewer:
            params["approvalsReviewer"] = request.approval_reviewer
        if request.model:
            params["model"] = request.model
        if request.developer_instructions:
            params["developerInstructions"] = request.developer_instructions
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

    async def compact_session(
        self,
        provider_native_session_id: str,
    ) -> AgentRuntimeResult:
        response = await self.transport.request(
            "thread/compact/start",
            {"threadId": provider_native_session_id},
        )
        return AgentRuntimeResult(
            provider_native_session_id=provider_native_session_id,
            payload=response if isinstance(response, dict) else {},
        )

    async def read_objective(
        self,
        provider_native_session_id: str,
    ) -> AgentRuntimeResult:
        response = await self.transport.request(
            "thread/goal/get",
            {"threadId": provider_native_session_id},
        )
        return AgentRuntimeResult(
            provider_native_session_id=provider_native_session_id,
            payload=response if isinstance(response, dict) else {},
        )

    async def set_objective(
        self,
        provider_native_session_id: str,
        request: AgentRuntimeObjectiveRequest,
    ) -> AgentRuntimeResult:
        params: dict[str, Any] = {"threadId": provider_native_session_id}
        if request.objective is not None:
            params["objective"] = request.objective
        if request.status is not None:
            params["status"] = request.status
        if request.token_budget is not None:
            params["tokenBudget"] = request.token_budget
        response = await self.transport.request("thread/goal/set", params)
        return AgentRuntimeResult(
            provider_native_session_id=provider_native_session_id,
            payload=response if isinstance(response, dict) else {},
        )

    async def clear_objective(
        self,
        provider_native_session_id: str,
    ) -> AgentRuntimeResult:
        response = await self.transport.request(
            "thread/goal/clear",
            {"threadId": provider_native_session_id},
        )
        return AgentRuntimeResult(
            provider_native_session_id=provider_native_session_id,
            payload=response if isinstance(response, dict) else {},
        )

    async def respond_approval(
        self,
        request_id: int | str,
        result: dict[str, Any],
    ) -> None:
        respond = getattr(self.transport, "respond_to_server_request", None)
        if not callable(respond):
            raise RuntimeError("Codex runtime approval response is unavailable")
        await respond(request_id, result)

    async def start_turn(
        self,
        provider_native_session_id: str,
        request: AgentRuntimeTurnRequest,
    ) -> AgentRuntimeResult:
        params: dict[str, Any] = {
            "threadId": provider_native_session_id,
            "input": [
                {
                    "type": "text",
                    "text": request.message,
                    "text_elements": [],
                }
            ],
        }
        if request.workspace_cwd:
            params["cwd"] = request.workspace_cwd
        if request.model:
            params["model"] = request.model
        if request.reasoning_effort:
            params["effort"] = request.reasoning_effort
        if request.developer_instructions:
            params["developerInstructions"] = request.developer_instructions
        if request.approval_policy:
            params["approvalPolicy"] = request.approval_policy
        if request.approval_reviewer:
            params["approvalsReviewer"] = request.approval_reviewer
        if request.sandbox_policy is not None:
            params["sandboxPolicy"] = request.sandbox_policy
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
