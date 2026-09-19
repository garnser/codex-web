from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from codex_web.conversation_channels import (
    ConversationChannelCapability,
    ConversationEventKind,
)
from codex_web.models import BotInboundMessage
from codex_web.services.bot_routing import BotRoutingService
from codex_web.services.conversation_channels import ConversationChannelService


def build_telegram_router(
    host: Any,
    routing_service: BotRoutingService,
    channel_service: ConversationChannelService | None = None,
) -> APIRouter:
    router = APIRouter(tags=["telegram"])

    @router.post("/bots/telegram/webhook")
    async def telegram_webhook(request: Request) -> dict[str, Any]:
        host._verify_telegram_secret(request)
        payload = await request.json()

        if channel_service is not None:
            provider = channel_service.registry.require(
                "telegram",
                ConversationChannelCapability.INBOUND_MESSAGES,
            )
            fact = provider.normalize(
                payload,
                provider_instance="unbound",
                event_id=(
                    str(payload.get("update_id"))
                    if payload.get("update_id") is not None
                    else None
                ),
            )
            if fact is None:
                return {"ok": True, "ignored": True}

            connection = host._bot_connection_for_conversation(
                "telegram",
                fact.address.external_conversation_id,
            )
            project_id = connection.project_id if connection else None
            tenant_id = None
            workspace_id = None
            if connection is not None:
                try:
                    project = host._project(connection.project_id)
                except Exception:
                    project = None
                if project is not None:
                    tenant_id = getattr(project, "organization_id", None)
                    workspace_id = getattr(project, "workspace_id", None)
                fact = fact.model_copy(
                    update={
                        "address": fact.address.model_copy(
                            update={"provider_instance": connection.id}
                        )
                    }
                )

            delivery = await channel_service.ingest_fact(
                fact,
                project_id=project_id,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
            )
            if not delivery.inserted:
                return {
                    "ok": True,
                    "accepted": False,
                    "duplicate": True,
                }
            if (
                fact.sender.is_service
                or fact.event_kind
                not in {ConversationEventKind.MESSAGE, ConversationEventKind.EDIT}
            ):
                return {
                    "ok": True,
                    "accepted": False,
                    "canonicalOnly": True,
                }
            message = channel_service.to_bot_inbound(
                fact,
                connection_id=connection.id if connection else None,
                project_id=project_id,
            )
            if not message.text.strip():
                return {"ok": True, "accepted": False, "ignored": True}
        else:
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

            sender = message_payload.get("from") or {}
            message = BotInboundMessage(
                provider="telegram",
                external_conversation_id=str(chat_id),
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
