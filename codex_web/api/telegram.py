from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from codex_web.models import BotInboundMessage
from codex_web.services.bot_connections import BotConnectionService
from codex_web.services.bot_routing import BotRoutingService
from codex_web.services.bot_webhook_security import BotWebhookSecurityService
from codex_web.services.conversation_channels import ConversationChannelService


def build_telegram_router(
    routing_service: BotRoutingService,
    *,
    connections: BotConnectionService,
    webhook_security: BotWebhookSecurityService,
    conversation_channels: ConversationChannelService | None = None,
) -> APIRouter:
    router = APIRouter(tags=["telegram"])

    @router.post("/bots/telegram/webhook")
    async def telegram_webhook(request: Request) -> dict[str, Any]:
        await webhook_security.verify_telegram(request)
        payload = await request.json()
        message_payload = (
            payload.get("message")
            or payload.get("edited_message")
            or {}
        )
        text = (message_payload.get("text") or "").strip()
        chat = message_payload.get("chat") or {}
        chat_id = chat.get("id")
        if not text or chat_id is None:
            return {"ok": True, "ignored": True}

        connection = connections.for_conversation(
            "telegram",
            str(chat_id),
        )
        project_id = connection.project_id if connection else "home"
        if conversation_channels is not None:
            receipts = await conversation_channels.ingest_raw(
                "telegram",
                connection.id if connection else "telegram:default",
                payload,
                actor=connections.runtime_actor(project_id),
                connection_id=connection.id if connection else None,
                project_id=project_id,
            )
            if not receipts:
                return {"ok": True, "ignored": True}
            result = conversation_channels.legacy_routing_result(
                receipts[0]
            )
            if (
                not result.get("routed", True)
                and not result.get("threadId")
            ):
                return {
                    "ok": True,
                    "accepted": False,
                    "conversationOutcome": result.get(
                        "conversationOutcome"
                    ),
                    "canonicalEventId": result.get(
                        "canonicalEventId"
                    ),
                }
        else:
            sender = message_payload.get("from") or {}
            message = BotInboundMessage(
                provider="telegram",
                external_conversation_id=str(chat_id),
                connection_id=connection.id if connection else None,
                external_name=(
                    chat.get("title")
                    or chat.get("username")
                    or str(chat_id)
                ),
                sender_id=(
                    str(sender.get("id"))
                    if sender.get("id") is not None
                    else None
                ),
                sender_name=(
                    sender.get("username")
                    or sender.get("first_name")
                ),
                text=text,
                project_id=project_id,
                external_thread_id=(
                    str(message_payload.get("message_thread_id"))
                    if message_payload.get("message_thread_id")
                    is not None
                    else None
                ),
                message_id=(
                    str(message_payload.get("message_id"))
                    if message_payload.get("message_id") is not None
                    else None
                ),
            )
            result = await routing_service.handle_inbound(message)
        if result.get("ambiguous"):
            return {
                "ok": True,
                "accepted": False,
                "ambiguous": True,
                "availablePrefixes": result["availablePrefixes"],
            }
        if result.get("timedOut"):
            return {
                "ok": True,
                "accepted": False,
                "timedOut": True,
                "threadId": result.get("threadId"),
            }
        return {
            "ok": True,
            "accepted": True,
            "threadId": result["threadId"],
        }

    return router
