from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections import deque
from typing import Any

from fastapi import HTTPException

from codex_web.identity import AuthenticationActor
from codex_web.models import ActiveThreadTurn, BotReplyTarget, Project, QueuedTurn
from codex_web.services.codex_worker_session import AssignmentBoundCodexSessionManager
from codex_web.services.execution_workers import WorkerLeaseError
from codex_web.services.thread_bootstrap_bindings import (
    ThreadBootstrapBindingNotFoundError,
    ThreadBootstrapBindingService,
)
from codex_web.services.turn_execution_binding import TurnExecutionBindingService


class TurnExecutionService:
    """Own turn execution, queue draining, activity and terminal recovery state."""

    def __init__(
        self,
        host: Any,
        *,
        binding_service: TurnExecutionBindingService | None = None,
        session_manager: AssignmentBoundCodexSessionManager | None = None,
        bootstrap_bindings: ThreadBootstrapBindingService | None = None,
        control_actor: AuthenticationActor | None = None,
    ) -> None:
        self.host = host
        self.binding_service = binding_service
        self.session_manager = session_manager
        self.bootstrap_bindings = bootstrap_bindings
        self.control_actor = control_actor
        self.turn_start_lock = asyncio.Lock()
        self.queue_drain_tasks: dict[str, asyncio.Task[None]] = {}
        self.terminal_recovery_tasks: dict[str, asyncio.Task[None]] = {}
        self.assignment_completion_tasks: dict[str, asyncio.Task[None]] = {}
        self.thread_completion_tasks: dict[str, asyncio.Task[None]] = {}
        self.terminal_failures: dict[str, deque[tuple[float, str]]] = {}
        self.last_inputs: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _new_execution_id() -> str:
        return f"thread-turn-{__import__('uuid').uuid4().hex}"

    def _require_worker_routing(self) -> tuple[
        TurnExecutionBindingService,
        AssignmentBoundCodexSessionManager,
    ]:
        if self.binding_service is None or self.session_manager is None:
            raise HTTPException(
                status_code=503,
                detail="assignment-bound Codex turn execution is unavailable",
            )
        return self.binding_service, self.session_manager

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
    ) -> QueuedTurn:
        h = self.host
        queues = h._load_turn_queues()
        items = queues.setdefault(thread_id, [])
        for existing in items:
            if existing.source == source and existing.message == message:
                return existing
        incoming_entries = h._work_item_wakeup_entries(message) if reply_target is None else []
        if incoming_entries:
            wakeup_items = [
                existing
                for existing in items
                if existing.reply_target is None and h._work_item_wakeup_entries(existing.message)
            ]
            if wakeup_items:
                representative = wakeup_items[0]
                entries = [
                    entry
                    for existing in wakeup_items
                    for entry in h._work_item_wakeup_entries(existing.message)
                ]
                representative.message = h._render_work_item_wakeup_batch(entries + incoming_entries)
                wakeup_ids = {id(existing) for existing in wakeup_items[1:]}
                queues[thread_id] = [existing for existing in items if id(existing) not in wakeup_ids]
                h._save_turn_queues(queues)
                return representative
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
            id=h.uuid.uuid4().hex[:12] if hasattr(h, "uuid") else __import__("uuid").uuid4().hex[:12],
            thread_id=thread_id,
            project_id=project_id,
            message=message,
            execution_id=execution_id or self._new_execution_id(),
            sandbox=sandbox,
            approval_policy=approval_policy,
            model=model,
            reasoning_effort=reasoning_effort,
            source=source,
            reply_target=reply_target,
            created_at=time.time(),
        )
        items.append(queued)
        h._save_turn_queues(queues)
        return queued

    def find_duplicate_queued_turn(self, thread_id: str, *, message: str, source: str) -> QueuedTurn | None:
        for queued in self.host._thread_queue(thread_id):
            if queued.source == source and queued.message == message:
                return queued
        return None

    def pop_next_queued_turn(self, thread_id: str) -> QueuedTurn | None:
        h = self.host
        queues = h._load_turn_queues()
        items = queues.get(thread_id) or []
        if not items:
            return None
        queued = items.pop(0)
        if items:
            queues[thread_id] = items
        else:
            queues.pop(thread_id, None)
        h._save_turn_queues(queues)
        return queued

    def pop_latest_queued_turn(self, thread_id: str) -> QueuedTurn | None:
        h = self.host
        queues = h._load_turn_queues()
        items = queues.get(thread_id) or []
        if not items:
            return None
        queued = items.pop()
        if items:
            queues[thread_id] = items
        else:
            queues.pop(thread_id, None)
        h._save_turn_queues(queues)
        return queued

    def pop_queued_turn(self, thread_id: str, queued_id: str) -> QueuedTurn | None:
        h = self.host
        queues = h._load_turn_queues()
        items = queues.get(thread_id) or []
        for index, queued in enumerate(items):
            if queued.id != queued_id:
                continue
            items.pop(index)
            if items:
                queues[thread_id] = items
            else:
                queues.pop(thread_id, None)
            h._save_turn_queues(queues)
            return queued
        return None

    def requeue_turn_front(self, queued: QueuedTurn) -> None:
        h = self.host
        queues = h._load_turn_queues()
        queues.setdefault(queued.thread_id, []).insert(0, queued)
        h._save_turn_queues(queues)

    def thread_is_active(self, thread_id: str | None) -> bool:
        return bool(thread_id and thread_id in self.host._load_active_turns())

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
        active = self.host._load_active_turns().get(thread_id)
        assignment_id = active.assignment_id if active is not None else None
        bootstrap = None
        if not assignment_id:
            bootstrap = self._bootstrap_binding_for_thread(thread_id)
            assignment_id = bootstrap.assignment_id if bootstrap is not None else None
        if not assignment_id:
            return None
        manager = self.session_manager
        if manager is None:
            raise HTTPException(
                status_code=503,
                detail="assignment-bound Codex session manager is unavailable",
            )
        session = manager.get(assignment_id)
        if session is None:
            subject = (
                "thread bootstrap binding"
                if bootstrap is not None
                else "active thread assignment"
            )
            try:
                assignment = manager.reconcile_missing_session(assignment_id)
            except WorkerLeaseError as exc:
                raise HTTPException(
                    status_code=503,
                    detail=(
                        f"{subject} has no live Codex session while its "
                        "canonical worker lease remains valid"
                    ),
                ) from exc
            except Exception as exc:
                raise HTTPException(
                    status_code=503,
                    detail=(
                        f"{subject} has no live Codex session and canonical "
                        "assignment recovery failed"
                    ),
                ) from exc

            status = getattr(assignment.status, "value", str(assignment.status))
            if active is not None:
                self.clear_thread_active(thread_id)
            self.host._append_bot_event(
                {
                    "type": "codex_session_missing_reconciled",
                    "thread_id": thread_id,
                    "execution_id": assignment.execution_id,
                    "assignment_id": assignment.id,
                    "execution_workspace_id": assignment.execution_workspace_id,
                    "worker_id": assignment.assigned_worker_id,
                    "fence": assignment.fence,
                    "assignment_status": status,
                    "bootstrap": bootstrap is not None,
                }
            )
            raise HTTPException(
                status_code=503,
                detail=(
                    f"{subject} has no live Codex session; canonical "
                    f"assignment is {status}"
                ),
            )
        session.validate_current()
        return session

    async def request_for_thread(
        self,
        thread_id: str,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        session = self._assignment_session_for_thread(thread_id)
        if session is not None:
            return await session.request(method, params)
        return await self.host.codex.request(method, params)

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
    ) -> None:
        if not thread_id:
            return
        h = self.host
        now = time.time()
        active_turns = h._load_active_turns()
        current = active_turns.get(thread_id)
        settings = h._thread_run_settings(thread_id)
        active_turns[thread_id] = ActiveThreadTurn(
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
            started_at=current.started_at if current else now,
            updated_at=now,
            resume_attempts=current.resume_attempts if current else 0,
            last_resume_at=current.last_resume_at if current else None,
        )
        h._save_active_turns(active_turns)

    def clear_thread_active(self, thread_id: str | None, turn_id: str | None = None) -> None:
        if not thread_id:
            return
        h = self.host
        active_turns = h._load_active_turns()
        active = active_turns.get(thread_id)
        if active and turn_id and active.turn_id and active.turn_id != turn_id:
            return
        if active:
            active_turns.pop(thread_id, None)
            h._save_active_turns(active_turns)
            if not h.IS_SHUTTING_DOWN and h._autonomy_enabled():
                h._schedule_native_recovery_cycles(reason="thread-became-idle")

    def _schedule_assignment_completion(
        self,
        active: ActiveThreadTurn,
        *,
        succeeded: bool,
        message: dict[str, Any],
    ) -> None:
        manager = self.session_manager
        assignment_id = active.assignment_id
        if manager is None or not assignment_id:
            return
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
            if not h.IS_SHUTTING_DOWN:
                self.clear_thread_active(thread_id, turn_id=turn_id)
        elif method == "thread/status/changed":
            status_type = (params.get("status") or {}).get("type")
            if status_type == "active":
                self.mark_thread_active(thread_id)
            elif status_type in {"idle", "systemError", "notLoaded"} and not h.IS_SHUTTING_DOWN:
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
    ) -> dict[str, Any]:
        h = self.host
        binding_service, session_manager = self._require_worker_routing()
        settings = h._thread_run_settings(thread_id)
        effective_sandbox = sandbox or settings.sandbox or project.sandbox
        effective_approval_policy = (
            approval_policy or settings.approval_policy or project.approval_policy
        )
        effective_model = model or settings.model or project.model
        effective_reasoning_effort = reasoning_effort or settings.reasoning_effort
        effective_developer_instructions = h._effective_developer_instructions(
            thread_id,
            settings.developer_instructions,
        )
        requested_execution_id = execution_id or self._new_execution_id()

        async with self.turn_start_lock:
            bootstrap = self._bootstrap_binding_for_thread(thread_id)
            if bootstrap is not None:
                session = session_manager.get(bootstrap.assignment_id)
                if session is None:
                    raise HTTPException(
                        status_code=503,
                        detail="thread bootstrap binding has no live Codex session",
                    )
                assignment = session.validate_current()
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
                canonical_execution_id = bootstrap.execution_id
                assignment_id = bootstrap.assignment_id
                workspace_id = bootstrap.execution_workspace_id
            else:
                canonical_execution_id = requested_execution_id
                binding = binding_service.prepare(
                    thread_id=thread_id,
                    execution_id=canonical_execution_id,
                    project_id=project.id,
                    sandbox=effective_sandbox,
                    approval_policy=effective_approval_policy,
                )
                session = await session_manager.start(binding.assignment_id)
                assignment_id = binding.assignment_id
                workspace_id = binding.workspace_id

            status = session.status()
            workspace_path = session.workspace_path
            if workspace_path is None or status.fence is None:
                raise HTTPException(
                    status_code=503,
                    detail="assignment-bound Codex session lacks canonical workspace/fence",
                )
            workspace_cwd = str(workspace_path)
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
            try:
                await session.request("thread/resume", resume_params)
            except Exception as exc:
                with contextlib.suppress(Exception):
                    await session_manager.complete(
                        assignment_id,
                        succeeded=False,
                        failure_code="codex_thread_resume_failed",
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
                worker_id=status.worker_id,
                fence=status.fence,
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
            params["input"][0]["text"] = h._with_relay_guard(
                params["input"][0]["text"],
                h._turn_source_for_relay_guard(thread_id, source),
            )
            try:
                response = await session.request("turn/start", params)
            except Exception as exc:
                if not h._is_codex_timeout_error(exc):
                    self.clear_thread_active(thread_id)
                    with contextlib.suppress(Exception):
                        await session_manager.complete(
                            assignment_id,
                            succeeded=False,
                            failure_code="codex_turn_start_failed",
                            failure_message=str(exc)[:500],
                        )
                raise
            turn_id = (
                (response.get("turn") or {}).get("id")
                if isinstance(response, dict)
                else None
            )
            self.last_inputs[thread_id] = {
                "project_id": project.id,
                "message": message,
                "sandbox": effective_sandbox,
                "approval_policy": effective_approval_policy,
                "model": effective_model,
                "reasoning_effort": effective_reasoning_effort,
                "source": source,
                "reply_target": reply_target,
                "execution_id": canonical_execution_id,
                "assignment_id": assignment_id,
                "execution_workspace_id": workspace_id,
                "worker_id": status.worker_id,
                "fence": status.fence,
                "bootstrap_id": bootstrap.bootstrap_id if bootstrap is not None else None,
                "requested_execution_id": (
                    requested_execution_id
                    if bootstrap is not None
                    and requested_execution_id != canonical_execution_id
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
                worker_id=status.worker_id,
                fence=status.fence,
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
                "worker_id": status.worker_id,
                "fence": status.fence,
                "bootstrap_id": bootstrap.bootstrap_id if bootstrap is not None else None,
            }
        )
        await self.publish_queue_status(thread_id)
        return response

    async def drain_thread_queue(self, thread_id: str) -> None:
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

    async def resume_active_threads_after_startup(self, thread_ids: set[str] | None = None) -> None:
        h = self.host
        active_turns = h._load_active_turns()
        if not active_turns:
            for thread_id in h._load_turn_queues():
                self.schedule_queue_drain(thread_id)
            return
        for thread_id, active in list(active_turns.items()):
            if thread_ids is not None and thread_id not in thread_ids:
                continue
            if active.resume_attempts >= 3:
                continue
            project = None
            if active.project_id:
                with contextlib.suppress(Exception):
                    project = h._project(active.project_id)
            if project is None:
                with contextlib.suppress(Exception):
                    thread_response = await h.codex.request(
                        "thread/read",
                        {"threadId": thread_id, "includeTurns": False},
                    )
                    thread = (
                        thread_response.get("thread", thread_response)
                        if isinstance(thread_response, dict)
                        else {}
                    )
                    project = h._project_for_cwd(thread.get("cwd"))
            if project is None:
                self.clear_thread_active(thread_id)
                continue
            settings = h._thread_run_settings(thread_id)
            sandbox = active.sandbox or settings.sandbox or project.sandbox
            approval_policy = active.approval_policy or settings.approval_policy or project.approval_policy
            model = active.model or settings.model or project.model
            reasoning_effort = active.reasoning_effort or settings.reasoning_effort
            active.resume_attempts += 1
            active.last_resume_at = time.time()
            active.updated_at = time.time()
            active_turns[thread_id] = active
            h._save_active_turns(active_turns)
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
                    source=f"restart-recovery:{active.source or 'unknown'}",
                    reply_target=active.reply_target,
                    execution_id=active.execution_id,
                )
                self.mark_thread_active(
                    thread_id,
                    turn_id=(response.get("turn") or {}).get("id") if isinstance(response, dict) else active.turn_id,
                    project_id=project.id,
                    sandbox=sandbox,
                    approval_policy=approval_policy,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    source=f"restart-recovery:{active.source or 'unknown'}",
                    reply_target=active.reply_target,
                )
                h._append_bot_event(
                    {"type": "active_thread_resumed", "thread_id": thread_id, "project_id": project.id}
                )
            except Exception as exc:
                h._append_bot_event(
                    {"type": "active_thread_resume_failed", "thread_id": thread_id, "error": str(exc)}
                )
        for thread_id in h._load_turn_queues():
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
        if not thread_id or thread_id in self.terminal_recovery_tasks or h.IS_SHUTTING_DOWN:
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
    session_manager: AssignmentBoundCodexSessionManager | None = None,
    bootstrap_bindings: ThreadBootstrapBindingService | None = None,
    control_actor: AuthenticationActor | None = None,
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
    else:
        service = TurnExecutionService(
            host,
            binding_service=binding_service,
            session_manager=session_manager,
            bootstrap_bindings=bootstrap_bindings,
            control_actor=control_actor,
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
