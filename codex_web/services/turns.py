from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Callable
from typing import Any, Protocol

from fastapi import HTTPException

from codex_web.models import BotBinding, QueuedTurn, TurnCreate
from codex_web.services.bot_binding_selection import BotBindingSelectionService
from codex_web.services.project_runtime import ProjectRuntimeService
from codex_web.services.provider_capacity import ProviderCapacityBlockedError
from codex_web.services.thread_execution_settings import (
    ThreadExecutionSettingsService,
)
from codex_web.services.thread_recovery import ThreadRecoveryService
from codex_web.services.thread_resume import ThreadResumeService
from codex_web.services.turn_queue_policy import TurnQueuePolicy


EventSink = Callable[[dict[str, Any]], None]
TextTruncator = Callable[[str, int], str]
BindingProjector = Callable[[BotBinding], dict[str, Any]]


class TurnExecutionRuntime(Protocol):
    def enqueue_turn(self, **kwargs: Any) -> QueuedTurn: ...

    async def publish_queue_status(self, thread_id: str) -> None: ...

    def wait_for_thread_capacity(
        self,
        *,
        thread_id: str,
        execution_id: str | None,
        provider_keys: tuple[str, ...],
        retry_at: float | None,
        reason: str,
    ) -> Any: ...

    def thread_is_active(self, thread_id: str | None) -> bool: ...

    async def start_thread_turn_now(
        self,
        thread_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]: ...

    def schedule_queue_drain(self, thread_id: str | None) -> None: ...

    def pop_latest_queued_turn(self, thread_id: str) -> QueuedTurn | None: ...

    def pop_queued_turn(
        self,
        thread_id: str,
        queued_id: str,
    ) -> QueuedTurn | None: ...

    def requeue_turn_front(self, queued: QueuedTurn) -> None: ...

    def clear_thread_active(self, thread_id: str | None) -> None: ...

    async def request_for_thread(
        self,
        thread_id: str,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...


class TurnService:
    """Web-facing turn lifecycle over explicit runtime collaborators."""

    def __init__(
        self,
        *,
        projects: ProjectRuntimeService,
        settings: ThreadExecutionSettingsService,
        recovery: ThreadRecoveryService,
        resume_runtime: ThreadResumeService,
        bindings: BotBindingSelectionService,
        queue_policy: TurnQueuePolicy,
        execution: TurnExecutionRuntime,
        event_sink: EventSink,
        truncate_text: TextTruncator,
        binding_public: BindingProjector,
    ) -> None:
        self.projects = projects
        self.settings = settings
        self.recovery = recovery
        self.resume_runtime = resume_runtime
        self.bindings = bindings
        self.queue_policy = queue_policy
        self.execution = execution
        self.event_sink = event_sink
        self.truncate_text = truncate_text
        self.binding_public = binding_public

    async def resume(
        self,
        thread_id: str,
        project_id: str | None = None,
        sandbox: str | None = None,
        approval_policy: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        force_resume: bool = False,
    ) -> dict[str, Any]:
        self.recovery.raise_if_thread_replaced(thread_id)
        project = self.projects.get(project_id)
        remembered = self.settings.get(thread_id)
        effective_sandbox = sandbox or remembered.sandbox or project.sandbox
        effective_approval_policy = (
            approval_policy
            or remembered.approval_policy
            or project.approval_policy
        )
        effective_model = model or remembered.model or project.model
        effective_reasoning_effort = (
            reasoning_effort or remembered.reasoning_effort
        )
        effective_developer_instructions = (
            self.settings.effective_developer_instructions(
                thread_id,
                remembered.developer_instructions,
            )
        )
        self.settings.remember(
            thread_id,
            sandbox=effective_sandbox,
            approval_policy=effective_approval_policy,
            model=effective_model,
            reasoning_effort=effective_reasoning_effort,
            developer_instructions=remembered.developer_instructions,
        )
        params = {
            "threadId": thread_id,
            **self.projects.params(
                project,
                {
                    "sandbox": effective_sandbox,
                    "approvalPolicy": effective_approval_policy,
                    "model": effective_model,
                    "developerInstructions": effective_developer_instructions,
                },
            ),
        }
        if not force_resume:
            return {
                "ok": True,
                "threadId": thread_id,
                "skipped": True,
                "reason": "web_load_uses_thread_read",
            }

        task, scheduled = self.resume_runtime.schedule(
            thread_id,
            project.id,
            params,
        )
        timeout = self.resume_runtime.handoff_timeout()
        try:
            return await asyncio.wait_for(
                asyncio.shield(task),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            if scheduled:
                self.event_sink(
                    {
                        "type": "web_resume_backgrounded",
                        "thread_id": thread_id,
                        "project_id": project.id,
                        "timeout_seconds": timeout,
                    }
                )
            return {
                "ok": True,
                "resuming": True,
                "threadId": thread_id,
                "alreadyResuming": not scheduled,
            }

    async def replace(self, thread_id: str) -> dict[str, Any]:
        existing_replacement = self.recovery.replacement_thread_id(thread_id)
        if existing_replacement:
            return {
                "ok": True,
                "alreadyReplaced": True,
                "oldThreadId": thread_id,
                "newThreadId": existing_replacement,
                "queueDepth": self.queue_policy.depth(existing_replacement),
            }
        bindings = self.bindings.for_thread(thread_id)
        if not bindings:
            raise HTTPException(
                status_code=404,
                detail="No bot binding for this thread",
            )
        replacement = await self.recovery.replace_stale_bot_thread(
            bindings[0],
            "manual replacement requested",
        )
        self.execution.schedule_queue_drain(replacement.thread_id)
        return {
            "ok": True,
            "oldThreadId": thread_id,
            "newThreadId": replacement.thread_id,
            "binding": self.binding_public(replacement),
            "queueDepth": self.queue_policy.depth(replacement.thread_id),
        }

    async def start(
        self,
        thread_id: str,
        payload: TurnCreate,
    ) -> dict[str, Any]:
        self.recovery.raise_if_thread_replaced(thread_id)
        project = self.projects.get(payload.project_id)
        remembered = self.settings.get(thread_id)
        effective_sandbox = (
            payload.sandbox or remembered.sandbox or project.sandbox
        )
        effective_approval_policy = (
            payload.approval_policy
            or remembered.approval_policy
            or project.approval_policy
        )
        effective_model = payload.model or remembered.model or project.model
        effective_reasoning_effort = (
            payload.reasoning_effort or remembered.reasoning_effort
        )
        self.settings.remember(
            thread_id,
            sandbox=effective_sandbox,
            approval_policy=effective_approval_policy,
            model=effective_model,
            reasoning_effort=effective_reasoning_effort,
            developer_instructions=remembered.developer_instructions,
        )
        execution_id = f"thread-turn-{uuid.uuid4().hex}"

        async def queue_web_turn(
            event_type: str,
            reason: str | None = None,
            capacity_error: ProviderCapacityBlockedError | None = None,
        ) -> dict[str, Any]:
            queued = self.execution.enqueue_turn(
                thread_id=thread_id,
                project_id=project.id,
                message=payload.message,
                sandbox=effective_sandbox,
                approval_policy=effective_approval_policy,
                model=effective_model,
                reasoning_effort=effective_reasoning_effort,
                execution_id=execution_id,
            )
            queue_depth = self.queue_policy.depth(thread_id)
            event_payload: dict[str, Any] = {
                "type": event_type,
                "thread_id": thread_id,
                "project_id": project.id,
                "queued_id": queued.id,
                "queue_depth": queue_depth,
            }
            if reason:
                event_payload["reason"] = self.truncate_text(reason, 500)
            self.event_sink(event_payload)
            await self.execution.publish_queue_status(thread_id)
            wait = None
            if capacity_error is not None:
                wait = self.execution.wait_for_thread_capacity(
                    thread_id=thread_id,
                    execution_id=execution_id,
                    provider_keys=(capacity_error.record.key,),
                    retry_at=capacity_error.retry_at,
                    reason=str(capacity_error),
                )
            elif not self.execution.thread_is_active(thread_id):
                asyncio.get_running_loop().call_later(
                    5,
                    self.execution.schedule_queue_drain,
                    thread_id,
                )
            return {
                "queued": True,
                "queuedId": queued.id,
                "queueDepth": queue_depth,
                "threadId": thread_id,
                "waitingForCapacity": capacity_error is not None,
                "capacityWaitId": getattr(wait, "id", None),
                "retryAt": (
                    capacity_error.retry_at
                    if capacity_error is not None
                    else None
                ),
            }

        self.recovery.release_stale_active_turn(thread_id, "web:start")
        if (
            self.execution.thread_is_active(thread_id)
            or self.queue_policy.depth(thread_id)
        ):
            return await queue_web_turn("web_turn_queued")
        try:
            return await self.execution.start_thread_turn_now(
                thread_id,
                project=project,
                message=payload.message,
                sandbox=effective_sandbox,
                approval_policy=effective_approval_policy,
                model=effective_model,
                reasoning_effort=effective_reasoning_effort,
                execution_id=execution_id,
            )
        except Exception as exc:
            if isinstance(exc, ProviderCapacityBlockedError):
                result = await queue_web_turn(
                    "web_turn_waiting_for_capacity",
                    str(exc),
                    capacity_error=exc,
                )
                result["providerKey"] = exc.record.key
                result["capacityStatus"] = exc.record.status.value
                return result
            if (
                isinstance(exc, HTTPException)
                and exc.status_code == 409
                and isinstance(exc.detail, dict)
                and exc.detail.get("code") == "thread_turn_already_active"
            ):
                return await queue_web_turn(
                    "web_turn_queued_after_concurrent_start",
                    "thread became active while waiting for isolated start lock",
                )
            if self.resume_runtime.is_timeout_error(exc):
                if self.execution.thread_is_active(thread_id):
                    return {
                        "queued": False,
                        "resuming": True,
                        "threadId": thread_id,
                        "executionId": execution_id,
                        "timedOut": True,
                        "error": str(getattr(exc, "detail", exc)),
                    }
                result = await queue_web_turn(
                    "web_turn_queued_after_timeout",
                    str(exc),
                )
                result["timedOut"] = True
                result["error"] = str(getattr(exc, "detail", exc))
                return result
            if self.resume_runtime.is_stale_thread_error(exc):
                bindings = self.bindings.for_thread(thread_id)
                if bindings:
                    replacement = await self.recovery.replace_stale_bot_thread(
                        bindings[0],
                        str(exc),
                    )
                    new_thread_id = replacement.thread_id
                else:
                    new_thread_id = await self.recovery.replace_stale_web_thread(
                        thread_id,
                        project,
                        str(exc),
                    )
                return {
                    "ok": False,
                    "staleThreadReplaced": True,
                    "threadId": new_thread_id,
                    "oldThreadId": thread_id,
                    "newThreadId": new_thread_id,
                }
            raise

    def queue(self, thread_id: str) -> dict[str, Any]:
        self.recovery.raise_if_thread_replaced(thread_id)
        return {
            "threadId": thread_id,
            "active": self.execution.thread_is_active(thread_id),
            "queueDepth": self.queue_policy.depth(thread_id),
            "queued": [
                {
                    "id": queued.id,
                    "source": queued.source,
                    "createdAt": queued.created_at,
                    "attempts": queued.attempts,
                }
                for queued in self.queue_policy.queue(thread_id)
            ],
        }

    async def steer_latest(self, thread_id: str) -> dict[str, Any]:
        self.recovery.raise_if_thread_replaced(thread_id)
        queued = self.execution.pop_latest_queued_turn(thread_id)
        if not queued:
            raise HTTPException(
                status_code=404,
                detail="No queued message for this thread",
            )
        return await self._steer(thread_id, queued)

    async def steer(
        self,
        thread_id: str,
        queued_id: str,
    ) -> dict[str, Any]:
        self.recovery.raise_if_thread_replaced(thread_id)
        queued = self.execution.pop_queued_turn(thread_id, queued_id)
        if not queued:
            raise HTTPException(
                status_code=404,
                detail="Queued message not found for this thread",
            )
        return await self._steer(thread_id, queued)

    async def _steer(
        self,
        thread_id: str,
        queued: QueuedTurn,
    ) -> dict[str, Any]:
        if self.execution.thread_is_active(thread_id):
            try:
                self.queue_policy.record_steer(thread_id)
            except HTTPException:
                self.execution.requeue_turn_front(queued)
                raise
            with contextlib.suppress(Exception):
                await self.execution.request_for_thread(
                    thread_id,
                    "turn/interrupt",
                    {"threadId": thread_id},
                )
            self.execution.clear_thread_active(thread_id)

        project = self.projects.get(queued.project_id)
        response = await self.execution.start_thread_turn_now(
            thread_id,
            project=project,
            message=queued.message,
            sandbox=queued.sandbox or project.sandbox,
            approval_policy=(
                queued.approval_policy or project.approval_policy
            ),
            model=queued.model,
            reasoning_effort=queued.reasoning_effort,
            source=f"steer:{queued.source}",
            reply_target=queued.reply_target,
            execution_id=queued.execution_id,
        )
        return {
            "ok": True,
            "steeredId": queued.id,
            "queueDepth": self.queue_policy.depth(thread_id),
            "turn": (
                response.get("turn")
                if isinstance(response, dict)
                else None
            ),
        }
