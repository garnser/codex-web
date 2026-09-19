from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from codex_web.conversation_channels import ConversationProjectionOutcome
from codex_web.models import BotBindingCreate, BotInboundMessage
from codex_web.services.bot_delivery import BotDeliveryService


class BotRoutingService:
    """Own inbound bot routing, queueing and stale-thread recovery decisions."""

    def __init__(self, host: Any, delivery: BotDeliveryService) -> None:
        self.host = host
        self.delivery = delivery
        self.conversation_channels: Any | None = None

    async def handle_inbound(self, message: BotInboundMessage) -> dict[str, Any]:
        if self.conversation_channels is None:
            return await self.route_normalized(message)
        h = self.host
        provider = message.provider.lower()
        bindings = h._bindings_for_connection(
            provider,
            message.external_conversation_id,
        )
        project_id = message.project_id or (
            bindings[0].project_id if bindings else "home"
        )
        scoped = message.model_copy(update={"project_id": project_id})
        actor = h._bot_runtime_actor(project_id)
        receipt = await self.conversation_channels.ingest_bot_message(
            scoped,
            actor=actor,
            provider_instance=message.connection_id or f"{provider}:default",
        )
        result: dict[str, Any] = dict(receipt.routing_result)
        result["conversationOutcome"] = receipt.outcome.value
        result["canonicalEventId"] = receipt.canonical_event_id
        if receipt.thread_id is not None:
            result.setdefault("threadId", receipt.thread_id)
        if receipt.queued_id is not None:
            result.setdefault("queuedId", receipt.queued_id)
        if receipt.outcome == ConversationProjectionOutcome.DUPLICATE:
            result["duplicate"] = True
            result.setdefault("ok", True)
        elif receipt.outcome == ConversationProjectionOutcome.REQUIRES_RECONCILIATION:
            result["ok"] = False
            result["requiresReconciliation"] = True
            result["error"] = receipt.reason
        elif receipt.outcome in {
            ConversationProjectionOutcome.STALE,
            ConversationProjectionOutcome.UPDATED,
            ConversationProjectionOutcome.DELETED,
            ConversationProjectionOutcome.REACTION,
            ConversationProjectionOutcome.IGNORED,
        }:
            result.setdefault("ok", True)
            result["routed"] = False
        return result

    async def route_normalized(self, message: BotInboundMessage) -> dict[str, Any]:
        h = self.host
        provider = message.provider.lower()
        bindings = h._bindings_for_connection(provider, message.external_conversation_id)
        project_id = message.project_id or (bindings[0].project_id if bindings else "home")
        steer_now, route_message = h._steer_route_message(message)
        master_catch_all = not steer_now and h._has_single_master_binding(bindings)
        prefer_external_thread = (
            not steer_now
            and not master_catch_all
            and not h._is_top_level_external_message(route_message)
        )
        exact_binding = None if not prefer_external_thread else h._binding_for_external_target(
            provider,
            project_id,
            message.external_conversation_id,
            message.external_thread_id,
        )
        if exact_binding is not None:
            binding = (
                exact_binding
                if exact_binding.external_conversation_id == message.external_conversation_id
                else h._clone_binding_for_conversation(exact_binding, message)
            )
            routed_text = route_message.text
            route_error = False
        else:
            binding, routed_text, route_error = h._resolve_bot_binding(
                bindings,
                route_message,
                prefer_external_thread=prefer_external_thread,
                allow_master_fallback=not steer_now,
                allow_bare_prefix=steer_now,
            )
        if binding is None:
            cross_binding, cross_text, cross_ambiguous = h._cross_channel_binding_for_message(
                provider,
                project_id,
                bindings,
                route_message,
                allow_bare_prefix=steer_now,
            )
            if cross_binding is not None:
                binding = (
                    cross_binding
                    if cross_binding.external_conversation_id == message.external_conversation_id
                    else h._clone_binding_for_conversation(cross_binding, message)
                )
                routed_text = cross_text
                route_error = False
            elif cross_ambiguous:
                route_error = True
        if route_error:
            available_prefixes = [h._binding_prefix(item) for item in bindings]
            if not available_prefixes:
                available_prefixes = [
                    h._binding_prefix(item)
                    for item in h._bindings_for_project(provider, project_id)
                    if h._binding_prefix(item)
                ]
            h._append_bot_event(
                {
                    "type": "inbound_ambiguous",
                    "provider": provider,
                    "external_conversation_id": message.external_conversation_id,
                    "message_id": message.message_id,
                    "available_prefixes": available_prefixes,
                }
            )
            return {
                "ok": False,
                "ambiguous": True,
                "availablePrefixes": available_prefixes,
            }
        if binding is None:
            binding = await h._start_bot_thread(
                BotBindingCreate(
                    connection_id=message.connection_id,
                    provider=provider,
                    external_conversation_id=message.external_conversation_id,
                    project_id=project_id,
                    external_name=message.external_name,
                )
            )
            routed_text = route_message.text
        elif message.connection_id and not binding.connection_id:
            binding.connection_id = message.connection_id
            binding.updated_at = time.time()
            binding = h._upsert_bot_binding(binding)

        project = h._project(binding.project_id)
        settings = h._thread_run_settings(binding.thread_id)
        effective_model = settings.model or project.model
        effective_reasoning_effort = settings.reasoning_effort
        if not routed_text.strip():
            return {"ok": False, "empty": True, "threadId": binding.thread_id}
        reply_target = h._remember_bot_reply_target(binding, message)
        if h._is_details_command(routed_text):
            delivery = await self.delivery.send_details(binding)
            return {
                "ok": True,
                "threadId": binding.thread_id,
                "details": True,
                "delivery": delivery,
            }
        prompt = h._format_bot_prompt(message, provider, routed_text)
        if steer_now and h._thread_is_active(binding.thread_id):
            with contextlib.suppress(Exception):
                await h.codex.request("turn/interrupt", {"threadId": binding.thread_id})
            h._clear_thread_active(binding.thread_id)
        if not steer_now:
            h._release_stale_active_turn(binding.thread_id, f"{provider}:inbound")

        async def queue_inbound_turn(event_type: str, reason: str | None = None) -> dict[str, Any]:
            queued = h._enqueue_turn(
                thread_id=binding.thread_id,
                project_id=project.id,
                message=prompt,
                sandbox=binding.sandbox,
                approval_policy=binding.approval_policy,
                model=effective_model,
                reasoning_effort=effective_reasoning_effort,
                source=provider,
                reply_target=reply_target,
            )
            binding.updated_at = time.time()
            h._upsert_bot_binding(binding)
            queue_depth = h._thread_queue_depth(binding.thread_id)
            event_payload: dict[str, Any] = {
                "type": event_type,
                "provider": provider,
                "external_conversation_id": message.external_conversation_id,
                "message_id": message.message_id,
                "thread_id": binding.thread_id,
                "queued_id": queued.id,
                "queue_depth": queue_depth,
            }
            if reason:
                event_payload["reason"] = h._truncate_text(reason, 500)
            h._append_bot_event(event_payload)
            await h._publish_queue_status(binding.thread_id)
            if not h._thread_is_active(binding.thread_id):
                asyncio.get_running_loop().call_later(5, h._schedule_queue_drain, binding.thread_id)
            await h.hub.publish(
                {
                    "type": "bot.inbound",
                    "provider": provider,
                    "externalConversationId": message.external_conversation_id,
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

        if not steer_now and (
            h._thread_is_active(binding.thread_id)
            or h._thread_queue_depth(binding.thread_id)
        ):
            return await queue_inbound_turn("inbound_turn_queued")

        turn: dict[str, Any] | None = None
        for attempt in range(3):
            try:
                await h.codex.request(
                    "thread/resume",
                    {
                        "threadId": binding.thread_id,
                        **h._project_params(
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
                            "text": h._with_relay_guard(prompt, provider),
                            "text_elements": [],
                        }
                    ],
                    "cwd": project.path,
                    "approvalPolicy": binding.approval_policy,
                    "sandboxPolicy": h._sandbox_policy(binding.sandbox, project.path),
                }
                if effective_model:
                    turn_params["model"] = effective_model
                if effective_reasoning_effort:
                    turn_params["effort"] = effective_reasoning_effort
                turn = await h.codex.request("turn/start", turn_params)
                h.THREAD_LAST_INPUTS[binding.thread_id] = {
                    "project_id": project.id,
                    "message": prompt,
                    "sandbox": binding.sandbox,
                    "approval_policy": binding.approval_policy,
                    "model": effective_model,
                    "reasoning_effort": effective_reasoning_effort,
                    "source": f"steer:{provider}" if steer_now else provider,
                    "reply_target": reply_target,
                }
                break
            except Exception as exc:
                if h._is_codex_timeout_error(exc):
                    h._append_bot_event(
                        {
                            "type": "inbound_timeout",
                            "provider": provider,
                            "external_conversation_id": message.external_conversation_id,
                            "message_id": message.message_id,
                            "thread_id": binding.thread_id,
                            "steered": steer_now,
                            "error": h._truncate_text(str(exc), 500),
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
                if not h._is_stale_thread_error(exc):
                    raise
                stale_thread_id = binding.thread_id
                h._forget_bot_reply_target(stale_thread_id)
                binding = await h._replace_stale_bot_thread(binding, str(exc))
                if binding.external_conversation_id != message.external_conversation_id:
                    binding = h._clone_binding_for_conversation(binding, message)
                elif message.connection_id and not binding.connection_id:
                    binding.connection_id = message.connection_id
                    binding.updated_at = time.time()
                    binding = h._upsert_bot_binding(binding)
                h._append_bot_event(
                    {
                        "type": "stale_binding_repair",
                        "provider": provider,
                        "external_conversation_id": message.external_conversation_id,
                        "old_thread_id": stale_thread_id,
                        "new_thread_id": binding.thread_id,
                        "error": h._truncate_text(str(exc), 500),
                    }
                )
                if attempt >= 2:
                    raise
                project = h._project(binding.project_id)
                settings = h._thread_run_settings(binding.thread_id)
                effective_model = settings.model or project.model
                effective_reasoning_effort = settings.reasoning_effort
                reply_target = h._remember_bot_reply_target(binding, message) or reply_target

        h._mark_thread_active(
            binding.thread_id,
            turn_id=(turn.get("turn") or {}).get("id") if isinstance(turn, dict) else None,
            project_id=binding.project_id,
            sandbox=binding.sandbox,
            approval_policy=binding.approval_policy,
            model=effective_model,
            reasoning_effort=effective_reasoning_effort,
            source=f"steer:{provider}" if steer_now else provider,
            reply_target=reply_target,
        )
        binding.updated_at = time.time()
        h._upsert_bot_binding(binding)
        h._append_bot_event(
            {
                "type": "inbound_turn_steered" if steer_now else "inbound_turn_started",
                "provider": provider,
                "external_conversation_id": message.external_conversation_id,
                "message_id": message.message_id,
                "thread_id": binding.thread_id,
                "sender_id": message.sender_id,
                "steered": steer_now,
            }
        )
        await h.hub.publish(
            {
                "type": "bot.inbound",
                "provider": provider,
                "externalConversationId": message.external_conversation_id,
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
) -> BotRoutingService:
    existing = getattr(app.state, "bot_routing_service", None)
    if isinstance(existing, BotRoutingService) and existing.host is host:
        service = existing
    else:
        service = BotRoutingService(host, delivery)
        app.state.bot_routing_service = service
    host._handle_bot_inbound = service.handle_inbound
    return service
