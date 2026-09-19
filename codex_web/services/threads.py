from __future__ import annotations

import contextlib
import os
import uuid
from typing import Any

from fastapi import HTTPException

from codex_web.agent_runtime import AgentRuntimeListRequest, AgentRuntimeSessionRequest
from codex_web.identity import AuthenticationActor
from codex_web.models import (
    ThreadPrimaryChannelUpdate,
    ThreadPrimaryUpdate,
    ThreadRunSettings,
)

from codex_web.services.agent_runtime import AgentSessionService
from codex_web.services.codex_agent_runtime import CodexAgentRuntimeAdapter
from codex_web.services.codex_worker_session import (
    AssignmentBoundCodexSessionManager,
)
from codex_web.services.thread_bootstrap_bindings import (
    ThreadBootstrapBindingService,
)
from codex_web.services.turn_execution_binding import (
    TurnExecutionBindingService,
)


class _ThreadRuntimeTransport:
    def __init__(self, host: Any, thread_id: str) -> None:
        self.host = host
        self.thread_id = thread_id

    async def request(self, method: str, params: dict[str, Any] | None = None):
        return await self.host._codex_request_for_thread(
            self.thread_id,
            method,
            params or {},
        )


class ThreadService:
    """Thread/query operations that do not own turn queue orchestration yet."""

    DEFAULT_MESSAGE_LIMIT = 100

    def __init__(
        self,
        host: Any,
        *,
        binding_service: TurnExecutionBindingService | None = None,
        session_manager: AssignmentBoundCodexSessionManager | None = None,
        bootstrap_bindings: ThreadBootstrapBindingService | None = None,
        control_actor: AuthenticationActor | None = None,
        agent_sessions: AgentSessionService | None = None,
    ) -> None:
        self.host = host
        self.binding_service = binding_service
        self.session_manager = session_manager
        self.bootstrap_bindings = bootstrap_bindings
        self.control_actor = control_actor
        self.agent_sessions = agent_sessions

    def _require_bootstrap_routing(self) -> tuple[
        TurnExecutionBindingService,
        AssignmentBoundCodexSessionManager,
        ThreadBootstrapBindingService,
        AuthenticationActor,
    ]:
        if (
            self.binding_service is None
            or self.session_manager is None
            or self.bootstrap_bindings is None
            or self.control_actor is None
        ):
            raise HTTPException(
                status_code=503,
                detail="isolated thread bootstrap routing is unavailable",
            )
        return (
            self.binding_service,
            self.session_manager,
            self.bootstrap_bindings,
            self.control_actor,
        )

    def _codex_adapter(
        self,
        thread_id: str | None = None,
    ) -> CodexAgentRuntimeAdapter:
        transport = (
            self.host.codex
            if thread_id is None
            else _ThreadRuntimeTransport(self.host, thread_id)
        )
        return CodexAgentRuntimeAdapter(transport)

    def default_message_limit(self) -> int:
        try:
            limit = int(
                os.environ.get("CODEX_WEB_THREAD_MESSAGE_LIMIT")
                or os.environ.get("CODEX_WEB_THREAD_TURN_LIMIT")
                or self.DEFAULT_MESSAGE_LIMIT
            )
        except ValueError:
            return self.DEFAULT_MESSAGE_LIMIT
        return max(1, min(limit, 1000))

    def coerce_message_limit(self, limit: int | None) -> int:
        if limit is None:
            return self.default_message_limit()
        return max(1, min(int(limit), 1000))

    @staticmethod
    def trim_messages(response: dict[str, Any], limit: int) -> dict[str, Any]:
        if limit <= 0:
            return response
        thread = response.get("thread") if isinstance(response.get("thread"), dict) else response
        turns = thread.get("turns") if isinstance(thread, dict) else None
        if not isinstance(turns, list):
            return response
        total_items = sum(len(turn.get("items") or []) for turn in turns if isinstance(turn, dict))
        if total_items <= limit:
            thread["messageLimit"] = limit
            return response
        remaining = limit
        kept_turns: list[dict[str, Any]] = []
        for turn in reversed(turns):
            if not isinstance(turn, dict):
                continue
            items = turn.get("items") or []
            if not isinstance(items, list):
                items = []
            if remaining <= 0:
                break
            if len(items) <= remaining:
                kept_turns.append(turn)
                remaining -= len(items)
                continue
            kept_turn = {**turn, "items": items[-remaining:]}
            kept_turns.append(kept_turn)
            remaining = 0
        thread["turns"] = list(reversed(kept_turns))
        thread["messagesTruncated"] = True
        thread["messagesOmitted"] = total_items - limit
        thread["messageLimit"] = limit
        return response

    async def list(
        self,
        project_id: str | None = None,
        archived: bool = False,
        search: str | None = None,
    ) -> dict[str, Any]:
        project_path = self.host._project(project_id).path if project_id else None
        runtime_request = AgentRuntimeListRequest(
            workspace_cwd=project_path,
            archived=archived,
            search=search,
            limit=100,
        )
        try:
            runtime_result = await self._codex_adapter().list_sessions(runtime_request)
            result = runtime_result.payload
        except Exception as exc:
            if archived:
                raise HTTPException(status_code=504, detail=str(exc)) from exc
            result = {"data": []}
        if archived:
            return result

        items = result.get("data") or result.get("threads") or []
        indexed_threads = self.host._load_thread_index()
        indexed_by_id = {indexed.id: indexed for indexed in indexed_threads}
        active_turns = self.host._load_active_turns()
        for item in items:
            if not isinstance(item, dict):
                continue
            indexed = indexed_by_id.get(item.get("id"))
            if indexed:
                item["name"] = indexed.name
                item["updatedAt"] = max(
                    item.get("updatedAt") or 0,
                    indexed.updatedAt or 0,
                    active_turns.get(indexed.id).updated_at if indexed.id in active_turns else 0,
                )
        existing_ids = {item.get("id") for item in items if isinstance(item, dict)}
        search_term = search.casefold() if search else None
        for indexed in indexed_threads:
            if indexed.id in existing_ids:
                continue
            if project_path and indexed.cwd and indexed.cwd != project_path:
                continue
            if search_term and search_term not in indexed.name.casefold():
                continue
            item: dict[str, Any] = {
                "id": indexed.id,
                "sessionId": indexed.id,
                "preview": "",
                "name": indexed.name,
                "cwd": indexed.cwd,
                "path": indexed.path,
                "updatedAt": max(
                    indexed.updatedAt or 0,
                    active_turns.get(indexed.id).updated_at if indexed.id in active_turns else 0,
                ),
                "status": {"type": "notLoaded"},
                "turns": [],
            }
            with contextlib.suppress(Exception):
                thread_response = await self.host._codex_request_for_thread(
                    indexed.id,
                    "thread/read",
                    {"threadId": indexed.id, "includeTurns": False},
                )
                thread = thread_response.get("thread", thread_response) if isinstance(thread_response, dict) else {}
                item.update(thread)
                item["name"] = indexed.name
                item["updatedAt"] = max(
                    item.get("updatedAt") or 0,
                    indexed.updatedAt or 0,
                    active_turns.get(indexed.id).updated_at if indexed.id in active_turns else 0,
                )
            items.append(item)
            existing_ids.add(indexed.id)

        items.sort(key=lambda item: item.get("updatedAt") or 0, reverse=True)
        if "data" in result:
            result["data"] = items
        elif "threads" in result:
            result["threads"] = items
        else:
            result["data"] = items
        return result

    async def create(
        self,
        project_id: str | None = None,
        sandbox: str | None = None,
        approval_policy: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        (
            binding_service,
            session_manager,
            bootstrap_bindings,
            control_actor,
        ) = self._require_bootstrap_routing()
        project = self.host._project(project_id)
        effective_sandbox = sandbox or project.sandbox
        effective_approval_policy = approval_policy or project.approval_policy
        token = uuid.uuid4().hex
        bootstrap_id = f"bootstrap-{token}"
        execution_id = f"thread-bootstrap-{token}"

        binding = binding_service.prepare_bootstrap(
            bootstrap_id=bootstrap_id,
            execution_id=execution_id,
            project_id=project.id,
            sandbox=effective_sandbox,
            approval_policy=effective_approval_policy,
        )
        session = await session_manager.start(binding.assignment_id)
        status = session.status()
        workspace_path = session.workspace_path
        if workspace_path is None or status.fence is None:
            with contextlib.suppress(Exception):
                await session_manager.complete(
                    binding.assignment_id,
                    succeeded=False,
                    failure_code="codex_thread_bootstrap_invalid",
                    failure_message=(
                        "assignment-bound bootstrap session lacks canonical "
                        "workspace/fence"
                    ),
                )
            raise HTTPException(
                status_code=503,
                detail=(
                    "assignment-bound bootstrap session lacks canonical "
                    "workspace/fence"
                ),
            )

        workspace_cwd = str(workspace_path)
        params = self.host._project_params(
            project,
            {
                "sessionStartSource": "startup",
                "sandbox": effective_sandbox,
                "approvalPolicy": effective_approval_policy,
                "model": model,
            },
        )
        params["cwd"] = workspace_cwd
        params["sandboxPolicy"] = self.host._sandbox_policy(
            effective_sandbox,
            workspace_cwd,
        )

        runtime_request = AgentRuntimeSessionRequest(
            project_id=project.id,
            sandbox=effective_sandbox,
            approval_policy=effective_approval_policy,
            approval_reviewer=params.get("approvalsReviewer"),
            workspace_cwd=workspace_cwd,
            sandbox_policy=params.get("sandboxPolicy"),
            execution_id=binding.execution_id,
            assignment_id=binding.assignment_id,
            execution_workspace_id=binding.workspace_id,
            worker_id=status.worker_id,
            model=model or project.model,
        )
        codex_adapter = CodexAgentRuntimeAdapter(session)
        try:
            runtime_result = await codex_adapter.create_session(runtime_request)
            response = runtime_result.payload
        except Exception as exc:
            with contextlib.suppress(Exception):
                await session_manager.complete(
                    binding.assignment_id,
                    succeeded=False,
                    failure_code="codex_thread_start_failed",
                    failure_message=str(exc)[:500],
                )
            raise

        thread = response.get("thread", response) if isinstance(response, dict) else {}
        thread_id = thread.get("id") if isinstance(thread, dict) else None
        if not thread_id:
            with contextlib.suppress(Exception):
                await session_manager.complete(
                    binding.assignment_id,
                    succeeded=False,
                    failure_code="codex_thread_id_missing",
                    failure_message="thread/start returned no canonical thread id",
                )
            raise HTTPException(
                status_code=502,
                detail="thread/start returned no canonical thread id",
            )

        try:
            bootstrap = bootstrap_bindings.bind(
                bootstrap_id=bootstrap_id,
                thread_id=thread_id,
                execution_id=binding.execution_id,
                assignment_id=binding.assignment_id,
                execution_workspace_id=binding.workspace_id,
                actor=control_actor,
            )
        except Exception:
            with contextlib.suppress(Exception):
                await session_manager.complete(
                    binding.assignment_id,
                    succeeded=False,
                    failure_code="codex_thread_binding_failed",
                    failure_message=(
                        "returned Codex thread could not be durably bound "
                        "to its bootstrap execution"
                    ),
                )
            raise

        canonical_session = None
        if self.agent_sessions is not None:
            canonical_session = self.agent_sessions.adopt(
                provider_id=codex_adapter.provider_id,
                runtime_id=codex_adapter.runtime_id,
                runtime_type=codex_adapter.runtime_type,
                provider_native_session_id=thread_id,
                request=runtime_request,
                actor=control_actor,
                capability_snapshot=codex_adapter.capabilities,
            )

        self.host._remember_thread_run_settings(
            thread_id,
            sandbox=effective_sandbox,
            approval_policy=effective_approval_policy,
            model=model or project.model,
            reasoning_effort=reasoning_effort,
            developer_instructions=None,
        )
        self.host._append_bot_event(
            {
                "type": "thread_bootstrap_bound",
                "thread_id": thread_id,
                "bootstrap_id": bootstrap.bootstrap_id,
                "execution_id": bootstrap.execution_id,
                "assignment_id": bootstrap.assignment_id,
                "execution_workspace_id": bootstrap.execution_workspace_id,
                "worker_id": status.worker_id,
                "fence": status.fence,
                "agent_session_id": (
                    canonical_session.id if canonical_session is not None else None
                ),
            }
        )
        if canonical_session is not None and isinstance(response, dict):
            response = {**response, "agentSessionId": canonical_session.id}
        return response

    async def read(
        self,
        thread_id: str,
        message_limit: int | None = None,
        turn_limit: int | None = None,
    ) -> dict[str, Any]:
        self.host._raise_if_thread_replaced(thread_id)
        limit = self.host._coerce_thread_message_limit(
            message_limit if message_limit is not None else turn_limit
        )
        resume_task = self.host.WEB_THREAD_RESUME_TASKS.get(thread_id)
        if resume_task and not resume_task.done():
            return self.host._thread_read_timeout_response(
                thread_id,
                limit,
                "thread/resume still in progress",
                event_type="web_read_deferred_for_resume",
            )
        try:
            runtime_result = await self._codex_adapter(thread_id).read_session(thread_id)
            response = runtime_result.payload
        except Exception as exc:
            if self.host._is_codex_timeout_error(exc):
                return self.host._thread_read_timeout_response(thread_id, limit, exc)
            raise
        return self.host._trim_thread_messages(response, limit)

    async def rename(self, thread_id: str, name: str) -> dict[str, Any]:
        self.host._raise_if_thread_replaced(thread_id)
        await self.host._set_thread_name(thread_id, name)
        return {"ok": True, "threadId": thread_id, "name": name}

    def update_settings(self, thread_id: str, payload: ThreadRunSettings) -> dict[str, Any]:
        self.host._raise_if_thread_replaced(thread_id)
        settings = self.host._remember_thread_run_settings(
            thread_id,
            sandbox=payload.sandbox,
            approval_policy=payload.approval_policy,
            model=payload.model,
            reasoning_effort=payload.reasoning_effort,
            developer_instructions=payload.developer_instructions,
        )
        return {"ok": True, "threadId": thread_id, **settings.model_dump()}

    def list_settings(self) -> dict[str, Any]:
        return {
            thread_id: settings.model_dump()
            for thread_id, settings in self.host._load_thread_settings().items()
        }

    def get_settings(self, thread_id: str) -> dict[str, Any]:
        self.host._raise_if_thread_replaced(thread_id)
        return {"threadId": thread_id, **self.host._thread_run_settings(thread_id).model_dump()}

    async def update_primary(self, thread_id: str, payload: ThreadPrimaryUpdate) -> dict[str, Any]:
        bindings = await self.host._set_thread_primary(thread_id, payload.project_id, payload.primary)
        return {
            "ok": True,
            "threadId": thread_id,
            "projectId": payload.project_id,
            "primary": payload.primary,
            "bindings": [binding.model_dump() for binding in bindings],
        }

    async def update_primary_channel(
        self,
        thread_id: str,
        payload: ThreadPrimaryChannelUpdate,
    ) -> dict[str, Any]:
        bindings = await self.host._set_thread_primary_channel(
            thread_id,
            payload.project_id,
            payload.provider,
            payload.external_conversation_id,
        )
        return {
            "ok": True,
            "threadId": thread_id,
            "projectId": payload.project_id,
            "provider": payload.provider,
            "externalConversationId": payload.external_conversation_id,
            "bindings": [binding.model_dump() for binding in bindings],
        }

    async def archive(self, thread_id: str) -> dict[str, Any]:
        return (
            await self._codex_adapter(thread_id).close_session(thread_id)
        ).payload

    async def unarchive(self, thread_id: str) -> dict[str, Any]:
        return (
            await self._codex_adapter(thread_id).restore_session(thread_id)
        ).payload

    async def interrupt(self, thread_id: str) -> dict[str, Any]:
        return (
            await self._codex_adapter(thread_id).interrupt(thread_id)
        ).payload
