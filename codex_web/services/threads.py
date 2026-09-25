from __future__ import annotations

import base64
import contextlib
import json
import os
import time
import uuid
from typing import Any, Callable, Mapping

from fastapi import HTTPException

from codex_web.agent_runtime import AgentRuntimeListRequest, AgentRuntimeSessionRequest
from codex_web.agent_routing import AgentRoutingRequest
from codex_web.execution_workers import ExecutionRuntimeBinding
from codex_web.identity import AuthenticationActor
from codex_web.models import (
    IndexedThread,
    ThreadPrimaryChannelUpdate,
    ThreadPrimaryUpdate,
    ThreadRunSettings,
)

from codex_web.services.agent_profiles import AgentProfileAccessDenied, AgentProfileService
from codex_web.services.agent_runtime import AgentSessionService
from codex_web.services.agent_routing import AgentRoutingService
from codex_web.services.codex_agent_runtime import CodexAgentRuntimeAdapter
from codex_web.services.agent_worker_session import AssignmentBoundAgentSessionManager
from codex_web.services.thread_bootstrap_bindings import (
    ThreadBootstrapBindingService,
)
from codex_web.services.project_runtime import ProjectRuntimeService
from codex_web.services.thread_bot_collaboration import ThreadBotCollaborationService
from codex_web.services.thread_execution_settings import ThreadExecutionSettingsService
from codex_web.services.thread_naming import ThreadNamingService
from codex_web.services.thread_recovery import ThreadRecoveryService
from codex_web.services.thread_resume import ThreadResumeService
from codex_web.storage.thread_index import ThreadIndexRepository
from codex_web.services.turn_execution_binding import (
    TurnExecutionBindingError,
    TurnExecutionBindingService,
)


class _ThreadRuntimeTransport:
    def __init__(
        self,
        request_for_thread: Callable[
            [str, str, dict[str, Any] | None],
            Any,
        ],
        thread_id: str,
    ) -> None:
        self.request_for_thread = request_for_thread
        self.thread_id = thread_id

    async def request(self, method: str, params: dict[str, Any] | None = None):
        return await self.request_for_thread(
            self.thread_id,
            method,
            params or {},
        )


class ThreadService:
    """Thread/query operations that do not own turn queue orchestration yet."""

    DEFAULT_MESSAGE_LIMIT = 100
    DEFAULT_LIST_LIMIT = 50
    MAX_LIST_LIMIT = 100

    def __init__(
        self,
        *,
        runtime_transport: Any,
        runtime_request_for_thread: Callable[
            [str, str, dict[str, Any] | None],
            Any,
        ],
        event_sink: Callable[[dict[str, Any]], None],
        binding_service: TurnExecutionBindingService | None = None,
        session_manager: AssignmentBoundAgentSessionManager | None = None,
        bootstrap_bindings: ThreadBootstrapBindingService | None = None,
        control_actor: AuthenticationActor | None = None,
        agent_sessions: AgentSessionService | None = None,
        agent_profiles: AgentProfileService | None = None,
        routing_service: AgentRoutingService | None = None,
        session_managers: Mapping[tuple[str, str], AssignmentBoundAgentSessionManager] | None = None,
        runtime_adapter_factory: Callable[[ExecutionRuntimeBinding, Any], Any] | None = None,
        project_runtime: ProjectRuntimeService | None = None,
        settings: ThreadExecutionSettingsService | None = None,
        recovery: ThreadRecoveryService | None = None,
        resume_runtime: ThreadResumeService | None = None,
        naming: ThreadNamingService | None = None,
        collaboration: ThreadBotCollaborationService | None = None,
        thread_index: ThreadIndexRepository | None = None,
        active_turn_loader: Callable[[], dict[str, Any]] | None = None,
        active_turn_getter: Callable[[str], Any | None] | None = None,
    ) -> None:
        self.runtime_transport = runtime_transport
        self.runtime_request_for_thread = runtime_request_for_thread
        self.event_sink = event_sink
        self.binding_service = binding_service
        self.session_manager = session_manager
        self.bootstrap_bindings = bootstrap_bindings
        self.control_actor = control_actor
        self.agent_sessions = agent_sessions
        self.agent_profiles = agent_profiles
        self.routing_service = routing_service
        self.session_managers = dict(session_managers or {})
        if session_manager is not None:
            self.session_managers.setdefault(("openai", "codex"), session_manager)
        self.runtime_adapter_factory = runtime_adapter_factory
        self.project_runtime = project_runtime
        self.settings = settings
        self.recovery = recovery
        self.resume_runtime = resume_runtime
        self.naming = naming
        self.collaboration = collaboration
        self.thread_index = thread_index
        self.active_turn_loader = active_turn_loader
        self.active_turn_getter = active_turn_getter

    @staticmethod
    def _required(value: Any, name: str):
        if value is None:
            raise HTTPException(
                status_code=503,
                detail=f"{name} is unavailable",
            )
        return value

    def _projects(self) -> ProjectRuntimeService:
        return self._required(self.project_runtime, "project runtime")

    def _settings(self) -> ThreadExecutionSettingsService:
        return self._required(self.settings, "thread settings")

    def _recovery(self) -> ThreadRecoveryService:
        return self._required(self.recovery, "thread recovery")

    def _resume(self) -> ThreadResumeService:
        return self._required(self.resume_runtime, "thread resume runtime")

    def _naming(self) -> ThreadNamingService:
        return self._required(self.naming, "thread naming")

    def _collaboration(self) -> ThreadBotCollaborationService:
        return self._required(
            self.collaboration,
            "thread bot collaboration",
        )

    def _index(self) -> ThreadIndexRepository:
        return self._required(self.thread_index, "thread index")

    def _require_bootstrap_routing(self) -> tuple[
        TurnExecutionBindingService,
        AssignmentBoundAgentSessionManager,
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

    async def _select_runtime_route(
        self,
        *,
        project_id: str,
        sandbox: str,
        provider_id: str | None = None,
        runtime_id: str | None = None,
        actor: AuthenticationActor | None = None,
        agent_profile_id: str | None = None,
        agent_profile_revision: int | None = None,
    ) -> tuple[ExecutionRuntimeBinding | None, Any | None]:
        if self.routing_service is None or self.control_actor is None:
            return None, None
        routed = await self.routing_service.route(
            AgentRoutingRequest(
                project_id=project_id,
                agent_profile_id=agent_profile_id,
                agent_profile_revision=agent_profile_revision,
                required_sandbox_profile=sandbox,
                required_network_profile="brokered-model-egress",
                require_persistent_session=True,
                allowed_provider_ids=((provider_id,) if provider_id else ()),
                allowed_runtime_ids=((runtime_id,) if runtime_id else ()),
                preferred_provider_ids=((provider_id,) if provider_id else ()),
                preferred_runtime_ids=((runtime_id,) if runtime_id else ()),
                allow_fallback=not bool(provider_id or runtime_id),
            ),
            actor=actor or self.control_actor,
        )
        return (
            routed.selected_runtime.execution_binding(),
            getattr(routed, "agent_profile", None),
        )

    async def _select_runtime_binding(
        self,
        *,
        project_id: str,
        sandbox: str,
        provider_id: str | None = None,
        runtime_id: str | None = None,
    ) -> ExecutionRuntimeBinding | None:
        binding, _profile = await self._select_runtime_route(
            project_id=project_id,
            sandbox=sandbox,
            provider_id=provider_id,
            runtime_id=runtime_id,
        )
        return binding

    def _manager_for_binding(
        self,
        binding: ExecutionRuntimeBinding | None,
    ) -> AssignmentBoundAgentSessionManager:
        if binding is None:
            if self.session_manager is None:
                raise HTTPException(
                    status_code=503,
                    detail="isolated thread bootstrap routing is unavailable",
                )
            return self.session_manager
        manager = self.session_managers.get((binding.provider_id, binding.runtime_id))
        if manager is None:
            raise HTTPException(
                status_code=503,
                detail=(
                    "selected agent runtime has no assignment-bound session manager"
                ),
            )
        return manager

    def _adapter_for_binding(
        self,
        binding: ExecutionRuntimeBinding | None,
        session: Any,
    ):
        if binding is None or (
            binding.provider_id == "openai" and binding.runtime_id == "codex"
        ):
            return CodexAgentRuntimeAdapter(session)
        if self.runtime_adapter_factory is None:
            raise HTTPException(
                status_code=503,
                detail="selected agent runtime adapter is unavailable",
            )
        return self.runtime_adapter_factory(binding, session)

    def _codex_adapter(
        self,
        thread_id: str | None = None,
    ) -> CodexAgentRuntimeAdapter:
        transport = (
            self.runtime_transport
            if thread_id is None
            else _ThreadRuntimeTransport(
                self.runtime_request_for_thread,
                thread_id,
            )
        )
        return CodexAgentRuntimeAdapter(transport)

    def _adopt_legacy_thread(
        self,
        item: dict[str, Any],
        *,
        project_id: str | None,
    ) -> None:
        if self.agent_sessions is None or self.control_actor is None:
            return
        native_id = item.get("id") or item.get("threadId")
        if not native_id:
            return
        existing = self.agent_sessions.find_by_native_id(
            str(native_id),
            self.control_actor,
            provider_id="openai",
            runtime_id="codex",
        )
        if existing is not None:
            item["agentSessionId"] = existing.id
            return

        project = None
        if project_id:
            with contextlib.suppress(Exception):
                project = self._projects().get(project_id)
        if project is None:
            with contextlib.suppress(Exception):
                project = self._projects().find_by_cwd(item.get("cwd"))
        if project is None:
            return

        adapter = self._codex_adapter()
        session = self.agent_sessions.adopt(
            provider_id=adapter.provider_id,
            runtime_id=adapter.runtime_id,
            runtime_type=adapter.runtime_type,
            provider_native_session_id=str(native_id),
            request=AgentRuntimeSessionRequest(
                project_id=project.id,
                workspace_cwd=item.get("cwd"),
                model=item.get("model") or getattr(project, "model", None),
            ),
            actor=self.control_actor,
            capability_snapshot=adapter.capabilities,
        )
        item["agentSessionId"] = session.id

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

    def coerce_list_limit(self, limit: int | None) -> int:
        if limit is None:
            return self.DEFAULT_LIST_LIMIT
        try:
            value = int(limit)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400,
                detail="thread list limit must be an integer",
            )
        return max(1, min(value, self.MAX_LIST_LIMIT))

    @staticmethod
    def _encode_list_cursor(
        *,
        after: str,
        project_id: str | None,
        archived: bool,
        search: str | None,
        revision: float | None,
    ) -> str:
        payload = {
            "after": after,
            "projectId": project_id or "",
            "archived": bool(archived),
            "search": str(search or "").strip(),
            "revision": revision,
        }
        raw = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    @staticmethod
    def _decode_list_cursor(
        cursor: str,
        *,
        project_id: str | None,
        archived: bool,
        search: str | None,
    ) -> dict[str, Any]:
        try:
            padding = "=" * (-len(cursor) % 4)
            payload = json.loads(
                base64.urlsafe_b64decode(
                    (cursor + padding).encode()
                )
            )
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail="invalid thread list cursor",
            ) from exc
        if not isinstance(payload, dict) or not payload.get("after"):
            raise HTTPException(
                status_code=400,
                detail="invalid thread list cursor",
            )
        expected = {
            "projectId": project_id or "",
            "archived": bool(archived),
            "search": str(search or "").strip(),
        }
        actual = {
            "projectId": str(payload.get("projectId") or ""),
            "archived": bool(payload.get("archived")),
            "search": str(payload.get("search") or "").strip(),
        }
        if actual != expected:
            raise HTTPException(
                status_code=400,
                detail=(
                    "thread list cursor does not match the "
                    "current project/filter query"
                ),
            )
        return payload

    @staticmethod
    def _compact_runtime_thread(
        item: dict[str, Any],
        *,
        project_id: str | None,
        archived: bool,
    ) -> IndexedThread | None:
        thread_id = str(item.get("id") or item.get("threadId") or "").strip()
        if not thread_id:
            return None
        preview = str(item.get("preview") or "").strip() or None
        name = str(item.get("name") or preview or "Untitled thread").strip()
        updated_at = item.get("updatedAt") or item.get("updated_at") or 0
        try:
            normalized_updated = float(updated_at or 0)
        except (TypeError, ValueError):
            normalized_updated = 0.0
        return IndexedThread(
            id=thread_id,
            name=name,
            cwd=item.get("cwd"),
            path=item.get("path"),
            updatedAt=normalized_updated,
            preview=(preview[:240] if preview else None),
            model=(
                str(item.get("model"))
                if item.get("model") is not None
                else None
            ),
            project_id=project_id,
            archived=archived,
        )

    def _active_turn_for_list(
        self,
        thread_id: str,
        fallback: dict[str, Any] | None = None,
    ) -> Any | None:
        if self.active_turn_getter is not None:
            return self.active_turn_getter(thread_id)
        return (fallback or {}).get(thread_id)

    async def list(
        self,
        project_id: str | None = None,
        archived: bool = False,
        search: str | None = None,
        *,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        page_size = self.coerce_list_limit(limit)
        project = self._projects().get(project_id) if project_id else None
        project_path = project.path if project is not None else None
        cursor_payload = (
            self._decode_list_cursor(
                cursor,
                project_id=project_id,
                archived=archived,
                search=search,
            )
            if cursor
            else None
        )
        expected_revision = (
            cursor_payload.get("revision")
            if cursor_payload is not None
            else None
        )
        current_revision = self._index().revision(
            project_path=project_path,
        )
        if (
            cursor_payload is not None
            and expected_revision != current_revision
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "thread_list_cursor_stale",
                    "message": (
                        "thread list changed after this cursor was issued; "
                        "restart pagination from the first page"
                    ),
                },
            )

        # Runtime discovery is deliberately bounded and only performed when a
        # caller starts a new pagination snapshot. Subsequent pages are served
        # entirely from the canonical compact index projection.
        runtime_rows: dict[str, dict[str, Any]] = {}
        if cursor_payload is None:
            runtime_request = AgentRuntimeListRequest(
                workspace_cwd=project_path,
                archived=archived,
                search=search,
                limit=page_size,
            )
            try:
                runtime_result = await self._codex_adapter().list_sessions(
                    runtime_request
                )
                runtime_payload = runtime_result.payload
            except Exception as exc:
                if archived:
                    raise HTTPException(
                        status_code=504,
                        detail=str(exc),
                    ) from exc
                runtime_payload = {"data": []}

            runtime_items = (
                runtime_payload.get("data")
                or runtime_payload.get("threads")
                or []
            )
            indexed_updates: list[IndexedThread] = []
            for item in runtime_items:
                if not isinstance(item, dict):
                    continue
                indexed = self._compact_runtime_thread(
                    item,
                    project_id=project_id,
                    archived=archived,
                )
                if indexed is None:
                    continue
                # Runtime rows are kept only for bounded summary enrichment;
                # full turns/items never enter the list projection.
                runtime_rows[indexed.id] = item
                indexed_updates.append(indexed)
            if indexed_updates:
                self._index().upsert_many(indexed_updates)
            current_revision = self._index().revision(
                project_path=project_path,
            )

        after = (
            str(cursor_payload.get("after"))
            if cursor_payload is not None
            else None
        )
        indexed_page, next_after, scan_truncated = self._index().page(
            project_path=project_path,
            archived=archived,
            search=search,
            after=after,
            limit=page_size,
        )

        fallback_active_turns = (
            self.active_turn_loader()
            if self.active_turn_getter is None
            and self.active_turn_loader is not None
            else {}
        )
        rows: list[dict[str, Any]] = []
        for indexed in indexed_page:
            runtime_item = runtime_rows.get(indexed.id)
            active = self._active_turn_for_list(
                indexed.id,
                fallback_active_turns,
            )
            runtime_status = (
                runtime_item.get("status")
                if isinstance(runtime_item, dict)
                and isinstance(runtime_item.get("status"), dict)
                else None
            )
            updated_at = max(
                float(indexed.updatedAt or 0),
                float(
                    getattr(active, "updated_at", 0)
                    if active is not None
                    else 0
                ),
            )
            row = {
                "id": indexed.id,
                "sessionId": indexed.id,
                "name": indexed.name,
                "preview": indexed.preview or "",
                "cwd": indexed.cwd,
                "path": indexed.path,
                "updatedAt": updated_at,
                "model": indexed.model,
                "projectId": indexed.project_id or project_id,
                "archived": bool(indexed.archived),
                "status": (
                    runtime_status
                    or (
                        {"type": "running"}
                        if active is not None
                        else {"type": "notLoaded"}
                    )
                ),
                "runtimeLoaded": runtime_item is not None,
                "needsRefresh": runtime_item is None,
                "staleIndex": runtime_item is None,
            }
            rows.append(row)
            self._adopt_legacy_thread(row, project_id=project_id)

        next_cursor = (
            self._encode_list_cursor(
                after=next_after,
                project_id=project_id,
                archived=archived,
                search=search,
                revision=current_revision,
            )
            if next_after
            else None
        )
        return {
            "data": rows,
            "pageSize": page_size,
            "nextCursor": next_cursor,
            "hasMore": bool(next_cursor),
            "scanTruncated": bool(scan_truncated),
            "revision": current_revision,
        }

    async def create(
        self,
        project_id: str | None = None,
        sandbox: str | None = None,
        approval_policy: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        provider_id: str | None = None,
        runtime_id: str | None = None,
        repository_resource_id: str | None = None,
        read_only_repository_resource_ids: tuple[str, ...] = (),
        execution_profile_id: str | None = None,
        actor: AuthenticationActor | None = None,
        agent_profile_id: str | None = None,
        agent_profile_revision: int | None = None,
    ) -> dict[str, Any]:
        (
            binding_service,
            _default_session_manager,
            bootstrap_bindings,
            control_actor,
        ) = self._require_bootstrap_routing()
        project = self._projects().get(project_id)
        profile = None
        if agent_profile_id is not None:
            if actor is None or self.agent_profiles is None:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "agent_profile_unavailable",
                        "message": (
                            "Agent Profile thread creation requires an "
                            "authenticated actor and Agent Profile service"
                        ),
                        "target_type": "agent_profile",
                        "target_id": agent_profile_id,
                    },
                )
            try:
                profile, _decision = self.agent_profiles.resolve_for_execution(
                    agent_profile_id,
                    actor=actor,
                    project_id=project.id,
                    revision=agent_profile_revision,
                )
            except AgentProfileAccessDenied as exc:
                raise HTTPException(
                    status_code=403,
                    detail={
                        "code": "agent_profile_access_denied",
                        "message": str(exc),
                    },
                ) from exc
            except Exception as exc:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "agent_profile_unavailable",
                        "message": str(exc),
                        "target_type": "agent_profile",
                        "target_id": agent_profile_id,
                    },
                ) from exc
            agent_profile_revision = profile.revision

        profile_sandbox = (
            profile.sandbox_requirement
            if profile is not None
            else None
        )
        if sandbox and profile_sandbox and sandbox != profile_sandbox:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "agent_profile_sandbox_conflict",
                    "requestedSandbox": sandbox,
                    "agentProfileSandbox": profile_sandbox,
                },
            )
        effective_sandbox = profile_sandbox or sandbox or project.sandbox

        profile_execution_id = (
            profile.execution_profile_id
            if profile is not None
            else None
        )
        if (
            execution_profile_id
            and profile_execution_id
            and execution_profile_id != profile_execution_id
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "agent_profile_execution_profile_conflict",
                    "requestedExecutionProfileId": execution_profile_id,
                    "agentProfileExecutionProfileId": profile_execution_id,
                },
            )
        effective_execution_profile_id = (
            profile_execution_id or execution_profile_id
        )
        effective_approval_policy = approval_policy or project.approval_policy
        runtime_binding, agent_profile_binding = await self._select_runtime_route(
            project_id=project.id,
            sandbox=effective_sandbox,
            provider_id=provider_id,
            runtime_id=runtime_id,
            actor=actor,
            agent_profile_id=agent_profile_id,
            agent_profile_revision=agent_profile_revision,
        )
        if agent_profile_id is not None and agent_profile_binding is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "agent_profile_routing_unavailable",
                    "message": (
                        "Agent Profile routing did not return a canonical "
                        "execution binding"
                    ),
                    "target_type": "agent_profile",
                    "target_id": agent_profile_id,
                },
            )
        session_manager = self._manager_for_binding(runtime_binding)
        token = uuid.uuid4().hex
        bootstrap_id = f"bootstrap-{token}"
        execution_id = f"thread-bootstrap-{token}"

        try:
            binding = binding_service.prepare_bootstrap(
                bootstrap_id=bootstrap_id,
                execution_id=execution_id,
                project_id=project.id,
                sandbox=effective_sandbox,
                approval_policy=effective_approval_policy,
                runtime_binding=runtime_binding,
                explicit_repository_id=repository_resource_id,
                read_only_repository_ids=read_only_repository_resource_ids,
                execution_profile_id=effective_execution_profile_id,
                agent_profile=agent_profile_binding,
            )
        except TurnExecutionBindingError as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "execution_preflight_blocked",
                    "message": str(exc),
                    "blockers": [exc.public()],
                    "retryable": False,
                },
            ) from exc
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
        params = self._projects().params(
            project,
            {
                "sessionStartSource": "startup",
                "sandbox": effective_sandbox,
                "approvalPolicy": effective_approval_policy,
                "model": model,
            },
        )
        params["cwd"] = workspace_cwd
        params["sandboxPolicy"] = self._projects().sandbox_policy(
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
        effective_runtime_binding = getattr(
            binding,
            "runtime_binding",
            runtime_binding,
        )
        runtime_adapter = self._adapter_for_binding(
            effective_runtime_binding,
            session,
        )
        try:
            runtime_result = await runtime_adapter.create_session(runtime_request)
            response = runtime_result.payload
        except Exception as exc:
            with contextlib.suppress(Exception):
                await session_manager.complete(
                    binding.assignment_id,
                    succeeded=False,
                    failure_code=(
                        "codex_thread_start_failed"
                        if effective_runtime_binding is None
                        or (
                            effective_runtime_binding.provider_id == "openai"
                            and effective_runtime_binding.runtime_id == "codex"
                        )
                        else "agent_thread_start_failed"
                    ),
                    failure_message=str(exc)[:500],
                )
            raise

        thread = response.get("thread", response) if isinstance(response, dict) else {}
        thread_id = (
            thread.get("id")
            if isinstance(thread, dict)
            else None
        ) or runtime_result.provider_native_session_id
        if not thread_id:
            with contextlib.suppress(Exception):
                await session_manager.complete(
                    binding.assignment_id,
                    succeeded=False,
                    failure_code=(
                        "codex_thread_id_missing"
                        if effective_runtime_binding is None
                        or (
                            effective_runtime_binding.provider_id == "openai"
                            and effective_runtime_binding.runtime_id == "codex"
                        )
                        else "agent_thread_id_missing"
                    ),
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
                    failure_code=(
                        "codex_thread_binding_failed"
                        if effective_runtime_binding is None
                        or (
                            effective_runtime_binding.provider_id == "openai"
                            and effective_runtime_binding.runtime_id == "codex"
                        )
                        else "agent_thread_binding_failed"
                    ),
                    failure_message=(
                        "returned Codex thread could not be durably bound "
                        "to its bootstrap execution"
                    ),
                )
            raise

        canonical_session = None
        if self.agent_sessions is not None:
            canonical_session = self.agent_sessions.adopt(
                provider_id=runtime_adapter.provider_id,
                runtime_id=runtime_adapter.runtime_id,
                runtime_type=runtime_adapter.runtime_type,
                provider_native_session_id=thread_id,
                request=runtime_request,
                actor=control_actor,
                capability_snapshot=runtime_adapter.capabilities,
            )

        self._settings().remember(
            thread_id,
            sandbox=effective_sandbox,
            approval_policy=effective_approval_policy,
            model=model or project.model,
            reasoning_effort=reasoning_effort,
            developer_instructions=None,
            repository_resource_id=binding.repository_resource_id,
            read_only_repository_resource_ids=(
                binding.repository_target.read_only_repository_ids
            ),
            execution_profile_id=getattr(binding, "execution_profile_id", None),
        )
        self.event_sink(
            {
                "type": "thread_bootstrap_bound",
                "thread_id": thread_id,
                "bootstrap_id": bootstrap.bootstrap_id,
                "execution_id": bootstrap.execution_id,
                "assignment_id": bootstrap.assignment_id,
                "execution_workspace_id": bootstrap.execution_workspace_id,
                "worker_id": status.worker_id,
                "fence": status.fence,
                "repository_target": binding.repository_target.model_dump(mode="json"),
                "execution_profile_id": getattr(
                    binding,
                    "execution_profile_id",
                    None,
                ),
                "execution_profile_definition": (
                    getattr(binding, "execution_profile_definition", None).model_dump(
                        mode="json"
                    )
                    if getattr(binding, "execution_profile_definition", None)
                    is not None
                    else None
                ),
                "agent_session_id": (
                    canonical_session.id if canonical_session is not None else None
                ),
                "runtime_binding": (
                    effective_runtime_binding.model_dump(mode="json")
                    if effective_runtime_binding is not None
                    else None
                ),
                "agent_profile": (
                    agent_profile_binding.model_dump(mode="json")
                    if agent_profile_binding is not None
                    else None
                ),
            }
        )
        if not isinstance(response, dict):
            response = {}
        if "thread" not in response:
            response = {
                **response,
                "thread": {
                    "id": thread_id,
                    "cwd": workspace_cwd,
                    "status": {"type": "idle"},
                    "turns": [],
                },
            }
        if canonical_session is not None:
            response = {**response, "agentSessionId": canonical_session.id}
        thread_payload = (
            response.get("thread")
            if isinstance(response.get("thread"), dict)
            else response
        )
        if isinstance(thread_payload, dict):
            indexed = self._compact_runtime_thread(
                {
                    **thread_payload,
                    "updatedAt": (
                        thread_payload.get("updatedAt")
                        or time.time()
                    ),
                    "model": (
                        thread_payload.get("model")
                        or model
                        or getattr(project, "model", None)
                    ),
                },
                project_id=project.id,
                archived=False,
            )
            if indexed is not None and self.thread_index is not None:
                self.thread_index.upsert(indexed)
        return response

    async def read(
        self,
        thread_id: str,
        message_limit: int | None = None,
        turn_limit: int | None = None,
    ) -> dict[str, Any]:
        self._recovery().raise_if_thread_replaced(thread_id)
        limit = self.coerce_message_limit(
            message_limit if message_limit is not None else turn_limit
        )
        resume_task = self._resume().active_task(thread_id)
        if resume_task is not None:
            return self._resume().read_timeout_response(
                thread_id,
                limit,
                "thread/resume still in progress",
                event_type="web_read_deferred_for_resume",
            )
        try:
            runtime_result = await self._codex_adapter(thread_id).read_session(thread_id)
            response = runtime_result.payload
        except Exception as exc:
            if self._resume().is_timeout_error(exc):
                return self._resume().read_timeout_response(thread_id, limit, exc)
            raise
        return self.trim_messages(response, limit)

    async def rename(self, thread_id: str, name: str) -> dict[str, Any]:
        self._recovery().raise_if_thread_replaced(thread_id)
        await self._naming().set_name(thread_id, name)
        return {"ok": True, "threadId": thread_id, "name": name}

    def update_settings(self, thread_id: str, payload: ThreadRunSettings) -> dict[str, Any]:
        self._recovery().raise_if_thread_replaced(thread_id)
        settings = self._settings().remember(
            thread_id,
            sandbox=payload.sandbox,
            approval_policy=payload.approval_policy,
            model=payload.model,
            reasoning_effort=payload.reasoning_effort,
            developer_instructions=payload.developer_instructions,
            repository_resource_id=payload.repository_resource_id,
            writable_repository_resource_ids=payload.writable_repository_resource_ids,
            read_only_repository_resource_ids=payload.read_only_repository_resource_ids,
            execution_profile_id=payload.execution_profile_id,
        )
        return {"ok": True, "threadId": thread_id, **settings.model_dump()}

    def list_settings(self) -> dict[str, Any]:
        return {
            thread_id: settings.model_dump()
            for thread_id, settings in self._settings().all().items()
        }

    def get_settings(self, thread_id: str) -> dict[str, Any]:
        self._recovery().raise_if_thread_replaced(thread_id)
        return {
            "threadId": thread_id,
            **self._settings().get(thread_id).model_dump(),
        }

    async def update_primary(self, thread_id: str, payload: ThreadPrimaryUpdate) -> dict[str, Any]:
        bindings = await self._collaboration().set_primary(
            thread_id,
            payload.project_id,
            payload.primary,
        )
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
        bindings = await self._collaboration().set_primary_channel(
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
        response = (
            await self._codex_adapter(thread_id).close_session(thread_id)
        ).payload
        indexed = self._index().get(thread_id)
        if indexed is not None:
            self._index().upsert(
                indexed.model_copy(
                    update={
                        "archived": True,
                        "updatedAt": time.time(),
                    }
                )
            )
        return response

    async def unarchive(self, thread_id: str) -> dict[str, Any]:
        response = (
            await self._codex_adapter(thread_id).restore_session(thread_id)
        ).payload
        indexed = self._index().get(thread_id)
        if indexed is not None:
            self._index().upsert(
                indexed.model_copy(
                    update={
                        "archived": False,
                        "updatedAt": time.time(),
                    }
                )
            )
        return response

    async def interrupt(self, thread_id: str) -> dict[str, Any]:
        return (
            await self._codex_adapter(thread_id).interrupt(thread_id)
        ).payload
