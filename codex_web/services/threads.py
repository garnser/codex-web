from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import time
import uuid
from typing import Any, Callable, Mapping

from codex_web.services.post_turn_readiness import PostTurnReadinessService

from fastapi import HTTPException
from pydantic import ValidationError

from codex_web.agent_runtime import AgentRuntimeListRequest, AgentRuntimeSessionRequest
from codex_web.agent_routing import AgentRoutingRequest
from codex_web.execution_workers import ExecutionRuntimeBinding
from codex_web.identity import AuthenticationActor
from codex_web.runtime.execution import _runtime_for_model
from codex_web.models import (
    BotBinding,
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
    ThreadBootstrapBindingNotFoundError,
)
from codex_web.services.project_runtime import (
    ProjectRuntimeService,
    assignment_sandbox_policy,
)
from codex_web.services.thread_scope import ThreadScopeService
from codex_web.services.thread_bot_collaboration import ThreadBotCollaborationService
from codex_web.services.thread_execution_settings import ThreadExecutionSettingsService
from codex_web.services.thread_naming import ThreadNamingService
from codex_web.services.thread_recovery import ThreadRecoveryService
from codex_web.services.thread_resume import ThreadResumeService
from codex_web.storage.agent_sessions import AgentSessionConflictError
from codex_web.storage.thread_history import ThreadHistoryRepository
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


class ThreadBootstrapPrestartRefusal(HTTPException):
    """Controller-owned proof that provider thread creation was never called."""


_BOOTSTRAP_PREPARATION_STAGES = frozenset({
    'request_validation', 'profile_resolution', 'runtime_routing',
    'admission_before_reservation', 'bootstrap_reservation',
    'admission_before_worker_start', 'worker_start',
    'resource_context_validation', 'session_workspace_validation',
    'runtime_request_preparation', 'admission_before_provider_start',
})
_BOOTSTRAP_EXCEPTION_TYPES = frozenset({
    'TimeoutError', 'ValueError', 'TypeError', 'LookupError',
    'OSError', 'RuntimeError', 'AssertionError', 'Exception',
    'AttributeError', 'NameError', 'KeyError', 'IndexError',
    'PermissionError', 'FileNotFoundError', 'NotImplementedError',
})


_BOOTSTRAP_HTTP_REFUSAL_CODES = frozenset({
    'agent_profile_unavailable', 'agent_profile_access_denied',
    'agent_profile_sandbox_conflict', 'agent_profile_execution_profile_conflict',
    'agent_profile_routing_unavailable', 'execution_preflight_blocked',
})
_BOOTSTRAP_BLOCKER_CODES = frozenset({
    'authentication_mode_invalid', 'authentication_mode_denied',
    'authentication_mode_unsupported', 'local_session_unavailable',
    'authentication_unavailable', 'authentication_expired', 'authentication_revoked',
    'execution_profile_incompatible', 'sandbox_profile_unsupported',
    'control_plane_scope_missing', 'worker_capability_missing',
    'quota_or_capacity_blocked', 'project_readiness_blocked',
    'repository_target_missing', 'workspace_provisioning_blocked', 'lease_conflict',
})


def bootstrap_preparation_diagnostic(exc):
    """Closed diagnostics only; never provider text or retry authority."""
    if type(exc) is not ThreadBootstrapPrestartRefusal:
        return None
    detail = getattr(exc, '_controller_preparation_diagnostic', exc.detail)
    if (not isinstance(detail, dict)
            or detail.get('code') != 'bootstrap_prestart_failed'
            or not isinstance(detail.get('bootstrap_preparation_stage'), str)
            or not isinstance(detail.get('bootstrap_exception_type'), str)
            or detail.get('bootstrap_preparation_stage') not in _BOOTSTRAP_PREPARATION_STAGES
            or detail.get('bootstrap_exception_type') not in _BOOTSTRAP_EXCEPTION_TYPES
            or detail.get('provider_start_called') is not False):
        return None
    result = {key: detail[key] for key in (
        'bootstrap_preparation_stage', 'bootstrap_exception_type', 'provider_start_called')}
    status = detail.get('bootstrap_http_status')
    if type(status) is int and status in {400, 403, 404, 409, 422, 429, 500, 502, 503, 504}:
        result['bootstrap_http_status'] = status
    for key, allowed in (
        ('bootstrap_refusal_code', _BOOTSTRAP_HTTP_REFUSAL_CODES),
        ('bootstrap_blocker_code', _BOOTSTRAP_BLOCKER_CODES),
    ):
        value = detail.get(key)
        if isinstance(value, str) and value in allowed:
            result[key] = value
    return result


def bootstrap_exception_type(exc):
    # Class names and exception messages can contain untrusted provider data.
    for kind in (TimeoutError, AttributeError, NameError, KeyError, IndexError,
                 PermissionError, FileNotFoundError, NotImplementedError,
                 ValueError, TypeError, LookupError, OSError, RuntimeError, AssertionError):
        if isinstance(exc, kind):
            return kind.__name__
    return 'Exception'


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
        thread_scope: ThreadScopeService | None = None,
        thread_history: ThreadHistoryRepository | None = None,
        active_turn_loader: Callable[[], dict[str, Any]] | None = None,
        active_turn_getter: Callable[[str], Any | None] | None = None,
        transcript: Any | None = None,
        project_bindings: Callable[[str], list[BotBinding]] | None = None,
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
        self.thread_scope = thread_scope
        self.thread_history = thread_history
        self.active_turn_loader = active_turn_loader
        self.active_turn_getter = active_turn_getter
        self.transcript = transcript
        self.project_bindings = project_bindings

    def _transcript_response(
        self,
        thread_id: str,
        *,
        degraded_error: str | None = None,
    ) -> dict[str, Any] | None:
        if self.transcript is None:
            return None
        transcript = self.transcript.read(thread_id)
        turns = list(transcript.get("turns") or [])
        if not turns:
            return None
        indexed = self.thread_index.get(thread_id) if self.thread_index else None
        active = (
            self.active_turn_getter(thread_id)
            if self.active_turn_getter is not None
            else None
        )
        return {
            "ok": True,
            "runtimeReadDegraded": bool(degraded_error),
            "error": degraded_error,
            "thread": {
                "id": thread_id,
                "name": indexed.name if indexed else "Untitled thread",
                "cwd": indexed.cwd if indexed else None,
                "path": indexed.path if indexed else None,
                "turns": turns,
                "status": {"type": "active" if active else "idle"},
            },
        }

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
        # Legacy visibility is not authority to replace an existing runtime's
        # canonical custody. Inspect the complete bounded ownership index first;
        # malformed or ambiguous aliases must never become legacy fallback.
        try:
            owner = self.agent_sessions.store.native_owner(str(native_id))
            existing = self.agent_sessions.store.native_custody(str(native_id))
        except (AgentSessionConflictError, ValidationError):
            # Retained ambiguous history stays visible but cannot select custody
            # or authorize another adoption. Unexpected storage failures surface.
            return
        if owner is not None and (
            owner[:2] != (
                self.control_actor.organization_id,
                self.control_actor.workspace_id,
            )
            or (project_id is not None and owner[2] != project_id)
        ):
            return
        if existing is not None:
            if (
                (existing.organization_id, existing.workspace_id) != (
                    self.control_actor.organization_id,
                    self.control_actor.workspace_id,
                )
                or (project_id is not None and existing.project_id != project_id)
            ):
                return
            item["agentSessionId"] = existing.id
            return
        if owner is not None:
            # Ownership disappearing between bounded reads is not fresh absence.
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
        project_path: str | None = None,
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
            # Repository-backed sessions run from isolated execution-workspace
            # paths. The thread index is project-scoped by cwd, so retain the
            # canonical Project path for list membership instead of allowing a
            # temporary worker path to make the thread disappear from the UI.
            cwd=project_path or item.get("cwd"),
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

    def _runner_projection(self, thread_id: str, bindings=None) -> dict[str, Any] | None:
        """Read current worker custody, separately from retained turn history.

        One binding snapshot per list request, indexed assignment/worker reads.
        This projection never resumes a session, changes an
        assignment, or classifies the business outcome of an unfinished turn.
        """
        if not (self.bootstrap_bindings and self.binding_service and self.control_actor):
            return None
        observed_at = time.time()
        unknown = {"schemaVersion": "1.0", "state": "unknown", "live": False,
                   "observedAt": observed_at, "source": "assignment"}
        try:
            binding = (self.bootstrap_bindings.get_by_thread(thread_id, self.control_actor)
                       if bindings is None else bindings.get(thread_id))
            if binding is None:
                return None
            assignment = self.binding_service.workers.store.assignment(binding.assignment_id)
        except ThreadBootstrapBindingNotFoundError:
            return None
        except Exception:
            return {**unknown, "reason": "assignment_lookup_unavailable"}
        if assignment is None:
            return {**unknown, "reason": "assignment_missing"}
        indexed = self._index().get(thread_id)
        if (indexed is None
                or assignment.organization_id != self.control_actor.organization_id
                or assignment.workspace_id != self.control_actor.workspace_id
                or assignment.execution_id != binding.execution_id
                or assignment.id != binding.assignment_id
                or assignment.execution_workspace_id != binding.execution_workspace_id
                or assignment.project_id != indexed.project_id):
            return {**unknown, "reason": "assignment_scope_mismatch"}
        lease = assignment.lease
        lease_valid = bool(lease and lease.expires_at > observed_at
                           and lease.fence == assignment.fence
                           and lease.worker_id == assignment.assigned_worker_id)
        status = str(assignment.status)
        if status in {"running", "claimed"} and lease_valid:
            try:
                worker = self.binding_service.workers.store.worker(assignment.assigned_worker_id)
                lease_valid = bool(worker
                    and worker.organization_id == assignment.organization_id
                    and worker.workspace_id == assignment.workspace_id
                    and str(worker.lifecycle) in {"active", "draining"}
                    and (assignment.deadline_at is None or assignment.deadline_at > observed_at))
            except Exception:
                lease_valid = False
        state = {"cancelled": "cancelled", "succeeded": "idle", "failed": "failed",
                 "lost": "lost", "pending": "waiting", "claimed": "waiting"}.get(status, "unknown")
        if status == "running":
            state = "running" if lease_valid else "unknown"
        elif status == "claimed" and not lease_valid:
            state = "unknown"
        if status == "pending" and assignment.fence > 0:
            state = "recovering"
        process_observation = {}
        try:
            manager = self._manager_for_binding(getattr(assignment, "runtime_binding", None))
            session = manager.get(assignment.id)
            local = session.status() if session is not None else None
            if session is None:
                observe_unowned = getattr(manager, "unowned_lost_observation", None)
                ownership = observe_unowned(assignment) if callable(observe_unowned) else None
                if ownership is not None:
                    process_observation = {"processOwnershipObservation": ownership}
            if local and local.fence == assignment.fence and local.worker_id == assignment.assigned_worker_id:
                process_observation = {"processRunning": local.running,
                    "diagnosticState": getattr(local, "diagnostic_state", "none"),
                    "validationCustodyState": getattr(local, "validation_custody_state", "unavailable"),
                    "failureObservation": getattr(local, "failure_observation", None),
                    "pendingExecutorWork": getattr(local, "pending_executor_work", 0)}
                if status == "running" and not local.running:
                    state = "stopped"
        except Exception:
            # An unavailable process observation cannot establish live activity.
            # Canonical lease custody remains distinct from process evidence.
            pass
        return {**unknown, "state": state,
                "assignmentId": assignment.id, "assignmentStatus": status,
                "assignmentCreatedAt": assignment.created_at, "updatedAt": assignment.updated_at,
                "fence": assignment.fence, "leaseValid": lease_valid,
                "leaseExpiresAt": lease.expires_at if lease else None,
                "reason": assignment.failure_code, **process_observation,
                "live": status == "running" and lease_valid and process_observation.get("processRunning", True)}

    async def list(
        self,
        project_id: str | None = None,
        archived: bool = False,
        search: str | None = None,
        *,
        limit: int | None = None,
        cursor: str | None = None,
        actor: AuthenticationActor | None = None,
    ) -> dict[str, Any]:
        page_size = self.coerce_list_limit(limit)
        if self.thread_scope is not None:
            effective_actor = actor or self.control_actor
            if effective_actor is None:
                raise HTTPException(status_code=403, detail="Thread actor is required")
            project_id = self.thread_scope.project(project_id, effective_actor).id
        project = self._projects().get(project_id) if project_id else None
        project_path = project.path if project is not None else None
        ownership = self.thread_scope.ownership_snapshot() if self.thread_scope else None
        effective_actor = actor or self.control_actor

        def belongs(thread_id: str) -> bool:
            if self.thread_scope is None:
                indexed = self._index().get(thread_id)
                return not (project_id and indexed and indexed.project_id and indexed.project_id != project_id)
            if effective_actor is None:
                return False
            try:
                self.thread_scope.for_thread(thread_id, effective_actor, project_id, ownership=ownership)
                return True
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
                return False

        def indexed_belongs(indexed: IndexedThread) -> bool:
            if self.thread_scope is None:
                return not (
                    project_id
                    and indexed.project_id
                    and indexed.project_id != project_id
                )
            if effective_actor is None or project is None:
                return False
            owners = (ownership or {}).get(indexed.id, set())
            if owners:
                return owners == {
                    (
                        effective_actor.organization_id,
                        effective_actor.workspace_id,
                        project.id,
                    )
                }
            if indexed.project_id:
                return indexed.project_id == project.id
            return bool(indexed.cwd and indexed.cwd == project.path)
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
        if cursor_payload is None and project is not None and self.project_bindings is not None:
            def repair_bound_project_rows() -> None:
                repaired: dict[str, IndexedThread] = {}
                seen: set[str] = set()
                for binding in self.project_bindings(project.id):
                    if binding.project_id != project.id or binding.thread_id in seen:
                        continue
                    seen.add(binding.thread_id)
                    if not belongs(binding.thread_id):
                        continue
                    indexed = self._index().get(binding.thread_id)
                    if indexed is not None and indexed.project_id is not None:
                        continue
                    if indexed is None:
                        name = binding.thread_name or binding.route_prefix
                        if not name:
                            continue
                        indexed = IndexedThread(
                            id=binding.thread_id, name=name,
                            updatedAt=binding.updated_at,
                        )
                    repaired[binding.thread_id] = indexed.model_copy(update={
                        "project_id": project.id,
                        "cwd": indexed.cwd or project.path,
                        "path": indexed.path or project.path,
                    })
                if repaired:
                    self._index().upsert_many(list(repaired.values()))
            await asyncio.to_thread(repair_bound_project_rows)
        expected_revision = (
            cursor_payload.get("revision")
            if cursor_payload is not None
            else None
        )
        current_revision = self._index().revision(
            project_id=project_id,
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
                candidate_id = str(item.get("id") or item.get("threadId") or "").strip()
                if not candidate_id or not belongs(candidate_id):
                    continue
                existing = self._index().get(candidate_id)
                if existing is not None and existing.archived and not archived:
                    # Local archival survives a stale or unavailable provider.
                    # Explicit unarchive clears this canonical flag separately.
                    continue
                indexed = self._compact_runtime_thread(
                    item,
                    project_id=project_id,
                    project_path=project_path,
                    archived=archived,
                )
                if indexed is None:
                    continue
                if indexed.project_id is None:
                    existing = self._index().get(indexed.id)
                    canonical_project_id = (
                        existing.project_id
                        if existing is not None
                        else None
                    )
                    if (
                        canonical_project_id is None
                        and self.agent_sessions is not None
                        and self.control_actor is not None
                    ):
                        session = self.agent_sessions.find_by_native_id(
                            indexed.id,
                            self.control_actor,
                        )
                        canonical_project_id = (
                            session.project_id
                            if session is not None
                            else None
                        )
                    if canonical_project_id:
                        indexed = indexed.model_copy(
                            update={"project_id": canonical_project_id}
                        )
                # Runtime rows are kept only for bounded summary enrichment;
                # full turns/items never enter the list projection.
                runtime_rows[indexed.id] = item
                indexed_updates.append(indexed)
            if indexed_updates:
                self._index().upsert_many(indexed_updates)
            current_revision = self._index().revision(
                project_id=project_id,
            )

        after = (
            str(cursor_payload.get("after"))
            if cursor_payload is not None
            else None
        )
        indexed_page, next_after, scan_truncated = self._index().page(
            project_id=project_id,
            archived=archived,
            search=search,
            after=after,
            limit=page_size,
            include=indexed_belongs,
        )

        fallback_active_turns = (
            self.active_turn_loader()
            if self.active_turn_getter is None
            and self.active_turn_loader is not None
            else {}
        )
        # The binding store is a namespace document, not an indexed point store.
        # Decode once for this page; assignment/worker reads remain page-bounded.
        runner_bindings = {}
        runner_lookup_failed = False
        if indexed_page and self.bootstrap_bindings and self.binding_service and self.control_actor:
            try:
                runner_bindings = {item.thread_id: item for item in await asyncio.to_thread(
                    self.bootstrap_bindings.list, self.control_actor)}
            except Exception:
                runner_lookup_failed = True
        rows: list[dict[str, Any]] = []
        for indexed in indexed_page:
            runtime_item = runtime_rows.get(indexed.id)
            if self.active_turn_getter is not None:
                active = await asyncio.to_thread(
                    self._active_turn_for_list,
                    indexed.id,
                    fallback_active_turns,
                )
            else:
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
            runner = ({"schemaVersion": "1.0", "state": "unknown", "live": False,
                       "observedAt": time.time(), "source": "assignment",
                       "reason": "assignment_lookup_unavailable"} if runner_lookup_failed
                      else await asyncio.to_thread(self._runner_projection, indexed.id, runner_bindings))
            if runner is not None:
                row["runner"] = runner
                row["activity"] = await asyncio.to_thread(self._runner_activity, indexed.id, runner)
                if not runner["live"]:
                    row["status"] = {"type": "notLoaded" if runner["state"] == "unknown" else "idle"}
            rows.append(row)
            await asyncio.to_thread(
                self._adopt_legacy_thread,
                row,
                project_id=project_id,
            )

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

    async def create(self, *args, **kwargs) -> dict[str, Any]:
        provider_start_called = False
        preparation_stage = 'request_validation'
        def preparing(stage):
            nonlocal preparation_stage
            if stage not in _BOOTSTRAP_PREPARATION_STAGES:
                raise ValueError('Invalid bootstrap preparation stage')
            preparation_stage = stage
        def before_provider_start():
            nonlocal provider_start_called
            provider_start_called = True
        try:
            return await self._create(*args, **kwargs, before_provider_start=before_provider_start,
                                      preparation_stage=preparing)
        except Exception as exc:
            if provider_start_called:
                if isinstance(exc, ThreadBootstrapPrestartRefusal):
                    # An adapter cannot supply controller pre-start authority
                    # after the provider boundary, even with the same type.
                    raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
                raise
            diagnostic = {'bootstrap_preparation_stage': preparation_stage,
                          'bootstrap_exception_type': bootstrap_exception_type(exc),
                          'provider_start_called': False}
            if isinstance(exc, HTTPException):
                diagnostic['bootstrap_http_status'] = exc.status_code
                if isinstance(exc.detail, dict):
                    code = exc.detail.get('code')
                    if isinstance(code, str) and code in _BOOTSTRAP_HTTP_REFUSAL_CODES:
                        diagnostic['bootstrap_refusal_code'] = code
                    blockers = exc.detail.get('blockers')
                    if isinstance(blockers, list) and len(blockers) == 1 and isinstance(blockers[0], dict):
                        code = blockers[0].get('code')
                        if isinstance(code, str) and code in _BOOTSTRAP_BLOCKER_CODES:
                            diagnostic['bootstrap_blocker_code'] = code
                # Preserve the original public status/detail/cause. This private
                # controller attribute is metadata, never retry or process proof
                # from a provider-supplied exception after its start boundary.
                refusal = ThreadBootstrapPrestartRefusal(status_code=exc.status_code, detail=exc.detail)
                refusal._controller_preparation_diagnostic = {'code': 'bootstrap_prestart_failed', **diagnostic}
                raise refusal from exc
            event = {'type': 'thread_bootstrap_prestart_failed', **diagnostic}
            for key in ('project_id', 'agent_profile_id'):
                value = kwargs.get(key)
                if (isinstance(value, str) and len(value) <= 128
                        and all(c.isascii() and (c.isalnum() or c in '._:-') for c in value)):
                    event[key] = value
            token = kwargs.get('bootstrap_token')
            if (isinstance(token, str) and len(token) == 32
                    and all(c in '0123456789abcdef' for c in token)):
                event['bootstrap_id'] = 'bootstrap-' + token
                event['execution_id'] = 'thread-bootstrap-' + token
            with contextlib.suppress(Exception):
                self.event_sink(event)
            raise ThreadBootstrapPrestartRefusal(status_code=503, detail={
                'code':'bootstrap_prestart_failed',
                'message':'Bootstrap preparation failed before provider thread startup.',
                **diagnostic}) from exc

    async def _create(
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
        resource_context=None,
        execution_profile_id: str | None = None,
        actor: AuthenticationActor | None = None,
        agent_profile_id: str | None = None,
        agent_profile_revision: int | None = None,
        writable_repository_resource_ids: tuple[str, ...] = (),
        bootstrap_token: str | None = None,
        before_provider_start: Callable[[], None] | None = None,
        admission_guard: Callable[[], None] | None = None,
        assignment_admission=None,
        recipient_origin=None,
        preparation_stage: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        def stage(value):
            if preparation_stage is not None:
                preparation_stage(value)
        # Internal recovery correlation; the public create request cannot set it.
        if bootstrap_token is not None:
            try:
                if uuid.UUID(hex=bootstrap_token).hex != bootstrap_token:
                    raise ValueError('noncanonical bootstrap token')
            except (ValueError, AttributeError) as exc:
                raise HTTPException(status_code=422, detail='Invalid bootstrap recovery correlation.') from exc
        (
            binding_service,
            _default_session_manager,
            bootstrap_bindings,
            control_actor,
        ) = self._require_bootstrap_routing()
        project = self._projects().get(project_id)
        stage('profile_resolution')
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
        if provider_id is None and runtime_id is None:
            desired = _runtime_for_model(model)
            if desired is not None:
                provider_id, runtime_id = desired
        stage('runtime_routing')
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
        token = bootstrap_token or uuid.uuid4().hex
        bootstrap_id = f"bootstrap-{token}"
        execution_id = f"thread-bootstrap-{token}"

        if admission_guard is not None:
            stage('admission_before_reservation')
            await PostTurnReadinessService._run_owned(
                lambda: asyncio.to_thread(admission_guard), seconds=5.,
                name='thread-admission-current-guard')
        try:
            stage('bootstrap_reservation')
            binding = await asyncio.to_thread(
                binding_service.prepare_bootstrap,
                bootstrap_id=bootstrap_id,
                execution_id=execution_id,
                project_id=project.id,
                sandbox=effective_sandbox,
                approval_policy=effective_approval_policy,
                runtime_binding=runtime_binding,
                explicit_repository_id=repository_resource_id,
                read_only_repository_ids=read_only_repository_resource_ids,
                **({"resource_context":resource_context} if resource_context is not None else {}),
                execution_profile_id=effective_execution_profile_id,
                agent_profile=agent_profile_binding,
                **({'assignment_admission': assignment_admission} if assignment_admission is not None else {}),
                **({'writable_repository_ids':writable_repository_resource_ids}
                   if writable_repository_resource_ids else {}),
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
        if admission_guard is not None:
            try:
                stage('admission_before_worker_start')
                await PostTurnReadinessService._run_owned(
                    lambda: asyncio.to_thread(admission_guard), seconds=5.,
                    name='thread-admission-current-guard')
            except Exception as exc:
                from codex_web.execution_workers import AssignmentCancelRequest
                from codex_web.thread_bootstrap import BootstrapPreworkerRefusal
                # A just-prepared bootstrap has not crossed worker startup.
                # Cancellation is fenced and retains all files, as elsewhere.
                await asyncio.to_thread(binding_service.workers.cancel_bootstrap,
                    binding.assignment_id, AssignmentCancelRequest(expected_fence=0,
                        reason="governor admission changed before isolated worker startup"), actor=control_actor,
                    preworker_refusal=BootstrapPreworkerRefusal(
                        bootstrap_id=bootstrap_id, execution_id=execution_id,
                        error_type=type(exc).__name__,
                        guard_deadline_exceeded=(isinstance(exc, TimeoutError)
                            and str(exc) == 'Post-turn readiness operation deadline exceeded'),
                        origin=recipient_origin,
                        error_http_status=(exc.status_code if isinstance(exc, HTTPException) else None)))
                raise
        stage('worker_start')
        session = await session_manager.start(binding.assignment_id)
        if resource_context is not None:
            stage('resource_context_validation')
            binding_service.validate_resource_context(resource_context,project,agent_profile_binding)
            await session._supervise("resource_context_authority",session._validate_current_authority,
                seconds=session._lease_budget())
        stage('session_workspace_validation')
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

        stage('runtime_request_preparation')
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
        params["sandboxPolicy"] = assignment_sandbox_policy(
            self._projects().sandbox_policy(
                effective_sandbox,
                workspace_cwd,
            ),
            mode=effective_sandbox,
            writable_roots=tuple(
                str(path)
                for path in (
                    getattr(session, "git_metadata_path", None),
                    getattr(session, "git_worktree_metadata_path", None),
                )
                if path is not None
            ),
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
            if admission_guard is not None:
                stage('admission_before_provider_start')
                await PostTurnReadinessService._run_owned(
                    lambda: asyncio.to_thread(admission_guard), seconds=5.,
                    name='thread-admission-current-guard')
            if before_provider_start is not None:
                before_provider_start()
            runtime_result = await runtime_adapter.create_session(runtime_request)
            response = runtime_result.payload
        except asyncio.CancelledError as exc:
            # This session already owns a registered worker fence. Request
            # cancellation must settle its canonical cleanup before returning;
            # another cancellation must not interrupt that cleanup task.
            cleanup = asyncio.create_task(session_manager.complete(
                binding.assignment_id,
                succeeded=False,
                failure_code="native_thread_create_cancelled",
                failure_message=(str(exc).strip() or type(exc).__name__)[:500],
            ))
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            try:
                cleanup.result()
            except BaseException as cleanup_error:
                raise exc from cleanup_error
            raise
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
                    failure_message=(str(exc).strip() or type(exc).__name__)[:500],
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
            **({"resource_context":resource_context} if resource_context is not None else {}),
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
                project_path=project.path,
                archived=False,
            )
            if indexed is not None and self.thread_index is not None:
                self.thread_index.upsert(indexed)
        return response

    def _canonical_history_response(
        self,
        thread_id: str,
    ) -> dict[str, Any] | None:
        canonical_thread = (
            self.thread_history.thread(thread_id)
            if self.thread_history is not None
            else None
        )
        if canonical_thread is None:
            return None
        indexed = None
        if self.thread_index is not None:
            with contextlib.suppress(Exception):
                indexed = self.thread_index.get(thread_id)
        if indexed is not None:
            canonical_thread.update(
                {
                    "name": indexed.name,
                    "cwd": indexed.cwd,
                    "path": indexed.path,
                    "model": indexed.model,
                }
            )
        if self._active_turn_for_list(thread_id) is not None:
            canonical_thread["status"] = {"type": "active"}
        return {"thread": canonical_thread}

    async def read(
        self,
        thread_id: str,
        message_limit: int | None = None,
        turn_limit: int | None = None,
    ) -> dict[str, Any]:
        response = await self._read(thread_id, message_limit, turn_limit)
        runner = await asyncio.to_thread(self._runner_projection, thread_id)
        if runner is None or not isinstance(response.get("thread"), dict):
            return response
        activity = await asyncio.to_thread(self._runner_activity, thread_id, runner)
        thread = {**response["thread"], "runner": runner, "activity": activity}
        if not runner["live"]:
            thread["status"] = {"type": "notLoaded" if runner["state"] == "unknown" else "idle"}
        return {**response, "thread": thread}

    async def runner(self, thread_id: str) -> dict[str, Any]:
        """Inspect custody without touching the provider or starting a session."""
        projection = await asyncio.to_thread(self._runner_projection, thread_id)
        activity = await asyncio.to_thread(self._runner_activity, thread_id, projection)
        return {"id": thread_id, "runner": projection, "activity": activity,
                "status": {"type": {"active": "active", "idle": "idle"}.get(activity["state"], "notLoaded")}}

    def _runner_activity(self, thread_id: str, runner: dict[str, Any] | None) -> dict[str, Any]:
        """Current exact-turn marker, never the retained transcript or lease alone.

        Persistent bootstrap custody can be live for many idle conversations.
        Missing/stale marker evidence cannot manufacture a turn or its outcome.
        """
        unknown = {"state": "unknown", "turnId": None, "source": "current_turn"}
        if runner is None or not runner["live"]:
            return unknown
        if self.active_turn_getter is None:
            return unknown
        try:
            active = self.active_turn_getter(thread_id)
        except Exception:
            return unknown
        if active is None:
            return {**unknown, "state": "idle"}
        if (active.assignment_id != runner["assignmentId"]
                or active.fence != runner["fence"]
                or not active.turn_id):
            return unknown
        return {**unknown, "state": "active", "turnId": active.turn_id}

    async def _read(
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
            if (
                getattr(exc, "status_code", None) == 503
                or self._resume().is_timeout_error(exc)
            ):
                canonical_response = self._canonical_history_response(thread_id)
                if canonical_response is not None:
                    return self.trim_messages(canonical_response, limit)
                transcript = self._transcript_response(
                    thread_id,
                    degraded_error=str(getattr(exc, "detail", exc)),
                )
                if transcript is not None:
                    return self.trim_messages(transcript, limit)
            if self._resume().is_timeout_error(exc):
                return self._resume().read_timeout_response(thread_id, limit, exc)
            raise
        runtime_thread = (
            response.get("thread")
            if isinstance(response, dict)
            and isinstance(response.get("thread"), dict)
            else response
        )
        runtime_status = (
            runtime_thread.get("status")
            if isinstance(runtime_thread, dict)
            else None
        )
        runtime_read_degraded = bool(
            isinstance(response, dict)
            and response.get("ok") is False
        ) or bool(
            isinstance(runtime_thread, dict)
            and runtime_thread.get("readTimedOut")
        ) or bool(
            isinstance(runtime_status, dict)
            and runtime_status.get("type") == "notLoaded"
        )
        canonical_response = self._canonical_history_response(thread_id)
        if (
            canonical_response is not None
            and (
                not isinstance(runtime_thread, dict)
                or not isinstance(runtime_thread.get("turns"), list)
                or runtime_read_degraded
            )
        ):
            return self.trim_messages(canonical_response, limit)
        if (
            isinstance(response, dict)
            and runtime_read_degraded
        ):
            transcript = self._transcript_response(
                thread_id,
                degraded_error=str(
                    response.get("error") or "agent runtime session is not live"
                ),
            )
            if transcript is not None:
                return self.trim_messages(transcript, limit)
            return self._resume().read_timeout_response(
                thread_id,
                limit,
                str(response.get("error") or "agent runtime session is not live"),
                event_type="web_read_degraded_for_runtime",
            )
        if (
            not isinstance(runtime_thread, dict)
            or not isinstance(runtime_thread.get("turns"), list)
        ):
            transcript = self._transcript_response(thread_id)
            if transcript is not None:
                return self.trim_messages(transcript, limit)
        return self.trim_messages(response, limit)

    async def rename(self, thread_id: str, name: str) -> dict[str, Any]:
        self._recovery().raise_if_thread_replaced(thread_id)
        await self._naming().set_name(thread_id, name)
        return {"ok": True, "threadId": thread_id, "name": name}

    def update_settings(self, thread_id: str, payload: ThreadRunSettings) -> dict[str, Any]:
        self._recovery().raise_if_thread_replaced(thread_id)
        if payload.resource_context is not None:
            raise HTTPException(status_code=409,detail={'code':'resource_context_requires_versioned_turn_admission'})
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
        indexed = self._index().get(thread_id)
        try:
            response = (
                await self._codex_adapter(thread_id).close_session(thread_id)
            ).payload
        except HTTPException as exc:
            if (
                exc.status_code != 503 or indexed is None
                or indexed.project_id is None or self.project_bindings is None
                or self.binding_service is None or self.bootstrap_bindings is None
                or self.control_actor is None
                or self._active_turn_for_list(thread_id) is not None
            ):
                raise
            bindings = await asyncio.to_thread(self.project_bindings, indexed.project_id)
            if any(binding.thread_id == thread_id for binding in bindings):
                raise
            try:
                bootstrap = await asyncio.to_thread(
                    self.bootstrap_bindings.get_by_thread, thread_id, self.control_actor,
                )
            except ThreadBootstrapBindingNotFoundError:
                raise exc
            assignment = await asyncio.to_thread(
                self.binding_service.workers.store.assignment, bootstrap.assignment_id,
            )
            if assignment is not None and (
                assignment.status not in {"failed", "succeeded", "cancelled", "lost"}
                or assignment.lease is not None
            ):
                raise
            # An inactive, unbound predecessor with no viable assignment can
            # be archived canonically without reviving its unavailable runtime.
            response = {"ok": True, "threadId": thread_id, "archived": True,
                        "providerArchived": False, "archivePending": True}
            self.event_sink({"type": "thread_archived_locally", "thread_id": thread_id,
                             "project_id": indexed.project_id, "provider_archived": False})
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
        active = (
            self.active_turn_getter(thread_id)
            if self.active_turn_getter is not None
            else None
        )
        return (
            await self._codex_adapter(thread_id).interrupt(
                thread_id,
                getattr(active, "turn_id", None),
            )
        ).payload
