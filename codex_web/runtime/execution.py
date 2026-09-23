from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from collections import deque
from typing import Any, Callable, Mapping

from fastapi import HTTPException

from codex_web.agent_profiles import AgentProfileExecutionBinding
from codex_web.agent_runtime import (
    AgentRuntimeEvent,
    AgentRuntimeSessionRequest,
    AgentRuntimeTurnRequest,
)
from codex_web.agent_routing import AgentRoutingRequest
from codex_web.execution_workers import ExecutionRuntimeBinding
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
from codex_web.services.replicated_ownership import ReplicatedOwnershipService
from codex_web.services.agent_worker_session import AssignmentBoundAgentSessionManager
from codex_web.services.thread_bootstrap_bindings import (
    ThreadBootstrapBindingNotFoundError,
    ThreadBootstrapBindingService,
)
from codex_web.services.turn_execution_binding import (
    TurnExecutionBindingError,
    TurnExecutionBindingService,
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


class TurnExecutionService:
    """Own turn execution, queue draining, activity and terminal recovery state."""

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
    ) -> None:
        self.host = host
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
        self.turn_start_lock = asyncio.Lock()
        self.queue_drain_tasks: dict[str, asyncio.Task[None]] = {}
        self.terminal_recovery_tasks: dict[str, asyncio.Task[None]] = {}
        self.assignment_completion_tasks: dict[str, asyncio.Task[None]] = {}
        self.thread_completion_tasks: dict[str, asyncio.Task[None]] = {}
        self.terminal_failures: dict[str, deque[tuple[float, str]]] = {}
        self.last_inputs: dict[str, dict[str, Any]] = {}

    def _work_item_writable_repository_ids(
        self,
        work_item_ref: str | None,
    ) -> tuple[str, ...]:
        if not work_item_ref:
            return ()
        loader = getattr(self.host, "_load_work_item_states", None)
        if not callable(loader):
            return ()
        try:
            state = loader().get(work_item_ref)
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
            raise HTTPException(
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
            raise HTTPException(
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

    async def publish_queue_status(self, thread_id: str) -> None:
        h = self.host
        await h.hub.publish(
            {
                "type": "queue.status",
                "threadId": thread_id,
                "queueDepth": h._thread_queue_depth(thread_id),
                "active": self.thread_is_active(thread_id),
            }
        )

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
        repository_resource_id: str | None = None,
        writable_repository_resource_ids: tuple[str, ...] = (),
        read_only_repository_resource_ids: tuple[str, ...] = (),
        execution_profile_id: str | None = None,
        agent_profile_id: str | None = None,
        agent_profile_revision: int | None = None,
        agent_profile_actor_id: str | None = None,
    ) -> QueuedTurn:
        h = self.host
        selected: dict[str, QueuedTurn] = {}

        def mutate(items: list[QueuedTurn]) -> list[QueuedTurn]:
            for existing in items:
                if (
                    existing.source == source
                    and existing.message == message
                ):
                    selected["item"] = existing
                    return items

            incoming_entries = (
                h._work_item_wakeup_entries(message)
                if reply_target is None
                else []
            )
            if incoming_entries:
                wakeup_items = [
                    existing
                    for existing in items
                    if existing.reply_target is None
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

            if len(items) >= h._max_thread_queue_depth():
                raise HTTPException(
                    status_code=429,
                    detail={
                        "code": "thread_queue_full",
                        "threadId": thread_id,
                        "queueDepth": len(items),
                        "maxQueueDepth": h._max_thread_queue_depth(),
                    },
                )

            queued = QueuedTurn(
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
                execution_profile_id=execution_profile_id,
                agent_profile_id=agent_profile_id,
                agent_profile_revision=agent_profile_revision,
                agent_profile_actor_id=agent_profile_actor_id,
                source=source,
                reply_target=reply_target,
                created_at=time.time(),
            )
            items.append(queued)
            selected["item"] = queued
            return items

        self._update_thread_queue(thread_id, mutate)
        return selected["item"]

    def find_duplicate_queued_turn(self, thread_id: str, *, message: str, source: str) -> QueuedTurn | None:
        for queued in self.host._thread_queue(thread_id):
            if queued.source == source and queued.message == message:
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
    ) -> QueuedTurn | None:
        selected: dict[str, QueuedTurn] = {}

        def mutate(items: list[QueuedTurn]) -> list[QueuedTurn]:
            for index, queued in enumerate(items):
                if queued.id != queued_id:
                    continue
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
        return bool(thread_id and self._active_turn(thread_id) is not None)

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
        except HTTPException as exc:
            codex_compatibility_only = not any(
                key != ("openai", "codex")
                for key in self.session_managers
            )
            if codex_compatibility_only:
                detail = (
                    "thread bootstrap binding has no live Codex session"
                    if bootstrap is not None
                    else "active thread assignment has no live Codex session"
                )
            else:
                detail = (
                    "thread bootstrap binding has no live agent runtime session"
                    if bootstrap is not None
                    else "active thread assignment has no live agent runtime session"
                )
            raise HTTPException(status_code=503, detail=detail) from exc
        assignment = session.validate_current()
        return manager, session, assignment

    async def request_for_thread(
        self,
        thread_id: str,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        resolved = self._assignment_session_for_thread(thread_id)
        if resolved is None:
            return await self.host.codex.request(method, params)
        _manager, session, assignment = resolved
        binding = getattr(assignment, "runtime_binding", None)
        if binding is None or (
            binding.provider_id == "openai" and binding.runtime_id == "codex"
        ):
            return await session.request(method, params)

        adapter = self._adapter_for_binding(binding, session)
        native_session_id = getattr(
            getattr(session, "runtime", None),
            "native_session_id",
            None,
        )
        if not native_session_id:
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
        execution_profile_id: str | None = None,
        agent_profile: AgentProfileExecutionBinding | None = None,
        agent_profile_actor_id: str | None = None,
    ) -> None:
        if not thread_id:
            return
        h = self.host
        now = time.time()
        current = self._active_turn(thread_id)
        settings = h._thread_run_settings(thread_id)
        active = ActiveThreadTurn(
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
            started_at=current.started_at if current else now,
            updated_at=now,
            resume_attempts=current.resume_attempts if current else 0,
            last_resume_at=current.last_resume_at if current else None,
        )
        self._save_active_turn(active)

    def clear_thread_active(self, thread_id: str | None, turn_id: str | None = None) -> None:
        if not thread_id:
            return
        h = self.host
        active = self._active_turn(thread_id)
        if active and turn_id and active.turn_id and active.turn_id != turn_id:
            return
        if active and self._delete_active_turn(thread_id):
            if not getattr(h, "IS_SHUTTING_DOWN", False) and h._autonomy_enabled():
                h._schedule_native_recovery_cycles(reason="thread-became-idle")

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
                    "succeeded": succeeded,
                    "session_retained": True,
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
                        "succeeded" if succeeded else "failed",
                    )
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
        self.record_thread_activity(message)
        hub = getattr(self.host, "hub", None)
        if hub is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(
            hub.publish({"type": "codex.event", "message": message})
        )

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

    def record_thread_activity(self, message: dict[str, Any]) -> None:
        h = self.host
        method = message.get("method")
        params = message.get("params") or {}
        thread_id = params.get("threadId") or (params.get("turn") or {}).get("threadId")
        turn_id = params.get("turnId") or (params.get("turn") or {}).get("id")
        if method in {"turn/started", "item/started"}:
            self.mark_thread_active(thread_id, turn_id=turn_id)
        elif method in {"turn/completed", "turn/failed"}:
            active = h._load_active_turns().get(thread_id) if thread_id else None
            if active is not None:
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
        repository_resource_id: str | None = None,
        writable_repository_resource_ids: tuple[str, ...] = (),
        read_only_repository_resource_ids: tuple[str, ...] = (),
        execution_profile_id: str | None = None,
        actor: AuthenticationActor | None = None,
        agent_profile_id: str | None = None,
        agent_profile_revision: int | None = None,
        agent_profile_actor_id: str | None = None,
    ) -> dict[str, Any]:
        h = self.host
        binding_service, default_session_manager = self._require_worker_routing()
        settings = h._thread_run_settings(thread_id)
        effective_sandbox = sandbox or settings.sandbox or project.sandbox
        effective_approval_policy = (
            approval_policy or settings.approval_policy or project.approval_policy
        )
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
        work_item_writable_repositories = self._work_item_writable_repository_ids(
            work_item_ref
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

        async with self.turn_start_lock:
            if self.thread_is_active(thread_id):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "thread_turn_already_active",
                        "threadId": thread_id,
                    },
                )
            bootstrap = self._bootstrap_binding_for_thread(thread_id)
            canonical_repository_resource_id: str | None = None
            agent_profile_binding = None
            if bootstrap is not None:
                session_manager, session = self._session_for_assignment(
                    bootstrap.assignment_id
                )
                assignment = session.validate_current()
                runtime_binding = getattr(assignment, "runtime_binding", None)
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
                        raise HTTPException(
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
                    if effective_writable_repositories != effective_writable:
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
                            read_only_repository_ids=(
                                read_only_repository_resource_ids
                                or settings.read_only_repository_resource_ids
                            ),
                            work_item_ref=work_item_ref,
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
                    assignment = binding
                    runtime_binding = getattr(binding, "runtime_binding", runtime_binding)
                    canonical_repository_resource_id = getattr(
                        binding,
                        "repository_resource_id",
                        repository_resource_id or settings.repository_resource_id,
                    )
                    assignment_id = binding.assignment_id
                    workspace_id = binding.workspace_id

            skill_context_selection = None
            if (
                agent_profile_binding is not None
                and agent_profile_binding.skill_refs
            ):
                if self.skill_context_resolver is None:
                    raise HTTPException(
                        status_code=503,
                        detail={
                            "code": "execution_preflight_blocked",
                            "message": (
                                "Agent Profile references Skills but Skill "
                                "context resolution is unavailable"
                            ),
                            "blockers": [
                                {
                                    "code": "skill_definition_unavailable",
                                    "message": (
                                        "Skill context resolution is unavailable"
                                    ),
                                    "retryable": False,
                                    "target_type": "agent_profile",
                                    "target_id": agent_profile_binding.profile_id,
                                    "remediation_route": "/api/skills",
                                }
                            ],
                            "retryable": False,
                        },
                    )
                try:
                    skill_context_selection = self.skill_context_resolver(
                        agent_profile_binding.skill_refs,
                        project,
                        message,
                    )
                except Exception as exc:
                    raise HTTPException(
                        status_code=503,
                        detail={
                            "code": "execution_preflight_blocked",
                            "message": str(exc),
                            "blockers": [
                                {
                                    "code": "skill_definition_unavailable",
                                    "message": str(exc),
                                    "retryable": False,
                                    "target_type": "agent_profile",
                                    "target_id": agent_profile_binding.profile_id,
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
            if effective_sandbox:
                resume_params["sandboxPolicy"] = h._sandbox_policy(
                    effective_sandbox,
                    workspace_cwd,
                )
            runtime_session_request = AgentRuntimeSessionRequest(
                project_id=project.id,
                sandbox=effective_sandbox,
                approval_policy=effective_approval_policy,
                approval_reviewer=resume_params.get("approvalsReviewer"),
                workspace_cwd=workspace_cwd,
                sandbox_policy=resume_params.get("sandboxPolicy"),
                execution_id=canonical_execution_id,
                assignment_id=assignment_id,
                execution_workspace_id=workspace_id,
                worker_id=worker_id,
                model=effective_model,
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
            try:
                await runtime_adapter.resume_session(
                    native_session_id,
                    runtime_session_request,
                )
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
                raise

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
                agent_profile_actor_id=(
                    actor.identity_id
                    if actor is not None
                    else agent_profile_actor_id
                ),
            )

            params: dict[str, Any] = {
                "threadId": thread_id,
                "input": [{"type": "text", "text": message, "text_elements": []}],
                "cwd": workspace_cwd,
            }
            if effective_model:
                params["model"] = effective_model
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
            params["input"][0]["text"] = self.with_relay_guard(
                params["input"][0]["text"],
                self.turn_source_for_relay_guard(thread_id, source),
            )
            runtime_turn_request = AgentRuntimeTurnRequest(
                message=params["input"][0]["text"],
                model=effective_model,
                reasoning_effort=effective_reasoning_effort,
                workspace_cwd=workspace_cwd,
                approval_policy=effective_approval_policy,
                sandbox_policy=params.get("sandboxPolicy"),
                developer_instructions=effective_developer_instructions,
            )
            try:
                runtime_turn_result = await runtime_adapter.start_turn(
                    native_session_id,
                    runtime_turn_request,
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
                        *(getattr(profile, "skill_refs", ()) or ()),
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
                capacity_error = await self._capacity_error(runtime_binding, exc)
                if capacity_error is not None:
                    self.clear_thread_active(thread_id)
                    raise capacity_error from exc
                if not h._is_codex_timeout_error(exc):
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
        await self.publish_queue_status(thread_id)
        return response

    async def drain_thread_queue(self, thread_id: str) -> None:
        if self.ownership is None:
            await self._drain_thread_queue_owned(thread_id)
            return

        async def operation() -> None:
            await self._drain_thread_queue_owned(thread_id)

        await self.ownership.run_exclusive(
            f"thread-queue:{thread_id}",
            operation,
        )

    async def _drain_thread_queue_owned(self, thread_id: str) -> None:
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
        queued = self.pop_next_queued_turn(thread_id)
        if not queued:
            await self.publish_queue_status(thread_id)
            return
        queued.attempts += 1
        reschedule_queue = True
        try:
            project = h._project(queued.project_id)
            queued_actor = None
            if queued.agent_profile_id:
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
            await self.start_thread_turn_now(
                thread_id,
                project=project,
                message=queued.message,
                sandbox=queued.sandbox or project.sandbox,
                approval_policy=queued.approval_policy or project.approval_policy,
                model=queued.model,
                reasoning_effort=queued.reasoning_effort,
                source=f"queued:{queued.source}",
                reply_target=queued.reply_target,
                execution_id=queued.execution_id,
                work_item_ref=queued.work_item_ref,
                repository_resource_id=queued.repository_resource_id,
                writable_repository_resource_ids=queued.writable_repository_resource_ids,
                read_only_repository_resource_ids=queued.read_only_repository_resource_ids,
                execution_profile_id=queued.execution_profile_id,
                actor=queued_actor,
                agent_profile_id=queued.agent_profile_id,
                agent_profile_revision=queued.agent_profile_revision,
                agent_profile_actor_id=queued.agent_profile_actor_id,
            )
            h._append_bot_event(
                {
                    "type": "queued_turn_started",
                    "thread_id": thread_id,
                    "queued_id": queued.id,
                    "remaining": h._thread_queue_depth(thread_id),
                }
            )
        except Exception as exc:
            if isinstance(exc, ProviderCapacityBlockedError):
                queued.attempts = max(0, queued.attempts - 1)
                self.requeue_turn_front(queued)
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
                    queued.thread_id = replacement.thread_id
                    if queued.reply_target and queued.reply_target.thread_id == thread_id:
                        queued.reply_target = queued.reply_target.model_copy(
                            update={"thread_id": replacement.thread_id}
                        )
                    queued.attempts = 0
                    self.requeue_turn_front(queued)
                    h._append_bot_event(
                        {
                            "type": "queued_turn_retargeted",
                            "old_thread_id": thread_id,
                            "new_thread_id": replacement.thread_id,
                            "queued_id": queued.id,
                            "error": h._truncate_text(str(exc), 500),
                        }
                    )
                    asyncio.get_running_loop().call_soon(
                        self.schedule_queue_drain,
                        replacement.thread_id,
                    )
                    return
            if h._is_codex_timeout_error(exc):
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
                    execution_profile_id=active.execution_profile_id,
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

    @staticmethod
    def terminal_failure_window_seconds() -> float:
        try:
            value = float(os.environ.get("CODEX_WEB_TERMINAL_FAILURE_WINDOW_SECONDS") or "600")
        except ValueError:
            return 600.0
        return max(30.0, value)

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
        error = h._turn_failure_text(message)
        if not thread_id:
            return False
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
        )
        app.state.turn_execution_service = service

    host._enqueue_turn = service.enqueue_turn
    host._find_duplicate_queued_turn = service.find_duplicate_queued_turn
    host._pop_next_queued_turn = service.pop_next_queued_turn
    host._pop_latest_queued_turn = service.pop_latest_queued_turn
    host._pop_queued_turn = service.pop_queued_turn
    host._requeue_turn_front = service.requeue_turn_front
    host._thread_is_active = service.thread_is_active
    host._codex_request_for_thread = service.request_for_thread
    host._mark_thread_active = service.mark_thread_active
    host._clear_thread_active = service.clear_thread_active
    host._record_thread_activity = service.record_thread_activity
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
    return service
