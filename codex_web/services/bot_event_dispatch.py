from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import HTTPException

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


def _concurrent_start_refusal(exc: Exception, thread_id: str) -> bool:
    # The runtime refuses under its start lock before native admission.
    return (isinstance(exc, HTTPException) and exc.status_code == 409
            and isinstance(exc.detail, dict)
            and exc.detail.get("code") == "thread_turn_already_active"
            and exc.detail.get("threadId") == thread_id)


async def _retain_concurrent_request(thread_id, require_idle, queue_turn, scope=None):
    if require_idle:
        result = {"ok": True, "queued": False, "skipped": "already_active",
                  "threadId": thread_id}
        if scope is not None:
            result["dispatchScope"] = scope
        return result
    return await queue_turn("event_turn_queued_after_concurrent_start",
                            "thread became active before native admission")


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
        agent_profile_resolver: Callable[[BotBinding], tuple[Any, Any] | None]
        | None = None,
        execution_profiles: Any | None = None,
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
        self.agent_profile_resolver = agent_profile_resolver
        self.execution_profiles = execution_profiles
        self.owner_routing_context = None

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
        live_session_check = getattr(
            self.execution,
            "thread_has_live_agent_runtime_session",
            None,
        )
        missing_live_session = bool(
            callable(live_session_check)
            and not live_session_check(binding.thread_id)
        )
        inflight_assignment_check = getattr(
            self.execution,
            "thread_has_inflight_agent_runtime_assignment",
            None,
        )
        if (
            missing_live_session
            and callable(inflight_assignment_check)
            and inflight_assignment_check(binding.thread_id)
        ):
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
        if (
            not missing_live_session
            and dispatch_count < self.replacement_threshold()
        ):
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
                "thread_id": replacement.thread_id,
                "logical_name": self.binding_name(binding),
            }
        )
        return replacement

    async def dispatch(
        self,
        binding: BotBinding,
        text: str,
        source: str,
        *,
        work_item_ref: str | None = None,
        handoff_requested_at: float | None = None,
        repository_resource_id: str | None = None,
        writable_repository_resource_ids: tuple[str, ...] = (),
        read_only_repository_resource_ids: tuple[str, ...] = (),
        require_idle: bool = False,
        provider_notification_pin=None,
        owner_routing_pin=None,
    ) -> dict[str, Any]:
        recipient_thread_id = (owner_routing_pin.recipient.thread_id
            if getattr(owner_routing_pin, 'recipient', None) is not None else binding.thread_id)
        project = self.projects.get(binding.project_id)
        settings = self.settings.get(recipient_thread_id)
        effective_model = settings.model or project.model
        effective_reasoning_effort = settings.reasoning_effort
        reply_target = (
            await asyncio.to_thread(self.targets.event_reply_target, binding)
            or self.targets.conversation_target(binding)
        )
        profile_context = (
            await asyncio.to_thread(self.agent_profile_resolver, binding)
            if self.agent_profile_resolver is not None and owner_routing_pin is None
            else None
        )
        if owner_routing_pin is not None:
            if self.owner_routing_context is None:
                raise HTTPException(status_code=409, detail={'code': 'owner_routing_context_unavailable'})
            profile_context = await asyncio.to_thread(self.owner_routing_context, owner_routing_pin, binding)
        agent_profile, profile_actor = (
            profile_context if profile_context is not None else (None, None)
        )
        profile_kwargs = {
            "execution_profile_id": getattr(
                agent_profile,
                "execution_profile_id",
                None,
            ) or getattr(settings, "execution_profile_id", None),
            "agent_profile_id": getattr(agent_profile, "profile_id", None),
            "agent_profile_revision": getattr(agent_profile, "revision", None),
            "agent_profile_actor_id": getattr(profile_actor, "identity_id", None),
        }
        # A task's repository is context, not a grant of repository access to
        # its recipient. Resolve the published execution contract before either
        # queueing or starting the event so scratch-only agents retain their scope.
        if self.execution_profiles is not None:
            execution_profile, _ = await asyncio.to_thread(
                self.execution_profiles.resolve,
                profile_kwargs["execution_profile_id"]
                or getattr(settings, "execution_profile_id", None),
                organization_id=project.organization_id,
                workspace_id=project.workspace_id,
                project_id=project.id,
            )
            profile_kwargs["execution_profile_id"] = getattr(
                execution_profile,
                "id",
                profile_kwargs["execution_profile_id"],
            )
            if execution_profile.repository_access == "none":
                repository_resource_id = None
                writable_repository_resource_ids = ()
                read_only_repository_resource_ids = ()

        dispatch_scope = {
            "work_item_ref": work_item_ref,
            "execution_profile_id": profile_kwargs[
                "execution_profile_id"
            ],
            "repository_resource_id": repository_resource_id,
            "writable_repository_resource_ids": list(
                writable_repository_resource_ids
            ),
            "read_only_repository_resource_ids": list(
                read_only_repository_resource_ids
            ),
        }

        async def queue_turn(
            event_type: str,
            reason: str | None = None,
        ) -> dict[str, Any]:
            duplicate = self.execution.find_duplicate_queued_turn(
                recipient_thread_id,
                message=text,
                source=source,
                project_id=project.id,
                sandbox=binding.sandbox,
                approval_policy=binding.approval_policy,
                model=effective_model,
                reasoning_effort=effective_reasoning_effort,
                work_item_ref=work_item_ref,
                repository_resource_id=repository_resource_id,
                writable_repository_resource_ids=writable_repository_resource_ids,
                read_only_repository_resource_ids=read_only_repository_resource_ids,
                reply_target=reply_target,
                handoff_requested_at=handoff_requested_at,
                provider_notification_pin=provider_notification_pin,
                **({'owner_routing_pin': owner_routing_pin} if owner_routing_pin is not None else {}),
                **profile_kwargs,
            )
            if duplicate:
                self.telemetry.append(
                    {
                        "type": "event_turn_duplicate_skipped",
                        "source": source,
                        "thread_id": recipient_thread_id,
                        "external_conversation_id": (
                            binding.external_conversation_id
                        ),
                        "queued_id": duplicate.id,
                        "queue_depth": self.queue_policy.depth(
                            recipient_thread_id
                        ),
                    }
                )
                await self.execution.publish_queue_status(
                    recipient_thread_id
                )
                return {
                    "ok": True,
                    "queued": True,
                    "duplicate": True,
                    "threadId": recipient_thread_id,
                    "queuedId": duplicate.id,
                    "dispatchScope": dispatch_scope,
                }

            queued = self.execution.enqueue_turn(
                thread_id=recipient_thread_id,
                project_id=project.id,
                message=text,
                sandbox=binding.sandbox,
                approval_policy=binding.approval_policy,
                model=effective_model,
                reasoning_effort=effective_reasoning_effort,
                work_item_ref=work_item_ref,
                **({"handoff_requested_at": handoff_requested_at} if handoff_requested_at is not None else {}),
                repository_resource_id=repository_resource_id,
                writable_repository_resource_ids=writable_repository_resource_ids,
                read_only_repository_resource_ids=read_only_repository_resource_ids,
                **profile_kwargs,
                source=source,
                reply_target=reply_target,
                **({"provider_notification_pin":provider_notification_pin} if provider_notification_pin is not None else {}),
                **({'owner_routing_pin': owner_routing_pin} if owner_routing_pin is not None else {}),
            )
            if getattr(owner_routing_pin, 'recipient', None) is None:
                binding.updated_at = time.time()
                await asyncio.to_thread(self.bindings.upsert, binding)
            payload: dict[str, Any] = {
                "type": event_type,
                "source": source,
                "thread_id": recipient_thread_id,
                "external_conversation_id": (
                    binding.external_conversation_id
                ),
                "queued_id": queued.id,
                "queue_depth": self.queue_policy.depth(
                    recipient_thread_id
                ),
            }
            if reason:
                payload["reason"] = str(reason)[:500]
            self.telemetry.append(payload)
            await self.execution.publish_queue_status(
                recipient_thread_id
            )
            await self.publish_event(
                {
                    "type": "bot.inbound",
                    "provider": source,
                    "externalConversationId": (
                        binding.external_conversation_id
                    ),
                    "threadId": recipient_thread_id,
                    "queued": True,
                    "queuedId": queued.id,
                    "queueDepth": self.queue_policy.depth(
                        recipient_thread_id
                    ),
                }
            )
            return {
                "ok": True,
                "queued": True,
                "threadId": recipient_thread_id,
                "queuedId": queued.id,
                "dispatchScope": dispatch_scope,
            }

        if owner_routing_pin is not None or provider_notification_pin is not None:
            # Recipient-pinned inputs need the durable retained claim and the
            # synchronous guarded native transport, including an idle thread.
            if require_idle:
                recipient_active = self.execution.thread_is_active(recipient_thread_id)
                if recipient_active or self.queue_policy.depth(recipient_thread_id):
                    return {
                        'ok': True, 'queued': False,
                        'skipped': ('already_active' if recipient_active else
                            TurnQueuePolicy.work_item_queue_outcome(
                                self.queue_policy.queue(recipient_thread_id),
                                work_item_ref=work_item_ref, project_id=project.id,
                                message=text, repository_resource_id=repository_resource_id,
                                writable_repository_resource_ids=writable_repository_resource_ids,
                                read_only_repository_resource_ids=read_only_repository_resource_ids,
                                handoff_requested_at=handoff_requested_at,
                            )),
                        'threadId': recipient_thread_id,
                        'dispatchScope': dispatch_scope,
                    }
            result = await queue_turn('event_turn_queued_owner_routing' if owner_routing_pin is not None
                                      else 'event_turn_queued_provider_notification')
            self.execution.schedule_queue_drain(recipient_thread_id)
            return result

        self.recovery.release_stale_active_turn(
            binding.thread_id,
            f"{source}:dispatch",
        )
        thread_active = self.execution.thread_is_active(binding.thread_id)
        queue_depth = self.queue_policy.depth(binding.thread_id)
        if thread_active or queue_depth:
            if require_idle:
                return {
                    "ok": True,
                    "queued": False,
                    "skipped": (
                        "already_active"
                        if thread_active
                        else TurnQueuePolicy.work_item_queue_outcome(
                            self.queue_policy.queue(binding.thread_id),
                            work_item_ref=work_item_ref, project_id=project.id,
                            message=text,
                            repository_resource_id=repository_resource_id,
                            writable_repository_resource_ids=writable_repository_resource_ids,
                            read_only_repository_resource_ids=read_only_repository_resource_ids,
                            handoff_requested_at=handoff_requested_at,
                        )
                    ),
                    "threadId": binding.thread_id,
                    "dispatchScope": dispatch_scope,
                }
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
                work_item_ref=work_item_ref,
                **({"handoff_requested_at": handoff_requested_at} if handoff_requested_at is not None else {}),
                repository_resource_id=repository_resource_id,
                writable_repository_resource_ids=writable_repository_resource_ids,
                read_only_repository_resource_ids=read_only_repository_resource_ids,
                actor=profile_actor,
                **profile_kwargs,
            )
        except Exception as exc:
            if _concurrent_start_refusal(exc, binding.thread_id):
                return await _retain_concurrent_request(
                    binding.thread_id, require_idle, queue_turn, dispatch_scope)
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
            recipient_thread_id = binding.thread_id
            project = self.projects.get(binding.project_id)
            settings = self.settings.get(binding.thread_id)
            effective_model = settings.model or project.model
            effective_reasoning_effort = settings.reasoning_effort
            reply_target = (
                await asyncio.to_thread(self.targets.event_reply_target, binding)
                or self.targets.conversation_target(binding)
            )
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
                    work_item_ref=work_item_ref,
                    **({"handoff_requested_at": handoff_requested_at} if handoff_requested_at is not None else {}),
                    repository_resource_id=repository_resource_id,
                    writable_repository_resource_ids=writable_repository_resource_ids,
                    read_only_repository_resource_ids=read_only_repository_resource_ids,
                    actor=profile_actor,
                    **profile_kwargs,
                )
            except Exception as replacement_error:
                if not _concurrent_start_refusal(replacement_error, binding.thread_id):
                    raise
                return await _retain_concurrent_request(
                    binding.thread_id, require_idle, queue_turn, dispatch_scope)

        binding.updated_at = time.time()
        await asyncio.to_thread(self.bindings.upsert, binding)
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
            "dispatchScope": dispatch_scope,
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
        *,
        work_item_ref: str | None = None,
        repository_resource_id: str | None = None,
        writable_repository_resource_ids: tuple[str, ...] = (),
        read_only_repository_resource_ids: tuple[str, ...] = (),
        require_idle: bool = False,
    ) -> dict[str, Any]:
        host = self.host
        project = host._project(binding.project_id)
        settings = host._thread_run_settings(binding.thread_id)
        effective_model = settings.model or project.model
        effective_reasoning_effort = settings.reasoning_effort
        captured_target_for = getattr(host, "_event_reply_target_for_binding", None)
        reply_target = (
            (await asyncio.to_thread(captured_target_for, binding)
             if callable(captured_target_for) else None)
            or host._conversation_target_for_binding(binding)
        )

        async def queue_binding_turn(
            event_type: str,
            reason: str | None = None,
        ) -> dict[str, Any]:
            duplicate = host._find_duplicate_queued_turn(
                binding.thread_id,
                message=text,
                source=source,
                project_id=project.id,
                sandbox=binding.sandbox,
                approval_policy=binding.approval_policy,
                model=effective_model,
                reasoning_effort=effective_reasoning_effort,
                work_item_ref=work_item_ref,
                repository_resource_id=repository_resource_id,
                writable_repository_resource_ids=writable_repository_resource_ids,
                read_only_repository_resource_ids=read_only_repository_resource_ids,
                reply_target=reply_target,
                handoff_requested_at=None,
                execution_profile_id=None,
                agent_profile_id=None,
                agent_profile_revision=None,
                agent_profile_actor_id=None,
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
                work_item_ref=work_item_ref,
                repository_resource_id=repository_resource_id,
                writable_repository_resource_ids=writable_repository_resource_ids,
                read_only_repository_resource_ids=read_only_repository_resource_ids,
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
        thread_active = host._thread_is_active(binding.thread_id)
        queue_depth = host._thread_queue_depth(binding.thread_id)
        if thread_active or queue_depth:
            if require_idle:
                return {
                    "ok": True,
                    "queued": False,
                    "skipped": (
                        "already_active"
                        if thread_active
                        else TurnQueuePolicy.work_item_queue_outcome(
                            host._thread_queue(binding.thread_id) if hasattr(host, '_thread_queue') else None,
                            work_item_ref=work_item_ref, project_id=project.id,
                            message=text,
                            repository_resource_id=repository_resource_id,
                            writable_repository_resource_ids=writable_repository_resource_ids,
                            read_only_repository_resource_ids=read_only_repository_resource_ids,
                        )
                    ),
                    "threadId": binding.thread_id,
                }
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
                work_item_ref=work_item_ref,
                repository_resource_id=repository_resource_id,
                writable_repository_resource_ids=writable_repository_resource_ids,
                read_only_repository_resource_ids=read_only_repository_resource_ids,
            )
        except Exception as exc:
            if _concurrent_start_refusal(exc, binding.thread_id):
                return await _retain_concurrent_request(
                    binding.thread_id, require_idle, queue_binding_turn, None)
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
            captured_target_for = getattr(host, "_event_reply_target_for_binding", None)
            reply_target = (
                (await asyncio.to_thread(captured_target_for, binding)
                 if callable(captured_target_for) else None)
                or host._conversation_target_for_binding(binding)
            )
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
                    work_item_ref=work_item_ref,
                    repository_resource_id=repository_resource_id,
                    writable_repository_resource_ids=writable_repository_resource_ids,
                    read_only_repository_resource_ids=read_only_repository_resource_ids,
                )
            except Exception as replacement_error:
                if not _concurrent_start_refusal(replacement_error, binding.thread_id):
                    raise
                return await _retain_concurrent_request(
                    binding.thread_id, require_idle, queue_binding_turn, None)

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
