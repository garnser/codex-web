from __future__ import annotations

import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

from codex_web.models import BotBinding
from codex_web.services.bot_bindings import BotBindingLifecycleService
from codex_web.services.bot_runtime_telemetry import BotRuntimeTelemetry
from codex_web.services.bot_targets import BotTargetService
from codex_web.services.project_runtime import ProjectRuntimeService
from codex_web.services.thread_execution_settings import (
    ThreadExecutionSettingsService,
)
from codex_web.services.thread_recovery import ThreadRecoveryService
from codex_web.services.thread_resume import ThreadResumeService
from codex_web.services.turn_queue_policy import TurnQueuePolicy


class BotEventDispatchService:
    """Dispatch event-driven work to canonical bot threads."""

    def __init__(
        self,
        *,
        projects: ProjectRuntimeService,
        settings: ThreadExecutionSettingsService,
        targets: BotTargetService,
        bindings: BotBindingLifecycleService,
        execution: Any,
        queue_policy: TurnQueuePolicy,
        recovery: ThreadRecoveryService,
        resume: ThreadResumeService,
        telemetry: BotRuntimeTelemetry,
        publish_event: Callable[
            [dict[str, Any]], Awaitable[object]
        ],
        binding_name: Callable[[BotBinding], str],
    ) -> None:
        self.projects = projects
        self.settings = settings
        self.targets = targets
        self.bindings = bindings
        self.execution = execution
        self.queue_policy = queue_policy
        self.recovery = recovery
        self.resume = resume
        self.telemetry = telemetry
        self.publish_event = publish_event
        self.binding_name = binding_name

    @staticmethod
    def recent_activity_grace_seconds() -> float:
        try:
            seconds = float(
                os.environ.get(
                    "CODEX_WEB_WATCHDOG_RECENT_ACTIVITY_GRACE_SECONDS"
                )
                or "300"
            )
        except ValueError:
            return 300.0
        return max(30.0, seconds)

    @staticmethod
    def replacement_threshold() -> int:
        try:
            value = int(
                os.environ.get(
                    "CODEX_WEB_WATCHDOG_REPLACEMENT_THRESHOLD"
                )
                or "3"
            )
        except ValueError:
            return 3
        return max(2, value)

    def thread_recently_active(self, thread_id: str | None) -> bool:
        age = self.telemetry.thread_recent_activity_age_seconds(
            thread_id
        )
        return (
            age is not None
            and age < self.recent_activity_grace_seconds()
        )

    async def replace_nonperforming_thread(
        self,
        binding: BotBinding,
        reason: str,
    ) -> BotBinding:
        if not binding.thread_id:
            return binding
        if (
            self.execution.thread_is_active(binding.thread_id)
            or self.queue_policy.depth(binding.thread_id)
            or self.thread_recently_active(binding.thread_id)
        ):
            return binding
        dispatch_count = self.telemetry.thread_recent_event_count(
            binding.thread_id,
            {
                "owner_work_watchdog_dispatched",
                "release_gate_watchdog_dispatched",
                "work_item_sla_dispatched",
                "work_item_handoff_watchdog_dispatched",
            },
        )
        if dispatch_count < self.replacement_threshold():
            return binding
        replacement = await self.recovery.replace_stale_bot_thread(
            binding,
            (
                "autonomous replacement after "
                f"{dispatch_count} watchdog dispatches ({reason})"
            ),
        )
        self.telemetry.append(
            {
                "type": "autonomous_thread_replaced",
                "reason": reason,
                "old_thread_id": binding.thread_id,
                "new_thread_id": replacement.thread_id,
                "logical_name": self.binding_name(binding),
            }
        )
        return replacement

    async def dispatch(
        self,
        binding: BotBinding,
        text: str,
        source: str,
    ) -> dict[str, Any]:
        project = self.projects.get(binding.project_id)
        settings = self.settings.get(binding.thread_id)
        effective_model = settings.model or project.model
        effective_reasoning_effort = settings.reasoning_effort
        reply_target = self.targets.conversation_target(binding)

        async def queue_turn(
            event_type: str,
            reason: str | None = None,
        ) -> dict[str, Any]:
            duplicate = self.execution.find_duplicate_queued_turn(
                binding.thread_id,
                message=text,
                source=source,
            )
            if duplicate:
                self.telemetry.append(
                    {
                        "type": "event_turn_duplicate_skipped",
                        "source": source,
                        "thread_id": binding.thread_id,
                        "external_conversation_id": (
                            binding.external_conversation_id
                        ),
                        "queued_id": duplicate.id,
                        "queue_depth": self.queue_policy.depth(
                            binding.thread_id
                        ),
                    }
                )
                await self.execution.publish_queue_status(
                    binding.thread_id
                )
                return {
                    "ok": True,
                    "queued": True,
                    "duplicate": True,
                    "threadId": binding.thread_id,
                    "queuedId": duplicate.id,
                }

            queued = self.execution.enqueue_turn(
                thread_id=binding.thread_id,
                project_id=project.id,
                message=text,
                sandbox=binding.sandbox,
                approval_policy=binding.approval_policy,
                model=effective_model,
                reasoning_effort=effective_reasoning_effort,
                source=source,
                reply_target=reply_target,
            )
            binding.updated_at = time.time()
            self.bindings.upsert(binding)
            payload: dict[str, Any] = {
                "type": event_type,
                "source": source,
                "thread_id": binding.thread_id,
                "external_conversation_id": (
                    binding.external_conversation_id
                ),
                "queued_id": queued.id,
                "queue_depth": self.queue_policy.depth(
                    binding.thread_id
                ),
            }
            if reason:
                payload["reason"] = str(reason)[:500]
            self.telemetry.append(payload)
            await self.execution.publish_queue_status(
                binding.thread_id
            )
            await self.publish_event(
                {
                    "type": "bot.inbound",
                    "provider": source,
                    "externalConversationId": (
                        binding.external_conversation_id
                    ),
                    "threadId": binding.thread_id,
                    "queued": True,
                    "queuedId": queued.id,
                    "queueDepth": self.queue_policy.depth(
                        binding.thread_id
                    ),
                }
            )
            return {
                "ok": True,
                "queued": True,
                "threadId": binding.thread_id,
                "queuedId": queued.id,
            }

        self.recovery.release_stale_active_turn(
            binding.thread_id,
            f"{source}:dispatch",
        )
        if (
            self.execution.thread_is_active(binding.thread_id)
            or self.queue_policy.depth(binding.thread_id)
        ):
            return await queue_turn("event_turn_queued")

        try:
            turn = await self.execution.start_thread_turn_now(
                binding.thread_id,
                project=project,
                message=text,
                sandbox=binding.sandbox,
                approval_policy=binding.approval_policy,
                model=effective_model,
                reasoning_effort=effective_reasoning_effort,
                source=source,
                reply_target=reply_target,
            )
        except Exception as exc:
            if self.resume.is_timeout_error(exc):
                return await queue_turn(
                    "event_turn_queued_after_timeout",
                    str(getattr(exc, "detail", exc)),
                )
            if not self.resume.is_stale_thread_error(exc):
                raise
            binding = await self.recovery.replace_stale_bot_thread(
                binding,
                str(exc),
            )
            project = self.projects.get(binding.project_id)
            settings = self.settings.get(binding.thread_id)
            effective_model = settings.model or project.model
            effective_reasoning_effort = settings.reasoning_effort
            reply_target = self.targets.conversation_target(binding)
            turn = await self.execution.start_thread_turn_now(
                binding.thread_id,
                project=project,
                message=text,
                sandbox=binding.sandbox,
                approval_policy=binding.approval_policy,
                model=effective_model,
                reasoning_effort=effective_reasoning_effort,
                source=source,
                reply_target=reply_target,
            )

        binding.updated_at = time.time()
        self.bindings.upsert(binding)
        await self.publish_event(
            {
                "type": "bot.inbound",
                "provider": source,
                "externalConversationId": (
                    binding.external_conversation_id
                ),
                "threadId": binding.thread_id,
            }
        )
        return {
            "ok": True,
            "queued": False,
            "threadId": binding.thread_id,
            "turn": turn,
        }


class BotEventDispatchCompatibilityFacade:
    """Dynamic historical dispatch seam for verified import-server callers."""

    def __init__(self, host: Any) -> None:
        self.host = host

    async def dispatch(
        self,
        binding: BotBinding,
        text: str,
        source: str,
    ) -> dict[str, Any]:
        host = self.host
        project = host._project(binding.project_id)
        settings = host._thread_run_settings(binding.thread_id)
        effective_model = settings.model or project.model
        effective_reasoning_effort = settings.reasoning_effort
        reply_target = host._conversation_target_for_binding(binding)

        async def queue_binding_turn(
            event_type: str,
            reason: str | None = None,
        ) -> dict[str, Any]:
            duplicate = host._find_duplicate_queued_turn(
                binding.thread_id,
                message=text,
                source=source,
            )
            if duplicate:
                host._append_bot_event(
                    {
                        "type": "event_turn_duplicate_skipped",
                        "source": source,
                        "thread_id": binding.thread_id,
                        "external_conversation_id": (
                            binding.external_conversation_id
                        ),
                        "queued_id": duplicate.id,
                        "queue_depth": host._thread_queue_depth(
                            binding.thread_id
                        ),
                    }
                )
                await host._publish_queue_status(binding.thread_id)
                return {
                    "ok": True,
                    "queued": True,
                    "duplicate": True,
                    "threadId": binding.thread_id,
                    "queuedId": duplicate.id,
                }

            queued = host._enqueue_turn(
                thread_id=binding.thread_id,
                project_id=project.id,
                message=text,
                sandbox=binding.sandbox,
                approval_policy=binding.approval_policy,
                model=effective_model,
                reasoning_effort=effective_reasoning_effort,
                source=source,
                reply_target=reply_target,
            )
            binding.updated_at = time.time()
            host._upsert_bot_binding(binding)
            payload: dict[str, Any] = {
                "type": event_type,
                "source": source,
                "thread_id": binding.thread_id,
                "external_conversation_id": (
                    binding.external_conversation_id
                ),
                "queued_id": queued.id,
                "queue_depth": host._thread_queue_depth(binding.thread_id),
            }
            if reason:
                truncate = getattr(
                    host,
                    "_truncate_text",
                    lambda value, limit: str(value)[:limit],
                )
                payload["reason"] = truncate(reason, 500)
            host._append_bot_event(payload)
            await host._publish_queue_status(binding.thread_id)
            await host.hub.publish(
                {
                    "type": "bot.inbound",
                    "provider": source,
                    "externalConversationId": (
                        binding.external_conversation_id
                    ),
                    "threadId": binding.thread_id,
                    "queued": True,
                    "queuedId": queued.id,
                    "queueDepth": host._thread_queue_depth(
                        binding.thread_id
                    ),
                }
            )
            return {
                "ok": True,
                "queued": True,
                "threadId": binding.thread_id,
                "queuedId": queued.id,
            }

        host._release_stale_active_turn(
            binding.thread_id,
            f"{source}:dispatch",
        )
        if (
            host._thread_is_active(binding.thread_id)
            or host._thread_queue_depth(binding.thread_id)
        ):
            return await queue_binding_turn("event_turn_queued")

        try:
            turn = await host._start_thread_turn_now(
                binding.thread_id,
                project=project,
                message=text,
                sandbox=binding.sandbox,
                approval_policy=binding.approval_policy,
                model=effective_model,
                reasoning_effort=effective_reasoning_effort,
                source=source,
                reply_target=reply_target,
            )
        except Exception as exc:
            if host._is_codex_timeout_error(exc):
                return await queue_binding_turn(
                    "event_turn_queued_after_timeout",
                    str(getattr(exc, "detail", exc)),
                )
            if not host._is_stale_thread_error(exc):
                raise
            binding = await host._replace_stale_bot_thread(
                binding,
                str(exc),
            )
            project = host._project(binding.project_id)
            settings = host._thread_run_settings(binding.thread_id)
            effective_model = settings.model or project.model
            effective_reasoning_effort = settings.reasoning_effort
            reply_target = host._conversation_target_for_binding(binding)
            turn = await host._start_thread_turn_now(
                binding.thread_id,
                project=project,
                message=text,
                sandbox=binding.sandbox,
                approval_policy=binding.approval_policy,
                model=effective_model,
                reasoning_effort=effective_reasoning_effort,
                source=source,
                reply_target=reply_target,
            )

        binding.updated_at = time.time()
        host._upsert_bot_binding(binding)
        await host.hub.publish(
            {
                "type": "bot.inbound",
                "provider": source,
                "externalConversationId": (
                    binding.external_conversation_id
                ),
                "threadId": binding.thread_id,
            }
        )
        return {
            "ok": True,
            "queued": False,
            "threadId": binding.thread_id,
            "turn": turn,
        }
