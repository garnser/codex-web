from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from codex_web.models import BotInboundMessage
from codex_web.services.bot_routing import BotRoutingService


def build_telegram_router(host: Any, routing_service: BotRoutingService) -> APIRouter:
    router = APIRouter(tags=["telegram"])

    @router.post("/bots/telegram/webhook")
    async def telegram_webhook(request: Request) -> dict[str, Any]:
        host._verify_telegram_secret(request)
        payload = await request.json()
        message_payload = payload.get("message") or payload.get("edited_message") or {}
        text = (message_payload.get("text") or "").strip()
        chat = message_payload.get("chat") or {}
        chat_id = chat.get("id")
        if not text or chat_id is None:
            return {"ok": True, "ignored": True}

        sender = message_payload.get("from") or {}
        message = BotInboundMessage(
            provider="telegram",
            external_conversation_id=str(chat_id),
            external_name=chat.get("title") or chat.get("username") or str(chat_id),
            sender_id=str(sender.get("id")) if sender.get("id") is not None else None,
            sender_name=sender.get("username") or sender.get("first_name"),
            text=text,
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
        return {"ok": True, "accepted": True, "threadId": result["threadId"]}

    return router
