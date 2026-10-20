from __future__ import annotations

import asyncio
import contextlib
import re
import time
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

from codex_web.services.conversation_channels import ConversationChannelReconciliationError
from codex_web.models import BotInboundMessage, BotRouteTest, ThreadRunSettings
from codex_web.paths import SLACK_RELAY_NOTICE
from codex_web.runtime.execution import TurnExecutionService
from codex_web.services.bot_binding_selection import BotBindingSelectionService
from codex_web.services.bot_bindings import BotBindingLifecycleService
from codex_web.services.bot_connections import BotConnectionService
from codex_web.services.bot_delivery import BotDeliveryService
from codex_web.services.bot_presentation import BotPresentationService
from codex_web.services.bot_runtime_telemetry import BotRuntimeTelemetry
from codex_web.services.bot_targets import BotTargetService
from codex_web.services.project_runtime import ProjectRuntimeService
from codex_web.services.thread_execution_settings import (
    ThreadExecutionSettingsService,
)
from codex_web.services.thread_recovery import ThreadRecoveryService
from codex_web.services.thread_resume import ThreadResumeService
from codex_web.services.turn_queue_policy import TurnQueuePolicy


class BotRoutingService:
    """Own inbound bot routing, queueing and stale-thread recovery decisions."""

    def __init__(
        self,
        host: Any | None = None,
        *,
        delivery: BotDeliveryService,
        connections: BotConnectionService | Any | None = None,
        bindings: BotBindingSelectionService | Any | None = None,
        binding_lifecycle: BotBindingLifecycleService | Any | None = None,
        targets: BotTargetService | Any | None = None,
        presentation: BotPresentationService | Any | None = None,
        telemetry: BotRuntimeTelemetry | Any | None = None,
        projects: ProjectRuntimeService | Any | None = None,
        settings: ThreadExecutionSettingsService | Any | None = None,
        recovery: ThreadRecoveryService | Any | None = None,
        resume: ThreadResumeService | Any | None = None,
        queue_policy: TurnQueuePolicy | Any | None = None,
        execution: TurnExecutionService | Any | None = None,
        publish_event: Callable[[dict[str, Any]], Awaitable[object]] | None = None,
    ) -> None:
        async def _publish_noop(_event: dict[str, Any]) -> None:
            return None

        if host is not None:
            bindings = bindings or SimpleNamespace(
                for_connection=getattr(
                    host,
                    "_bindings_for_connection",
                    lambda _provider, _conversation: [],
                ),
                for_project=getattr(
                    host,
                    "_bindings_for_project",
                    lambda _provider, _project_id: [],
                ),
            )
            binding_lifecycle = binding_lifecycle or SimpleNamespace(
                has_single_master_binding=getattr(
                    host,
                    "_has_single_master_binding",
                    lambda _bindings: False,
                ),
                is_top_level_external_message=getattr(
                    host,
                    "_is_top_level_external_message",
                    lambda _message: True,
                ),
                for_external_target=lambda *_args, **_kwargs: None,
                resolve=getattr(
                    host,
                    "_resolve_bot_binding",
                    lambda _bindings, message, **_kwargs: (
                        None,
                        message.text,
                        True,
                    ),
                ),
                cross_channel_for_message=getattr(
                    host,
                    "_cross_channel_binding_for_message",
                    lambda _provider, _project_id, _bindings, message, **_kwargs: (
                        None,
                        message.text,
                        False,
                    ),
                ),
            )
            presentation = presentation or SimpleNamespace(
                binding_prefix=getattr(
                    host,
                    "_binding_prefix",
                    lambda binding: (
                        getattr(binding, "route_prefix", None)
                        or getattr(binding, "thread_id", "")
                    ),
                )
            )
            telemetry = telemetry or SimpleNamespace(
                append=getattr(host, "_append_bot_event", lambda _event: None)
            )

        self.delivery = delivery
        self.connections = connections or SimpleNamespace()
        self.bindings = bindings or SimpleNamespace(
            for_connection=lambda _provider, _conversation: [],
            for_project=lambda _provider, _project_id: [],
        )
        self.binding_lifecycle = binding_lifecycle or SimpleNamespace()
        self.targets = targets or SimpleNamespace()
        self.presentation = presentation or SimpleNamespace()
        self.telemetry = telemetry or SimpleNamespace(append=lambda _event: None)
        self.projects = projects or SimpleNamespace()
        self.settings = settings or SimpleNamespace()
        self.recovery = recovery or SimpleNamespace()
        self.resume = resume or SimpleNamespace()
        self.queue_policy = queue_policy or SimpleNamespace()
        self.execution = execution or SimpleNamespace()
        self.publish_event = publish_event or _publish_noop
        self.conversation_channels: Any | None = None

    @staticmethod
    def _steer_route_message(
        message: BotInboundMessage,
    ) -> tuple[bool, BotInboundMessage]:
        match = re.match(
            r"^\s*steer(?:\s+|:\s*)(.+)$",
            message.text,
            re.IGNORECASE | re.DOTALL,
        )
        if not match:
            return False, message
        routed_text = match.group(1).strip()
        if not routed_text:
            return False, message
        return True, message.model_copy(update={"text": routed_text})

    @staticmethod
    def _with_relay_guard(message: str, source: str | None) -> str:
        normalized_source = (source or "").lower()
        if "slack" not in normalized_source:
            return message
        if SLACK_RELAY_NOTICE in message:
            return message
        return f"{SLACK_RELAY_NOTICE}\n\n{message}"

    def preview(self, payload: BotRouteTest) -> dict[str, Any]:
        message = BotInboundMessage(**payload.model_dump())
        provider = message.provider.lower()
        if provider not in {"slack", "telegram"}:
            from fastapi import HTTPException

            raise HTTPException(
                status_code=400,
                detail="Provider must be slack or telegram",
            )
        bindings = self.bindings.for_connection(
            provider,
            message.external_conversation_id,
        )
        project_id = message.project_id or (
            bindings[0].project_id if bindings else "home"
        )
        self.projects.get(project_id)
        steer_now, route_message = self._steer_route_message(message)
        binding = None
        routed_text = route_message.text
        route_error = False
        route_source = "none"
        would_clone = False
        master_catch_all = (
            not steer_now
            and self.binding_lifecycle.has_single_master_binding(bindings)
        )
        prefer_external_thread = (
            not steer_now
            and not master_catch_all
            and not self.binding_lifecycle.is_top_level_external_message(
                message
            )
        )
        exact_binding = (
            None
            if not prefer_external_thread
            else self.binding_lifecycle.for_external_target(
                provider,
                project_id,
                message.external_conversation_id,
                message.external_thread_id,
            )
        )
        if exact_binding is not None:
            binding = exact_binding
            would_clone = (
                exact_binding.external_conversation_id
                != message.external_conversation_id
            )
            route_source = "external-thread"
        else:
            binding, routed_text, route_error = (
                self.binding_lifecycle.resolve(
                    bindings,
                    route_message,
                    prefer_external_thread=prefer_external_thread,
                    allow_master_fallback=not steer_now,
                    allow_bare_prefix=steer_now,
                )
            )
            if binding:
                route_source = "connection-prefix-or-primary"

        if binding is None:
            cross_binding, cross_text, cross_ambiguous = (
                self.binding_lifecycle.cross_channel_for_message(
                    provider,
                    project_id,
                    bindings,
                    route_message,
                    allow_bare_prefix=steer_now,
                )
            )
            if cross_binding is not None:
                binding = cross_binding
                routed_text = cross_text
                route_error = False
                would_clone = (
                    cross_binding.external_conversation_id
                    != message.external_conversation_id
                )
                route_source = "cross-channel-prefix-or-primary"
            elif cross_ambiguous:
                route_error = True

        available_prefixes = [
            self.presentation.binding_prefix(item)
            for item in bindings
            if self.presentation.binding_prefix(item)
        ]
        if not available_prefixes:
            available_prefixes = [
                self.presentation.binding_prefix(item)
                for item in self.bindings.for_project(provider, project_id)
                if self.presentation.binding_prefix(item)
            ]

        if route_error:
            return {
                "ok": False,
                "ambiguous": True,
                "projectId": project_id,
                "provider": provider,
                "externalConversationId": (
                    message.external_conversation_id
                ),
                "availablePrefixes": sorted(set(available_prefixes)),
                "steer": steer_now,
            }

        if binding is None:
            return {
                "ok": True,
                "wouldCreateThread": True,
                "projectId": project_id,
                "provider": provider,
                "externalConversationId": (
                    message.external_conversation_id
                ),
                "routedText": routed_text,
                "routeSource": route_source,
                "steer": steer_now,
            }

        active = self.execution.thread_is_active(binding.thread_id)
        queue_depth = self.queue_policy.depth(binding.thread_id)
        return {
            "ok": True,
            "wouldCreateThread": False,
            "wouldCloneBinding": would_clone,
            "projectId": binding.project_id,
            "provider": provider,
            "externalConversationId": message.external_conversation_id,
            "bindingId": binding.id,
            "threadId": binding.thread_id,
            "threadName": binding.thread_name,
            "prefix": self.presentation.binding_prefix(binding),
            "routedText": routed_text,
            "routeSource": route_source,
            "steer": steer_now,
            "active": active,
            "queueDepth": queue_depth,
            "wouldQueue": (
                not steer_now and (active or queue_depth > 0)
            ),
        }

    async def handle_inbound(
        self,
        message: BotInboundMessage,
    ) -> dict[str, Any]:
        if self.conversation_channels is None:
            return await self.route_normalized(message)
        provider = message.provider.lower()
        bindings = self.bindings.for_connection(
            provider,
            message.external_conversation_id,
        )
        project_id = message.project_id or (
            bindings[0].project_id if bindings else "home"
        )
        scoped = message.model_copy(update={"project_id": project_id})
        actor = self.connections.runtime_actor(project_id)
        receipt = await self.conversation_channels.ingest_bot_message(
            scoped,
            actor=actor,
            provider_instance=(
                message.connection_id or f"{provider}:default"
            ),
        )
        return self.conversation_channels.legacy_routing_result(receipt)

    async def route_normalized(
        self,
        message: BotInboundMessage,
    ) -> dict[str, Any]:
        if message.canonical_ingress_id and message._ingress_authority is None:
            raise ConversationChannelReconciliationError("captured_ingress_authority_unavailable")
        provider = message.provider.lower()
        bindings = self.bindings.for_connection(
            provider,
            message.external_conversation_id,
        )
        project_id = message.project_id or (
            bindings[0].project_id if bindings else "home"
        )
        steer_now, route_message = self._steer_route_message(message)
        master_catch_all = (
            not steer_now
            and self.binding_lifecycle.has_single_master_binding(bindings)
        )
        prefer_external_thread = (
            not steer_now
            and not master_catch_all
            and not self.binding_lifecycle.is_top_level_external_message(
                route_message
            )
        )
        exact_binding = (
            None
            if not prefer_external_thread
            else self.binding_lifecycle.for_external_target(
                provider,
                project_id,
                message.external_conversation_id,
                message.external_thread_id,
            )
        )
        if exact_binding is not None:
            binding = (
                exact_binding
                if exact_binding.external_conversation_id
                == message.external_conversation_id
                else self.binding_lifecycle.clone_for_conversation(
                    exact_binding,
                    message,
                )
            )
            routed_text = route_message.text
            route_error = False
        else:
            binding, routed_text, route_error = self.binding_lifecycle.resolve(
                bindings,
                route_message,
                prefer_external_thread=prefer_external_thread,
                allow_master_fallback=not steer_now,
                allow_bare_prefix=steer_now,
            )
        if binding is None:
            (
                cross_binding,
                cross_text,
                cross_ambiguous,
            ) = self.binding_lifecycle.cross_channel_for_message(
                provider,
                project_id,
                bindings,
                route_message,
                allow_bare_prefix=steer_now,
            )
            if cross_binding is not None:
                binding = (
                    cross_binding
                    if cross_binding.external_conversation_id
                    == message.external_conversation_id
                    else self.binding_lifecycle.clone_for_conversation(
                        cross_binding,
                        message,
                    )
                )
                routed_text = cross_text
                route_error = False
            elif cross_ambiguous:
                route_error = True
        if route_error:
            available_prefixes = [
                self.presentation.binding_prefix(item)
                for item in bindings
            ]
            if not available_prefixes:
                available_prefixes = [
                    self.presentation.binding_prefix(item)
                    for item in self.bindings.for_project(
                        provider,
                        project_id,
                    )
                    if self.presentation.binding_prefix(item)
                ]
            self.telemetry.append(
                {
                    "type": "inbound_ambiguous",
                    "provider": provider,
                    "external_conversation_id": (
                        message.external_conversation_id
                    ),
                    "message_id": message.message_id,
                    "available_prefixes": available_prefixes,
                }
            )
            return {
                "ok": False,
                "ambiguous": True,
                "availablePrefixes": available_prefixes,
            }
        captured_default = None
        if binding is None:
            from codex_web.models import BotBindingCreate

            captured_default = message._ingress_authority.get("new_binding") if message.canonical_ingress_id else None
            if message.canonical_ingress_id and captured_default is None:
                raise ConversationChannelReconciliationError(message._ingress_authority.get(
                    "new_binding_error", "captured_auto_binding_scope_unavailable"))

            binding = await self.binding_lifecycle.start(
                BotBindingCreate(
                    **(captured_default["binding"] if captured_default else {}),
                    connection_id=message.connection_id,
                    provider=provider,
                    external_conversation_id=(
                        message.external_conversation_id
                    ),
                    project_id=project_id,
                    external_name=message.external_name,
                )
            )
            routed_text = route_message.text
        elif message.connection_id and not binding.connection_id:
            binding.connection_id = message.connection_id
            binding.updated_at = time.time()
            binding = self.binding_lifecycle.upsert(binding)

        project = self.projects.get(binding.project_id)
        settings = self.settings.get(binding.thread_id)
        captured = None
        if message.canonical_ingress_id:
            captured = next((row for row in message._ingress_authority.get("routing", ())
                if row["binding"]["thread_id"] == binding.thread_id and row["binding"]["project_id"] == project.id), None)
            if captured is None:
                captured = captured_default
            if captured is None:
                raise ConversationChannelReconciliationError("selected_binding_authority_unavailable")
            # Scope comes from the protected original, never from settings that
            # changed while this routing coroutine awaited another boundary.
            settings = ThreadRunSettings.model_validate(captured["settings"])
            binding = binding.model_copy(update={"sandbox": captured["binding"]["sandbox"],
                "approval_policy": captured["binding"]["approval_policy"]})
        effective_model = settings.model or project.model
        effective_reasoning_effort = settings.reasoning_effort
        if not routed_text.strip():
            return {
                "ok": False,
                "empty": True,
                "threadId": binding.thread_id,
            }
        reply_target = self.targets.remember_reply_target(binding, message)
        if self.presentation.is_details_command(routed_text):
            delivery = (await self.delivery.admit_details(binding, message)
                if message.canonical_ingress_id else await self.delivery.send_details(binding))
            return {
                "ok": True,
                "threadId": binding.thread_id,
                "details": True,
                "delivery": delivery,
                "actionIntentId": delivery.get("actionIntentId"),
                "actionIntentStatus": delivery.get("status"),
            }
        prompt = self.presentation.format_prompt(
            message,
            provider,
            routed_text,
        )
        if steer_now and self.execution.thread_is_active(binding.thread_id):
            if message.canonical_ingress_id:
                # A failed interrupt may have taken effect. Keep the durable
                # claim uncertain; do not start another turn or replay it.
                await self.execution.request_for_thread(
                    binding.thread_id,
                    "turn/interrupt",
                    {"threadId": binding.thread_id},
                )
            else:
                with contextlib.suppress(Exception):
                    await self.execution.request_for_thread(binding.thread_id, "turn/interrupt", {"threadId": binding.thread_id})
            self.execution.clear_thread_active(binding.thread_id)
        if not steer_now:
            self.recovery.release_stale_active_turn(
                binding.thread_id,
                f"{provider}:inbound",
            )

        async def queue_inbound_turn(
            event_type: str,
            reason: str | None = None,
        ) -> dict[str, Any]:
            ingress_scope = {}
            if message.canonical_ingress_id:
                profile = captured.get("profile") or {}
                from codex_web.definitions import DefinitionReference
                ingress_scope = {
                    "ingress_execution_profile_definition": DefinitionReference.model_validate(captured["definition"]),
                    "repository_resource_id": settings.repository_resource_id,
                    "writable_repository_resource_ids": settings.writable_repository_resource_ids,
                    "read_only_repository_resource_ids": settings.read_only_repository_resource_ids,
                    "execution_profile_id": profile.get("execution_profile_id") or captured["execution_profile"]["id"],
                    "agent_profile_id": profile.get("profile_id"),
                    "agent_profile_revision": profile.get("revision"),
                    "agent_profile_actor_id": message._ingress_actor_id,
                }
                if captured["execution_profile"]["repository_access"] == "none":
                    ingress_scope.update(repository_resource_id=None, writable_repository_resource_ids=(), read_only_repository_resource_ids=())
            queued = self.execution.enqueue_turn(
                **({"admit_first": True} if message.canonical_ingress_id and steer_now else {}),
                **ingress_scope,
                **({"canonical_ingress_id": message.canonical_ingress_id} if message.canonical_ingress_id else {}),
                thread_id=binding.thread_id,
                project_id=project.id,
                message=prompt,
                sandbox=binding.sandbox,
                approval_policy=binding.approval_policy,
                model=effective_model,
                reasoning_effort=effective_reasoning_effort,
                source=f"steer:{provider}" if steer_now else provider,
                reply_target=reply_target,
            )
            binding.updated_at = time.time()
            self.binding_lifecycle.upsert(binding)
            queue_depth = self.queue_policy.depth(binding.thread_id)
            event_payload: dict[str, Any] = {
                "type": event_type,
                "provider": provider,
                "external_conversation_id": (
                    message.external_conversation_id
                ),
                "message_id": message.message_id,
                "thread_id": binding.thread_id,
                "queued_id": queued.id,
                "queue_depth": queue_depth,
            }
            if reason:
                event_payload["reason"] = (
                    self.presentation.truncate_text(reason, 500)
                )
            self.telemetry.append(event_payload)
            await self.execution.publish_queue_status(binding.thread_id)
            if not (message.canonical_ingress_id and steer_now) and not self.execution.thread_is_active(binding.thread_id):
                asyncio.get_running_loop().call_later(
                    5,
                    self.execution.schedule_queue_drain,
                    binding.thread_id,
                )
            await self.publish_event(
                {
                    "type": "bot.inbound",
                    "provider": provider,
                    "externalConversationId": (
                        message.external_conversation_id
                    ),
                    "threadId": binding.thread_id,
                    "text": routed_text,
                    "senderId": message.sender_id,
                    "senderName": message.sender_name,
                    "messageId": message.message_id,
                    "queued": True,
                    "queuedId": queued.id,
                    "queueDepth": queue_depth,
                }
            )
            return {
                "ok": True,
                "queued": True,
                "threadId": binding.thread_id,
                "queuedId": queued.id,
                "queueDepth": queue_depth,
            }

        if message.canonical_ingress_id:
            result = await queue_inbound_turn("inbound_turn_steered" if steer_now else "inbound_turn_queued")
            if steer_now:
                # The canonical drain persists the selected queue snapshot and
                # preflight/start claim before invoking the worker. It carries
                # the captured per-turn profile/actor/repository scope and owns
                # unknown native-start outcomes; never use the raw RPC path.
                await self.execution.drain_thread_queue(binding.thread_id)
                result["steered"] = True
            return result
        if not steer_now and (
            self.execution.thread_is_active(binding.thread_id)
            or self.queue_policy.depth(binding.thread_id)
        ):
            return await queue_inbound_turn("inbound_turn_queued")

        turn: dict[str, Any] | None = None
        for attempt in range(1 if message.canonical_ingress_id else 3):
            try:
                await self.execution.request_for_thread(
                    binding.thread_id,
                    "thread/resume",
                    {
                        "threadId": binding.thread_id,
                        **self.projects.params(
                            project,
                            {
                                "sandbox": binding.sandbox,
                                "approvalPolicy": binding.approval_policy,
                            },
                        ),
                    },
                )
                turn_params: dict[str, Any] = {
                    "threadId": binding.thread_id,
                    "input": [
                        {
                            "type": "text",
                            "text": self._with_relay_guard(
                                prompt,
                                provider,
                            ),
                            "text_elements": [],
                        }
                    ],
                    "cwd": project.path,
                    "approvalPolicy": binding.approval_policy,
                    "sandboxPolicy": self.projects.sandbox_policy(
                        binding.sandbox,
                        project.path,
                    ),
                }
                if effective_model:
                    turn_params["model"] = effective_model
                if effective_reasoning_effort:
                    turn_params["effort"] = effective_reasoning_effort
                turn = await self.execution.request_for_thread(
                    binding.thread_id,
                    "turn/start",
                    turn_params,
                )
                break
            except Exception as exc:
                if message.canonical_ingress_id:
                    raise
                if self.resume.is_timeout_error(exc):
                    self.telemetry.append(
                        {
                            "type": "inbound_timeout",
                            "provider": provider,
                            "external_conversation_id": (
                                message.external_conversation_id
                            ),
                            "message_id": message.message_id,
                            "thread_id": binding.thread_id,
                            "steered": steer_now,
                            "error": self.presentation.truncate_text(
                                str(exc),
                                500,
                            ),
                        }
                    )
                    if not steer_now:
                        return await queue_inbound_turn(
                            "inbound_turn_queued_after_timeout",
                            str(exc),
                        )
                    return {
                        "ok": False,
                        "timedOut": True,
                        "threadId": binding.thread_id,
                        "steered": True,
                        "error": str(getattr(exc, "detail", exc)),
                    }
                if not self.resume.is_stale_thread_error(exc):
                    raise
                stale_thread_id = binding.thread_id
                self.targets.forget_reply_target(stale_thread_id)
                binding = await self.recovery.replace_stale_bot_thread(
                    binding,
                    str(exc),
                )
                if (
                    binding.external_conversation_id
                    != message.external_conversation_id
                ):
                    binding = (
                        self.binding_lifecycle.clone_for_conversation(
                            binding,
                            message,
                        )
                    )
                elif message.connection_id and not binding.connection_id:
                    binding.connection_id = message.connection_id
                    binding.updated_at = time.time()
                    binding = self.binding_lifecycle.upsert(binding)
                self.telemetry.append(
                    {
                        "type": "stale_binding_repair",
                        "provider": provider,
                        "external_conversation_id": (
                            message.external_conversation_id
                        ),
                        "old_thread_id": stale_thread_id,
                        "new_thread_id": binding.thread_id,
                        "error": self.presentation.truncate_text(
                            str(exc),
                            500,
                        ),
                    }
                )
                if attempt >= 2:
                    raise
                project = self.projects.get(binding.project_id)
                settings = self.settings.get(binding.thread_id)
                effective_model = settings.model or project.model
                effective_reasoning_effort = settings.reasoning_effort
                reply_target = (
                    self.targets.remember_reply_target(binding, message)
                    or reply_target
                )

        if message.canonical_ingress_id and not (turn.get("turn") or {}).get("id"):
            raise RuntimeError("native start has no authoritative turn receipt; reconciliation required")
        self.execution.mark_thread_active(
            binding.thread_id,
            turn_id=(
                (turn.get("turn") or {}).get("id")
                if isinstance(turn, dict)
                else None
            ),
            project_id=binding.project_id,
            sandbox=binding.sandbox,
            approval_policy=binding.approval_policy,
            model=effective_model,
            reasoning_effort=effective_reasoning_effort,
            source=f"steer:{provider}" if steer_now else provider,
            reply_target=reply_target,
        )
        binding.updated_at = time.time()
        self.binding_lifecycle.upsert(binding)
        self.telemetry.append(
            {
                "type": (
                    "inbound_turn_steered"
                    if steer_now
                    else "inbound_turn_started"
                ),
                "provider": provider,
                "external_conversation_id": (
                    message.external_conversation_id
                ),
                "message_id": message.message_id,
                "thread_id": binding.thread_id,
                "sender_id": message.sender_id,
                "steered": steer_now,
            }
        )
        await self.publish_event(
            {
                "type": "bot.inbound",
                "provider": provider,
                "externalConversationId": (
                    message.external_conversation_id
                ),
                "threadId": binding.thread_id,
                "text": routed_text,
                "senderId": message.sender_id,
                "senderName": message.sender_name,
                "messageId": message.message_id,
                "steered": steer_now,
            }
        )
        return {
            "ok": True,
            "threadId": binding.thread_id,
            "turn": turn,
            "steered": steer_now,
        }


def install_bot_routing_service(
    app: Any,
    host: Any,
    delivery: BotDeliveryService,
    *,
    connections=None,
    bindings=None,
    binding_lifecycle=None,
    targets=None,
    presentation=None,
    telemetry=None,
    projects=None,
    settings=None,
    recovery=None,
    resume=None,
    queue_policy=None,
    execution=None,
    publish_event=None,
) -> BotRoutingService:
    if (
        connections is None
        and not hasattr(app.state, "bot_connection_service")
    ):
        service = BotRoutingService(host, delivery=delivery)
    else:
        service = BotRoutingService(
            delivery=delivery,
            connections=connections or app.state.bot_connection_service,
            bindings=bindings or app.state.bot_binding_selection_service,
            binding_lifecycle=(
                binding_lifecycle or app.state.bot_binding_lifecycle_service
            ),
            targets=targets or app.state.bot_target_service,
            presentation=presentation or app.state.bot_presentation_service,
            telemetry=telemetry or app.state.bot_runtime_telemetry,
            projects=projects or app.state.project_runtime_service,
            settings=settings or app.state.thread_execution_settings_service,
            recovery=recovery or app.state.thread_recovery_service,
            resume=resume or app.state.thread_resume_service,
            queue_policy=queue_policy or app.state.turn_queue_policy,
            execution=execution or app.state.turn_execution_service,
            publish_event=publish_event or host.hub.publish,
        )
    app.state.bot_routing_service = service
    host._handle_bot_inbound = service.handle_inbound
    return service
