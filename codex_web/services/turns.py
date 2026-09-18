from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from fastapi import HTTPException

from codex_web.models import QueuedTurn, TurnCreate


class TurnService:
    """Owns the web-facing turn lifecycle and queue orchestration."""

    def __init__(self, host: Any) -> None:
        self.host = host

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
        self.host._raise_if_thread_replaced(thread_id)
        project = self.host._project(project_id)
        remembered = self.host._thread_run_settings(thread_id)
        effective_sandbox = sandbox or remembered.sandbox or project.sandbox
        effective_approval_policy = approval_policy or remembered.approval_policy or project.approval_policy
        effective_model = model or remembered.model or project.model
        effective_reasoning_effort = reasoning_effort or remembered.reasoning_effort
        effective_developer_instructions = self.host._effective_developer_instructions(
            thread_id,
            remembered.developer_instructions,
        )
        self.host._remember_thread_run_settings(
            thread_id,
            sandbox=effective_sandbox,
            approval_policy=effective_approval_policy,
            model=effective_model,
            reasoning_effort=effective_reasoning_effort,
            developer_instructions=remembered.developer_instructions,
        )
        params = {
            "threadId": thread_id,
            **self.host._project_params(
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
        task, scheduled = self.host._web_thread_resume_task(thread_id, project.id, params)
        try:
            return await asyncio.wait_for(
                asyncio.shield(task),
                timeout=self.host._web_thread_resume_handoff_timeout(),
            )
        except asyncio.TimeoutError:
            if scheduled:
                self.host._append_bot_event(
                    {
                        "type": "web_resume_backgrounded",
                        "thread_id": thread_id,
                        "project_id": project.id,
                        "timeout_seconds": self.host._web_thread_resume_handoff_timeout(),
                    }
                )
            return {
                "ok": True,
                "resuming": True,
                "threadId": thread_id,
                "alreadyResuming": not scheduled,
            }

    async def replace(self, thread_id: str) -> dict[str, Any]:
        existing_replacement = self.host._replacement_thread_id(thread_id)
        if existing_replacement:
            return {
                "ok": True,
                "alreadyReplaced": True,
                "oldThreadId": thread_id,
                "newThreadId": existing_replacement,
                "queueDepth": self.host._thread_queue_depth(existing_replacement),
            }
        bindings = self.host._bindings_for_thread(thread_id)
        if not bindings:
            raise HTTPException(status_code=404, detail="No bot binding for this thread")
        replacement = await self.host._replace_stale_bot_thread(
            bindings[0],
            "manual replacement requested",
        )
        self.host._schedule_queue_drain(replacement.thread_id)
        return {
            "ok": True,
            "oldThreadId": thread_id,
            "newThreadId": replacement.thread_id,
            "binding": self.host._binding_public(replacement),
            "queueDepth": self.host._thread_queue_depth(replacement.thread_id),
        }

    async def start(self, thread_id: str, payload: TurnCreate) -> dict[str, Any]:
        self.host._raise_if_thread_replaced(thread_id)
        project = self.host._project(payload.project_id)
        remembered = self.host._thread_run_settings(thread_id)
        effective_sandbox = payload.sandbox or remembered.sandbox or project.sandbox
        effective_approval_policy = payload.approval_policy or remembered.approval_policy or project.approval_policy
        effective_model = payload.model or remembered.model or project.model
        effective_reasoning_effort = payload.reasoning_effort or remembered.reasoning_effort
        self.host._remember_thread_run_settings(
            thread_id,
            sandbox=effective_sandbox,
            approval_policy=effective_approval_policy,
            model=effective_model,
            reasoning_effort=effective_reasoning_effort,
            developer_instructions=remembered.developer_instructions,
        )
        execution_id = f"thread-turn-{__import__('uuid').uuid4().hex}"

        async def queue_web_turn(event_type: str, reason: str | None = None) -> dict[str, Any]:
            queued = self.host._enqueue_turn(
                thread_id=thread_id,
                project_id=project.id,
                message=payload.message,
                sandbox=effective_sandbox,
                approval_policy=effective_approval_policy,
                model=effective_model,
                reasoning_effort=effective_reasoning_effort,
                execution_id=execution_id,
            )
            queue_depth = self.host._thread_queue_depth(thread_id)
            event_payload = {
                "type": event_type,
                "thread_id": thread_id,
                "project_id": project.id,
                "queued_id": queued.id,
                "queue_depth": queue_depth,
            }
            if reason:
                event_payload["reason"] = self.host._truncate_text(reason, 500)
            self.host._append_bot_event(event_payload)
            await self.host._publish_queue_status(thread_id)
            if not self.host._thread_is_active(thread_id):
                asyncio.get_running_loop().call_later(
                    5,
                    self.host._schedule_queue_drain,
                    thread_id,
                )
            return {
                "queued": True,
                "queuedId": queued.id,
                "queueDepth": queue_depth,
                "threadId": thread_id,
            }

        self.host._release_stale_active_turn(thread_id, "web:start")
        if self.host._thread_is_active(thread_id) or self.host._thread_queue_depth(thread_id):
            return await queue_web_turn("web_turn_queued")
        try:
            return await self.host._start_thread_turn_now(
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
            if self.host._is_codex_timeout_error(exc):
                if self.host._thread_is_active(thread_id):
                    return {
                        "queued": False,
                        "resuming": True,
                        "threadId": thread_id,
                        "executionId": execution_id,
                        "timedOut": True,
                        "error": str(getattr(exc, "detail", exc)),
                    }
                result = await queue_web_turn("web_turn_queued_after_timeout", str(exc))
                result["timedOut"] = True
                result["error"] = str(getattr(exc, "detail", exc))
                return result
            if self.host._is_stale_thread_error(exc):
                bindings = self.host._bindings_for_thread(thread_id)
                if bindings:
                    replacement = await self.host._replace_stale_bot_thread(bindings[0], str(exc))
                    new_thread_id = replacement.thread_id
                else:
                    new_thread_id = await self.host._replace_stale_web_thread(
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
        self.host._raise_if_thread_replaced(thread_id)
        return {
            "threadId": thread_id,
            "active": self.host._thread_is_active(thread_id),
            "queueDepth": self.host._thread_queue_depth(thread_id),
            "queued": [
                {
                    "id": queued.id,
                    "source": queued.source,
                    "createdAt": queued.created_at,
                    "attempts": queued.attempts,
                }
                for queued in self.host._thread_queue(thread_id)
            ],
        }

    async def steer_latest(self, thread_id: str) -> dict[str, Any]:
        self.host._raise_if_thread_replaced(thread_id)
        queued = self.host._pop_latest_queued_turn(thread_id)
        if not queued:
            raise HTTPException(status_code=404, detail="No queued message for this thread")
        return await self._steer(thread_id, queued)

    async def steer(self, thread_id: str, queued_id: str) -> dict[str, Any]:
        self.host._raise_if_thread_replaced(thread_id)
        queued = self.host._pop_queued_turn(thread_id, queued_id)
        if not queued:
            raise HTTPException(status_code=404, detail="Queued message not found for this thread")
        return await self._steer(thread_id, queued)

    async def _steer(self, thread_id: str, queued: QueuedTurn) -> dict[str, Any]:
        if self.host._thread_is_active(thread_id):
            try:
                self.host._record_thread_steer(thread_id)
            except HTTPException:
                self.host._requeue_turn_front(queued)
                raise
            with contextlib.suppress(Exception):
                await self.host.codex.request("turn/interrupt", {"threadId": thread_id})
            self.host._clear_thread_active(thread_id)
        project = self.host._project(queued.project_id)
        response = await self.host._start_thread_turn_now(
            thread_id,
            project=project,
            message=queued.message,
            sandbox=queued.sandbox or project.sandbox,
            approval_policy=queued.approval_policy or project.approval_policy,
            model=queued.model,
            reasoning_effort=queued.reasoning_effort,
            source=f"steer:{queued.source}",
            reply_target=queued.reply_target,
        )
        return {
            "ok": True,
            "steeredId": queued.id,
            "queueDepth": self.host._thread_queue_depth(thread_id),
            "turn": response.get("turn") if isinstance(response, dict) else None,
        }
