from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
import json
import os
import time
import weakref
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Mapping

from fastapi import HTTPException

from codex_web.agent_profiles import AgentProfileExecutionBinding
from codex_web.agent_runtime import (
    AgentRuntimeEvent,
    AgentRuntimeSessionRequest,
    AgentRuntimeTurnRequest,
)
from codex_web.agent_routing import AgentRoutingRequest
from codex_web.execution_workers import (
    AssignmentCancelRequest,
    AssignmentStatus,
    ExecutionRuntimeBinding,
)
from codex_web.ux_briefs import UxBriefTurnBinding
from codex_web.identity import AuthenticationActor
from codex_web.models import ActiveThreadTurn, BotBinding, BotReplyTarget, Project, QueuedTurn
from codex_web.paths import SLACK_RELAY_NOTICE
from codex_web.provider_capacity import ProviderCapacityWaitCreate
from codex_web.resources import RepositoryTargetSource
from codex_web.services.codex_agent_runtime import CodexAgentRuntimeAdapter
from codex_web.services.agent_routing import (
    AgentCapacityRoutingError,
    AgentRoutingBlockedError,
    AgentRoutingError,
    AgentRoutingService,
)
from codex_web.services.provider_capacity import (
    ProviderCapacityBlockedError,
    ProviderCapacityService,
)
from codex_web.services.project_runtime import assignment_sandbox_policy
from codex_web.services.local_execution_worker import LocalExecutionWorkerCapacityError
from codex_web.services.execution_preflight import QueuedRetentionRefused
from codex_web.services.replicated_ownership import ReplicatedOwnershipService
from codex_web.services.turn_queue_policy import TurnQueuePolicy
from codex_web.services.agent_worker_session import (
    AssignmentBoundAgentSessionManager,
    AssignmentBoundAgentSessionStaleError,
)
from codex_web.services.thread_bootstrap_bindings import (
    ThreadBootstrapBindingNotFoundError,
    ThreadBootstrapBindingService,
)
from codex_web.services.turn_execution_binding import (
    TurnExecutionBindingError,
    TurnExecutionBindingService,
)
from codex_web.services.work_item_contracts import assignment_control_plane_instructions
from codex_web.storage.thread_history import ThreadHistoryRepository


def _turn_failure_text(message: dict[str, Any]) -> str | None:
    """Extract a stable error string from Codex terminal-turn events."""

    params = message.get("params") or {}
    turn = params.get("turn") or {}
    raw_error = params.get("error") or turn.get("error")
    if raw_error is None:
        return None
    if isinstance(raw_error, str):
        return raw_error.strip() or None
    if isinstance(raw_error, dict):
        for key in ("message", "detail", "error", "codexErrorInfo"):
            value = raw_error.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return json.dumps(raw_error, sort_keys=True, default=str)
    return str(raw_error).strip() or None


class ExecutionPrestartRefusal(HTTPException):
    """Controller-owned routing/profile refusal raised before native turn input."""


def _execution_preflight_blocked(
    exc: TurnExecutionBindingError,
) -> HTTPException:
    """Translate deterministic binding failures at every bootstrap boundary."""

    return ExecutionPrestartRefusal(
        status_code=503,
        detail={
            "code": "execution_preflight_blocked",
            "message": str(exc),
            "blockers": [exc.public()],
            "retryable": exc.public().get("retryable") is True,
        },
    )


class _ThreadRuntimeTransport:
    def __init__(self, service: "TurnExecutionService", thread_id: str) -> None:
        self.service = service
        self.thread_id = thread_id

    async def request(self, method: str, params: dict[str, Any] | None = None):
        return await self.service.request_for_thread(
            self.thread_id,
            method,
            params or {},
        )


def _runtime_for_model(model: str | None) -> tuple[str, str] | None:
    """Map a model's provider prefix onto its execution runtime.

    Deterministic affinity only: the model prefix selects the runtime family
    that can execute it. It never grants authority or widens routing policy.
    Unprefixed model ids stay on the thread's current runtime.
    """

    value = str(model or "").strip()
    if value.startswith("mammouth-ai/"):
        return ("mammouth-ai", "mammouth-cli")
    if value.startswith("codex/"):
        return ("openai", "codex")
    return None


def _runtime_model_id(model: str | None, runtime_binding) -> str | None:
    """Strip the provider prefix a runtime must not see.

    Codex model values are namespaced (``codex/gpt-5.6-sol``) so both
    catalogs can be listed unambiguously; the codex app-server itself
    expects the bare model id.
    """

    value = str(model or "").strip()
    provider = getattr(runtime_binding, "provider_id", None)
    if provider == "openai" and value.startswith("codex/"):
        return value[len("codex/"):]
    return value or None


class AutomatedAdmissionPaused(HTTPException):
    """A refusal proven locally before native start, never a provider outcome."""


class TurnExecutionService:
    """Own turn execution, queue draining, activity and terminal recovery state."""

    @staticmethod
    async def _settle_custody_call(function, *args, work_owner=None, **kwargs):
        # Retain the REAL concurrent Future, as the worker supervisor does.
        # Cancelling a to_thread Task/wrapper is not actual thread completion.
        # Production uses the session's existing serial executor/custody set;
        # standalone callers own and join a bounded one-worker executor.
        executor = getattr(work_owner, '_supervision_executor', None)
        private_executor = executor is None
        if private_executor:
            executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='session-custody')
        future = executor.submit(contextvars.copy_context().run, function, *args, **kwargs)
        tracked = getattr(work_owner, '_executor_work', None)
        if tracked is not None:
            tracked.add(future)
            future.add_done_callback(tracked.discard)
        cancelled = False
        try:
            while not future.done():
                try:
                    # A cancelled async wrapper can be replaced; the exact
                    # submitted work is never replaced, replayed or abandoned.
                    await asyncio.shield(asyncio.wrap_future(future))
                except asyncio.CancelledError:
                    cancelled = True
                except Exception:
                    break  # Observe the real terminal exception below.
            if cancelled:
                if not future.cancelled():
                    future.exception()
                raise asyncio.CancelledError
            return future.result()
        finally:
            if private_executor:
                executor.shutdown(wait=True)

    def __init__(
        self,
        host: Any,
        *,
        binding_service: TurnExecutionBindingService | None = None,
        session_manager: AssignmentBoundAgentSessionManager | None = None,
        bootstrap_bindings: ThreadBootstrapBindingService | None = None,
        control_actor: AuthenticationActor | None = None,
        routing_service: AgentRoutingService | None = None,
        session_managers: Mapping[tuple[str, str], AssignmentBoundAgentSessionManager] | None = None,
        runtime_adapter_factory: Callable[[ExecutionRuntimeBinding, Any], Any] | None = None,
        provider_capacity: ProviderCapacityService | None = None,
        ownership: ReplicatedOwnershipService | None = None,
        bindings_for_thread: Callable[[str], list[BotBinding]] | None = None,
        actor_resolver: Callable[[str, Project], AuthenticationActor] | None = None,
        skill_context_resolver: Callable[
            [tuple, Project, str], Any
        ] | None = None,
        work_item_context_resolver: Callable[[str], dict[str, Any]] | None = None,
        work_item_context_recorder: Callable[..., Any] | None = None,
        work_item_outcome_recorder: Callable[..., Any] | None = None,
        thread_history: ThreadHistoryRepository | None = None,
        transcript: Any | None = None,
        maintenance_admission=None,
    ) -> None:
        self.host = host
        self.handoff_delivery_recorder: Callable[..., Any] | None = None
        self.queued_preflight = None
        # Diagnostic throttle only: canonical routes are revalidated on every wake.
        self._queued_retention_notice_at: dict[str, float] = {}
        self.owner_routing_validator = None
        self.procedure_admission = None
        self.native_input_guard_factory = None
        self.readiness_origin_blocker = None
        self.post_turn_readiness = None
        self.session_custody = None
        self.notification_batch_handler = None
        self.automated_admission_allowed: Callable[[], bool] | None = None
        self.project_coordination_admission = None
        try:
            self._event_loop = asyncio.get_running_loop()
        except RuntimeError:
            self._event_loop = None
        self.binding_service = binding_service
        self.session_manager = session_manager
        self.bootstrap_bindings = bootstrap_bindings
        self.control_actor = control_actor
        self.routing_service = routing_service
        self.session_managers = dict(session_managers or {})
        if session_manager is not None:
            self.session_managers.setdefault(("openai", "codex"), session_manager)
        self.runtime_adapter_factory = runtime_adapter_factory
        self.provider_capacity = provider_capacity
        self.ownership = ownership
        self.bindings_for_thread = (
            bindings_for_thread
            or getattr(host, "_bindings_for_thread", lambda _thread_id: [])
        )
        self.actor_resolver = actor_resolver
        self.skill_context_resolver = skill_context_resolver
        self.work_item_context_resolver = work_item_context_resolver
        self.work_item_context_recorder = work_item_context_recorder
        self.work_item_outcome_recorder = work_item_outcome_recorder
        self.thread_history = thread_history
        self.transcript = transcript
        self.maintenance_admission = maintenance_admission
        self.upgrade_turn_admission_guard = None
        self.turn_start_lock = asyncio.Lock()
        self._turn_start_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        self.queue_drain_tasks: dict[str, asyncio.Task[None]] = {}
        self.queue_admission_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        self.terminal_recovery_tasks: dict[str, asyncio.Task[None]] = {}
        self.assignment_completion_tasks: dict[str, asyncio.Task[None]] = {}
        self.thread_completion_tasks: dict[str, asyncio.Task[None]] = {}
        self.thread_handoffs: set[str] = set()
        self.activity_heartbeat_at: dict[str, float] = {}
        self.terminal_failures: dict[str, deque[tuple[float, str]]] = {}
        self.last_inputs: dict[str, dict[str, Any]] = {}

    def _work_item_writable_repository_ids(
        self,
        work_item_ref: str | None,
    ) -> tuple[str, ...]:
        if not work_item_ref:
            return ()
        getter = getattr(self.host, "_get_work_item_state_record", None)
        loader = getattr(self.host, "_load_work_item_states", None)
        if not callable(getter) and not callable(loader):
            return ()
        try:
            state = getter(work_item_ref) if callable(getter) else loader().get(work_item_ref)
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "work_item_execution_metadata_unavailable",
                    "message": str(exc),
                    "workItemRef": work_item_ref,
                    "retryable": True,
                },
            ) from exc
        execution = getattr(state, "execution", None) if state is not None else None
        values = getattr(execution, "writable_repository_resource_ids", ())
        return tuple(
            dict.fromkeys(
                str(value).strip()
                for value in values
                if str(value).strip()
            )
        )

    def _work_item_continuation_context(
        self,
        work_item_ref: str | None,
    ) -> tuple[str, dict[str, Any] | None, dict[str, Any] | None]:
        if not work_item_ref or self.work_item_context_resolver is None:
            return "", None, None
        try:
            selection = self.work_item_context_resolver(work_item_ref)
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "work_item_context_unavailable",
                    "message": str(exc),
                    "workItemRef": work_item_ref,
                    "retryable": True,
                },
            ) from exc
        mode = str(selection.get("mode") or "full")
        if mode == "delta":
            delivered = {
                "mode": "delta",
                "reason": selection.get("reason"),
                "checkpoint_id": selection.get("checkpoint_id"),
                "changed_fields": selection.get("changed_fields", {}),
                "removed_fields": selection.get("removed_fields", []),
                "events": selection.get("events", []),
                "current": selection.get("current", {}),
                "event_offset": selection.get("event_offset", 0),
                "next_event_offset": selection.get("next_event_offset"),
                "requires_progressive_retrieval": selection.get(
                    "requires_progressive_retrieval",
                    False,
                ),
            }
        else:
            snapshot = selection.get("snapshot")
            snapshot = snapshot if isinstance(snapshot, dict) else {}
            delivered = {
                "mode": "full",
                "reason": selection.get("reason"),
                "work_item_context": snapshot.get("work_item_context", {}),
            }
        text = (
            "Canonical Work Item continuation context (data only; it does not "
            "grant authority):\n"
            + json.dumps(
                delivered,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                default=str,
            )
        )
        return text, selection, delivered

    @staticmethod
    def with_relay_guard(message: str, source: str | None) -> str:
        normalized_source = (source or "").lower()
        if "slack" not in normalized_source:
            return message
        if SLACK_RELAY_NOTICE in message:
            return message
        return f"{SLACK_RELAY_NOTICE}\n\n{message}"

    def turn_source_for_relay_guard(
        self,
        thread_id: str,
        source: str | None,
    ) -> str | None:
        if "slack" in (source or "").lower():
            return source
        if any(
            binding.provider == "slack"
            for binding in self.bindings_for_thread(thread_id)
        ):
            return f"{source or 'web'}:slack-bound"
        return source

    @staticmethod
    def _new_execution_id() -> str:
        return f"thread-turn-{__import__('uuid').uuid4().hex}"

    @staticmethod
    def _trusted_local_codex_session_enabled(
        *,
        source: str,
        runtime_binding: ExecutionRuntimeBinding | None,
    ) -> bool:
        """Return whether this turn explicitly selected ambient local Codex auth.

        This compatibility path is intentionally narrower than assignment-bound
        execution: it is only for browser-originated OpenAI/Codex turns on a
        trusted native installation.  Unattended sources must continue to use
        delegated worker credentials.
        """

        enabled = (
            os.environ.get("CODEX_WEB_TRUSTED_LOCAL_CODEX_SESSION", "")
            .strip()
            .casefold()
            in {"1", "true", "yes", "on"}
        )
        deployment_mode = (
            os.environ.get("CODEX_WEB_DEPLOYMENT_MODE", "local")
            .strip()
            .casefold()
        )
        original_source = source.strip().casefold()
        while True:
            prefix, separator, remainder = original_source.partition(":")
            if separator and prefix in {"queued", "steer"}:
                original_source = remainder
                continue
            break
        if (
            not enabled
            or deployment_mode != "local"
            or original_source != "web"
        ):
            return False
        return runtime_binding is None or (
            runtime_binding.provider_id == "openai"
            and runtime_binding.runtime_id == "codex"
        )

    def _require_worker_routing(self) -> tuple[
        TurnExecutionBindingService,
        AssignmentBoundAgentSessionManager,
    ]:
        if self.binding_service is None or self.session_manager is None:
            raise HTTPException(
                status_code=503,
                detail="assignment-bound Codex turn execution is unavailable",
            )
        return self.binding_service, self.session_manager

    async def _select_runtime_binding(
        self,
        *,
        project_id: str,
        sandbox: str,
        trusted_local_codex_session: bool = False,
        actor: AuthenticationActor | None = None,
        agent_profile_id: str | None = None,
        agent_profile_revision: int | None = None,
    ):
        if self.routing_service is None or self.control_actor is None:
            return None, None
        routing_actor = actor or self.control_actor
        try:
            routed = await self.routing_service.route(
                AgentRoutingRequest(
                    project_id=project_id,
                    agent_profile_id=agent_profile_id,
                    agent_profile_revision=agent_profile_revision,
                    require_persistent_session=True,
                    allowed_provider_ids=(
                        ("openai",)
                        if trusted_local_codex_session
                        else ()
                    ),
                    allowed_runtime_ids=(
                        ("codex",)
                        if trusted_local_codex_session
                        else ()
                    ),
                    preferred_provider_ids=(
                        ("openai",)
                        if trusted_local_codex_session
                        else ()
                    ),
                    preferred_runtime_ids=(
                        ("codex",)
                        if trusted_local_codex_session
                        else ()
                    ),
                    allow_fallback=not trusted_local_codex_session,
                    required_sandbox_profile=(
                        None
                        if trusted_local_codex_session
                        else sandbox
                    ),
                    required_network_profile=None,
                    allowed_network_profiles=(
                        ()
                        if trusted_local_codex_session
                        else (
                            "brokered-model-egress",
                            "direct-provider-egress",
                        )
                    ),
                ),
                actor=routing_actor,
            )
        except AgentCapacityRoutingError:
            raise
        except AgentRoutingBlockedError as exc:
            raise ExecutionPrestartRefusal(
                status_code=409,
                detail={
                    "code": "execution_preflight_blocked",
                    "message": str(exc),
                    "blockers": [exc.public()],
                    "retryable": exc.retryable,
                },
            ) from exc
        except AgentRoutingError as exc:
            detail = str(exc)
            lowered = detail.casefold()
            if "sandbox_profile_mismatch" in lowered:
                code = "sandbox_profile_unsupported"
                remediation = "/api/agent-providers"
            elif "network_profile_mismatch" in lowered:
                code = "network_policy_unsupported"
                remediation = "/api/agent-providers"
            elif "agent profile" in lowered:
                code = "agent_profile_incompatible"
                remediation = "/api/agent-profiles"
            else:
                code = "execution_profile_incompatible"
                remediation = "/api/agent-providers"
            raise ExecutionPrestartRefusal(
                status_code=409,
                detail={
                    "code": "execution_preflight_blocked",
                    "message": detail,
                    "blockers": [
                        {
                            "code": code,
                            "message": detail,
                            "retryable": False,
                            "target_type": (
                                "agent_profile"
                                if agent_profile_id
                                else "agent_runtime"
                            ),
                            "target_id": agent_profile_id,
                            "remediation_route": remediation,
                        }
                    ],
                    "retryable": False,
                },
            ) from exc
        return (
            routed.selected_runtime.execution_binding(),
            routed.agent_profile,
        )

    def _manager_for_binding(
        self,
        binding: ExecutionRuntimeBinding | None,
    ) -> AssignmentBoundAgentSessionManager:
        if binding is None:
            if self.session_manager is None:
                raise HTTPException(
                    status_code=503,
                    detail="assignment-bound turn execution is unavailable",
                )
            return self.session_manager
        manager = self.session_managers.get((binding.provider_id, binding.runtime_id))
        if manager is None:
            raise HTTPException(
                status_code=503,
                detail="selected agent runtime has no assignment-bound session manager",
            )
        return manager

    async def _capacity_error(
        self,
        runtime_binding: ExecutionRuntimeBinding | None,
        exc: Exception,
    ) -> ProviderCapacityBlockedError | None:
        if self.provider_capacity is None or self.control_actor is None:
            return None
        provider_id = (
            runtime_binding.provider_id
            if runtime_binding is not None
            else "openai"
        )
        runtime_id = (
            runtime_binding.runtime_id
            if runtime_binding is not None
            else "codex"
        )
        record = None
        if (provider_id, runtime_id) == ("openai", "codex"):
            try:
                record = await self.provider_capacity.refresh_if_due(
                    provider_id,
                    runtime_id,
                    actor=self.control_actor,
                )
            except Exception:
                record = None
            if (
                record is not None
                and record.blocks(float(self.provider_capacity.clock()))
            ):
                return ProviderCapacityBlockedError(record)
        record = self.provider_capacity.report_exception(
            provider_id,
            runtime_id,
            exc,
            actor=self.control_actor,
            source=f"agent-runtime:{provider_id}/{runtime_id}",
        )
        if (
            record is not None
            and record.blocks(float(self.provider_capacity.clock()))
        ):
            return ProviderCapacityBlockedError(record)
        return None

    def wait_for_thread_capacity(
        self,
        *,
        thread_id: str,
        execution_id: str | None,
        provider_keys: tuple[str, ...],
        retry_at: float | None,
        reason: str,
    ):
        if self.provider_capacity is None or self.control_actor is None:
            delay = self.host._thread_resume_retry_delay()
            asyncio.get_running_loop().call_later(
                delay,
                self.schedule_queue_drain,
                thread_id,
            )
            return None
        effective_retry_at = retry_at
        if effective_retry_at is None:
            effective_retry_at = (
                float(self.provider_capacity.clock())
                + self.provider_capacity.default_retry_seconds
            )
        return self.provider_capacity.wait_for_capacity(
            ProviderCapacityWaitCreate(
                thread_id=thread_id,
                execution_id=execution_id,
                provider_keys=provider_keys,
                retry_at=effective_retry_at,
                reason=reason,
            ),
            actor=self.control_actor,
        )

    def _session_for_assignment(
        self,
        assignment_id: str,
    ) -> tuple[AssignmentBoundAgentSessionManager, Any]:
        managers = tuple(dict.fromkeys(self.session_managers.values()))
        if self.session_manager is not None and self.session_manager not in managers:
            managers = (*managers, self.session_manager)
        for manager in managers:
            session = manager.get(assignment_id)
            if session is not None:
                return manager, session
        raise HTTPException(
            status_code=503,
            detail="thread bootstrap binding has no live agent runtime session",
        )

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

    def _thread_queue(self, thread_id: str) -> list[QueuedTurn]:
        loader = getattr(self.host, "_thread_queue_record", None)
        if callable(loader):
            return list(loader(thread_id))
        return list(self.host._load_turn_queues().get(thread_id) or [])

    def _save_thread_queue(
        self,
        thread_id: str,
        items: list[QueuedTurn],
    ) -> None:
        saver = getattr(self.host, "_put_thread_queue_record", None)
        deleter = getattr(self.host, "_delete_thread_queue_record", None)
        if items and callable(saver):
            saver(thread_id, items)
            return
        if not items and callable(deleter):
            deleter(thread_id)
            return
        queues = self.host._load_turn_queues()
        if items:
            queues[thread_id] = items
        else:
            queues.pop(thread_id, None)
        self.host._save_turn_queues(queues)

    def _update_thread_queue(
        self,
        thread_id: str,
        updater: Callable[[list[QueuedTurn]], list[QueuedTurn]],
    ) -> list[QueuedTurn]:
        update_record = getattr(
            self.host,
            "_update_thread_queue_record",
            None,
        )
        if callable(update_record):
            return list(update_record(thread_id, updater))
        items = self._thread_queue(thread_id)
        updated = list(updater(items))
        self._save_thread_queue(thread_id, updated)
        return updated

    def _active_turn(self, thread_id: str) -> ActiveThreadTurn | None:
        loader = getattr(self.host, "_get_active_turn_record", None)
        if callable(loader):
            return loader(thread_id)
        return self.host._load_active_turns().get(thread_id)

    def _save_active_turn(self, active: ActiveThreadTurn) -> None:
        saver = getattr(self.host, "_put_active_turn_record", None)
        if callable(saver):
            saver(active.thread_id, active)
            return
        active_turns = self.host._load_active_turns()
        active_turns[active.thread_id] = active
        self.host._save_active_turns(active_turns)

    def _delete_active_turn(self, thread_id: str) -> bool:
        deleter = getattr(self.host, "_delete_active_turn_record", None)
        if callable(deleter):
            return bool(deleter(thread_id))
        active_turns = self.host._load_active_turns()
        removed = active_turns.pop(thread_id, None) is not None
        if removed:
            self.host._save_active_turns(active_turns)
        return removed

    @staticmethod
    def _human_turn_source(source: str) -> bool:
        return TurnQueuePolicy.is_human_source(source)

    def automation_wait_reason(self, source: str) -> dict[str, Any] | None:
        # Sources are protocol provenance assigned by server-side producers,
        # never inferred from task text. Unknown origins are automated.
        if self._human_turn_source(source):
            return None
        if self.automated_admission_allowed is None or self.automated_admission_allowed():
            return None
        return {
            "code": "autonomy_paused",
            "message": "Automated work is waiting for autonomy to be enabled and resumed",
            "retryable": True,
        }

    def _require_automated_admission(self, source: str, operator_admission_validator=None) -> None:
        if operator_admission_validator is not None:
            operator_admission_validator()
            return
        reason = self.automation_wait_reason(source)
        if reason is not None:
            raise AutomatedAdmissionPaused(status_code=409, detail=reason)

    def _project_dispatch_validator(self, queued):
        if getattr(queued, "owner_lane_recovery", None) is not None:
            recovery = getattr(self, "owner_lane_recovery", None)
            if recovery is None:
                raise HTTPException(status_code=409, detail={"code": "owner_recovery_unavailable"})
            return recovery.admission_validator(queued)
        if queued.project_coordination_dispatch is None:
            return None
        if (queued.source != "project-coordination"
                or self.project_coordination_admission is None):
            raise HTTPException(status_code=409, detail={"code": "project_delegation_unavailable"})
        def validate():
            try:
                self.project_coordination_admission(queued)
            except (HTTPException, ValueError, RuntimeError) as exc:
                # A revoked delegation is a pre-input admission stop, not an
                # unknown provider outcome or a reason to drop the original.
                detail = exc.detail if isinstance(exc, HTTPException) else None
                message = detail.get("message") if isinstance(detail, dict) else None
                raise AutomatedAdmissionPaused(status_code=409, detail={
                    "code": "project_delegation_blocked", "retryable": False,
                    "message": "Project delegation blocked: " + str(message or
                        "current Goal binding or origin authority no longer permits admission")[:400],
                }) from exc
        return validate

    def _queued_automation_wait_reason(self, queued):
        try:
            validator = self._project_dispatch_validator(queued)
            if validator is not None:
                validator()
                return None
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            return {"code": str(detail.get("code") or "owner_recovery_blocked")
                    if getattr(queued, "owner_lane_recovery", None) is not None else "project_delegation_blocked",
                "message": str(detail.get("message") or
                "Project delegation blocked: its canonical admission service is unavailable")[:500]}
        return self.automation_wait_reason(queued.source)

    def resume_automated_queues(self) -> None:
        # The canonical control-change request wakes existing queues. Admission
        # still rechecks both controls; a runtime disable marker cannot be overridden.
        for thread_id in self.host._load_turn_queues():
            current = self.queue_drain_tasks.get(thread_id)
            if current is not None and not current.done():
                # A resume arriving while a paused admission is being restored
                # must survive the drainer's existing task coalescing.
                current.add_done_callback(
                    lambda _task, target=thread_id: self.schedule_queue_drain(target))
            else:
                self.schedule_queue_drain(thread_id)

    def queue_admission_status(self, thread_id: str) -> dict[str, Any]:
        items = self.host._thread_queue(thread_id)
        reason = self.automation_wait_reason("queue-automation")
        waiting = 0
        blockers = {}
        for item in items:
            blocker = (self._queued_automation_wait_reason(item)
                       if item.project_coordination_dispatch is not None or item.owner_lane_recovery is not None
                       else reason if not self._human_turn_source(item.source) else None)
            if blocker is not None:
                waiting += 1
                blockers.setdefault(blocker["code"], blocker)
        actual_reason = None
        if blockers:
            # At most the delegation and ordinary automation reasons. Preserve
            # both in mixed queues; unpausing cannot repair a revoked binding.
            values = list(blockers.values())[:2]
            actual_reason = dict(values[0])
            if len(values) > 1:
                actual_reason = {"code": "queue_admission_blocked", "message": " ".join(
                    str(v.get("message", "Admission blocked"))[:500] for v in values)}
        priority_count = sum(TurnQueuePolicy.admission_priority(item.source) != "standard"
                             for item in items)
        slack_count = sum(TurnQueuePolicy.source_origin(item.source) == "slack"
                          for item in items)
        return {"automationWaiting": waiting,
                "automationWaitReason": actual_reason,
                "humanPriorityCount": priority_count,
                "humanPriorityReason": (
                    "Human messages have priority over automated work when this thread is ready."
                    if priority_count else None),
                "humanSlackPriorityCount": slack_count,
                "humanSlackPriorityReason": (
                    "Human messages have priority over automated work when this thread is ready."
                    if slack_count else None)}

    async def publish_queue_status(self, thread_id: str) -> None:
        h = self.host
        depth = h._thread_queue_depth(thread_id)
        await h.hub.publish(
            {
                "type": "queue.status",
                "threadId": thread_id,
                "queueDepth": depth,
                **self.queue_admission_status(thread_id),
                **TurnQueuePolicy.retention_status(depth, h._max_thread_queue_depth()),
                "active": self.thread_is_active(thread_id),
            }
        )

    async def _publish_queued_retention_refusals(self, thread_id, count, sample):
        if not count:
            return
        now = time.monotonic()
        previous = self._queued_retention_notice_at.get(thread_id)
        if previous is not None and now - previous < 60.0:
            return
        if thread_id not in self._queued_retention_notice_at and len(self._queued_retention_notice_at) >= 256:
            self._queued_retention_notice_at.pop(next(iter(self._queued_retention_notice_at)))
        self._queued_retention_notice_at[thread_id] = now
        code = 'retained_queued_route_unverified'
        message = (f'{count} retained request(s) need verified replacement provenance. '
                   'Original requests remain held; operator reconciliation is required.')
        self.host._append_bot_event({'type': 'queued_retention_refused',
            'thread_id': thread_id, 'code': code, 'count': count,
            'queued_ids': sample[:10], 'retryable': False})
        await asyncio.wait_for(self.host.hub.publish({'type': 'queue.error', 'threadId': thread_id,
            'queueDepth': self.host._thread_queue_depth(thread_id),
            'code': code, 'error': message, 'retryable': False}), timeout=1.0)

    def enqueue_turn(
        self,
        *,
        thread_id: str,
        project_id: str,
        message: str,
        sandbox: str | None = None,
        approval_policy: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        source: str = "web",
        reply_target: BotReplyTarget | None = None,
        execution_id: str | None = None,
        work_item_ref: str | None = None,
        handoff_requested_at: float | None = None,
        repository_resource_id: str | None = None,
        writable_repository_resource_ids: tuple[str, ...] = (),
        read_only_repository_resource_ids: tuple[str, ...] = (),
        resource_context=None,
        execution_profile_id: str | None = None,
        agent_profile_id: str | None = None,
        agent_profile_revision: int | None = None,
        agent_profile_actor_id: str | None = None,
        memory_context_binding: UxBriefTurnBinding | None = None,
        provider_notification_pin=None,
        owner_routing_pin=None,
        canonical_ingress_id: str | None = None,
        ingress_execution_profile_definition=None,
        project_coordination_dispatch=None,
        admit_first: bool = False,
    ) -> QueuedTurn:
        h = self.host
        selected: dict[str, QueuedTurn] = {}

        def mutate(items: list[QueuedTurn]) -> list[QueuedTurn]:
            for existing in items:
                if (
                    existing.canonical_ingress_id == canonical_ingress_id
                    and
                    existing.source == source
                    and existing.message == message
                    and existing.project_coordination_dispatch == project_coordination_dispatch
                    and existing.project_id == project_id
                    and existing.work_item_ref == work_item_ref
                    and existing.handoff_requested_at == handoff_requested_at
                    and existing.repository_resource_id == repository_resource_id
                    and existing.writable_repository_resource_ids == writable_repository_resource_ids
                    and existing.read_only_repository_resource_ids == read_only_repository_resource_ids
                    and existing.resource_context == resource_context
                    and existing.execution_profile_id == execution_profile_id
                    and existing.agent_profile_id == agent_profile_id
                    and existing.agent_profile_revision == agent_profile_revision
                    and existing.agent_profile_actor_id == agent_profile_actor_id
                    and existing.memory_context_binding == memory_context_binding
                    and existing.provider_notification_pin == provider_notification_pin
                    and existing.owner_routing_pin == owner_routing_pin
                    and existing.reply_target == reply_target
                    and existing.sandbox == sandbox
                    and existing.approval_policy == approval_policy
                    and existing.model == model
                    and existing.reasoning_effort == reasoning_effort
                ):
                    selected["item"] = existing
                    return items

            incoming_entries = (
                h._work_item_wakeup_entries(message)
                if (reply_target is None and memory_context_binding is None and provider_notification_pin is None and owner_routing_pin is None
                    and source != "project-coordination" and project_coordination_dispatch is None)
                else []
            )
            if incoming_entries:
                wakeup_items = [
                    existing
                    for existing in items
                    if existing.reply_target is None
                    and existing.source != "project-coordination"
                    and existing.project_coordination_dispatch is None
                    and existing.memory_context_binding is None
                    and existing.provider_notification_pin is None
                    and existing.owner_routing_pin is None
                    and h._work_item_wakeup_entries(existing.message)
                ]
                if wakeup_items:
                    representative = wakeup_items[0]
                    entries = [
                        entry
                        for existing in wakeup_items
                        for entry in h._work_item_wakeup_entries(
                            existing.message
                        )
                    ]
                    representative.message = (
                        h._render_work_item_wakeup_batch(
                            entries + incoming_entries
                        )
                    )
                    wakeup_ids = {
                        id(existing)
                        for existing in wakeup_items[1:]
                    }
                    selected["item"] = representative
                    return [
                        existing
                        for existing in items
                        if id(existing) not in wakeup_ids
                    ]

            # This is durable admission, not execution admission. A full
            # scheduling window must not discard a provider message that has
            # already been acknowledged or canonically deduplicated upstream.
            # Retain the task atomically; the existing serial drain and worker
            # capacity gates still govern execution. Storage failure propagates
            # rather than reporting successful admission.

            queued = QueuedTurn(
                canonical_ingress_id=canonical_ingress_id,
                ingress_execution_profile_definition=ingress_execution_profile_definition,
                id=(
                    h.uuid.uuid4().hex[:12]
                    if hasattr(h, "uuid")
                    else __import__("uuid").uuid4().hex[:12]
                ),
                thread_id=thread_id,
                project_id=project_id,
                message=message,
                execution_id=execution_id or self._new_execution_id(),
                work_item_ref=work_item_ref,
                handoff_requested_at=handoff_requested_at,
                sandbox=sandbox,
                approval_policy=approval_policy,
                model=model,
                reasoning_effort=reasoning_effort,
                repository_resource_id=repository_resource_id,
                writable_repository_resource_ids=(
                    writable_repository_resource_ids
                ),
                read_only_repository_resource_ids=(
                    read_only_repository_resource_ids
                ),
                **({"resource_context":resource_context} if resource_context is not None else {}),
                execution_profile_id=execution_profile_id,
                agent_profile_id=agent_profile_id,
                agent_profile_revision=agent_profile_revision,
                agent_profile_actor_id=agent_profile_actor_id,
                **({"memory_context_binding": memory_context_binding} if memory_context_binding is not None else {}),
                **({"provider_notification_pin":provider_notification_pin} if provider_notification_pin is not None else {}),
                **({"owner_routing_pin": owner_routing_pin} if owner_routing_pin is not None else {}),
                source=source,
                **({"project_coordination_dispatch": project_coordination_dispatch}
                   if project_coordination_dispatch is not None else {}),
                reply_target=reply_target,
                created_at=time.time(),
            )
            if self.queued_preflight is not None:
                retained = self.queued_preflight.store.matching_queued(queued.model_dump(mode='json'))
                if retained is not None:
                    raise ExecutionPrestartRefusal(status_code=409, detail={
                        'code':'execution_preflight_blocked',
                        'message':'This original request is retained for recovery; use its existing preflight action.',
                        'attemptId':retained.id,'retainedMessage':True,
                        'status':retained.status,'retryable':False})
            if admit_first:
                items.insert(0, queued)
            else:
                items.append(queued)
            selected["item"] = queued
            return items

        if canonical_ingress_id is not None:
            h._update_thread_queue_with_ingress(thread_id, canonical_ingress_id, mutate)
        else:
            self._update_thread_queue(thread_id, mutate)
        return selected["item"]

    def find_duplicate_queued_turn(
        self, thread_id: str, *, message: str, source: str, **identity: Any,
    ) -> QueuedTurn | None:
        # Text alone cannot prove that scope, actor or handoff generation match.
        # Legacy callers without identity defer to atomic enqueue deduplication.
        if not identity:
            return None
        identity.setdefault('owner_routing_pin', None)
        for queued in self.host._thread_queue(thread_id):
            if (queued.source == source and queued.message == message
                    and all(getattr(queued, key) == value for key, value in identity.items())):
                return queued
        return None

    def pop_next_queued_turn(
        self,
        thread_id: str,
    ) -> QueuedTurn | None:
        selected: dict[str, QueuedTurn] = {}

        def mutate(items: list[QueuedTurn]) -> list[QueuedTurn]:
            if not items:
                return items
            selected["item"] = items.pop(0)
            return items

        self._update_thread_queue(thread_id, mutate)
        return selected.get("item")

    def pop_latest_queued_turn(
        self,
        thread_id: str,
    ) -> QueuedTurn | None:
        selected: dict[str, QueuedTurn] = {}

        def mutate(items: list[QueuedTurn]) -> list[QueuedTurn]:
            if not items:
                return items
            selected["item"] = items.pop()
            return items

        self._update_thread_queue(thread_id, mutate)
        return selected.get("item")

    def pop_queued_turn(
        self,
        thread_id: str,
        queued_id: str,
        *,
        order_snapshot: list[str] | None = None,
    ) -> QueuedTurn | None:
        selected: dict[str, QueuedTurn] = {}

        def mutate(items: list[QueuedTurn]) -> list[QueuedTurn]:
            for index, queued in enumerate(items):
                if queued.id != queued_id:
                    continue
                if order_snapshot is not None:
                    order_snapshot[:] = [item.id for item in items]
                selected["item"] = items.pop(index)
                break
            return items

        self._update_thread_queue(thread_id, mutate)
        return selected.get("item")

    def requeue_turn_front(self, queued: QueuedTurn) -> None:
        def mutate(items: list[QueuedTurn]) -> list[QueuedTurn]:
            if any(item.id == queued.id for item in items):
                return items
            items.insert(0, queued)
            return items

        self._update_thread_queue(queued.thread_id, mutate)

    def thread_is_active(self, thread_id: str | None) -> bool:
        completion = self.thread_completion_tasks.get(thread_id) if thread_id else None
        return bool(thread_id and (self._active_turn(thread_id) is not None
            or completion is not None and not completion.done()))

    def active_execution_id(self, thread_id: str | None) -> str | None:
        if not thread_id:
            return None
        active = self._active_turn(thread_id)
        return active.execution_id if active is not None else None

    def _bootstrap_binding_for_thread(self, thread_id: str):
        service = self.bootstrap_bindings
        actor = self.control_actor
        if service is None or actor is None:
            return None
        try:
            return service.get_by_thread(thread_id, actor)
        except ThreadBootstrapBindingNotFoundError:
            return None

    def _assignment_session_for_thread(self, thread_id: str):
        active = self._active_turn(thread_id)
        assignment_id = active.assignment_id if active is not None else None
        bootstrap = None
        if not assignment_id:
            bootstrap = self._bootstrap_binding_for_thread(thread_id)
            assignment_id = bootstrap.assignment_id if bootstrap is not None else None
        if not assignment_id:
            return None
        try:
            manager, session = self._session_for_assignment(assignment_id)
        except HTTPException:
            # No live session (restart, supersede, or recovery): reads
            # degrade to a notLoaded view instead of failing the request;
            # the next turn re-acquires or supersedes the binding. The
            # ambient runtime must never be used for a bound thread.
            return ("degraded", None, None)
        status = session.status()
        if not hasattr(status, "running") or not hasattr(status, "ready"):
            try:
                assignment = session.validate_current()
            except AssignmentBoundAgentSessionStaleError:
                return ("degraded", None, None)
            return manager, session, getattr(
                assignment,
                "runtime_binding",
                getattr(session, "runtime_binding", None),
            )
        if not status.running or not status.ready:
            return ("degraded", None, None)
        # Canonical authority is revalidated by the async request path and the
        # one-second session watchdog. Status projections must not repeatedly
        # decode the monolithic worker catalog merely to report liveness.
        return manager, session, session.runtime_binding

    def thread_has_live_agent_runtime_session(
        self,
        thread_id: str,
    ) -> bool:
        """Report whether a bootstrap-bound thread has its required session."""

        if self._bootstrap_binding_for_thread(thread_id) is None:
            return True
        resolved = self._assignment_session_for_thread(thread_id)
        return bool(
            resolved is not None
            and resolved[0] != "degraded"
        )

    def thread_has_inflight_agent_runtime_assignment(
        self,
        thread_id: str,
    ) -> bool:
        """Report whether a bootstrap is still acquiring its runtime session.

        A freshly prepared bootstrap has a durable assignment before its
        process session becomes ready.  Watchdogs must treat that interval as
        in-flight work; otherwise each cycle supersedes the new bootstrap and
        creates another workspace before the worker can finish starting.
        """

        bootstrap = self._bootstrap_binding_for_thread(thread_id)
        if bootstrap is None:
            return False
        assignment = self._assignment_record(bootstrap.assignment_id)
        return bool(
            assignment is not None
            and assignment.status
            in {
                AssignmentStatus.PENDING,
                AssignmentStatus.CLAIMED,
                AssignmentStatus.RUNNING,
            }
        )

    async def request_for_thread(
        self,
        thread_id: str,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self.procedure_admission is not None and method in {"turn/start", "thread/resume", "thread/goal/set"}:
            raise ExecutionPrestartRefusal(status_code=409, detail={
                "code": "procedure_unsupported_enforcement",
                "message": "Raw native mutation/resume must enter canonical turn admission; provider RPC cannot bypass procedure discovery",
                "threadId": thread_id,
            })
        if method == "turn/interrupt" and not (params or {}).get("turnId"):
            active = await asyncio.to_thread(self._active_turn, thread_id)
            if active is not None and active.turn_id:
                params = {**(params or {}), "turnId": active.turn_id}
        resolved = await asyncio.to_thread(
            self._assignment_session_for_thread,
            thread_id,
        )
        if resolved is None:
            return await self.host.codex.request(method, params)
        _manager, session, binding = resolved
        if _manager == "degraded":
            if method != "thread/read":
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "thread bootstrap binding has no live agent runtime "
                        "session"
                    ),
                )
            return {
                "ok": False,
                "timedOut": True,
                "threadId": thread_id,
                "error": (
                    "agent runtime session is not live; send a message to "
                    "re-acquire it"
                ),
                "thread": {
                    "id": thread_id,
                    "turns": [],
                    "status": {"type": "notLoaded"},
                    "readTimedOut": True,
                },
            }
        if binding is None or (
            binding.provider_id == "openai" and binding.runtime_id == "codex"
        ):
            return await session.request(method, params)

        await asyncio.to_thread(session.validate_current)
        adapter = self._adapter_for_binding(binding, session)
        native_session_id = getattr(
            getattr(session, "runtime", None),
            "native_session_id",
            None,
        )
        if not native_session_id:
            if method == "thread/read":
                return {
                    "ok": False,
                    "timedOut": True,
                    "threadId": thread_id,
                    "error": (
                        "agent runtime session has no provider-native "
                        "session id"
                    ),
                    "thread": {
                        "id": thread_id,
                        "turns": [],
                        "status": {"type": "notLoaded"},
                        "readTimedOut": True,
                    },
                }
            raise HTTPException(
                status_code=503,
                detail="agent runtime session has no provider-native session id",
            )
        if method == "thread/read":
            return (await adapter.read_session(native_session_id)).payload
        if method in {"thread/archive", "thread/close"}:
            return (await adapter.close_session(native_session_id)).payload
        if method in {"thread/unarchive", "thread/restore"}:
            return (await adapter.restore_session(native_session_id)).payload
        if method == "turn/interrupt":
            return (await adapter.interrupt(native_session_id)).payload
        return await session.request(method, params)

    def mark_thread_active(
        self,
        thread_id: str | None,
        *,
        turn_id: str | None = None,
        project_id: str | None = None,
        sandbox: str | None = None,
        approval_policy: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        source: str | None = None,
        reply_target: BotReplyTarget | None = None,
        execution_id: str | None = None,
        assignment_id: str | None = None,
        execution_workspace_id: str | None = None,
        worker_id: str | None = None,
        fence: int | None = None,
        repository_resource_id: str | None = None,
        writable_repository_resource_ids: tuple[str, ...] = (),
        resource_context=None,
        execution_profile_id: str | None = None,
        agent_profile: AgentProfileExecutionBinding | None = None,
        agent_profile_actor_id: str | None = None,
        memory_context_binding: UxBriefTurnBinding | None = None,
    ) -> None:
        if not thread_id:
            return
        with contextlib.suppress(RuntimeError):
            self._event_loop = asyncio.get_running_loop()
        h = self.host
        now = time.time()
        current = self._active_turn(thread_id)
        settings = h._thread_run_settings(thread_id)
        resource_context=resource_context or (current.resource_context if current else None)
        active = ActiveThreadTurn(
            resource_context=resource_context,
            thread_id=thread_id,
            turn_id=turn_id or (current.turn_id if current else None),
            project_id=project_id or (current.project_id if current else None),
            sandbox=sandbox or settings.sandbox or (current.sandbox if current else None),
            approval_policy=approval_policy or settings.approval_policy or (current.approval_policy if current else None),
            model=model or settings.model or (current.model if current else None),
            reasoning_effort=(
                reasoning_effort
                or settings.reasoning_effort
                or (current.reasoning_effort if current else None)
            ),
            source=source or (current.source if current else None),
            reply_target=reply_target or (current.reply_target if current else None),
            execution_id=execution_id or (current.execution_id if current else None),
            assignment_id=assignment_id or (current.assignment_id if current else None),
            execution_workspace_id=(
                execution_workspace_id
                or (current.execution_workspace_id if current else None)
            ),
            worker_id=worker_id or (current.worker_id if current else None),
            fence=fence if fence is not None else (current.fence if current else None),
            repository_resource_id=(
                repository_resource_id
                or settings.repository_resource_id
                or (current.repository_resource_id if current else None)
            ),
            writable_repository_resource_ids=(
                writable_repository_resource_ids
                or (
                    current.writable_repository_resource_ids
                    if current
                    else ()
                )
            ),
            execution_profile_id=(
                execution_profile_id
                or settings.execution_profile_id
                or (current.execution_profile_id if current else None)
            ),
            agent_profile=(
                agent_profile
                or (current.agent_profile if current else None)
            ),
            agent_profile_actor_id=(
                agent_profile_actor_id
                or (
                    current.agent_profile_actor_id
                    if current
                    else None
                )
            ),
            memory_context_binding=memory_context_binding or (current.memory_context_binding if current else None),
            started_at=current.started_at if current else now,
            updated_at=now,
            resume_attempts=current.resume_attempts if current else 0,
            last_resume_at=current.last_resume_at if current else None,
        )
        self._save_active_turn(active)
        self.activity_heartbeat_at[thread_id] = now

    def clear_thread_active(self, thread_id: str | None, turn_id: str | None = None) -> None:
        if not thread_id:
            return
        if self.thread_handoff_in_progress(thread_id):
            return
        h = self.host
        active = self._active_turn(thread_id)
        if active and turn_id and active.turn_id and active.turn_id != turn_id:
            return
        if active and self._delete_active_turn(thread_id):
            self.activity_heartbeat_at.pop(thread_id, None)
            if not getattr(h, "IS_SHUTTING_DOWN", False) and h._autonomy_enabled():
                h._schedule_native_recovery_cycles(reason="thread-became-idle")

    def begin_thread_handoff(self, thread_id: str) -> bool:
        if thread_id in self.thread_handoffs:
            return False
        self.thread_handoffs.add(thread_id)
        return True

    def thread_handoff_in_progress(self, thread_id: str | None) -> bool:
        return bool(thread_id and thread_id in self.thread_handoffs)

    def finish_thread_handoff(
        self,
        thread_id: str,
        *,
        clear_active: bool = False,
    ) -> None:
        self.thread_handoffs.discard(thread_id)
        if clear_active:
            self.clear_thread_active(thread_id)

    def _schedule_assignment_completion(
        self,
        active: ActiveThreadTurn,
        *,
        succeeded: bool,
        message: dict[str, Any],
    ) -> None:
        assignment_id = active.assignment_id
        if not assignment_id:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = getattr(self, "_event_loop", None)
            if loop is None or loop.is_closed():
                raise RuntimeError("terminal assignment completion has no owner event loop")
            # Durable activity projection runs in a storage worker. Marshal
            # only scheduling back to the owning loop; never create a task or
            # mutate task registries from that worker thread. Canonical session
            # validation, checkpointing and completion remain unchanged.
            loop.call_soon_threadsafe(
                lambda: self._schedule_assignment_completion(
                    active, succeeded=succeeded, message=message
                )
            )
            return
        self._event_loop = loop
        try:
            manager, _session = self._session_for_assignment(assignment_id)
        except HTTPException:
            return
        completion_work_item_ref = None
        with contextlib.suppress(Exception):
            completion_assignment = _session.validate_current()
            completion_work_item_ref = getattr(
                completion_assignment,
                "work_item_ref",
                None,
            )
        bootstrap = self._bootstrap_binding_for_thread(active.thread_id)
        if bootstrap is not None and bootstrap.assignment_id == assignment_id:
            checkpoint = getattr(manager, "checkpoint", None)

            def record_bootstrap_result(
                *,
                checkpoint_count: int = 0,
                checkpoint_error: str | None = None,
            ) -> None:
                checkpoint_succeeded = checkpoint_error is None
                self.host._append_bot_event(
                    {
                        "type": "thread_bootstrap_turn_completed",
                        "thread_id": active.thread_id,
                        "turn_id": active.turn_id,
                        "execution_id": bootstrap.execution_id,
                        "assignment_id": bootstrap.assignment_id,
                        "execution_workspace_id": bootstrap.execution_workspace_id,
                        "worker_id": active.worker_id,
                        "fence": active.fence,
                        "succeeded": succeeded and checkpoint_succeeded,
                        "session_retained": True,
                        "repository_checkpoint_count": checkpoint_count,
                        "repository_checkpoint_error": checkpoint_error,
                    }
                )
                if (
                    completion_work_item_ref
                    and self.work_item_outcome_recorder is not None
                ):
                    with contextlib.suppress(Exception):
                        self.work_item_outcome_recorder(
                            completion_work_item_ref,
                            bootstrap.execution_id,
                            (
                                "succeeded"
                                if succeeded and checkpoint_succeeded
                                else "failed"
                            ),
                        )

            if callable(checkpoint) or self.post_turn_readiness is not None:
                existing = self.assignment_completion_tasks.get(assignment_id)
                if existing is not None and not existing.done():
                    return

                async def checkpoint_retained_session() -> None:
                    try:
                        values = await checkpoint(assignment_id) if callable(checkpoint) else ()
                        await self._settle_post_turn_readiness(active, manager)
                        record_bootstrap_result(checkpoint_count=len(values))
                    except Exception as exc:
                        record_bootstrap_result(checkpoint_error=str(exc)[:500])
                    finally:
                        current_task = asyncio.current_task()
                        if self.assignment_completion_tasks.get(assignment_id) is current_task:
                            self.assignment_completion_tasks.pop(assignment_id, None)
                        if self.thread_completion_tasks.get(active.thread_id) is current_task:
                            self.thread_completion_tasks.pop(active.thread_id, None)

                task = asyncio.create_task(
                    checkpoint_retained_session(),
                    name=f"repository-checkpoint-{assignment_id}",
                )
                self.assignment_completion_tasks[assignment_id] = task
                self.thread_completion_tasks[active.thread_id] = task
            else:
                record_bootstrap_result()
            return
        existing = self.assignment_completion_tasks.get(assignment_id)
        if existing is not None and not existing.done():
            return

        async def complete() -> None:
            try:
                params = message.get("params") or {}
                raw_error = params.get("error") or (params.get("turn") or {}).get("error")
                failure_message = None if succeeded else str(
                    raw_error or "Codex turn failed"
                )[:500]
                await self._settle_post_turn_readiness(active, manager)
                completed = await manager.complete(
                    assignment_id,
                    succeeded=succeeded,
                    failure_code=None if succeeded else "codex_turn_failed",
                    failure_message=failure_message,
                )
                self.host._append_bot_event(
                    {
                        "type": "turn_assignment_completed",
                        "thread_id": active.thread_id,
                        "turn_id": active.turn_id,
                        "execution_id": active.execution_id,
                        "assignment_id": completed.id,
                        "execution_workspace_id": active.execution_workspace_id,
                        "worker_id": active.worker_id,
                        "fence": active.fence,
                        "succeeded": succeeded,
                    }
                )
                if (
                    completion_work_item_ref
                    and active.execution_id
                    and self.work_item_outcome_recorder is not None
                ):
                    with contextlib.suppress(Exception):
                        self.work_item_outcome_recorder(
                            completion_work_item_ref,
                            active.execution_id,
                            "succeeded" if succeeded else "failed",
                        )
            except Exception as exc:
                self.host._append_bot_event(
                    {
                        "type": "turn_assignment_completion_failed",
                        "thread_id": active.thread_id,
                        "turn_id": active.turn_id,
                        "execution_id": active.execution_id,
                        "assignment_id": assignment_id,
                        "error": str(exc)[:500],
                    }
                )
                if (
                    completion_work_item_ref
                    and active.execution_id
                    and self.work_item_outcome_recorder is not None
                ):
                    with contextlib.suppress(Exception):
                        self.work_item_outcome_recorder(
                            completion_work_item_ref,
                            active.execution_id,
                            "ambiguous",
                        )
            finally:
                current = asyncio.current_task()
                if self.assignment_completion_tasks.get(assignment_id) is current:
                    self.assignment_completion_tasks.pop(assignment_id, None)
                if self.thread_completion_tasks.get(active.thread_id) is current:
                    self.thread_completion_tasks.pop(active.thread_id, None)

        task = asyncio.create_task(
            complete(),
            name=f"turn-assignment-complete-{assignment_id}",
        )
        self.assignment_completion_tasks[assignment_id] = task
        self.thread_completion_tasks[active.thread_id] = task

    async def _settle_post_turn_readiness(self, active, manager) -> None:
        callback = self.post_turn_readiness
        local_worker = getattr(manager, 'local_worker', None)
        actor = getattr(local_worker, 'worker_actor', None)
        if callback is None or actor is None:
            return
        try:
            await callback(active, actor=actor)
        except Exception as exc:
            # A callback refusal never invents successful settlement or turns
            # a completed native/checkpoint operation into a failed one. The
            # canonical unresolved-origin retirement guard preserves custody.
            self.host._append_bot_event({'type': 'post_turn_readiness_deferred',
                'assignment_id': active.assignment_id, 'turn_id': active.turn_id,
                'error_type': type(exc).__name__})

    @staticmethod
    def _normalize_agent_runtime_item(item: Any) -> Any:
        if not isinstance(item, dict):
            return item
        normalized = dict(item)
        type_map = {
            "agent_message": "agentMessage",
            "command_execution": "commandExecution",
            "file_change": "fileChange",
        }
        item_type = normalized.get("type")
        if item_type in type_map:
            normalized["type"] = type_map[item_type]
        if "aggregated_output" in normalized and "aggregatedOutput" not in normalized:
            normalized["aggregatedOutput"] = normalized["aggregated_output"]
        return normalized

    def _publish_agent_runtime_message(self, message: dict[str, Any]) -> None:
        params = message.get("params") or {}
        thread_id = params.get("threadId") or (
            params.get("turn") or {}
        ).get("threadId")

        def project_state() -> None:
            if self.thread_history is not None and thread_id:
                try:
                    self.thread_history.project_message(str(thread_id), message)
                except Exception as exc:
                    append_event = getattr(self.host, "_append_bot_event", None)
                    if callable(append_event):
                        with contextlib.suppress(Exception):
                            append_event({
                                "type": "thread_history_projection_failed",
                                "thread_id": str(thread_id),
                                "method": str(message.get("method") or ""),
                                "error_type": type(exc).__name__,
                            })
            self.record_thread_activity(message)

        hub = getattr(self.host, "hub", None)
        try:
            loop = asyncio.get_running_loop() if hub is not None else None
        except RuntimeError:
            loop = None
        if loop is None:
            project_state()
            return

        # State stores are synchronous. Match the native reader's offload,
        # but serialize each thread so late heartbeats cannot revive a turn
        # after its terminal event. Other threads progress independently.
        tasks = getattr(self, "agent_runtime_notification_tasks", None)
        if tasks is None:
            tasks = self.agent_runtime_notification_tasks = {}
        key = str(thread_id or "")
        previous = tasks.get(key)

        async def publish() -> None:
            try:
                if previous is not None:
                    await previous
                await asyncio.to_thread(project_state)
                await hub.publish({"type": "codex.event", "message": message})
                method = message.get("method")
                if method == "item/completed":
                    outbound = getattr(self.host, "_record_bot_outbound", None)
                    if callable(outbound):
                        await outbound(message)
                if method in {"turn/completed", "turn/failed"}:
                    recorder = getattr(self.host, "_record_terminal_turn_result", None)
                    recovery_scheduled = (
                        await asyncio.to_thread(recorder, message)
                        if callable(recorder) else False
                    )
                    drain = getattr(self.host, "_schedule_queue_drain", None)
                    if not recovery_scheduled and callable(drain):
                        drain(thread_id)
            except Exception as exc:
                append_event = getattr(self.host, "_append_bot_event", None)
                if callable(append_event):
                    append_event({
                        "type": "agent_runtime_notification_failed",
                        "thread_id": str(thread_id or ""),
                        "method": str(message.get("method") or ""),
                        "error_type": type(exc).__name__,
                    })

        task = loop.create_task(publish(), name=f"agent-runtime-notification-{thread_id}")
        tasks[key] = task

        def finished(done: asyncio.Task) -> None:
            if tasks.get(key) is done:
                tasks.pop(key, None)

        task.add_done_callback(finished)

    def record_agent_runtime_event(
        self,
        thread_id: str,
        event: AgentRuntimeEvent,
    ) -> None:
        """Project provider-neutral runtime events into the existing thread bus."""

        method = str(event.event_type or "").replace(".", "/")
        if method == "turn/interrupted":
            method = "turn/failed"

        payload = dict(event.payload or {})
        payload.pop("type", None)
        params = payload
        params["threadId"] = thread_id

        if event.provider_native_turn_id:
            params["turnId"] = event.provider_native_turn_id
            turn = params.get("turn")
            if not isinstance(turn, dict):
                turn = {}
            else:
                turn = dict(turn)
            turn.setdefault("id", event.provider_native_turn_id)
            turn.setdefault("threadId", thread_id)
            params["turn"] = turn

        item = params.get("item")
        if isinstance(item, dict):
            normalized_item = self._normalize_agent_runtime_item(item)
            params["item"] = normalized_item
            if (
                method == "item/completed"
                and normalized_item.get("type") == "agentMessage"
                and normalized_item.get("text")
            ):
                self._publish_agent_runtime_message(
                    {
                        "method": "item/agentMessage/delta",
                        "params": {
                            "threadId": thread_id,
                            "delta": str(normalized_item["text"]),
                        },
                    }
                )

        if method == "turn/failed" and not params.get("error"):
            params["error"] = "agent runtime turn failed"

        self._publish_agent_runtime_message(
            {"method": method, "params": params}
        )

    def record_codex_runtime_transcript_event(
        self,
        message: dict[str, Any],
    ) -> None:
        """Durably project native app-server events before browser delivery."""
        if self.transcript is None:
            return
        method = str(message.get("method") or "").strip()
        params = message.get("params") or {}
        if method not in {"turn/started", "item/agentMessage/delta", "item/completed", "turn/completed", "turn/failed"} or not isinstance(params, dict):
            return
        turn = params.get("turn") or {}
        thread_id = str(
            params.get("threadId")
            or (turn.get("threadId") if isinstance(turn, dict) else "")
            or ""
        ).strip()
        if not thread_id:
            return
        turn_id = str(
            params.get("turnId")
            or (turn.get("id") if isinstance(turn, dict) else "")
            or ""
        ).strip() or None
        self.transcript.record_event(
            thread_id,
            AgentRuntimeEvent(
                event_type=method,
                provider_native_session_id=thread_id,
                provider_native_turn_id=turn_id,
                payload=dict(params),
            ),
        )

    def record_user_message(
        self,
        thread_id: str,
        turn_id: str,
        text: str,
    ) -> None:
        if self.transcript is not None:
            self.transcript.record_user(thread_id, turn_id, text)

    def fail_user_message(self, thread_id: str, turn_id: str) -> None:
        if self.transcript is not None:
            self.transcript.fail_pending(thread_id, turn_id)

    async def release_unviable_active_turn(self, thread_id: str) -> bool:
        active = self._active_turn(thread_id)
        if active is None or not active.assignment_id:
            return False
        try:
            manager, session = self._session_for_assignment(active.assignment_id)
            assignment = session.validate_current()
        except Exception:
            return False
        binding = getattr(assignment, "runtime_binding", None)
        if binding is None or (
            binding.provider_id == "openai" and binding.runtime_id == "codex"
        ):
            return False
        adapter = self._adapter_for_binding(binding, session)
        native_session_id = getattr(
            getattr(session, "runtime", None),
            "native_session_id",
            None,
        )
        try:
            runtime_state = (
                await adapter.read_session(native_session_id or thread_id)
            ).payload
        except Exception:
            # A failed health read is not proof that the provider turn is
            # terminal. Preserve the active marker and let normal stale-turn
            # recovery make the bounded decision instead.
            return False
        if runtime_state.get("active"):
            return False
        if native_session_id:
            return False
        self.clear_thread_active(thread_id)
        with contextlib.suppress(Exception):
            await manager.complete(
                active.assignment_id,
                succeeded=False,
                failure_code="agent_runtime_session_not_started",
                failure_message=(
                    "agent runtime turn did not acquire a provider-native session"
                ),
            )
        self.host._append_bot_event(
            {
                "type": "sessionless_active_turn_released",
                "thread_id": thread_id,
                "assignment_id": active.assignment_id,
                "execution_id": active.execution_id,
            }
        )
        return True

    def record_thread_activity(self, message: dict[str, Any]) -> None:
        h = self.host
        method = message.get("method")
        params = message.get("params") or {}
        thread_id = params.get("threadId") or (params.get("turn") or {}).get("threadId")
        turn_id = params.get("turnId") or (params.get("turn") or {}).get("id")
        if method in {
            "item/agentMessage/delta", "item/reasoning/textDelta",
            "item/reasoning/summaryTextDelta", "item/commandExecution/outputDelta",
            "item/fileChange/outputDelta",
        } and time.time() - self.activity_heartbeat_at.get(thread_id, 0) < 5:
            return
        if method in {"turn/started", "item/started"}:
            self.mark_thread_active(thread_id, turn_id=turn_id)
        elif method in {"turn/completed", "turn/failed"}:
            active = h._load_active_turns().get(thread_id) if thread_id else None
            if active is not None and not (
                turn_id and active.turn_id and active.turn_id != turn_id
            ):
                self._schedule_assignment_completion(
                    active,
                    succeeded=method == "turn/completed",
                    message=message,
                )
            if not getattr(h, "IS_SHUTTING_DOWN", False):
                self.clear_thread_active(thread_id, turn_id=turn_id)
        elif method == "thread/status/changed":
            status_type = (params.get("status") or {}).get("type")
            if status_type == "active":
                self.mark_thread_active(thread_id)
            elif (
                status_type in {"idle", "systemError", "notLoaded"}
                and not getattr(h, "IS_SHUTTING_DOWN", False)
            ):
                self.clear_thread_active(thread_id)
        elif thread_id and (active := self._active_turn(thread_id)) is not None:
            # Late events from a previous turn must not overwrite the current
            # turn identity or keep its assignment alive as a false heartbeat.
            if turn_id and active.turn_id and turn_id != active.turn_id:
                return
            # Provider turns can run longer than the stale-marker window and
            # some runtimes emit item/completed without a matching
            # item/started notification. Treat every non-terminal runtime
            # event as a heartbeat so queue recovery cannot release the
            # assignment and discard its dirty workspace while work is live.
            self.mark_thread_active(thread_id, turn_id=turn_id)

    async def _require_upgrade_turn_admission(
        self, thread_id: str, project: Project, assignment_id: str | None = None,
        *, allow_bootstrap_recovery: bool = False,
    ) -> tuple[str, str]:
        guard = self.upgrade_turn_admission_guard
        # A bound assignment owns its scope independently of the HTTP caller.
        # Read through the existing canonical assignment boundary, never infer
        # tenancy from a repository path or trust caller-supplied scope.
        owner = project
        if assignment_id is not None:
            owner = await asyncio.to_thread(self._assignment_record, assignment_id)
            if owner is None and allow_bootstrap_recovery:
                # Retention can prune the old row after its native session is
                # gone. Let the existing authorized bootstrap/profile recovery
                # run in the canonical project's scope; a still-bound runtime
                # with missing scope is not allowed this fallback.
                try:
                    _manager, old_session = self._session_for_assignment(assignment_id)
                except HTTPException as exc:
                    if (exc.status_code == 503 and exc.detail ==
                            "thread bootstrap binding has no live agent runtime session"):
                        owner = project
                    else:
                        raise
                else:
                    # A stale validation result is not process-exit evidence:
                    # a live process can lose its row or delegation. Only an
                    # observed exit permits ordinary bootstrap healing here.
                    proc = getattr(getattr(old_session, "runtime", None), "proc", None)
                    poll = getattr(proc, "poll", None)
                    if callable(poll):
                        try:
                            returncode = poll()
                        except Exception:
                            returncode = None
                        if type(returncode) is int:
                            owner = project
            if owner is None or (owner is not project and getattr(owner, "id", None) != assignment_id):
                raise HTTPException(status_code=409, detail={
                    "code": "upgrade_turn_scope_unknown", "threadId": thread_id,
                    "message": "Canonical assignment scope is unavailable",
                })
        organization_id = getattr(owner, "organization_id", None)
        workspace_id = getattr(owner, "workspace_id", None)
        if not organization_id or not workspace_id:
            raise HTTPException(status_code=409, detail={
                "code": "upgrade_turn_scope_unknown", "threadId": thread_id,
                "message": "Canonical native turn scope is unavailable",
            })
        if guard is not None and not await asyncio.to_thread(
            guard,
            organization_id,
            workspace_id,
        ):
            raise HTTPException(status_code=409, detail={
                "code": "upgrade_maintenance", "threadId": thread_id,
                "message": "Native turn admission is paused during upgrade maintenance",
            })
        return organization_id, workspace_id

    async def start_thread_turn_now(
        self,
        thread_id: str,
        *,
        project: Project,
        message: str,
        sandbox: str | None,
        approval_policy: str | None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        source: str = "web",
        reply_target: BotReplyTarget | None = None,
        execution_id: str | None = None,
        work_item_ref: str | None = None,
        handoff_requested_at: float | None = None,
        owner_routing_pin=None,
        repository_resource_id: str | None = None,
        writable_repository_resource_ids: tuple[str, ...] = (),
        read_only_repository_resource_ids: tuple[str, ...] = (),
        resource_context=None,
        read_only_scope_supplied: bool = False,
        resource_context_supplied: bool = False,
        execution_profile_id: str | None = None,
        ingress_execution_profile_definition=None,
        actor: AuthenticationActor | None = None,
        agent_profile_id: str | None = None,
        agent_profile_revision: int | None = None,
        agent_profile_actor_id: str | None = None,
        memory_context_binding: UxBriefTurnBinding | None = None,
        preserve_active_handoff: bool = False,
        queued_admission_attempt_id: str | None = None,
        queued_admission_claim_id: str | None = None,
        notification_batch_admission_id: str | None = None,
        notification_batch_control_current=None,
        operator_admission_validator=None,
    ) -> dict[str, Any]:
        await asyncio.to_thread(self._require_automated_admission, source, operator_admission_validator)
        scope = (project.organization_id, project.workspace_id)
        if (
            self.maintenance_admission is not None
            or self.upgrade_turn_admission_guard is not None
        ):
            current_bootstrap = self._bootstrap_binding_for_thread(thread_id)
            scope = await self._require_upgrade_turn_admission(
                thread_id,
                project,
                (
                    current_bootstrap.assignment_id
                    if current_bootstrap is not None
                    else None
                ),
                allow_bootstrap_recovery=True,
            )
        if self.maintenance_admission is not None:
            with self.maintenance_admission(*scope) as admitted:
                if not admitted:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "upgrade_maintenance_active",
                            "message": (
                                "Native turn admission is drained for a canonical upgrade"
                            ),
                            "retryable": True,
                            "threadId": thread_id,
                        },
                    )
                return await self._start_thread_turn_now_admitted(
                    thread_id,
                    project=project,
                    message=message,
                    sandbox=sandbox,
                    approval_policy=approval_policy,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    source=source,
                    reply_target=reply_target,
                    execution_id=execution_id,
                    work_item_ref=work_item_ref,
                    handoff_requested_at=handoff_requested_at,
                    **({'owner_routing_pin': owner_routing_pin} if owner_routing_pin is not None else {}),
                    repository_resource_id=repository_resource_id,
                    writable_repository_resource_ids=writable_repository_resource_ids,
                    read_only_repository_resource_ids=read_only_repository_resource_ids,
                    **({"resource_context":resource_context} if resource_context is not None else {}),
                    read_only_scope_supplied=read_only_scope_supplied,
                    **({"resource_context_supplied":True} if resource_context_supplied else {}),
                    execution_profile_id=execution_profile_id,
                    **({"ingress_execution_profile_definition": ingress_execution_profile_definition}
                       if ingress_execution_profile_definition is not None else {}),
                    actor=actor,
                    agent_profile_id=agent_profile_id,
                    agent_profile_revision=agent_profile_revision,
                    agent_profile_actor_id=agent_profile_actor_id,
                    **({"memory_context_binding": memory_context_binding} if memory_context_binding is not None else {}),
                    preserve_active_handoff=preserve_active_handoff,
                    queued_admission_attempt_id=queued_admission_attempt_id,
                    queued_admission_claim_id=queued_admission_claim_id,
                    notification_batch_admission_id=notification_batch_admission_id,
                    notification_batch_control_current=notification_batch_control_current,
                    operator_admission_validator=operator_admission_validator,
                )
        return await self._start_thread_turn_now_admitted(
            thread_id,
            project=project,
            message=message,
            sandbox=sandbox,
            approval_policy=approval_policy,
            model=model,
            reasoning_effort=reasoning_effort,
            source=source,
            reply_target=reply_target,
            execution_id=execution_id,
            work_item_ref=work_item_ref,
            handoff_requested_at=handoff_requested_at,
            **({'owner_routing_pin': owner_routing_pin} if owner_routing_pin is not None else {}),
            repository_resource_id=repository_resource_id,
            writable_repository_resource_ids=writable_repository_resource_ids,
            read_only_repository_resource_ids=read_only_repository_resource_ids,
            **({"resource_context":resource_context} if resource_context is not None else {}),
            read_only_scope_supplied=read_only_scope_supplied,
            **({"resource_context_supplied":True} if resource_context_supplied else {}),
            execution_profile_id=execution_profile_id,
                    **({"ingress_execution_profile_definition": ingress_execution_profile_definition}
                       if ingress_execution_profile_definition is not None else {}),
            actor=actor,
            agent_profile_id=agent_profile_id,
            agent_profile_revision=agent_profile_revision,
            agent_profile_actor_id=agent_profile_actor_id,
            **({"memory_context_binding": memory_context_binding} if memory_context_binding is not None else {}),
            preserve_active_handoff=preserve_active_handoff,
            queued_admission_attempt_id=queued_admission_attempt_id,
            queued_admission_claim_id=queued_admission_claim_id,
            notification_batch_admission_id=notification_batch_admission_id,
            notification_batch_control_current=notification_batch_control_current,
            operator_admission_validator=operator_admission_validator,
        )

    async def _start_thread_turn_now_admitted(
        self,
        thread_id: str,
        *,
        project: Project,
        message: str,
        sandbox: str | None,
        approval_policy: str | None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        source: str = "web",
        reply_target: BotReplyTarget | None = None,
        execution_id: str | None = None,
        work_item_ref: str | None = None,
        handoff_requested_at: float | None = None,
        owner_routing_pin=None,
        repository_resource_id: str | None = None,
        writable_repository_resource_ids: tuple[str, ...] = (),
        read_only_repository_resource_ids: tuple[str, ...] = (),
        resource_context=None,
        read_only_scope_supplied: bool = False,
        resource_context_supplied: bool = False,
        execution_profile_id: str | None = None,
        ingress_execution_profile_definition=None,
        actor: AuthenticationActor | None = None,
        agent_profile_id: str | None = None,
        agent_profile_revision: int | None = None,
        agent_profile_actor_id: str | None = None,
        memory_context_binding: UxBriefTurnBinding | None = None,
        preserve_active_handoff: bool = False,
        queued_admission_attempt_id: str | None = None,
        queued_admission_claim_id: str | None = None,
        notification_batch_admission_id: str | None = None,
        notification_batch_control_current=None,
        operator_admission_validator=None,
    ) -> dict[str, Any]:
        h = self.host
        def owner_admission_validator():
            if owner_routing_pin is None:
                return
            self._require_automated_admission(source, operator_admission_validator)
            try:
                if self.owner_routing_validator is None:
                    raise ValueError('owner_routing_validator_unavailable')
                self.owner_routing_validator(owner_routing_pin, thread_id=thread_id, project=project,
                    work_item_ref=work_item_ref, handoff_requested_at=handoff_requested_at,
                    queued_admission_attempt_id=queued_admission_attempt_id,
                    writable_repository_resource_ids=writable_repository_resource_ids,
                    read_only_repository_resource_ids=read_only_repository_resource_ids,
                    agent_profile_id=agent_profile_id, agent_profile_revision=agent_profile_revision,
                    agent_profile_actor_id=agent_profile_actor_id)
            except (LookupError, RuntimeError, ValueError, HTTPException) as exc:
                from codex_web.owner_routing import OwnerRoutingError
                code = str(exc) if isinstance(exc, OwnerRoutingError) else 'owner_routing_validation_unavailable'
                raise ExecutionPrestartRefusal(status_code=409, detail={
                    'code': code, 'message': 'Original owner routing no longer qualifies; inspect the retained request.',
                    'retryable': False}) from exc
        owner_admission_validator()
        if notification_batch_admission_id:
            if queued_admission_attempt_id or not queued_admission_claim_id or self.queued_preflight is None:
                raise ValueError("notification admission requires its exclusive retained claim")
            batch = await asyncio.to_thread(self.queued_preflight.store.get_notification_batch,
                                           notification_batch_admission_id)
            if batch is None:
                raise ValueError("notification admission is unavailable")
            member = batch.plan.members[0]
            if batch.plan.control_snapshot is not None and notification_batch_control_current is None:
                raise ValueError('notification admission requires current canonical controls')
            if notification_batch_control_current is not None and not await asyncio.to_thread(notification_batch_control_current):
                raise ValueError('notification batch controls changed before native gate')
            if (project.id != member.project_id or project.organization_id != batch.plan.organization_id
                    or project.workspace_id != batch.plan.workspace_id or actor is None
                    or actor.identity_id != member.agent_profile_actor_id
                    or actor.organization_id != batch.plan.organization_id or actor.workspace_id != batch.plan.workspace_id
                    or sandbox != (member.sandbox or project.sandbox)
                    or approval_policy != (member.approval_policy or project.approval_policy)
                    or model != member.model or reasoning_effort != member.reasoning_effort
                    or work_item_ref != member.work_item_ref or reply_target != member.reply_target
                    or repository_resource_id != member.repository_resource_id
                    or writable_repository_resource_ids != member.writable_repository_resource_ids
                    or read_only_repository_resource_ids != member.read_only_repository_resource_ids
                    or execution_profile_id != member.execution_profile_id
                    or agent_profile_id != member.agent_profile_id or agent_profile_revision != member.agent_profile_revision
                    or agent_profile_actor_id != member.agent_profile_actor_id
                    or memory_context_binding != member.memory_context_binding):
                raise ValueError("notification native request scope changed")
            await asyncio.to_thread(self.queued_preflight.store.consume_notification_native_start,
                notification_batch_admission_id, claim_id=queued_admission_claim_id,
                requested_execution_id=execution_id, thread_id=thread_id, message=message)
        if self.upgrade_turn_admission_guard is not None:
            current_bootstrap = self._bootstrap_binding_for_thread(thread_id)
            try:
                await self._require_upgrade_turn_admission(
                    thread_id, project,
                    current_bootstrap.assignment_id if current_bootstrap is not None else None,
                    allow_bootstrap_recovery=True,
                )
            except HTTPException as exc:
                # This control-plane guard precedes worker/session routing. Its
                # typed refusal is affirmative evidence, unlike absent receipts.
                code=exc.detail.get("code") if isinstance(exc.detail,dict) else None
                if notification_batch_admission_id and code in {
                        "upgrade_maintenance", "upgrade_maintenance_active", "upgrade_turn_scope_unknown"}:
                    await asyncio.to_thread(self.queued_preflight.store.record_notification_prestart_refusal,
                        notification_batch_admission_id,claim_id=queued_admission_claim_id,code=code)
                raise
        binding_service, default_session_manager = self._require_worker_routing()
        if ingress_execution_profile_definition is not None:
            catalogs = getattr(binding_service, "execution_profiles", None)
            if catalogs is None:
                raise ExecutionPrestartRefusal(status_code=409, detail={"code": "ingress_execution_definition_unavailable"})
            _, current_definition = catalogs.resolve(execution_profile_id,
                organization_id=project.organization_id, workspace_id=project.workspace_id, project_id=project.id)
            if current_definition != ingress_execution_profile_definition:
                raise ExecutionPrestartRefusal(status_code=409, detail={"code": "ingress_execution_definition_changed"})
        settings = h._thread_run_settings(thread_id)
        effective_sandbox = sandbox or settings.sandbox or project.sandbox
        effective_approval_policy = (
            approval_policy or settings.approval_policy or project.approval_policy
        )
        # Explicit public scope, including an empty set, survives recovery.
        # Omitted unscoped requests still inherit remembered thread defaults.
        scoped_read_context = bool(
            read_only_scope_supplied or work_item_ref
            or source.startswith('queued:') or queued_admission_attempt_id
        )
        effective_read_only_repositories = tuple(
            read_only_repository_resource_ids if scoped_read_context else
            (read_only_repository_resource_ids or settings.read_only_repository_resource_ids))
        resource_context=(resource_context if (resource_context_supplied or work_item_ref
            or source.startswith('queued:') or queued_admission_attempt_id) else
            resource_context or getattr(settings,'resource_context',None))
        if resource_context is not None and actor is None and not source.startswith(('queued:','restart-recovery:')):
            raise ExecutionPrestartRefusal(status_code=403,detail={'code':'resource_context_current_requester_required'})
        if resource_context is not None and actor is not None and actor.identity_id!=resource_context.requester_identity_id:
            raise ExecutionPrestartRefusal(status_code=403,detail={'code':'resource_context_requester_changed'})
        effective_model = model or settings.model or project.model
        effective_reasoning_effort = reasoning_effort or settings.reasoning_effort
        effective_execution_profile_id = (
            execution_profile_id or settings.execution_profile_id
        )
        effective_developer_instructions = h._effective_developer_instructions(
            thread_id,
            settings.developer_instructions,
        )
        requested_execution_id = execution_id or self._new_execution_id()
        requested_writable_repositories = tuple(
            dict.fromkeys(
                str(value).strip()
                for value in writable_repository_resource_ids
                if str(value).strip()
            )
        )
        work_item_writable_repositories = await asyncio.to_thread(
            self._work_item_writable_repository_ids, work_item_ref,
        )
        if (
            requested_writable_repositories
            and work_item_writable_repositories
            and requested_writable_repositories != work_item_writable_repositories
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "work_item_repository_scope_conflict",
                    "workItemRef": work_item_ref,
                    "requestedWritableRepositoryResourceIds": list(
                        requested_writable_repositories
                    ),
                    "workItemWritableRepositoryResourceIds": list(
                        work_item_writable_repositories
                    ),
                },
            )
        effective_writable_repositories = (
            requested_writable_repositories
            or work_item_writable_repositories
        )
        writable_repository_source = (
            RepositoryTargetSource.EXPLICIT
            if requested_writable_repositories
            else RepositoryTargetSource.WORK_ITEM
        )
        trusted_local_codex_requested = (
            self._trusted_local_codex_session_enabled(
                source=source,
                runtime_binding=None,
            )
        )
        trusted_local_codex_session = False

        # Admission remains serialized within a thread. Independent isolated
        # threads must not wait behind another thread's provider RPC/bootstrap.
        start_lock = self._turn_start_locks.get(thread_id)
        if start_lock is None:
            start_lock = asyncio.Lock()
            self._turn_start_locks[thread_id] = start_lock
        async with start_lock:
            if self.thread_is_active(thread_id) and not (
                preserve_active_handoff and self.thread_handoff_in_progress(thread_id)
            ):
                raise ExecutionPrestartRefusal(
                    status_code=409,
                    detail={
                        "code": "thread_turn_already_active",
                        "threadId": thread_id,
                    },
                )
            if scoped_read_context:
                remember = getattr(h, '_remember_thread_run_settings', None)
                if callable(remember):
                    # Recovery must recreate the request's scope, not an older
                    # thread-wide context. Preserve actor/profile and grants.
                    remember(thread_id,
                        repository_resource_id=repository_resource_id,
                        writable_repository_resource_ids=(
                            requested_writable_repositories or None),
                        read_only_repository_resource_ids=effective_read_only_repositories,
                        resource_context=resource_context,
                        resource_context_supplied=True)
            bootstrap = self._bootstrap_binding_for_thread(thread_id)
            desired_runtime = _runtime_for_model(effective_model)
            if bootstrap is None:
                bootstrap = await self._convert_legacy_thread_to_bootstrap(
                    thread_id=thread_id,
                    project=project,
                    desired_runtime=desired_runtime,
                    sandbox=effective_sandbox,
                    approval_policy=effective_approval_policy,
                    execution_profile_id=effective_execution_profile_id,
                    explicit_repository_id=(
                        repository_resource_id or settings.repository_resource_id
                    ),
                    writable_repository_ids=tuple(
                        effective_writable_repositories
                    ),
                    read_only_repository_ids=effective_read_only_repositories,
                    **({"resource_context":resource_context} if resource_context is not None else {}),
                    trusted_local_codex_requested=trusted_local_codex_requested,
                    actor=actor,
                    agent_profile_id=agent_profile_id,
                    agent_profile_revision=agent_profile_revision,
                )
            canonical_repository_resource_id: str | None = None
            agent_profile_binding = None
            if bootstrap is not None:
                session_manager = None
                session = None
                assignment = None
                runtime_binding = None
                recovered_profile = None
                try:
                    session_manager, session = self._session_for_assignment(
                        bootstrap.assignment_id
                    )
                    assignment = session.validate_current()
                    runtime_binding = getattr(
                        assignment,
                        "runtime_binding",
                        None,
                    )
                except AssignmentBoundAgentSessionStaleError:
                    # Evict a process whose canonical lease/fence expired so
                    # the normal bootstrap-healing path can bind a fresh
                    # assignment instead of surfacing an internal error.
                    if session_manager is not None:
                        with contextlib.suppress(Exception):
                            await session_manager.stop(bootstrap.assignment_id)
                    session = None
                    assignment = self._assignment_record(
                        bootstrap.assignment_id
                    )
                    runtime_binding = (
                        getattr(assignment, "runtime_binding", None)
                        if assignment is not None
                        else None
                    )
                except HTTPException:
                    assignment = self._assignment_record(
                        bootstrap.assignment_id
                    )
                    runtime_binding = (
                        getattr(assignment, "runtime_binding", None)
                        if assignment is not None
                        else None
                    )
                if (assignment is not None and getattr(assignment,'resource_context',None)!=resource_context
                        and (assignment.status!=AssignmentStatus.CANCELLED or session is not None)):
                    raise ExecutionPrestartRefusal(status_code=409,detail={
                        'code':'resource_context_rollover_required',
                        'next_action':'Preserve terminal custody, cancel/fence exact old binding, and admit fresh SAME-thread bootstrap; no live scope expansion.'})
                # Recovery may clear the historical assignment variable after
                # a failed start. Its immutable profile must survive that loss.
                recovered_profile = getattr(assignment, "agent_profile", None)
                profile_revision_drift = bool(agent_profile_id and recovered_profile is not None
                    and recovered_profile.profile_id == agent_profile_id
                    and agent_profile_revision is not None
                    and recovered_profile.profile_revision != agent_profile_revision)
                if profile_revision_drift:
                    # A new pinned revision of the same collaborator requires
                    # a freshly authorized binding, not rewriting the request
                    # or pretending the previous assignment has that revision.
                    recovered_profile = None
                if agent_profile_id and recovered_profile is not None and (
                    recovered_profile.profile_id != agent_profile_id
                    or (
                        agent_profile_revision is not None
                        and recovered_profile.profile_revision != agent_profile_revision
                    )
                ):
                    raise ExecutionPrestartRefusal(
                        status_code=409,
                        detail={
                            "code": "thread_agent_profile_immutable",
                            "threadId": thread_id,
                            "requestedAgentProfileId": agent_profile_id,
                            "requestedAgentProfileRevision": agent_profile_revision,
                            "effectiveAgentProfile": recovered_profile.model_dump(mode="json"),
                        },
                    )

                async def resolve_recovery_profile():
                    nonlocal runtime_binding, recovered_profile
                    if not agent_profile_id or recovered_profile is not None:
                        return
                    selected_runtime, selected_profile = await self._select_runtime_binding(
                        project_id=project.id,
                        sandbox=effective_sandbox,
                        trusted_local_codex_session=False,
                        actor=actor,
                        agent_profile_id=agent_profile_id,
                        agent_profile_revision=agent_profile_revision,
                    )
                    if selected_profile is not None and (
                        selected_profile.profile_id != agent_profile_id or (
                            agent_profile_revision is not None
                            and selected_profile.profile_revision != agent_profile_revision)):
                        raise ExecutionPrestartRefusal(status_code=409, detail={
                            'code':'execution_preflight_blocked',
                            'message':'Canonical recovery did not select the exact requested profile revision.',
                            'retryable':False})
                    expected_runtime = desired_runtime or (
                        (runtime_binding.provider_id, runtime_binding.runtime_id)
                        if runtime_binding is not None else None
                    )
                    if (
                        selected_runtime is None
                        or selected_profile is None
                        or (
                            expected_runtime is not None
                            and (selected_runtime.provider_id, selected_runtime.runtime_id)
                            != expected_runtime
                        )
                    ):
                        # Resolving a missing profile cannot implicitly change
                        # an inherited or explicitly requested runtime.
                        raise ExecutionPrestartRefusal(
                            status_code=409,
                            detail={
                                "code": "execution_preflight_blocked",
                                "message": "requested agent profile has no compatible recovery runtime",
                                "retryable": False,
                            },
                        )
                    runtime_binding = selected_runtime
                    recovered_profile = selected_profile

                if (
                    session is None
                    and assignment is None
                    and runtime_binding is None
                    and desired_runtime is None
                ):
                    # A durable bootstrap can outlive its historical assignment.
                    # Re-route through canonical access/authority checks rather
                    # than guessing a runtime or leaving the thread stuck.
                    runtime_binding, recovered_profile = await self._select_runtime_binding(
                        project_id=project.id,
                        sandbox=effective_sandbox,
                        trusted_local_codex_session=False,
                        actor=actor,
                        agent_profile_id=agent_profile_id,
                        agent_profile_revision=agent_profile_revision,
                    )
                switch_needed = profile_revision_drift or (
                    desired_runtime is not None
                    and (
                        runtime_binding is None
                        or (
                            runtime_binding.provider_id,
                            runtime_binding.runtime_id,
                        )
                        != desired_runtime
                    )
                )
                if not switch_needed and assignment is not None:
                    requested_primary = (
                        repository_resource_id or settings.repository_resource_id
                    )
                    assignment_primary = getattr(
                        getattr(assignment, "repository_target", None),
                        "mutable_repository_id",
                        None,
                    )
                    target_drift = (
                        requested_primary is not None
                        and assignment_primary != requested_primary
                    )
                    scope_drift = bool(
                        effective_writable_repositories
                        and set(effective_writable_repositories)
                        != set(
                            getattr(
                                getattr(
                                    assignment,
                                    "repository_scope",
                                    None,
                                ),
                                "writable_repository_ids",
                                (),
                            )
                        )
                    )
                    read_context_drift = set(effective_read_only_repositories) != set(
                        getattr(getattr(assignment, 'repository_scope', None),
                                'read_only_repository_ids', ()))
                    if target_drift or scope_drift or read_context_drift or getattr(assignment,'resource_context',None)!=resource_context:
                        switch_needed = True
                if switch_needed:
                    await resolve_recovery_profile()
                    switch_runtime = desired_runtime or (
                        (
                            runtime_binding.provider_id,
                            runtime_binding.runtime_id,
                        )
                        if runtime_binding is not None
                        else None
                    )
                    if switch_runtime is None:
                        raise HTTPException(
                            status_code=503,
                            detail=(
                                "thread bootstrap binding has no live agent "
                                "runtime session"
                            ),
                        )
                    bootstrap = await self._supersede_thread_bootstrap(
                        thread_id=thread_id,
                        project=project,
                        runtime_binding=(
                            runtime_binding
                            if recovered_profile is not None and runtime_binding is not None
                            and (runtime_binding.provider_id, runtime_binding.runtime_id) == switch_runtime
                            else ExecutionRuntimeBinding(
                                provider_id=switch_runtime[0],
                                runtime_id=switch_runtime[1],
                                capability_revision=1,
                                sandbox_profiles=("read-only", "workspace-write", "danger-full-access"),
                            )
                        ),
                        sandbox=effective_sandbox,
                        approval_policy=effective_approval_policy,
                        execution_profile_id=(recovered_profile.execution_profile_id
                            if profile_revision_drift else getattr(assignment,
                                "execution_profile_id", effective_execution_profile_id)),
                        agent_profile=(recovered_profile if profile_revision_drift else
                            getattr(assignment, "agent_profile", None) or recovered_profile),
                        # A repository-scoped turn is allowed to supersede a
                        # thread's previous bootstrap repository. Prefer the
                        # requested target here; using the old assignment
                        # first pairs its repository with the new writable
                        # scope and fails closed as a false scope conflict.
                        explicit_repository_id=(
                            repository_resource_id
                            or (
                                effective_writable_repositories[0]
                                if len(effective_writable_repositories) == 1
                                else None
                            )
                            or settings.repository_resource_id
                            or getattr(
                                getattr(
                                    assignment,
                                    "repository_target",
                                    None,
                                ),
                                "mutable_repository_id",
                                None,
                            )
                        ),
                        writable_repository_ids=tuple(
                            effective_writable_repositories
                        ),
                        read_only_repository_ids=effective_read_only_repositories,
                        **({"resource_context":resource_context} if resource_context is not None else {}),
                        previous_assignment_id=bootstrap.assignment_id,
                        recovery_admission=operator_admission_validator if getattr(operator_admission_validator, "begin_rollover", None) else None,
                    )
                    if not (
                        recovered_profile is not None and runtime_binding is not None
                        and (runtime_binding.provider_id, runtime_binding.runtime_id) == switch_runtime
                    ):
                        runtime_binding = ExecutionRuntimeBinding(
                            provider_id=switch_runtime[0],
                            runtime_id=switch_runtime[1],
                            capability_revision=1,
                        )
                    session_manager = None
                    session = None
                    assignment = None
                if session is None:
                    try:
                        session_manager = self._manager_for_binding(
                            runtime_binding
                        )
                        session = await session_manager.start(
                            bootstrap.assignment_id
                        )
                        assignment = session.validate_current()
                        runtime_binding = getattr(
                            assignment,
                            "runtime_binding",
                            None,
                        )
                    except Exception:
                        session = None
                        assignment = None
                if session is not None:
                    status = session.status()
                    if not getattr(status, "running", True):
                        session = None
                        assignment = None
                if session is None:
                    # The binding cannot be re-acquired (for example its
                    # workspace was discarded after a restart): heal the
                    # thread by superseding onto the bound or desired runtime
                    # with a fresh assignment and workspace.
                    await resolve_recovery_profile()
                    heal_runtime = desired_runtime or (
                        (
                            runtime_binding.provider_id,
                            runtime_binding.runtime_id,
                        )
                        if runtime_binding is not None
                        else None
                    )
                    if heal_runtime is None:
                        raise HTTPException(
                            status_code=503,
                            detail=(
                                "thread bootstrap binding has no live agent "
                                "runtime session"
                            ),
                        )
                    bootstrap = await self._supersede_thread_bootstrap(
                        thread_id=thread_id,
                        project=project,
                        runtime_binding=(
                            runtime_binding
                            if desired_runtime is None or (
                                recovered_profile is not None
                                and runtime_binding is not None
                                and (runtime_binding.provider_id, runtime_binding.runtime_id) == desired_runtime
                            )
                            else ExecutionRuntimeBinding(
                                provider_id=desired_runtime[0],
                                runtime_id=desired_runtime[1],
                                capability_revision=1,
                                sandbox_profiles=(
                                    "read-only",
                                    "workspace-write",
                                    "danger-full-access",
                                ),
                            )
                        ),
                        sandbox=effective_sandbox,
                        approval_policy=effective_approval_policy,
                        execution_profile_id=effective_execution_profile_id,
                        agent_profile=(
                            getattr(assignment, "agent_profile", None)
                            or recovered_profile
                        ),
                        explicit_repository_id=(
                            repository_resource_id
                            or settings.repository_resource_id
                        ),
                        writable_repository_ids=tuple(
                            effective_writable_repositories
                        ),
                        read_only_repository_ids=effective_read_only_repositories,
                        **({"resource_context":resource_context} if resource_context is not None else {}),
                        previous_assignment_id=bootstrap.assignment_id,
                        recovery_admission=operator_admission_validator if getattr(operator_admission_validator, "begin_rollover", None) else None,
                    )
                    session_manager = self._manager_for_binding(
                        runtime_binding
                        if desired_runtime is None or (
                            recovered_profile is not None
                            and runtime_binding is not None
                            and (runtime_binding.provider_id, runtime_binding.runtime_id) == desired_runtime
                        )
                        else ExecutionRuntimeBinding(
                            provider_id=desired_runtime[0],
                            runtime_id=desired_runtime[1],
                            capability_revision=1,
                        )
                    )
                    session = await session_manager.start(
                        bootstrap.assignment_id
                    )
                    assignment = session.validate_current()
                    runtime_binding = getattr(
                        assignment,
                        "runtime_binding",
                        None,
                    )
                agent_profile_binding = getattr(
                    assignment,
                    "agent_profile",
                    None,
                )
                if agent_profile_id:
                    if (
                        agent_profile_binding is None
                        or agent_profile_binding.profile_id != agent_profile_id
                        or (
                            agent_profile_revision is not None
                            and agent_profile_binding.profile_revision
                            != agent_profile_revision
                        )
                    ):
                        raise ExecutionPrestartRefusal(
                            status_code=409,
                            detail={
                                "code": "thread_agent_profile_immutable",
                                "threadId": thread_id,
                                "requestedAgentProfileId": agent_profile_id,
                                "requestedAgentProfileRevision": (
                                    agent_profile_revision
                                ),
                                "effectiveAgentProfile": (
                                    agent_profile_binding.model_dump(
                                        mode="json"
                                    )
                                    if agent_profile_binding is not None
                                    else None
                                ),
                            },
                        )
                if (
                    assignment.id != bootstrap.assignment_id
                    or assignment.execution_id != bootstrap.execution_id
                    or assignment.execution_workspace_id
                    != bootstrap.execution_workspace_id
                ):
                    raise HTTPException(
                        status_code=503,
                        detail=(
                            "thread bootstrap binding no longer matches "
                            "canonical assignment state"
                        ),
                    )
                if assignment.project_id != project.id:
                    raise HTTPException(
                        status_code=409,
                        detail="thread bootstrap project cannot change",
                    )
                if (
                    assignment.sandbox != effective_sandbox
                    or assignment.approval_policy != effective_approval_policy
                ):
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "thread bootstrap sandbox/approval controls are "
                            "immutable for the live isolated session"
                        ),
                    )
                if getattr(assignment, "execution_profile_id", None) is None:
                    if (
                        effective_execution_profile_id
                        not in {None, "repository-write"}
                    ):
                        raise HTTPException(
                            status_code=409,
                            detail={
                                "code": "thread_execution_profile_immutable",
                                "threadId": thread_id,
                                "requestedExecutionProfileId": effective_execution_profile_id,
                                "effectiveExecutionProfileId": None,
                            },
                        )
                    effective_execution_profile_id = None
                elif (
                    effective_execution_profile_id
                    and getattr(assignment, "execution_profile_id", None)
                    != effective_execution_profile_id
                ):
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "thread_execution_profile_immutable",
                            "threadId": thread_id,
                            "requestedExecutionProfileId": effective_execution_profile_id,
                            "effectiveExecutionProfileId": getattr(assignment, "execution_profile_id", None),
                        },
                    )
                else:
                    effective_execution_profile_id = getattr(assignment, "execution_profile_id", None)
                target = getattr(assignment, "repository_target", None)
                scope = getattr(assignment, "repository_scope", None)
                if effective_writable_repositories:
                    effective_writable = tuple(
                        getattr(scope, "writable_repository_ids", ())
                    )
                    # Repository authority is a set: ordering differences from
                    # policy resolution must not invalidate a thread binding.
                    if set(effective_writable_repositories) != set(
                        effective_writable
                    ):
                        raise HTTPException(
                            status_code=409,
                            detail={
                                "code": "thread_repository_scope_immutable",
                                "threadId": thread_id,
                                "requestedWritableRepositoryResourceIds": list(
                                    effective_writable_repositories
                                ),
                                "effectiveWritableRepositoryResourceIds": list(
                                    effective_writable
                                ),
                            },
                        )
                requested_repository = (
                    repository_resource_id or settings.repository_resource_id
                )
                if requested_repository and (
                    target is None
                    or target.mutable_repository_id != requested_repository
                ):
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "thread_repository_target_immutable",
                            "threadId": thread_id,
                            "requestedRepositoryResourceId": requested_repository,
                            "effectiveRepositoryResourceId": (
                                target.mutable_repository_id
                                if target is not None
                                else None
                            ),
                        },
                    )
                canonical_repository_resource_id = (
                    target.mutable_repository_id
                    if target is not None
                    else settings.repository_resource_id
                )
                canonical_execution_id = bootstrap.execution_id
                assignment_id = bootstrap.assignment_id
                workspace_id = bootstrap.execution_workspace_id
            else:
                desired_runtime = _runtime_for_model(effective_model)
                if desired_runtime is not None:
                    runtime_binding = ExecutionRuntimeBinding(
                        provider_id=desired_runtime[0],
                        runtime_id=desired_runtime[1],
                        capability_revision=1,
                        sandbox_profiles=(
                            "read-only",
                            "workspace-write",
                            "danger-full-access",
                        ),
                    )
                else:
                    runtime_binding, agent_profile_binding = (
                        await self._select_runtime_binding(
                            project_id=project.id,
                            sandbox=effective_sandbox,
                            trusted_local_codex_session=(
                                trusted_local_codex_requested
                            ),
                            actor=actor,
                            agent_profile_id=agent_profile_id,
                            agent_profile_revision=agent_profile_revision,
                        )
                    )
                if (
                    agent_profile_binding is not None
                    and agent_profile_binding.execution_profile_id
                ):
                    if (
                        effective_execution_profile_id
                        and effective_execution_profile_id
                        != agent_profile_binding.execution_profile_id
                    ):
                        raise HTTPException(
                            status_code=409,
                            detail={
                                "code": "agent_profile_execution_profile_conflict",
                                "threadId": thread_id,
                                "requestedExecutionProfileId": (
                                    effective_execution_profile_id
                                ),
                                "agentProfileExecutionProfileId": (
                                    agent_profile_binding.execution_profile_id
                                ),
                            },
                        )
                    effective_execution_profile_id = (
                        agent_profile_binding.execution_profile_id
                    )
                if runtime_binding is not None and (
                    runtime_binding.provider_id != "openai"
                    or runtime_binding.runtime_id != "codex"
                ):
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "switching a legacy/non-bootstrap thread to a different "
                            "agent runtime requires creating a new thread"
                        ),
                    )
                session_manager = self._manager_for_binding(runtime_binding)
                canonical_execution_id = requested_execution_id
                trusted_local_codex_session = (
                    self._trusted_local_codex_session_enabled(
                        source=source,
                        runtime_binding=runtime_binding,
                    )
                )
                if trusted_local_codex_session:
                    # The control-plane app-server was started outside a worker
                    # and owns its own interactive Codex login.  Do not copy or
                    # mount that credential material into repository execution.
                    session = h.codex
                    assignment = None
                    canonical_repository_resource_id = (
                        repository_resource_id
                        or settings.repository_resource_id
                    )
                    assignment_id = None
                    workspace_id = None
                    h._append_bot_event(
                        {
                            "type": "trusted_local_codex_session_selected",
                            "thread_id": thread_id,
                            "project_id": project.id,
                            "source": source,
                            "execution_id": canonical_execution_id,
                            "authentication_source": "local_codex_session",
                        }
                    )
                else:
                    try:
                        binding = binding_service.prepare(
                            thread_id=thread_id,
                            execution_id=canonical_execution_id,
                            project_id=project.id,
                            sandbox=effective_sandbox,
                            approval_policy=effective_approval_policy,
                            runtime_binding=runtime_binding,
                            explicit_repository_id=(
                                repository_resource_id
                                or settings.repository_resource_id
                            ),
                            writable_repository_ids=(
                                effective_writable_repositories
                            ),
                            writable_repository_source=writable_repository_source,
                            read_only_repository_ids=effective_read_only_repositories,
                            **({"resource_context":resource_context} if resource_context is not None else {}),
                            work_item_ref=work_item_ref,
                            execution_profile_id=effective_execution_profile_id,
                            agent_profile=agent_profile_binding,
                            skill_refs=tuple(settings.skill_refs or ()),
                        )
                    except TurnExecutionBindingError as exc:
                        raise _execution_preflight_blocked(exc) from exc
                    session = await session_manager.start(binding.assignment_id)
                    assignment = binding
                    runtime_binding = getattr(binding, "runtime_binding", runtime_binding)
                    canonical_repository_resource_id = getattr(
                        binding,
                        "repository_resource_id",
                        repository_resource_id or settings.repository_resource_id,
                    )
                    assignment_id = binding.assignment_id
                    workspace_id = binding.workspace_id

            procedure_discovery = None
            if self.procedure_admission is not None:
                procedure_discovery, procedure_text = await self.procedure_admission(
                    project=project, actor=actor or self.control_actor, input_actor=actor,
                    thread_id=thread_id, execution_id=canonical_execution_id,
                    assignment_id=assignment_id, sandbox=effective_sandbox,
                    source=source, request_checksum=hashlib.sha256(message.encode()).hexdigest(),
                )
                if procedure_text:
                    effective_developer_instructions = "\n\n".join(
                        item for item in (effective_developer_instructions, procedure_text) if item)

            input_admission_validator = None
            if self.native_input_guard_factory is not None:
                input_admission_validator = await asyncio.to_thread(self.native_input_guard_factory,
                    project=project, actor=actor, execution_id=canonical_execution_id,
                    assignment_id=assignment_id, source=source)

            skill_context_selection = None
            effective_skill_refs = tuple(dict.fromkeys(
                tuple(getattr(assignment, "skill_refs", ()) or ())
                or (
                    tuple(agent_profile_binding.skill_refs if agent_profile_binding is not None else ())
                    + tuple(settings.skill_refs or ())
                )
            ))
            if effective_skill_refs:
                if self.skill_context_resolver is None:
                    raise ExecutionPrestartRefusal(
                        status_code=503,
                        detail={
                            "code": "execution_preflight_blocked",
                            "message": (
                                "Execution references Skills but Skill "
                                "context resolution is unavailable"
                            ),
                            "blockers": [
                                {
                                    "code": "skill_definition_unavailable",
                                    "message": (
                                        "Skill context resolution is unavailable"
                                    ),
                                    "retryable": False,
                                    "target_type": "agent_profile" if agent_profile_binding is not None else "thread",
                                    "target_id": agent_profile_binding.profile_id if agent_profile_binding is not None else thread_id,
                                    "remediation_route": "/api/skills",
                                }
                            ],
                            "retryable": False,
                        },
                    )
                try:
                    skill_context_selection = await asyncio.to_thread(
                        self.skill_context_resolver,
                        effective_skill_refs,
                        project,
                        message,
                    )
                except Exception as exc:
                    raise ExecutionPrestartRefusal(
                        status_code=503,
                        detail={
                            "code": "execution_preflight_blocked",
                            "message": str(exc),
                            "blockers": [
                                {
                                    "code": "skill_definition_unavailable",
                                    "message": str(exc),
                                    "retryable": False,
                                    "target_type": "agent_profile" if agent_profile_binding is not None else "thread",
                                    "target_id": agent_profile_binding.profile_id if agent_profile_binding is not None else thread_id,
                                    "remediation_route": "/api/skills",
                                }
                            ],
                            "retryable": False,
                        },
                    ) from exc
                skill_text = str(
                    getattr(skill_context_selection, "text", "")
                    or ""
                ).strip()
                if skill_text:
                    effective_developer_instructions = "\n\n".join(
                        item
                        for item in (
                            effective_developer_instructions,
                            skill_text,
                        )
                        if item
                    )

            effective_work_item_ref = (
                work_item_ref
                or getattr(assignment, "work_item_ref", None)
            )
            (
                work_item_context_text,
                work_item_context_selection,
                delivered_work_item_context,
            ) = self._work_item_continuation_context(
                effective_work_item_ref
            )
            if work_item_context_text:
                effective_developer_instructions = "\n\n".join(
                    item
                    for item in (
                        effective_developer_instructions,
                        work_item_context_text,
                    )
                    if item
                )

            if assignment is not None:
                broker_instructions = assignment_control_plane_instructions()
                if broker_instructions not in (effective_developer_instructions or ""):
                    effective_developer_instructions = "\n\n".join(
                        item
                        for item in (effective_developer_instructions, broker_instructions)
                        if item
                    )

            if trusted_local_codex_session:
                workspace_cwd = project.path
                worker_id = "local-codex-app-server"
                fence = None
            else:
                status = session.status()
                workspace_path = session.workspace_path
                if workspace_path is None or status.fence is None:
                    raise HTTPException(
                        status_code=503,
                        detail="assignment-bound agent session lacks canonical workspace/fence",
                    )
                workspace_cwd = str(workspace_path)
                worker_id = status.worker_id
                fence = status.fence
            resume_params = {
                "threadId": thread_id,
                **h._project_params(
                    project,
                    {
                        "sandbox": effective_sandbox,
                        "approvalPolicy": effective_approval_policy,
                        "model": effective_model,
                        "developerInstructions": effective_developer_instructions,
                    },
                ),
            }
            resume_params["cwd"] = workspace_cwd
            effective_runtime_model = _runtime_model_id(
                effective_model,
                runtime_binding,
            )
            if effective_sandbox:
                resume_params["sandboxPolicy"] = h._sandbox_policy(
                    effective_sandbox,
                    workspace_cwd,
                )
                if not trusted_local_codex_session:
                    resume_params["sandboxPolicy"] = assignment_sandbox_policy(
                        resume_params["sandboxPolicy"],
                        mode=effective_sandbox,
                        writable_roots=tuple(
                            str(path)
                            for path in (
                                getattr(session, "git_metadata_path", None),
                                getattr(
                                    session,
                                    "git_worktree_metadata_path",
                                    None,
                                ),
                            )
                            if path is not None
                        ),
                    )
            runtime_session_request = AgentRuntimeSessionRequest(
                project_id=project.id,
                resource_ids=tuple(getattr(assignment, "resource_ids", ())) if assignment is not None else (),
                sandbox=effective_sandbox,
                approval_policy=effective_approval_policy,
                approval_reviewer=resume_params.get("approvalsReviewer"),
                workspace_cwd=workspace_cwd,
                sandbox_policy=resume_params.get("sandboxPolicy"),
                execution_id=canonical_execution_id,
                assignment_id=assignment_id,
                execution_workspace_id=workspace_id,
                worker_id=worker_id,
                model=effective_runtime_model,
                developer_instructions=effective_developer_instructions,
            )
            runtime_adapter = self._adapter_for_binding(
                runtime_binding,
                session,
            )
            native_session_id = (
                getattr(getattr(session, "runtime", None), "native_session_id", None)
                or thread_id
            )
            initial_turn_pending = bool(
                bootstrap is not None
                and getattr(bootstrap, "initial_turn_pending", False)
            )
            if (initial_turn_pending and isinstance(runtime_adapter, CodexAgentRuntimeAdapter)
                    and not runtime_adapter.initial_session_is_live(native_session_id)):
                # A durable pending flag cannot attest a thread in a new native
                # process. Refuse before resume/turn input and fence this idle
                # assignment; never turn a lost initial session into an unknown
                # turn/start or an automatic replacement/replay loop.
                retained = (await asyncio.to_thread(self.queued_preflight.store.get,
                    queued_admission_attempt_id) if queued_admission_attempt_id and self.queued_preflight else None)
                await session_manager.complete(assignment_id, succeeded=False,
                    failure_code="initial_bootstrap_process_lost",
                    failure_message="Pending initial native session belongs to a previous process")
                raise ExecutionPrestartRefusal(status_code=409, detail={
                    "code": "initial_bootstrap_process_lost",
                    "message": "Recover a fresh bootstrap for this exact original; its initial native process is unavailable.",
                    "phase": "before_turn_input", "threadId": thread_id,
                    "bootstrap_id": bootstrap.bootstrap_id, "assignment_id": assignment_id,
                    "preflight_attempt_id": queued_admission_attempt_id,
                    "admission_claim_id": queued_admission_claim_id,
                    "snapshot_sha256": self.queued_preflight.queued_snapshot_digest(retained.queued_turn_snapshot)
                        if retained and retained.queued_turn_snapshot is not None else None,
                    "remediation_route": "/api/threads/" + thread_id + "/preflight-attempts",
                })
            if not initial_turn_pending:
                resume_options = {}
                try:
                    custody_before = None
                    if self.session_custody is not None and bootstrap is not None and not trusted_local_codex_session:
                        from codex_web.services.agent_runtime import AgentRuntimeError
                        from codex_web.services.agent_session_custody import AgentSessionCustodyPrestartRefusal
                        def capture_custody():
                            try:
                                return self.session_custody.capture(thread_id, runtime_adapter,
                                    session, runtime_session_request, fence)
                            except AgentRuntimeError as refusal:
                                retained = (self.queued_preflight.store.get(queued_admission_attempt_id)
                                    if queued_admission_attempt_id and self.queued_preflight else None)
                                raise ExecutionPrestartRefusal(status_code=409, detail={
                                    **(refusal.detail if isinstance(refusal, AgentSessionCustodyPrestartRefusal) else {
                                        "code": "native_session_custody_unavailable", "message": str(refusal),
                                        "phase": "before_provider_resume",
                                        "remediation": "Inspect native session, assignment and binding provenance before retrying.",
                                    }), "threadId": thread_id,
                                    "bootstrap_id": bootstrap.bootstrap_id,
                                    "preflight_attempt_id": queued_admission_attempt_id,
                                    "admission_claim_id": queued_admission_claim_id,
                                    "snapshot_sha256": self.queued_preflight.queued_snapshot_digest(retained.queued_turn_snapshot)
                                        if retained and retained.queued_turn_snapshot is not None else None,
                                }) from refusal
                        custody_before = await self._settle_custody_call(capture_custody, work_owner=session)
                    resume_capture_supported = (queued_admission_attempt_id and queued_admission_claim_id
                            and self.queued_preflight is not None
                            and isinstance(runtime_adapter, CodexAgentRuntimeAdapter))
                    if input_admission_validator is not None:
                        await asyncio.to_thread(input_admission_validator)
                        if not resume_capture_supported:
                            raise ExecutionPrestartRefusal(status_code=409, detail={
                                'code': 'operator_input_transport_unsupported',
                                'message': 'Home operator input requires canonical guarded native transport',
                                'retryable': False})
                    if resume_capture_supported:
                        from codex_web.services.queued_native_resume import QueuedNativeResumeCapture
                        def validate_resume_admission():
                            if operator_admission_validator is not None:
                                operator_admission_validator()
                            owner_admission_validator()
                            if input_admission_validator is not None:
                                input_admission_validator()
                        resume_options['resume_capture'] = QueuedNativeResumeCapture(
                            self.queued_preflight, queued_admission_attempt_id,
                            queued_admission_claim_id, requested_execution_id, thread_id,
                            actor.identity_id if actor is not None else agent_profile_actor_id,
                            admission_validator=validate_resume_admission, require_send_guard=True)
                    resumed = await runtime_adapter.resume_session(
                        native_session_id,
                        runtime_session_request,
                        **resume_options,
                    )
                    if custody_before is not None:
                        await self._settle_custody_call(self.session_custody.complete,
                            custody_before, thread_id, runtime_adapter, session,
                            runtime_session_request, fence, resumed, work_owner=session)
                except Exception as exc:
                    capacity_error = await self._capacity_error(runtime_binding, exc)
                    if capacity_error is not None:
                        raise capacity_error from exc
                    if not trusted_local_codex_session:
                        with contextlib.suppress(Exception):
                            await session_manager.complete(
                                assignment_id,
                                succeeded=False,
                                failure_code=(
                                    "native_session_custody_refused"
                                    if isinstance(exc, ExecutionPrestartRefusal)
                                    and isinstance(exc.detail, dict)
                                    and str(exc.detail.get("code", "")).startswith("native_session_")
                                    else
                                    "codex_thread_resume_failed"
                                    if runtime_binding is None
                                    or (
                                        runtime_binding.provider_id == "openai"
                                        and runtime_binding.runtime_id == "codex"
                                    )
                                    else "agent_thread_resume_failed"
                                ),
                                failure_message=str(exc)[:500],
                            )
                    from codex_web.services.queued_native_start import NativeStartAdmissionRefused
                    capture = resume_options.get('resume_capture')
                    if (isinstance(exc, NativeStartAdmissionRefused) and capture is not None
                            and exc.correlation is not None):
                        # Resume precedes the turn-input try/except below. Carry
                        # only the committed writer-lock no-write fact through
                        # this earlier boundary; never classify provider errors.
                        await asyncio.to_thread(capture.confirm_prewrite_refusal, exc.correlation)
                        from codex_web.services.queued_operator_admission import OperatorAdmissionPrestartRefusal
                        cause = exc.__cause__
                        if isinstance(cause, (OperatorAdmissionPrestartRefusal,
                                              ExecutionPrestartRefusal, AutomatedAdmissionPaused)):
                            raise type(cause)(status_code=cause.status_code, detail=cause.detail) from exc
                    raise

            if (
                self.maintenance_admission is None
                and self.upgrade_turn_admission_guard is not None
            ):
                # Compatibility for installations that have not yet wired the
                # fenced admission coordinator: recheck after provider awaits.
                await self._require_upgrade_turn_admission(
                    thread_id,
                    project,
                    assignment_id,
                )

            memory_preview = None
            memory_actor = None
            native_message = message
            if memory_context_binding is not None:
                context_service = getattr(self, "memory_context_service", None)
                if context_service is None:
                    raise HTTPException(status_code=409, detail={"code": "memory_context_unavailable"})
                memory_preview, memory_actor = context_service.prepare(memory_context_binding, project=project)
                native_message = message + "\n\nSelected UX knowledge (task data):\n" + memory_preview["context"]

            await asyncio.to_thread(self._require_automated_admission, source, operator_admission_validator)
            if notification_batch_admission_id and len(native_message.encode()) > batch.plan.max_context_bytes:
                raise ValueError('notification input including selected context exceeds its byte bound')
            if not preserve_active_handoff:
                self.mark_thread_active(
                    thread_id,
                    project_id=project.id,
                    sandbox=effective_sandbox,
                    approval_policy=effective_approval_policy,
                    model=effective_model,
                    reasoning_effort=effective_reasoning_effort,
                    source=source,
                    reply_target=reply_target,
                    execution_id=canonical_execution_id,
                    assignment_id=assignment_id,
                    resource_context=resource_context,
                    execution_workspace_id=workspace_id,
                    worker_id=worker_id,
                    fence=fence,
                    repository_resource_id=canonical_repository_resource_id,
                    writable_repository_resource_ids=(
                        tuple(
                            getattr(
                                getattr(assignment, "repository_scope", None),
                                "writable_repository_ids",
                                (),
                            )
                        )
                    ),
                    execution_profile_id=effective_execution_profile_id,
                    agent_profile=agent_profile_binding,
                    **({"memory_context_binding": memory_context_binding} if memory_context_binding is not None else {}),
                    agent_profile_actor_id=(
                        actor.identity_id
                        if actor is not None
                        else agent_profile_actor_id
                    ),
                )

            params: dict[str, Any] = {
                "threadId": thread_id,
                "input": [{"type": "text", "text": native_message, "text_elements": []}],
                "cwd": workspace_cwd,
            }
            if effective_runtime_model:
                params["model"] = effective_runtime_model
            if effective_reasoning_effort:
                params["effort"] = effective_reasoning_effort
            if effective_developer_instructions:
                params["developerInstructions"] = effective_developer_instructions
            if effective_approval_policy:
                params["approvalPolicy"] = effective_approval_policy
            if effective_sandbox:
                params["sandboxPolicy"] = h._sandbox_policy(
                    effective_sandbox,
                    workspace_cwd,
                )
                if not trusted_local_codex_session:
                    params["sandboxPolicy"] = assignment_sandbox_policy(
                        params["sandboxPolicy"],
                        mode=effective_sandbox,
                        writable_roots=tuple(
                            str(path)
                            for path in (
                                getattr(session, "git_metadata_path", None),
                                getattr(
                                    session,
                                    "git_worktree_metadata_path",
                                    None,
                                ),
                            )
                            if path is not None
                        ),
                    )
            if (ingress_execution_profile_definition is not None
                    and getattr(assignment, "execution_profile_definition", None) != ingress_execution_profile_definition):
                raise ExecutionPrestartRefusal(status_code=409, detail={"code": "ingress_execution_definition_assignment_mismatch"})
            if ingress_execution_profile_definition is not None:
                assigned_scope = getattr(assignment, "repository_scope", None)
                assigned_writes = tuple(getattr(assigned_scope, "writable_repository_ids", ()))
                assigned_reads = tuple(getattr(assigned_scope, "read_only_repository_ids", ()))
                expected_writes = writable_repository_resource_ids or (
                    (repository_resource_id,) if repository_resource_id else ())
                if (set(assigned_writes) != set(expected_writes)
                        or set(assigned_reads) != set(read_only_repository_resource_ids)):
                    raise ExecutionPrestartRefusal(status_code=409, detail={"code": "ingress_repository_assignment_mismatch"})
            params["input"][0]["text"] = self.with_relay_guard(
                params["input"][0]["text"],
                self.turn_source_for_relay_guard(thread_id, source),
            )
            if resource_context is not None:
                binding_service.validate_assignment_resource_context(assignment)
                await session._supervise("resource_context_authority",session._validate_current_authority,
                seconds=session._lease_budget())
            runtime_turn_request = AgentRuntimeTurnRequest(
                message=params["input"][0]["text"],
                model=effective_runtime_model,
                reasoning_effort=effective_reasoning_effort,
                workspace_cwd=workspace_cwd,
                approval_policy=effective_approval_policy,
                sandbox_policy=params.get("sandboxPolicy"),
                developer_instructions=effective_developer_instructions,
            )
            history_started = False
            queued_native_start_confirmed = False
            preinput_admission_refused = False
            self.record_user_message(thread_id, requested_execution_id, message)
            try:
                if (
                    self.thread_history is not None
                    and getattr(runtime_adapter, "runtime_type", None) == "cli"
                ):
                    self.thread_history.start_turn(
                        thread_id,
                        turn_id=canonical_execution_id,
                        message=message,
                        provider_id=str(runtime_adapter.provider_id),
                        runtime_id=str(runtime_adapter.runtime_id),
                    )
                    history_started = True
                if notification_batch_control_current is not None and not await asyncio.to_thread(notification_batch_control_current):
                    raise ValueError('notification batch controls changed before native input')
                if operator_admission_validator is not None:
                    try:
                        await asyncio.to_thread(operator_admission_validator)
                    except (AutomatedAdmissionPaused, ExecutionPrestartRefusal):
                        preinput_admission_refused = True
                        raise
                try:
                    if input_admission_validator is not None:
                        await asyncio.to_thread(input_admission_validator)
                    owner_admission_validator()
                except (ExecutionPrestartRefusal, AutomatedAdmissionPaused):
                    preinput_admission_refused = True
                    raise
                start_options = {}
                delegated = (TurnQueuePolicy.source_origin(source) == "project-coordination"
                             and operator_admission_validator is not None)
                guarded_owner = owner_routing_pin is not None
                guarded_input = input_admission_validator is not None
                capture_supported = (queued_admission_attempt_id and queued_admission_claim_id
                        and self.queued_preflight is not None
                        and isinstance(runtime_adapter, CodexAgentRuntimeAdapter))
                if (delegated or guarded_owner or guarded_input) and not capture_supported:
                    preinput_admission_refused = True
                    refusal = ExecutionPrestartRefusal if guarded_owner or guarded_input else AutomatedAdmissionPaused
                    raise refusal(status_code=409, detail={
                        "code": ("owner_routing_transport_unsupported" if guarded_owner else
                                 "operator_input_transport_unsupported" if guarded_input else "project_delegation_transport_unsupported"),
                        "message": "Recipient-pinned input requires the canonical guarded native-start transport",
                        "retryable": False})
                if capture_supported:
                    from codex_web.services.queued_native_start import QueuedNativeStartCapture
                    def validate_native_admission():
                        if operator_admission_validator is not None:
                            operator_admission_validator()
                        owner_admission_validator()
                        if input_admission_validator is not None:
                            input_admission_validator()
                    start_options['start_capture'] = QueuedNativeStartCapture(
                        self.queued_preflight, queued_admission_attempt_id,
                        queued_admission_claim_id, requested_execution_id, thread_id,
                        actor.identity_id if actor is not None else agent_profile_actor_id,
                        admission_validator=validate_native_admission if guarded_owner or guarded_input else operator_admission_validator,
                        require_send_guard=delegated or guarded_owner or guarded_input)
                runtime_turn_result = await runtime_adapter.start_turn(
                    native_session_id,
                    runtime_turn_request,
                    **start_options,
                )
                actual_native_turn_id = runtime_turn_result.provider_native_turn_id or (
                    (runtime_turn_result.payload.get('turn') or {}).get('id')
                    if isinstance(runtime_turn_result.payload, dict) else None)
                if ((queued_admission_attempt_id or notification_batch_admission_id) and queued_admission_claim_id
                        and actual_native_turn_id and self.queued_preflight is not None):
                    from codex_web.models import QueuedNativeStartReceipt
                    queued_native_start_confirmed = True
                    self.mark_thread_active(thread_id, turn_id=actual_native_turn_id)
                    receipt = QueuedNativeStartReceipt(
                            admission_claim_id=queued_admission_claim_id,
                            requested_execution_id=requested_execution_id,
                            execution_id=canonical_execution_id, thread_id=thread_id,
                            provider_native_session_id=(runtime_turn_result.provider_native_session_id
                                or native_session_id),
                            provider_native_turn_id=actual_native_turn_id,
                            provider_id=str(runtime_adapter.provider_id),
                            runtime_id=str(runtime_adapter.runtime_id), observed_at=time.time())
                    if notification_batch_admission_id:
                        await asyncio.to_thread(self.queued_preflight.store.record_notification_native_start,
                            notification_batch_admission_id, receipt)
                        capture = getattr(h, "_capture_notification_results", None)
                        if callable(capture):
                            with contextlib.suppress(Exception):
                                await asyncio.to_thread(capture, notification_batch_admission_id)
                    else:
                        await asyncio.to_thread(self.queued_preflight.record_queued_native_start,
                            queued_admission_attempt_id, receipt)
                if memory_preview is not None and actual_native_turn_id:
                    try:
                        context_service.delivered(memory_preview, memory_actor,
                            thread_id=thread_id, execution_id=canonical_execution_id,
                            native_turn_id=actual_native_turn_id,
                            provider_id=runtime_adapter.provider_id,
                            runtime_id=runtime_adapter.runtime_id,
                            message=runtime_turn_request.message)
                    except Exception as audit_error:
                        # Accepted provider starts must never be retried because auditing failed.
                        h._append_bot_event({"type": "memory_context_delivery_audit_failed",
                            "thread_id": thread_id, "execution_id": canonical_execution_id,
                            "error_type": type(audit_error).__name__})
                if initial_turn_pending and self.bootstrap_bindings is not None:
                    self.bootstrap_bindings.mark_initial_turn_started(
                        thread_id,
                        self.control_actor,
                    )
                remember_native_session_id = getattr(
                    session,
                    "remember_native_session_id",
                    None,
                )
                if (
                    callable(remember_native_session_id)
                    and runtime_turn_result.provider_native_session_id
                ):
                    remember_native_session_id(
                        runtime_turn_result.provider_native_session_id
                    )
                response = runtime_turn_result.payload
                if (
                    effective_work_item_ref
                    and work_item_context_selection is not None
                    and delivered_work_item_context is not None
                    and self.work_item_context_recorder is not None
                ):
                    profile = getattr(assignment, "agent_profile", None)
                    definition_refs = []
                    for definition_ref in (
                        getattr(
                            assignment,
                            "execution_profile_definition",
                            None,
                        ),
                        getattr(profile, "instructions_ref", None),
                        *(getattr(assignment, "skill_refs", ()) or getattr(profile, "skill_refs", ()) or ()),
                        getattr(profile, "role_definition_ref", None),
                        getattr(profile, "authority_definition_ref", None),
                    ):
                        if (
                            definition_ref is not None
                            and definition_ref not in definition_refs
                        ):
                            definition_refs.append(definition_ref)
                    with contextlib.suppress(Exception):
                        self.work_item_context_recorder(
                            effective_work_item_ref,
                            canonical_execution_id,
                            work_item_context_selection,
                            delivered_work_item_context,
                            provenance={
                                "execution_contract_version": getattr(
                                    assignment,
                                    "execution_contract_version",
                                    None,
                                ),
                                "agent_profile_id": getattr(
                                    profile,
                                    "profile_id",
                                    None,
                                ),
                                "agent_profile_revision": getattr(
                                    profile,
                                    "profile_revision",
                                    None,
                                ),
                                "role_id": getattr(profile, "role_id", None),
                                "provider_id": (
                                    runtime_binding.provider_id
                                    if runtime_binding is not None
                                    else None
                                ),
                                "runtime_id": (
                                    runtime_binding.runtime_id
                                    if runtime_binding is not None
                                    else None
                                ),
                                "model_id": (
                                    effective_model
                                    or getattr(profile, "model_id", None)
                                ),
                                "session_ref": str(native_session_id),
                                "resource_ids": list(
                                    getattr(assignment, "resource_ids", ()) or ()
                                ),
                                "base_revision": getattr(
                                    assignment,
                                    "base_revision",
                                    None,
                                ),
                                "definition_refs": definition_refs,
                            },
                        )
            except Exception as exc:
                if (not queued_native_start_confirmed and queued_admission_attempt_id
                        and self.queued_preflight is not None):
                    # The reader may have committed positive evidence before
                    # the caller lost its response. Attribute that historical
                    # fact without renewing/resuming a possibly retired worker.
                    captured = await asyncio.to_thread(
                        self.queued_preflight.store.get, queued_admission_attempt_id)
                    correlation = captured.queued_start_correlation if captured else None
                    evidence = captured.queued_start_evidence if captured else None
                    if (correlation is not None and evidence is not None
                            and correlation.admission_claim_id == queued_admission_claim_id
                            and correlation.requested_execution_id == requested_execution_id
                            and correlation.execution_id == canonical_execution_id
                            and correlation.thread_id == thread_id):
                        queued_native_start_confirmed = True
                        actual_native_turn_id = evidence.provider_native_turn_id
                if queued_native_start_confirmed:
                    # The provider accepted the turn. Bookkeeping failure cannot
                    # cancel its ownership or authorize replay of the original.
                    h._append_bot_event({'type':'queued_native_start_bookkeeping_failed',
                        'thread_id':thread_id, 'turn_id':actual_native_turn_id,
                        'attempt_id':queued_admission_attempt_id})
                    raise
                from codex_web.services.queued_native_start import NativeStartAdmissionRefused
                if preinput_admission_refused or isinstance(exc, NativeStartAdmissionRefused):
                    # Only local pre-write witnesses reach this branch. Positive
                    # reader evidence was checked first; post-send failures keep
                    # their existing uncertainty/ownership behavior.
                    self.clear_thread_active(thread_id)
                    if self.transcript is not None:
                        self.transcript.discard_unstarted(thread_id, requested_execution_id)
                    if history_started and self.thread_history is not None:
                        self.thread_history.discard_unstarted(thread_id, canonical_execution_id)
                    if isinstance(exc, (AutomatedAdmissionPaused, ExecutionPrestartRefusal)):
                        raise
                    cause = exc.__cause__
                    if isinstance(cause, ExecutionPrestartRefusal):
                        raise ExecutionPrestartRefusal(status_code=cause.status_code, detail=cause.detail) from exc
                    if isinstance(cause, AutomatedAdmissionPaused):
                        raise AutomatedAdmissionPaused(status_code=cause.status_code, detail=cause.detail) from exc
                    raise AutomatedAdmissionPaused(status_code=409, detail={
                        "code": "project_delegation_preinput_refused",
                        "message": "Delegated input was refused before the native write; inspect its binding and transport",
                        "retryable": False}) from exc
                self.fail_user_message(thread_id, requested_execution_id)
                if history_started and self.thread_history is not None:
                    with contextlib.suppress(Exception):
                        self.thread_history.project_message(
                            thread_id,
                            {
                                "method": "turn/failed",
                                "params": {
                                    "threadId": thread_id,
                                    "turnId": canonical_execution_id,
                                },
                            },
                        )
                capacity_error = await self._capacity_error(runtime_binding, exc)
                if capacity_error is not None:
                    self.clear_thread_active(thread_id)
                    raise capacity_error from exc
                timeout_error = h._is_codex_timeout_error(exc)
                if timeout_error:
                    # Release only timeout markers that have no viable provider
                    # turn; live provider turns retain their recovery state.
                    with contextlib.suppress(Exception):
                        await self.release_unviable_active_turn(thread_id)
                sessionless_cli_start = (
                    getattr(runtime_adapter, "runtime_type", None) == "cli"
                    and not getattr(
                        getattr(session, "runtime", None),
                        "native_session_id",
                        None,
                    )
                )
                if sessionless_cli_start or not timeout_error:
                    self.clear_thread_active(thread_id)
                    if not trusted_local_codex_session:
                        with contextlib.suppress(Exception):
                            await session_manager.complete(
                                assignment_id,
                                succeeded=False,
                                failure_code=(
                                    "codex_turn_start_failed"
                                    if runtime_binding is None
                                    or (
                                        runtime_binding.provider_id == "openai"
                                        and runtime_binding.runtime_id == "codex"
                                    )
                                    else "agent_turn_start_failed"
                                ),
                                failure_message=str(exc)[:500],
                            )
                raise
            turn_id = (
                runtime_turn_result.provider_native_turn_id
                or (
                    (response.get("turn") or {}).get("id")
                    if isinstance(response, dict)
                    else None
                )
            )
            self.last_inputs[thread_id] = {
                "project_id": project.id,
                "message": message,
                **({"memory_context_binding": memory_context_binding} if memory_context_binding is not None else {}),
                "sandbox": effective_sandbox,
                "approval_policy": effective_approval_policy,
                "model": effective_model,
                "reasoning_effort": effective_reasoning_effort,
                "execution_profile_id": effective_execution_profile_id,
                "source": source,
                "reply_target": reply_target,
                "execution_id": canonical_execution_id,
                "assignment_id": assignment_id,
                "execution_workspace_id": workspace_id,
                "worker_id": worker_id,
                "fence": fence,
                "authentication_source": (
                    "local_codex_session"
                    if trusted_local_codex_session
                    else "delegated_worker"
                ),
                "bootstrap_id": bootstrap.bootstrap_id if bootstrap is not None else None,
                "requested_execution_id": (
                    requested_execution_id
                    if bootstrap is not None
                    and requested_execution_id != canonical_execution_id
                    else None
                ),
                "procedure_discovery": procedure_discovery.model_dump(mode="json") if procedure_discovery is not None else None,
                "skill_context": (
                    skill_context_selection.model_dump(mode="json")
                    if skill_context_selection is not None
                    and hasattr(skill_context_selection, "model_dump")
                    else None
                ),
            }
            self.mark_thread_active(
                thread_id,
                turn_id=turn_id,
                project_id=project.id,
                sandbox=effective_sandbox,
                approval_policy=effective_approval_policy,
                model=effective_model,
                reasoning_effort=effective_reasoning_effort,
                source=source,
                reply_target=reply_target,
                execution_id=canonical_execution_id,
                assignment_id=assignment_id,
                execution_workspace_id=workspace_id,
                worker_id=worker_id,
                fence=fence,
                repository_resource_id=canonical_repository_resource_id,
                writable_repository_resource_ids=(
                    tuple(
                        getattr(
                            getattr(assignment, "repository_scope", None),
                            "writable_repository_ids",
                            (),
                        )
                    )
                ),
                execution_profile_id=effective_execution_profile_id,
                agent_profile=agent_profile_binding,
                **({"memory_context_binding": memory_context_binding} if memory_context_binding is not None else {}),
                agent_profile_actor_id=(
                    actor.identity_id
                    if actor is not None
                    else agent_profile_actor_id
                ),
            )
        h._append_bot_event(
            {
                "type": "turn_started",
                "thread_id": thread_id,
                "turn_id": turn_id,
                "project_id": project.id,
                "source": source,
                "execution_id": canonical_execution_id,
                "assignment_id": assignment_id,
                "execution_workspace_id": workspace_id,
                "worker_id": worker_id,
                "fence": fence,
                "authentication_source": (
                    "local_codex_session"
                    if trusted_local_codex_session
                    else "delegated_worker"
                ),
                "bootstrap_id": bootstrap.bootstrap_id if bootstrap is not None else None,
                "agent_profile_id": (
                    agent_profile_binding.profile_id
                    if agent_profile_binding is not None
                    else None
                ),
                "agent_profile_revision": (
                    agent_profile_binding.profile_revision
                    if agent_profile_binding is not None
                    else None
                ),
                "agent_provider_id": (
                    agent_profile_binding.selected_provider_id
                    if agent_profile_binding is not None
                    else None
                ),
                "agent_runtime_id": (
                    agent_profile_binding.selected_runtime_id
                    if agent_profile_binding is not None
                    else None
                ),
            }
        )
        if (
            handoff_requested_at is not None and work_item_ref and turn_id
            and agent_profile_binding is not None and self.handoff_delivery_recorder is not None
        ):
            try:
                await asyncio.to_thread(
                    self.handoff_delivery_recorder,
                    work_item_ref, expected_requested_at=handoff_requested_at,
                    **({'owner_routing_pin': owner_routing_pin} if owner_routing_pin is not None else {}),
                    recipient=agent_profile_binding.role_id,
                    organization_id=getattr(assignment, "organization_id", None),
                    workspace_id=getattr(assignment, "workspace_id", None),
                    project_id=getattr(assignment, "project_id", None),
                    thread_id=thread_id, turn_id=turn_id,
                    execution_id=canonical_execution_id, started_at=time.time(),
                    execution_profile_id=getattr(assignment, "execution_profile_id", None),
                    repository_resource_id=getattr(
                        getattr(assignment, "repository_target", None), "mutable_repository_id", None),
                    writable_repository_resource_ids=tuple(getattr(
                        getattr(assignment, "repository_scope", None), "writable_repository_ids", ())),
                    read_only_repository_resource_ids=tuple(getattr(
                        getattr(assignment, "repository_scope", None), "read_only_repository_ids", ())),
                )
            except Exception:
                # The native turn already started; do not replay it because
                # recording metadata failed or invent a delivery timestamp.
                h._append_bot_event({"type":"handoff_delivery_record_failed",
                    "thread_id":thread_id,"ref":work_item_ref,
                    "expected_requested_at":handoff_requested_at})
        await self.publish_queue_status(thread_id)
        return response

    def queue_admission_lock(self, thread_id: str) -> asyncio.Lock:
        lock = self.queue_admission_locks.get(thread_id)
        if lock is None:
            lock = asyncio.Lock()
            self.queue_admission_locks[thread_id] = lock
        return lock

    async def drain_thread_queue(self, thread_id: str) -> None:
        async with self.queue_admission_lock(thread_id):
            if self.ownership is None:
                await self._drain_thread_queue_owned(thread_id)
                return

            async def operation() -> None:
                await self._drain_thread_queue_owned(thread_id)

            await self.ownership.run_exclusive(f"thread-queue:{thread_id}", operation)

    async def _drain_thread_queue_owned(self, thread_id: str, *, operator_original=None,
                                       operator_admission_validator=None) -> None:
        h = self.host
        if not thread_id:
            await self.publish_queue_status(thread_id)
            return
        completion = self.thread_completion_tasks.get(thread_id)
        if completion is not None and not completion.done():
            with contextlib.suppress(Exception):
                await asyncio.shield(completion)
        if self.thread_is_active(thread_id):
            h._release_stale_active_turn(thread_id, "queue-drain")
            if self.thread_is_active(thread_id):
                await self.publish_queue_status(thread_id)
                return
        retained_attempt = None
        admission_claim_id = None
        original_order = [item.id for item in h._thread_queue(thread_id)]
        if operator_original is not None:
            candidate, retained_attempt, admission_claim_id = operator_original
            operator_admission_validator()
            selected = {}
            def remove_reviewed(items):
                from codex_web.services.queue_order_recovery import QueueOrderRecoveryService
                if QueueOrderRecoveryService.digest(items) != retained_attempt.operator_admission.queue_sha256:
                    raise HTTPException(status_code=409, detail='Reviewed queue changed before exact removal')
                matches = [item for item in items if item.id == candidate.id]
                if len(matches) != 1 or matches[0].model_dump(mode='json') != candidate.model_dump(mode='json'):
                    raise HTTPException(status_code=409, detail='Reviewed original queue copy changed or disappeared')
                original_order[:] = [item.id for item in items]
                selected['item'] = matches[0]
                return [item for item in items if item.id != candidate.id]
            self._update_thread_queue(thread_id, remove_reviewed)
            queued = selected['item']
        elif self.queued_preflight is not None:
            items = h._thread_queue(thread_id)
            if not items:
                await self.publish_queue_status(thread_id)
                return
            queued = None
            refused_count = 0
            refused_sample = []
            for candidate in TurnQueuePolicy.admission_candidates(items):
                if await asyncio.to_thread(self._queued_automation_wait_reason, candidate) is not None:
                    continue
                try:
                    candidate_project = h._project(candidate.project_id)
                except HTTPException:
                    # Without canonical tenant provenance, keep this request
                    # in its original queue rather than inventing a tenant.
                    continue
                try:
                    attempt = await asyncio.to_thread(
                        self.queued_preflight.retain_queued, candidate,
                        organization_id=candidate_project.organization_id,
                        workspace_id=candidate_project.workspace_id,
                    )
                except QueuedRetentionRefused:
                    # Do not modify, pop, claim or retry this original. Its
                    # unknown outcome cannot starve independent eligible work.
                    refused_count += 1
                    if len(refused_sample) < 10:
                        refused_sample.append(candidate.id)
                    continue
                if attempt.status == 'started' and attempt.queued_start_receipt is not None:
                    # A restored queue copy is redundant only when the original
                    # retained admission has its actual immutable start receipt.
                    removed = self.pop_queued_turn(thread_id, candidate.id)
                    if removed is not None:
                        h._append_bot_event({'type':'queued_original_started_receipt_reconciled',
                            'thread_id':thread_id, 'queued_id':candidate.id,
                            'turn_id':attempt.queued_start_receipt.provider_native_turn_id})
                    continue
                if attempt.status != 'queued':
                    continue
                if self.notification_batch_handler is not None:
                    handled = await self.notification_batch_handler.try_drain(
                        self, candidate, candidate_project)
                    if handled:
                        await self.publish_queue_status(thread_id)
                        return
                if await asyncio.to_thread(self._queued_automation_wait_reason, candidate) is not None:
                    continue
                # Diagnostics run before claiming/removing fresh work. Even
                # cancellation here must leave it durably queued and unclaimed.
                with contextlib.suppress(Exception):
                    await self._publish_queued_retention_refusals(thread_id, refused_count, refused_sample)
                attempt, claim_id = await asyncio.to_thread(
                    self.queued_preflight.claim_queued_admission, attempt.id)
                if claim_id is None:
                    continue
                if await asyncio.to_thread(self._queued_automation_wait_reason, candidate) is not None:
                    await asyncio.to_thread(self.queued_preflight.queued_disposition,
                        attempt.id, status="queued", claim_id=claim_id)
                    continue
                queued = self.pop_queued_turn(thread_id, candidate.id, order_snapshot=original_order)
                if queued is not None:
                    retained_attempt = attempt
                    admission_claim_id = claim_id
                    break
                await asyncio.to_thread(self.queued_preflight.queued_disposition,
                    attempt.id, status='queued', claim_id=claim_id)
            if queued is None:
                with contextlib.suppress(Exception):
                    await self._publish_queued_retention_refusals(thread_id, refused_count, refused_sample)
                await self.publish_queue_status(thread_id)
                return
        else:
            candidate = next((item for item in TurnQueuePolicy.admission_candidates(h._thread_queue(thread_id))
                              if self._queued_automation_wait_reason(item) is None), None)
            queued = self.pop_queued_turn(thread_id, candidate.id, order_snapshot=original_order) if candidate else None
        if not queued:
            await self.publish_queue_status(thread_id)
            return
        if operator_admission_validator is None:
            operator_admission_validator = self._project_dispatch_validator(queued)
        original_queued = queued.model_copy(deep=True)
        def restore_original_position(original=original_queued):
            def restore(items):
                if any(item.id == original.id for item in items):
                    return items
                position = original_order.index(original.id)
                preceding = set(original_order[:position])
                following = set(original_order[position + 1:])
                # Anchor to surviving original peers. A concurrent explicit
                # retry inserted ahead of them must keep its priority; ordinary
                # arrivals behind them must not overtake the deferred original.
                predecessor = max((i for i, item in enumerate(items)
                    if item.id in preceding), default=-1)
                successor = next((i for i, item in enumerate(items)
                    if item.id in following), None)
                index = predecessor + 1 if predecessor >= 0 else successor
                if index is None:
                    # With no surviving peer, only the existing durable retry
                    # intent can establish explicit priority. New ordinary
                    # arrivals retain FIFO behind the older deferred request.
                    priority_positions = [i for i, item in enumerate(items)
                        if self.queued_preflight is not None
                        and self.queued_preflight.has_explicit_queue_priority(item)]
                    index = max(priority_positions, default=-1) + 1
                items.insert(index, original)
                return items
            self._update_thread_queue(thread_id, restore)

        queued.attempts += 1
        reschedule_queue = operator_original is None
        try:
            await asyncio.to_thread(self._require_automated_admission, queued.source, operator_admission_validator)
            if retained_attempt is not None:
                # This durable gate commits before any native start. A crash
                # afterwards is explicitly unknown, never an automatic replay.
                permitted = await asyncio.to_thread(self.queued_preflight.begin_queued_start,
                    retained_attempt.id, claim_id=admission_claim_id)
                if not permitted:
                    current = await asyncio.to_thread(self.queued_preflight.store.get,
                        retained_attempt.id)
                    if current is not None and current.status == 'queued':
                        original = QueuedTurn.model_validate(current.queued_turn_snapshot)
                        target = current.replacement_thread_id or current.thread_id
                        if original.reply_target and original.reply_target.thread_id == original.thread_id:
                            original.reply_target = original.reply_target.model_copy(update={'thread_id':target})
                        original.thread_id = target
                        if target == thread_id:
                            restore_original_position(original)
                        else:
                            self.requeue_turn_front(original)
                        if target != thread_id:
                            self.schedule_queue_drain(target)
                    # The recovery domain owns the original request. A fenced
                    # caller cannot restore mutated retry counters or an old
                    # Thread destination over a newer recovery/admission claim.
                    h._append_bot_event({'type':'queued_turn_admission_fenced',
                        'thread_id':thread_id, 'queued_id':queued.id,
                        'preflight_attempt_id':retained_attempt.id})
                    return
            project = h._project(queued.project_id)
            # Queue drains bypass ``TurnService.start``, which normally makes
            # the requested repository/runtime settings durable before an
            # isolated turn starts.  Persist the same effective settings here
            # so stale-thread recovery creates its replacement bootstrap for
            # the queued turn's repository.  Without this, a fresh provider
            # thread can be created in the previous repository, immediately
            # rebound to a second app-server for target drift, and then fail
            # its first ``turn/start`` because that second process has never
            # seen the provider-native thread id.
            remember_settings = getattr(
                h,
                "_remember_thread_run_settings",
                None,
            )
            if callable(remember_settings):
                current_settings = h._thread_run_settings(thread_id)
                remember_settings(
                    thread_id,
                    sandbox=(
                        queued.sandbox
                        or current_settings.sandbox
                        or project.sandbox
                    ),
                    approval_policy=(
                        queued.approval_policy
                        or current_settings.approval_policy
                        or project.approval_policy
                    ),
                    model=(queued.model or current_settings.model or project.model),
                    reasoning_effort=(
                        queued.reasoning_effort
                        or current_settings.reasoning_effort
                    ),
                    repository_resource_id=(
                        queued.repository_resource_id
                        if queued.canonical_ingress_id else
                        queued.repository_resource_id or current_settings.repository_resource_id
                    ),
                    writable_repository_resource_ids=(
                        queued.writable_repository_resource_ids
                        if queued.canonical_ingress_id else
                        queued.writable_repository_resource_ids or current_settings.writable_repository_resource_ids
                    ),
                    read_only_repository_resource_ids=queued.read_only_repository_resource_ids,
                    **({"resource_context":queued.resource_context} if queued.resource_context is not None else {}),
                    execution_profile_id=(
                        queued.execution_profile_id
                        or current_settings.execution_profile_id
                    ),
                )
            queued_actor = None
            if queued.agent_profile_id or queued.canonical_ingress_id:
                if (
                    not queued.agent_profile_actor_id
                    or self.actor_resolver is None
                ):
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "agent_profile_actor_unavailable",
                            "threadId": thread_id,
                            "agentProfileId": queued.agent_profile_id,
                        },
                    )
                queued_actor = self.actor_resolver(
                    queued.agent_profile_actor_id,
                    project,
                )
            start_response = await self.start_thread_turn_now(
                thread_id,
                project=project,
                message=queued.message,
                sandbox=queued.sandbox or project.sandbox,
                approval_policy=queued.approval_policy or project.approval_policy,
                model=queued.model,
                reasoning_effort=queued.reasoning_effort,
                source=f"queued:{queued.source}",
                reply_target=queued.reply_target,
                execution_id=(queued.execution_id or retained_attempt.execution_id
                    if retained_attempt is not None else queued.execution_id),
                work_item_ref=queued.work_item_ref,
                handoff_requested_at=queued.handoff_requested_at,
                **({'owner_routing_pin': queued.owner_routing_pin} if queued.owner_routing_pin is not None else {}),
                repository_resource_id=queued.repository_resource_id,
                writable_repository_resource_ids=queued.writable_repository_resource_ids,
                read_only_repository_resource_ids=queued.read_only_repository_resource_ids,
                **({"resource_context":queued.resource_context} if queued.resource_context is not None else {}),
                execution_profile_id=queued.execution_profile_id,
                **({"ingress_execution_profile_definition": queued.ingress_execution_profile_definition}
                   if queued.ingress_execution_profile_definition is not None else {}),
                actor=queued_actor,
                agent_profile_id=queued.agent_profile_id,
                agent_profile_revision=queued.agent_profile_revision,
                agent_profile_actor_id=queued.agent_profile_actor_id,
                **({"memory_context_binding": queued.memory_context_binding} if queued.memory_context_binding is not None else {}),
                **({'operator_admission_validator':operator_admission_validator}
                   if operator_admission_validator is not None else {}),
                **({'queued_admission_attempt_id':retained_attempt.id,
                    'queued_admission_claim_id':admission_claim_id} if retained_attempt is not None else {}),
            )
            returned_turn = start_response.get('turn', {}) if isinstance(start_response, dict) else {}
            if retained_attempt is not None:
                await asyncio.to_thread(self.queued_preflight.queued_disposition,
                    retained_attempt.id, status='started' if returned_turn.get('id') else 'reconciliation_required',
                    claim_id=admission_claim_id)
            h._append_bot_event(
                {
                    "type": ("queued_turn_started" if returned_turn.get('id')
                             else "queued_turn_start_outcome_unknown"),
                    "thread_id": thread_id,
                    "queued_id": queued.id,
                    "turn_id": returned_turn.get('id'),
                    "remaining": h._thread_queue_depth(thread_id),
                }
            )
        except asyncio.CancelledError:
            # A shutdown can cancel admission after the durable queue pop.
            # Restore the original item unless a durable active-turn marker
            # already owns its execution (including an unknown start result).
            reschedule_queue = False
            if not self.thread_is_active(thread_id):
                restore_original_position()
            raise
        except Exception as exc:
            detail = exc.detail if isinstance(exc, HTTPException) and isinstance(exc.detail, dict) else None
            deferred_admission = isinstance(exc, (LocalExecutionWorkerCapacityError, ProviderCapacityBlockedError, AutomatedAdmissionPaused)) or bool(
                detail and detail.get('code') in {
                    'upgrade_maintenance_active', 'upgrade_maintenance', 'upgrade_turn_scope_unknown'})
            if retained_attempt is not None and deferred_admission:
                await asyncio.to_thread(self.queued_preflight.queued_disposition,
                    retained_attempt.id, status='queued', detail=detail, claim_id=admission_claim_id,
                    preinput_refusal=isinstance(exc, (AutomatedAdmissionPaused, ExecutionPrestartRefusal)))
            if isinstance(exc, AutomatedAdmissionPaused):
                reschedule_queue = False
                if retained_attempt is not None:
                    current = await asyncio.to_thread(
                        self.queued_preflight.store.get, retained_attempt.id)
                    if (current.status != "queued"
                            or current.queued_admission_claim_id != admission_claim_id):
                        return
                restore_original_position()
                h._append_bot_event({"type": "queued_turn_waiting_for_autonomy",
                                     "thread_id": thread_id, "queued_id": queued.id})
                return
            if retained_attempt is not None and not deferred_admission:
                detail = exc.detail if isinstance(exc, HTTPException) and isinstance(exc.detail, dict) else None
                from codex_web.services.queued_operator_admission import OperatorAdmissionPrestartRefusal
                # Native HTTP detail codes are outcome data, never proof of nonstart.
                known_unstarted = bool(isinstance(exc,
                    (OperatorAdmissionPrestartRefusal, ExecutionPrestartRefusal))
                    and not self.thread_is_active(thread_id))
                disposition = await asyncio.to_thread(self.queued_preflight.queued_disposition,
                    retained_attempt.id,
                    status='blocked' if known_unstarted else 'reconciliation_required',
                    detail=detail or {'message':str(exc),'code':'native_start_outcome_unknown'},
                    claim_id=admission_claim_id, preinput_refusal=known_unstarted,
                )
                # A concurrent positive, possible turn, retirement or changed
                # claim wins over this local exception's earlier observation.
                known_unstarted = bool(known_unstarted and disposition.status == 'blocked'
                    and disposition.queued_admission_claim_id == admission_claim_id
                    and disposition.queued_start_correlation is None
                    and disposition.queued_start_evidence is None
                    and disposition.queued_start_receipt is None and disposition.started_at is None
                    and disposition.scheduling_retirement is None)
                if operator_original is not None and known_unstarted:
                    restore_original_position()
                # The complete request now belongs to the existing durable
                # preflight recovery domain, rather than a retry-exhaustion drop.
                h._append_bot_event({'type':'queued_turn_retained_for_recovery',
                    'thread_id':thread_id,'queued_id':queued.id,
                    'preflight_attempt_id':retained_attempt.id,
                    'known_unstarted':known_unstarted})
                await self.publish_queue_status(thread_id)
                return
            if (
                isinstance(exc, HTTPException)
                and exc.status_code == 409
                and isinstance(exc.detail, dict)
                and exc.detail.get("code") == "upgrade_maintenance_active"
            ):
                queued.attempts = original_queued.attempts
                restore_original_position()
                reschedule_queue = False
                h._append_bot_event(
                    {
                        "type": "queued_turn_waiting_for_upgrade_drain",
                        "thread_id": thread_id,
                        "queued_id": queued.id,
                    }
                )
                asyncio.get_running_loop().call_later(
                    30,
                    self.schedule_queue_drain,
                    thread_id,
                )
                return
            if (
                isinstance(exc, HTTPException)
                and isinstance(exc.detail, dict)
                and exc.detail.get("code")
                in {"upgrade_maintenance", "upgrade_turn_scope_unknown"}
            ):
                queued.attempts = original_queued.attempts
                restore_original_position()
                reschedule_queue = False
                h._append_bot_event({
                    "type": "queued_turn_waiting_for_upgrade_admission",
                    "thread_id": thread_id, "queued_id": queued.id,
                    "code": exc.detail["code"],
                })
                # Reuse the existing delayed queue admission mechanism. This
                # wait invokes no model and cannot immediately spin or charge
                # failure attempts while maintenance remains in force.
                if exc.detail["code"] == "upgrade_maintenance":
                    asyncio.get_running_loop().call_later(
                        30, self.schedule_queue_drain, thread_id,
                    )
                await self.publish_queue_status(thread_id)
                return
            if isinstance(exc, LocalExecutionWorkerCapacityError):
                queued.attempts = original_queued.attempts
                restore_original_position()
                reschedule_queue = False
                h._append_bot_event({
                    "type": "queued_turn_waiting_for_worker_capacity",
                    "thread_id": thread_id,
                    "queued_id": queued.id,
                    "error": str(exc),
                })
                asyncio.get_running_loop().call_later(30, self.schedule_queue_drain, thread_id)
                return
            if isinstance(exc, ProviderCapacityBlockedError):
                queued.attempts = original_queued.attempts
                restore_original_position()
                wait = self.wait_for_thread_capacity(
                    thread_id=thread_id,
                    execution_id=queued.execution_id,
                    provider_keys=(exc.record.key,),
                    retry_at=exc.retry_at,
                    reason=str(exc),
                )
                reschedule_queue = False
                h._append_bot_event(
                    {
                        "type": "queued_turn_waiting_for_capacity",
                        "thread_id": thread_id,
                        "queued_id": queued.id,
                        "capacity_status": exc.record.status.value,
                        "provider_key": exc.record.key,
                        "retry_at": exc.retry_at,
                        "capacity_wait_id": getattr(wait, "id", None),
                        "error": h._truncate_text(str(exc), 500),
                    }
                )
                await self.publish_queue_status(thread_id)
                return
            if h._is_stale_thread_error(exc):
                bindings = h._bindings_for_thread(thread_id)
                if bindings:
                    replacement = await h._replace_stale_bot_thread(bindings[0], str(exc))
                    replacement_id = replacement.thread_id
                elif queued.source == "web":
                    replacement_id = await h._replace_stale_web_thread(thread_id, project, str(exc))
                else:
                    replacement_id = None
                if replacement_id:
                    queued.thread_id = replacement_id
                    if queued.reply_target and queued.reply_target.thread_id == thread_id:
                        queued.reply_target = queued.reply_target.model_copy(
                            update={"thread_id": replacement_id}
                        )
                    queued.attempts = 0
                    self.requeue_turn_front(queued)
                    h._append_bot_event(
                        {
                            "type": "queued_turn_retargeted",
                            "old_thread_id": thread_id,
                            "new_thread_id": replacement_id,
                            "queued_id": queued.id,
                            "error": h._truncate_text(str(exc), 500),
                        }
                    )
                    asyncio.get_running_loop().call_soon(
                        self.schedule_queue_drain,
                        replacement_id,
                    )
                    return
            if h._is_codex_timeout_error(exc):
                bindings = h._bindings_for_thread(thread_id)
                if bindings and queued.attempts >= 2:
                    replacement = await h._replace_stale_bot_thread(
                        bindings[0],
                        (
                            "repeated runtime resume timeout: "
                            f"{str(getattr(exc, 'detail', exc))[:500]}"
                        ),
                    )
                    queued.thread_id = replacement.thread_id
                    if (
                        queued.reply_target
                        and queued.reply_target.thread_id == thread_id
                    ):
                        queued.reply_target = queued.reply_target.model_copy(
                            update={"thread_id": replacement.thread_id}
                        )
                    queued.attempts = 0
                    self.requeue_turn_front(queued)
                    reschedule_queue = False
                    h._append_bot_event(
                        {
                            "type": "queued_turn_retargeted_after_resume_timeout",
                            "old_thread_id": thread_id,
                            "new_thread_id": replacement.thread_id,
                            "queued_id": queued.id,
                            "error": h._truncate_text(
                                str(getattr(exc, "detail", exc)),
                                500,
                            ),
                        }
                    )
                    await self.publish_queue_status(thread_id)
                    asyncio.get_running_loop().call_soon(
                        self.schedule_queue_drain,
                        replacement.thread_id,
                    )
                    return
                if not bindings:
                    queued.attempts = max(0, queued.attempts - 1)
                self.requeue_turn_front(queued)
                delay = h._thread_resume_retry_delay()
                reschedule_queue = False
                h._append_bot_event(
                    {
                        "type": "queued_turn_resume_timeout",
                        "thread_id": thread_id,
                        "queued_id": queued.id,
                        "retry_delay_seconds": delay,
                        "error": h._truncate_text(str(getattr(exc, "detail", exc)), 500),
                    }
                )
                await self.publish_queue_status(thread_id)
                asyncio.get_running_loop().call_later(
                    delay,
                    self.schedule_queue_drain,
                    thread_id,
                )
                return
            if queued.attempts < 3:
                self.requeue_turn_front(queued)
            h._append_bot_event(
                {
                    "type": "queued_turn_failed",
                    "thread_id": thread_id,
                    "queued_id": queued.id,
                    "attempts": queued.attempts,
                    "error": str(exc),
                }
            )
            await h.hub.publish(
                {
                    "type": "queue.error",
                    "threadId": thread_id,
                    "queueDepth": h._thread_queue_depth(thread_id),
                    "error": str(exc),
                }
            )
        finally:
            await self.publish_queue_status(thread_id)
            if (
                reschedule_queue
                and h._thread_queue_depth(thread_id)
                and not self.thread_is_active(thread_id)
            ):
                asyncio.get_running_loop().call_soon(self.schedule_queue_drain, thread_id)

    def schedule_queue_drain(self, thread_id: str | None) -> None:
        if not thread_id:
            return
        task = self.queue_drain_tasks.get(thread_id)
        if task and not task.done():
            return

        async def run() -> None:
            try:
                await self.drain_thread_queue(thread_id)
            finally:
                current = asyncio.current_task()
                if self.queue_drain_tasks.get(thread_id) is current:
                    self.queue_drain_tasks.pop(thread_id, None)

        self.queue_drain_tasks[thread_id] = asyncio.create_task(
            run(),
            name=f"turn-queue-drain-{thread_id}",
        )

    async def resume_active_threads_after_startup(
        self,
        thread_ids: set[str] | None = None,
    ) -> None:
        self._event_loop = asyncio.get_running_loop()
        h = self.host
        if thread_ids is None:
            active_turns = h._load_active_turns()
        else:
            loader = getattr(h, "_get_active_turn_record", None)
            active_turns = {}
            for thread_id in sorted(thread_ids):
                active = (
                    loader(thread_id)
                    if callable(loader)
                    else h._load_active_turns().get(thread_id)
                )
                if active is not None:
                    active_turns[thread_id] = active

        if not active_turns:
            if thread_ids is None:
                for thread_id in h._load_turn_queues():
                    self.schedule_queue_drain(thread_id)
            return

        for thread_id, active in list(active_turns.items()):
            if active.resume_attempts >= 3:
                continue
            project = None
            if active.project_id:
                with contextlib.suppress(Exception):
                    project = h._project(active.project_id)
            if project is None:
                with contextlib.suppress(Exception):
                    thread_response = (
                        await CodexAgentRuntimeAdapter(
                            _ThreadRuntimeTransport(self, thread_id)
                        ).read_session(thread_id)
                    ).payload
                    thread = (
                        thread_response.get("thread", thread_response)
                        if isinstance(thread_response, dict)
                        else {}
                    )
                    project = h._project_for_cwd(thread.get("cwd"))
            if project is None:
                h._append_bot_event(
                    {
                        "type": "active_thread_resume_blocked",
                        "thread_id": thread_id,
                        "reason_code": "project_unresolved",
                    }
                )
                continue

            settings = h._thread_run_settings(thread_id)
            sandbox = (
                active.sandbox
                or settings.sandbox
                or project.sandbox
            )
            approval_policy = (
                active.approval_policy
                or settings.approval_policy
                or project.approval_policy
            )
            model = active.model or settings.model or project.model
            reasoning_effort = (
                active.reasoning_effort
                or settings.reasoning_effort
            )
            active.resume_attempts += 1
            active.last_resume_at = time.time()
            active.updated_at = time.time()
            self._save_active_turn(active)
            # This record describes the interrupted pre-restart turn.  The
            # normal start path rejects any thread that already has an active
            # record, so release it immediately before handing off to that
            # path.  A successful start installs a new canonical record.
            self._delete_active_turn(thread_id)
            try:
                response = await self.start_thread_turn_now(
                    thread_id,
                    project=project,
                    message=(
                        "codex-web was restarted while this thread had an active turn. "
                        "Continue the interrupted work from the latest available context. "
                        "Do not restart from scratch; inspect the current workspace state, infer what was in progress, "
                        "resume the next concrete step, and report only meaningful progress."
                    ),
                    sandbox=sandbox,
                    approval_policy=approval_policy,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    source=(
                        f"restart-recovery:"
                        f"{active.source or 'unknown'}"
                    ),
                    reply_target=active.reply_target,
                    execution_id=active.execution_id,
                    repository_resource_id=(
                        active.repository_resource_id
                    ),
                    writable_repository_resource_ids=(
                        active.writable_repository_resource_ids
                    ),
                    resource_context=active.resource_context,
                    resource_context_supplied=True,
                    execution_profile_id=active.execution_profile_id,
                    **({"memory_context_binding": active.memory_context_binding} if active.memory_context_binding is not None else {}),
                    actor=(
                        self.actor_resolver(
                            active.agent_profile_actor_id,
                            project,
                        )
                        if (
                            active.agent_profile is not None
                            and active.agent_profile_actor_id
                            and self.actor_resolver is not None
                        )
                        else None
                    ),
                    agent_profile_id=(
                        active.agent_profile.profile_id
                        if active.agent_profile is not None
                        else None
                    ),
                    agent_profile_revision=(
                        active.agent_profile.profile_revision
                        if active.agent_profile is not None
                        else None
                    ),
                    agent_profile_actor_id=active.agent_profile_actor_id,
                )
                self.mark_thread_active(
                    thread_id,
                    turn_id=(
                        (response.get("turn") or {}).get("id")
                        if isinstance(response, dict)
                        else active.turn_id
                    ),
                    project_id=project.id,
                    sandbox=sandbox,
                    approval_policy=approval_policy,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    source=(
                        f"restart-recovery:"
                        f"{active.source or 'unknown'}"
                    ),
                    reply_target=active.reply_target,
                    resource_context=active.resource_context,
                    execution_profile_id=active.execution_profile_id,
                )
                h._append_bot_event(
                    {
                        "type": "active_thread_resumed",
                        "thread_id": thread_id,
                        "project_id": project.id,
                    }
                )
            except Exception as exc:
                # Preserve the updated retry count when startup failed before
                # a replacement turn became active.  Do not overwrite a new
                # record created by a partially successful start.
                if not self.thread_is_active(thread_id):
                    self._save_active_turn(active)
                h._append_bot_event(
                    {
                        "type": "active_thread_resume_failed",
                        "thread_id": thread_id,
                        "error": str(exc),
                    }
                )

        if thread_ids is None:
            queue_ids = h._load_turn_queues()
        else:
            queue_ids = (
                thread_id
                for thread_id in thread_ids
                if h._thread_queue_depth(thread_id)
            )
        for thread_id in queue_ids:
            if not self.thread_is_active(thread_id):
                self.schedule_queue_drain(thread_id)

    def _assignment_record(self, assignment_id: str):
        for manager in dict(self.session_managers or {}).values():
            local_worker = getattr(manager, "local_worker", None)
            if local_worker is None:
                continue
            try:
                return local_worker._pending_assignment(assignment_id)
            except Exception:
                continue
        return None

    async def _supersede_thread_bootstrap(
        self,
        *,
        thread_id: str,
        project: Any,
        runtime_binding: ExecutionRuntimeBinding,
        sandbox: str,
        approval_policy: str,
        execution_profile_id: str | None,
        agent_profile: Any | None,
        explicit_repository_id: str | None,
        writable_repository_ids: tuple[str, ...] = (),
        read_only_repository_ids: tuple[str, ...] = (),
        resource_context=None,
        previous_assignment_id: str | None,
        retirement_reason: str | None = None,
        renewal_limits=None,
        recovery_admission=None,
    ):
        """Replace a thread's bootstrap through canonical retirement/admission.

        The canonical thread identity, settings and work-item context persist;
        the superseded assignment/session is completed (releasing its lease
        and workspace) and the thread rebinds to a fresh assignment on the
        selected runtime. Runtime switching uses a fresh provider-native session;
        exhaustion renewal retains the same native identity for ordinary resume.
        """

        from codex_web.services.execution_workers import AssignmentReadinessOriginError
        def retirement_refusal(exc):
            detail = exc.detail()
            return ExecutionPrestartRefusal(status_code=409, detail={**detail,
                'phase': 'before_bootstrap_retirement', 'thread_id': thread_id,
                'blockers': [{**detail, 'target_type': 'execution_assignment',
                    'target_id': previous_assignment_id,
                    'remediation_route': '/api/action-intents'}]})

        h = self.host
        if previous_assignment_id:
            previous = await asyncio.to_thread(self._assignment_record, previous_assignment_id)
            previous_runtime = getattr(previous, "runtime_binding", None)
            previous_scope = getattr(previous, "repository_scope", None)
            # A terminal failed bootstrap may never have resumed the native
            # session. Its requested scope is not the session's custody; leave
            # that restoration to the existing transactional pre-resume hook.
            if (getattr(previous, "status", None) in {
                AssignmentStatus.PENDING, AssignmentStatus.CLAIMED,
                AssignmentStatus.RUNNING, AssignmentStatus.LOST,
            } and previous_scope is not None and previous_runtime is not None and (
                previous_runtime.provider_id, previous_runtime.runtime_id
            ) == (runtime_binding.provider_id, runtime_binding.runtime_id)):
                requested_writable = (tuple(writable_repository_ids)
                    or ((explicit_repository_id,) if explicit_repository_id else
                        tuple(previous_scope.writable_repository_ids)))
                # The same native identity cannot acquire another repository
                # scope. Detect known ID drift before retiring its valid owner;
                # the transactional custody hook still checks the full scope
                # and current authority before any later provider resume.
                if (set(requested_writable) != set(previous_scope.writable_repository_ids)
                        or set(read_only_repository_ids) != set(previous_scope.read_only_repository_ids)):
                    raise ExecutionPrestartRefusal(status_code=409, detail={
                        "code": "native_session_scope_incompatible",
                        "phase": "before_bootstrap_retirement",
                        "message": "Native repository custody requires a separate compatible thread; the original assignment was preserved.",
                        "thread_id": thread_id,
                        "previous_assignment_id": previous_assignment_id,
                        "original_writable_repository_ids": list(previous_scope.writable_repository_ids),
                        "requested_writable_repository_ids": list(requested_writable),
                        "original_read_only_repository_ids": list(previous_scope.read_only_repository_ids),
                        "requested_read_only_repository_ids": list(read_only_repository_ids),
                        "retryable": False,
                    })
        recovery_fence = None
        if recovery_admission is not None:
            recovery_fence = await asyncio.to_thread(recovery_admission.begin_rollover, previous_assignment_id)
        if previous_assignment_id:
            released = False
            for manager in dict(self.session_managers or {}).values():
                if manager.get(previous_assignment_id) is not None:
                    if recovery_admission is not None:
                        # Its canonical LOST/FAILED record already released its
                        # lease. Settle/unregister only the positively stopped
                        # local handle; never rewrite loss into cancellation.
                        await manager.stop(previous_assignment_id)
                        released = True
                        break
                    try:
                        await manager.complete(
                            previous_assignment_id,
                            succeeded=False,
                            failure_code=(retirement_reason or "thread_runtime_switched"),
                            failure_message=(
                                "thread procedure admission history exhausted" if retirement_reason
                                else "thread superseded onto a different agent runtime"
                            ),
                        )
                    except AssignmentReadinessOriginError as exc:
                        raise retirement_refusal(exc) from exc
                    released = True
                    break
            if not released and recovery_admission is not None:
                recovery_admission()
                released = True  # Missing handle is safe only with retained positive teardown proof.
            if not released:
                local_worker = getattr(
                    next(iter(dict(self.session_managers or {}).values()), None),
                    "local_worker",
                    None,
                )
                if local_worker is not None:
                    def release_superseded() -> None:
                        # Terminal assignment retention may prune the record
                        # while its durable thread bootstrap binding survives.
                        # Verify actual absence before skipping cleanup; scoped
                        # lookup failures and storage errors must still fail.
                        if local_worker.worker_service.store.assignment(previous_assignment_id) is None:
                            return
                        assignment = local_worker._pending_assignment(previous_assignment_id)
                        # No registered session means this process cannot attest
                        # ownership of a live claim. Only unclaimed/lost records
                        # may be cancelled here; an active foreign claim fails.
                        if assignment.status in (AssignmentStatus.CLAIMED, AssignmentStatus.RUNNING):
                            raise HTTPException(
                                status_code=409,
                                detail="superseded bootstrap has an unowned live worker claim",
                            )
                        if assignment.status in (
                            AssignmentStatus.PENDING, AssignmentStatus.LOST,
                            AssignmentStatus.CANCELLED,
                        ):
                            local_worker.worker_service.cancel_bootstrap(
                                assignment.id,
                                AssignmentCancelRequest(
                                    expected_fence=(
                                        assignment.fence - 1
                                        if assignment.status == AssignmentStatus.CANCELLED
                                        else assignment.fence
                                    ),
                                    reason="thread superseded onto a different agent runtime",
                                ),
                                actor=local_worker.control_actor,
                            )

                    try:
                        await asyncio.to_thread(release_superseded)
                    except AssignmentReadinessOriginError as exc:
                        raise retirement_refusal(exc) from exc
        h._append_bot_event(
            {
                "type": "thread_assignment_renewal_started" if retirement_reason else "thread_runtime_switched",
                "thread_id": thread_id,
                "project_id": project.id,
                "target_runtime": (
                    f"{runtime_binding.provider_id}/{runtime_binding.runtime_id}"
                ),
            }
        )
        # Existing-thread healing can bypass routing's ordinary quota probe.
        # Cleanup above can also outlive its fresh account evidence. Refresh
        # only expired operator evidence, immediately before canonical
        # authentication preflight; never substitute transport readiness or
        # an assumed authentication result for a real account response.
        transport = getattr(h, "codex", None)
        account_available = getattr(transport, "authenticated_account_available", None)
        if (
            runtime_binding.provider_id == "openai"
            and runtime_binding.runtime_id == "codex"
            and callable(account_available)
            and not account_available()
        ):
            authentication_mode = runtime_binding.authentication_mode
            if authentication_mode is None and previous_assignment_id:
                # Repository drift may omit mode on the new runtime binding.
                # Preserve the previous canonical mode instead of assuming
                # an operator session for configured credential-backed work.
                previous = await asyncio.to_thread(
                    self._assignment_record, previous_assignment_id
                )
                previous_runtime = getattr(previous, "runtime_binding", None)
                if (
                    getattr(previous_runtime, "provider_id", None) == "openai"
                    and getattr(previous_runtime, "runtime_id", None) == "codex"
                ):
                    authentication_mode = previous_runtime.authentication_mode
            if authentication_mode == "trusted_local_session":
                await transport.request("account/read", {"refreshToken": False})

        token = __import__("uuid").uuid4().hex

        previous_context_assignment=(self._assignment_record(previous_assignment_id)
            if previous_assignment_id else None)
        context_cas=(resource_context is not None or
            getattr(previous_context_assignment,'resource_context',None) is not None)
        if context_cas and previous_context_assignment is not None and (previous_context_assignment.status not in (AssignmentStatus.CANCELLED,AssignmentStatus.FAILED,AssignmentStatus.SUCCEEDED) or previous_context_assignment.lease is not None):
            raise ExecutionPrestartRefusal(status_code=409,detail={'code':'resource_context_rollover_requires_terminal_fenced_assignment'})
        previous_context_fence=previous_context_assignment.fence if previous_context_assignment is not None else None
        def prepare_and_rebind():
            binding = self.binding_service.prepare_bootstrap(
                bootstrap_id=f"bootstrap-{token}",
                execution_id=f"thread-bootstrap-{token}",
                project_id=project.id,
                sandbox=sandbox,
                approval_policy=approval_policy,
                runtime_binding=runtime_binding,
                explicit_repository_id=explicit_repository_id,
                writable_repository_ids=writable_repository_ids,
                read_only_repository_ids=read_only_repository_ids,
                **({"resource_context":resource_context} if resource_context is not None else {}),
                execution_profile_id=execution_profile_id,
                agent_profile=agent_profile,
                **({'limits': renewal_limits} if retirement_reason else {}),
            )
            try:
                self.bootstrap_bindings.rebind(
                    bootstrap_id=f"bootstrap-{token}",
                    thread_id=thread_id,
                    execution_id=binding.execution_id,
                    assignment_id=binding.assignment_id,
                    execution_workspace_id=binding.workspace_id,
                    actor=self.control_actor,
                    **({'expected_assignment_id':previous_assignment_id,
                        'expected_assignment_fence':previous_context_fence} if context_cas and previous_assignment_id else
                        ({'expected_assignment_id': previous_assignment_id,
                          'expected_assignment_fence': recovery_fence,
                          'allow_lost_assignment': True} if recovery_admission is not None else
                         ({'expected_assignment_id': previous_assignment_id} if retirement_reason else {}))),
                )
            except Exception:
                self.binding_service.workers.cancel_bootstrap(
                    binding.assignment_id,
                    AssignmentCancelRequest(
                        expected_fence=0,
                        reason="bootstrap replacement could not be durably bound",
                    ),
                    actor=self.binding_service.control_actor,
                )
                raise
            rebound = self._bootstrap_binding_for_thread(thread_id)
            if recovery_admission is not None:
                recovery_admission.finish_rollover(rebound)
            return rebound

        try:
            if retirement_reason or context_cas or recovery_admission is not None:
                return await self._settle_custody_call(prepare_and_rebind)
            return await asyncio.to_thread(prepare_and_rebind)
        except TurnExecutionBindingError as exc:
            raise _execution_preflight_blocked(exc) from exc

    async def renew_exhausted_assignment(self, thread_id, *, project, actor, expected_assignment_id):
        try:
            return await self._renew_exhausted_assignment(thread_id, project=project,
                actor=actor, expected_assignment_id=expected_assignment_id)
        except HTTPException:
            raise
        except Exception as exc:
            message = 'The assignment could not be safely renewed. Inspect current ownership, worker diagnostics and recovery before retrying the retained original.'
            raise ExecutionPrestartRefusal(status_code=409, detail={
                'code': 'execution_preflight_blocked', 'message': message, 'retryable': True,
                'blockers': [{'code': 'procedure_assignment_renewal_blocked', 'message': message,
                    'retryable': True, 'target_type': 'execution_assignment',
                    'target_id': expected_assignment_id, 'remediation_route': '/api/execution-workers'}]}) from exc

    async def _renew_exhausted_assignment(self, thread_id, *, project, actor, expected_assignment_id):
        """One bounded, fenced idle renewal; the caller must retain input first.

        This sends no turn input and does not replace the native thread or Goal.
        A subsequent ordinary admission rechecks scope and resumes canonical
        session custody. Uncertain/busy execution and unpublished work stay held.
        """
        from pathlib import Path

        def blocked(message):
            return ExecutionPrestartRefusal(status_code=409, detail={
                'code': 'execution_preflight_blocked', 'message': message, 'retryable': True,
                'blockers': [{'code': 'procedure_assignment_renewal_blocked', 'message': message,
                    'retryable': True, 'target_type': 'execution_assignment',
                    'target_id': expected_assignment_id, 'remediation_route': '/api/execution-workers'}]})

        lock = self._turn_start_locks.get(thread_id)
        if lock is None:
            lock = asyncio.Lock()
            self._turn_start_locks[thread_id] = lock
        async with lock, asyncio.timeout(30):
            binding = self._bootstrap_binding_for_thread(thread_id)
            if binding is None:
                raise blocked('Assignment renewal requires the existing canonical thread bootstrap.')
            if binding.assignment_id != expected_assignment_id:
                current = self._assignment_record(binding.assignment_id)
                if (current is not None and current.created_by == actor.identity_id
                        and (current.organization_id, current.workspace_id, current.project_id)
                        == (actor.organization_id, actor.workspace_id, project.id)
                        and len(current.procedure_discoveries) < 64):
                    return binding
                raise blocked('The thread binding changed; inspect current assignment custody before retrying.')
            if self.thread_is_active(thread_id) or self.thread_handoff_in_progress(thread_id):
                raise blocked('Wait for the current native turn and steering handoff before renewing this assignment.')
            manager, session = self._session_for_assignment(expected_assignment_id)
            assignment = session.validate_current()
            if (assignment.id != binding.assignment_id or assignment.execution_id != binding.execution_id
                    or assignment.execution_workspace_id != binding.execution_workspace_id
                    or assignment.subject.kind != 'thread_bootstrap'
                    or assignment.created_by != actor.identity_id
                    or (assignment.organization_id, assignment.workspace_id, assignment.project_id)
                    != (actor.organization_id, actor.workspace_id, project.id)
                    or len(assignment.procedure_discoveries) < 64):
                raise blocked('The exhausted assignment no longer matches the authenticated thread owner and scope.')
            async def require_settled_origin():
                if self.readiness_origin_blocker is None:
                    raise blocked('Readiness origin inspection is unavailable; preserve this assignment.')
                if await asyncio.to_thread(self.readiness_origin_blocker, assignment.id):
                    raise blocked('An attempted READY action still requires same-origin read-only reconciliation; preserve its assignment.')
            await require_settled_origin()
            local_worker = getattr(manager, 'local_worker', None)
            custody = getattr(session, 'managed_validation', None)
            status = session.status()
            if (local_worker is None or not getattr(status, 'running', False)
                    or not getattr(status, 'ready', False) or getattr(status, 'pending_executor_work', None) != 0
                    or custody is None):
                raise blocked('The runner has pending work or unavailable custody; inspect it before renewal.')
            identity = local_worker.worker_service.identity
            current_actor = await self._settle_custody_call(identity.actor_for_identity,
                actor.identity_id, scope=actor.tenant, work_owner=session)
            if current_actor.identity_id != assignment.created_by:
                raise blocked('The assignment requester is no longer available for renewal.')
            if await self._settle_custody_call(custody.blocks_assignment_renewal, work_owner=session):
                raise blocked('Managed validation is running or its outcome is unknown; preserve its current assignment.')
            workspace = await self._settle_custody_call(local_worker.workspace_service.get,
                assignment.execution_workspace_id, current_actor, work_owner=session)
            members = workspace.repository_members
            if workspace.kind == 'scratch':
                # Scratch custody has no Git baseline to inspect. Its files
                # remain in the predecessor workspace after canonical fencing;
                # idle/validation/provider checks below still apply unchanged.
                target, scope = assignment.repository_target, assignment.repository_scope
                if (not workspace.path or members or workspace.resource_ids
                        or workspace.repository_resource_id is not None or workspace.resource_context is not None
                        or workspace.writable_repository_ids or workspace.read_only_resource_ids
                        or assignment.resource_ids or assignment.resource_context is not None
                        or target is None or target.source != 'orchestration_only'
                        or target.mutable_repository_id is not None or target.read_only_repository_ids
                        or scope is None or scope.source != 'orchestration_only'
                        or scope.writable_repository_ids or scope.read_only_repository_ids):
                    raise blocked('Scratch renewal requires canonical empty repository and resource scope.')
                Path(workspace.path).resolve(strict=True)
            elif workspace.kind == 'git_worktree':
                if not workspace.path or not members or len(members) > 64:
                    raise blocked('Automatic renewal requires an inspectable repository workspace; use canonical recovery for this workspace.')
                root = Path(workspace.path).resolve(strict=True)
                readonly_mounts, writable_mounts = await self._settle_custody_call(
                    local_worker.repository_mounts, assignment, work_owner=session)
                # Secondary members are canonical sibling worktrees, not children
                # of the primary checkout. Reuse the worker's scoped mount boundary.
                allowed_paths = {root, *(Path(source).resolve(strict=True)
                    for source, _ in (*readonly_mounts, *writable_mounts))}
                for member in members:
                    path = Path(member.workspace_path).resolve(strict=True)
                    if path not in allowed_paths:
                        raise blocked('A repository member is outside the canonical workspace.')
                    observed = await self._settle_custody_call(local_worker.inspect_git_metadata,
                        assignment, workspace_path=path, source_path=Path(member.source_path), work_owner=session)
                    if observed['dirty'] or observed['head'] != member.base_revision or observed['branch'] != (member.branch_name or ''):
                        raise blocked('Preserve and hand off the current repository changes before renewing this assignment.')
            else:
                raise blocked('Automatic renewal requires a canonical repository or scratch workspace.')
            runtime = assignment.runtime_binding
            native_id = getattr(getattr(session, 'runtime', None), 'native_session_id', None) or thread_id
            if runtime is None or (runtime.provider_id, runtime.runtime_id) != ('openai', 'codex') or native_id != thread_id:
                raise blocked('This runtime cannot attest same-native-thread idle renewal; use its canonical recovery controls.')
            reply = await session.request('thread/read', {'threadId': native_id, 'includeTurns': False})
            native = reply.get('thread') if isinstance(reply, dict) else None
            if (not isinstance(native, dict) or native.get('id') != native_id
                    or native.get('status') != {'type': 'idle'}):
                raise blocked('The provider has not confirmed that this exact native thread is idle.')
            current = session.validate_current()
            current_binding = self._bootstrap_binding_for_thread(thread_id)
            if (self.thread_is_active(thread_id) or self.thread_handoff_in_progress(thread_id)
                    or current_binding.assignment_id != assignment.id
                    or current.id != assignment.id or current.fence != assignment.fence
                    or current.lease != assignment.lease or current.execution_profile_definition != assignment.execution_profile_definition
                    or current.repository_scope != assignment.repository_scope
                    or current.resource_context != assignment.resource_context
                    or session.status().pending_executor_work != 0):
                raise blocked('Assignment ownership or activity changed during renewal inspection.')
            await require_settled_origin()
            scope = assignment.repository_scope
            renewed = await self._supersede_thread_bootstrap(thread_id=thread_id, project=project,
                runtime_binding=runtime, sandbox=assignment.sandbox, approval_policy=assignment.approval_policy,
                execution_profile_id=assignment.execution_profile_id, agent_profile=assignment.agent_profile,
                explicit_repository_id=assignment.repository_target.mutable_repository_id if assignment.repository_target else None,
                writable_repository_ids=tuple(scope.writable_repository_ids) if scope else (),
                read_only_repository_ids=tuple(scope.read_only_repository_ids) if scope else (),
                **({"resource_context":assignment.resource_context} if assignment.resource_context is not None else {}),
                previous_assignment_id=assignment.id, retirement_reason='procedure_assignment_exhausted',
                renewal_limits=assignment.limits)
            self.host._append_bot_event({'type': 'thread_assignment_renewed', 'thread_id': thread_id,
                'project_id': project.id, 'previous_assignment_id': assignment.id, 'assignment_id': renewed.assignment_id})
            return renewed

    async def _convert_legacy_thread_to_bootstrap(
        self,
        *,
        thread_id: str,
        project: Any,
        desired_runtime: tuple[str, str] | None,
        sandbox: str,
        approval_policy: str,
        execution_profile_id: str | None,
        explicit_repository_id: str | None,
        writable_repository_ids: tuple[str, ...] = (),
        read_only_repository_ids: tuple[str, ...] = (),
        resource_context=None,
        trusted_local_codex_requested: bool,
        actor: Any | None,
        agent_profile_id: str | None,
        agent_profile_revision: int | None,
    ):
        """Convert a legacy/non-bootstrap thread to the bootstrap mechanism.

        Legacy threads keep their canonical identity but gain a bootstrap
        binding so model/runtime switching works without creating new
        threads. The runtime comes from the model affinity when present,
        otherwise from deterministic routing.
        """

        if self.binding_service is None or self.bootstrap_bindings is None:
            return None
        if desired_runtime is not None:
            runtime_binding: ExecutionRuntimeBinding | None = (
                ExecutionRuntimeBinding(
                    provider_id=desired_runtime[0],
                    runtime_id=desired_runtime[1],
                    capability_revision=1,
                    sandbox_profiles=(
                        "read-only",
                        "workspace-write",
                        "danger-full-access",
                    ),
                )
            )
            agent_profile_binding = None
        else:
            runtime_binding, agent_profile_binding = (
                await self._select_runtime_binding(
                    project_id=project.id,
                    sandbox=sandbox,
                    trusted_local_codex_session=False,
                    actor=actor,
                    agent_profile_id=agent_profile_id,
                    agent_profile_revision=agent_profile_revision,
                )
            )
        if runtime_binding is None:
            return None
        h = self.host
        h._append_bot_event(
            {
                "type": "legacy_thread_converted_to_bootstrap",
                "thread_id": thread_id,
                "project_id": project.id,
                "runtime": (
                    f"{runtime_binding.provider_id}/{runtime_binding.runtime_id}"
                ),
            }
        )
        return await self._supersede_thread_bootstrap(
            thread_id=thread_id,
            project=project,
            runtime_binding=runtime_binding,
            sandbox=sandbox,
            approval_policy=approval_policy,
            execution_profile_id=execution_profile_id,
            agent_profile=agent_profile_binding,
            explicit_repository_id=explicit_repository_id,
            writable_repository_ids=writable_repository_ids,
            read_only_repository_ids=read_only_repository_ids,
            **({"resource_context":resource_context} if resource_context is not None else {}),
            previous_assignment_id=None,
        )

    @staticmethod
    def terminal_failure_window_seconds() -> float:
        try:
            value = float(os.environ.get("CODEX_WEB_TERMINAL_FAILURE_WINDOW_SECONDS") or "600")
        except ValueError:
            return 600.0
        return max(30.0, value)

    _unrecoverable_turn_error_markers = (
        "authentication",
        "unauthorized",
        "401",
        "403",
        "invalid api key",
        "incorrect api key",
        "model not found",
        "unsupported model",
        "unknown model",
        "no such model",
        "context length",
        "context window",
        "quota exceeded",
        "insufficient_quota",
        "billing",
        "permission denied",
    )

    @staticmethod
    def is_unrecoverable_turn_error(error: str) -> bool:
        text = str(error or "").casefold()
        return any(marker in text for marker in (
            TurnExecutionService._unrecoverable_turn_error_markers
        ))

    def schedule_terminal_thread_recovery(self, thread_id: str, error: str) -> bool:
        h = self.host
        if (
            not thread_id
            or thread_id in self.terminal_recovery_tasks
            or getattr(h, "IS_SHUTTING_DOWN", False)
        ):
            return False
        if not h._bindings_for_thread(thread_id):
            return False

        async def recover() -> None:
            try:
                bindings = h._bindings_for_thread(thread_id)
                if not bindings:
                    return
                last_input = self.last_inputs.get(thread_id)
                replacement = await h._replace_stale_bot_thread(bindings[0], error)
                h._append_bot_event(
                    {
                        "type": "terminal_thread_recovered",
                        "old_thread_id": thread_id,
                        "new_thread_id": replacement.thread_id,
                        "error": h._truncate_text(error, 500),
                    }
                )
                if last_input:
                    project = h._project(last_input["project_id"])
                    await self.start_thread_turn_now(
                        replacement.thread_id,
                        project=project,
                        message=last_input["message"],
                        **({"memory_context_binding": last_input["memory_context_binding"]} if last_input.get("memory_context_binding") is not None else {}),
                        sandbox=last_input.get("sandbox") or replacement.sandbox,
                        approval_policy=last_input.get("approval_policy") or replacement.approval_policy,
                        model=last_input.get("model"),
                        reasoning_effort=last_input.get("reasoning_effort"),
                        source=f"terminal-recovery:{last_input.get('source') or 'unknown'}",
                        reply_target=last_input.get("reply_target"),
                        execution_profile_id=last_input.get("execution_profile_id"),
                    )
                    self.last_inputs.pop(thread_id, None)
                else:
                    self.schedule_queue_drain(replacement.thread_id)
            except Exception as exc:
                h._append_bot_event(
                    {
                        "type": "terminal_thread_recovery_failed",
                        "thread_id": thread_id,
                        "error": h._truncate_text(str(exc), 500),
                    }
                )
            finally:
                self.terminal_recovery_tasks.pop(thread_id, None)

        self.terminal_recovery_tasks[thread_id] = asyncio.create_task(
            recover(),
            name=f"terminal-recovery-{thread_id}",
        )
        return True

    def record_terminal_turn_result(self, message: dict[str, Any]) -> bool:
        h = self.host
        method = message.get("method")
        if method not in {"turn/completed", "turn/failed"}:
            return False
        params = message.get("params") or {}
        turn = params.get("turn") or {}
        thread_id = params.get("threadId") or turn.get("threadId")
        status = str(turn.get("status") or "").lower()
        error = _turn_failure_text(message)
        if not thread_id:
            return False
        capture = getattr(h, "_capture_notification_terminal", None)
        if callable(capture):
            with contextlib.suppress(Exception):
                capture(message)
        if method != "turn/failed" and status != "failed" and not error:
            self.terminal_failures.pop(thread_id, None)
            self.last_inputs.pop(thread_id, None)
            return False
        if not error:
            error = "turn failed without an error message"
        now = time.time()
        failures = self.terminal_failures.setdefault(thread_id, deque())
        window = self.terminal_failure_window_seconds()
        while failures and now - failures[0][0] >= window:
            failures.popleft()
        failures.append((now, error))
        unrecoverable = h._is_unrecoverable_turn_error(error)
        h._append_bot_event(
            {
                "type": "terminal_turn_failed",
                "thread_id": thread_id,
                "turn_id": turn.get("id") or params.get("turnId"),
                "failure_count": len(failures),
                "unrecoverable": unrecoverable,
                "error": h._truncate_text(error, 500),
            }
        )
        threshold_reached = unrecoverable or len(failures) >= 2
        return threshold_reached and self.schedule_terminal_thread_recovery(thread_id, error)


def install_turn_execution_service(
    app: Any,
    host: Any,
    *,
    binding_service: TurnExecutionBindingService | None = None,
    session_manager: AssignmentBoundAgentSessionManager | None = None,
    bootstrap_bindings: ThreadBootstrapBindingService | None = None,
    control_actor: AuthenticationActor | None = None,
    routing_service: AgentRoutingService | None = None,
    session_managers: Mapping[tuple[str, str], AssignmentBoundAgentSessionManager] | None = None,
    runtime_adapter_factory: Callable[[ExecutionRuntimeBinding, Any], Any] | None = None,
    provider_capacity: ProviderCapacityService | None = None,
    ownership: ReplicatedOwnershipService | None = None,
    bindings_for_thread: Callable[[str], list[BotBinding]] | None = None,
    actor_resolver: Callable[[str, Project], AuthenticationActor] | None = None,
    skill_context_resolver: Callable[
        [tuple, Project, str], Any
    ] | None = None,
    work_item_context_resolver: Callable[[str], dict[str, Any]] | None = None,
    work_item_context_recorder: Callable[..., Any] | None = None,
    work_item_outcome_recorder: Callable[..., Any] | None = None,
    thread_history: ThreadHistoryRepository | None = None,
    transcript: Any | None = None,
    maintenance_admission=None,
) -> TurnExecutionService:
    existing = getattr(app.state, "turn_execution_service", None)
    if isinstance(existing, TurnExecutionService) and existing.host is host:
        service = existing
        service.binding_service = binding_service or service.binding_service
        service.session_manager = session_manager or service.session_manager
        service.bootstrap_bindings = (
            bootstrap_bindings or service.bootstrap_bindings
        )
        service.control_actor = control_actor or service.control_actor
        service.routing_service = routing_service or service.routing_service
        if session_managers:
            service.session_managers.update(session_managers)
        service.runtime_adapter_factory = (
            runtime_adapter_factory or service.runtime_adapter_factory
        )
        service.provider_capacity = provider_capacity or service.provider_capacity
        service.ownership = ownership or service.ownership
        if bindings_for_thread is not None:
            service.bindings_for_thread = bindings_for_thread
        if actor_resolver is not None:
            service.actor_resolver = actor_resolver
        if skill_context_resolver is not None:
            service.skill_context_resolver = skill_context_resolver
        if work_item_context_resolver is not None:
            service.work_item_context_resolver = work_item_context_resolver
        if work_item_context_recorder is not None:
            service.work_item_context_recorder = work_item_context_recorder
        if work_item_outcome_recorder is not None:
            service.work_item_outcome_recorder = work_item_outcome_recorder
        if thread_history is not None:
            service.thread_history = thread_history
        if transcript is not None:
            service.transcript = transcript
        if maintenance_admission is not None:
            service.maintenance_admission = maintenance_admission
    else:
        service = TurnExecutionService(
            host,
            binding_service=binding_service,
            session_manager=session_manager,
            bootstrap_bindings=bootstrap_bindings,
            control_actor=control_actor,
            routing_service=routing_service,
            session_managers=session_managers,
            runtime_adapter_factory=runtime_adapter_factory,
            provider_capacity=provider_capacity,
            ownership=ownership,
            bindings_for_thread=bindings_for_thread,
            actor_resolver=actor_resolver,
            skill_context_resolver=skill_context_resolver,
            work_item_context_resolver=work_item_context_resolver,
            work_item_context_recorder=work_item_context_recorder,
            work_item_outcome_recorder=work_item_outcome_recorder,
            thread_history=thread_history,
            transcript=transcript,
            maintenance_admission=maintenance_admission,
        )
        app.state.turn_execution_service = service

    host._enqueue_turn = service.enqueue_turn
    host._is_unrecoverable_turn_error = service.is_unrecoverable_turn_error
    host._find_duplicate_queued_turn = service.find_duplicate_queued_turn
    host._pop_next_queued_turn = service.pop_next_queued_turn
    host._pop_latest_queued_turn = service.pop_latest_queued_turn
    host._pop_queued_turn = service.pop_queued_turn
    host._requeue_turn_front = service.requeue_turn_front
    host._thread_is_active = service.thread_is_active
    host._begin_thread_handoff = service.begin_thread_handoff
    host._finish_thread_handoff = service.finish_thread_handoff
    host._thread_handoff_in_progress = service.thread_handoff_in_progress
    host._codex_request_for_thread = service.request_for_thread
    host._mark_thread_active = service.mark_thread_active
    host._clear_thread_active = service.clear_thread_active
    host._record_thread_activity = service.record_thread_activity
    host._record_codex_runtime_transcript_event = (
        service.record_codex_runtime_transcript_event
    )
    host._publish_queue_status = service.publish_queue_status
    host._start_thread_turn_now = service.start_thread_turn_now
    host._drain_thread_queue = service.drain_thread_queue
    host._schedule_queue_drain = service.schedule_queue_drain
    host._wait_for_thread_capacity = service.wait_for_thread_capacity
    host._resume_active_threads_after_startup = service.resume_active_threads_after_startup
    host._schedule_terminal_thread_recovery = service.schedule_terminal_thread_recovery
    host._record_terminal_turn_result = service.record_terminal_turn_result

    # Mirror the mutable registries only for legacy diagnostics/patching. Active
    # ownership lives on the service instance.
    host.CODEX_TURN_START_LOCK = service.turn_start_lock
    host.QUEUE_DRAIN_TASKS = service.queue_drain_tasks
    host.TERMINAL_RECOVERY_TASKS = service.terminal_recovery_tasks
    host.THREAD_TERMINAL_FAILURES = service.terminal_failures
    host.THREAD_LAST_INPUTS = service.last_inputs
    host.ASSIGNMENT_COMPLETION_TASKS = service.assignment_completion_tasks
    host.THREAD_ASSIGNMENT_COMPLETION_TASKS = service.thread_completion_tasks
    host.THREAD_HANDOFFS = service.thread_handoffs
    return service
