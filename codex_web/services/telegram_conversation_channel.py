from __future__ import annotations

import hashlib
import json
from typing import Any

from codex_web.conversation_channels import (
    ConversationAddress,
    ConversationAttachmentRef,
    ConversationChannelCapability,
    ConversationEventKind,
    ConversationFact,
    ConversationSenderRef,
)


class TelegramConversationChannelProvider:
    provider_type = "telegram"

    def capabilities(self) -> frozenset[ConversationChannelCapability]:
        return frozenset(
            {
                ConversationChannelCapability.INBOUND_MESSAGES,
                ConversationChannelCapability.THREADS,
                ConversationChannelCapability.ATTACHMENTS,
                ConversationChannelCapability.MENTIONS,
                ConversationChannelCapability.COMMANDS,
                ConversationChannelCapability.EDITS,
            }
        )

    @staticmethod
    def _fallback_event_id(payload: dict[str, Any]) -> str:
        encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        return "telegram-" + hashlib.sha256(encoded).hexdigest()[:32]

    def normalize(
        self,
        payload: dict[str, Any],
        *,
        provider_instance: str,
        event_id: str | None = None,
    ) -> ConversationFact | None:
        is_edit = "edited_message" in payload or "edited_channel_post" in payload
        message = (
            payload.get("message")
            or payload.get("edited_message")
            or payload.get("channel_post")
            or payload.get("edited_channel_post")
            or {}
        )
        if not isinstance(message, dict) or not message:
            return None
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            return None
        text = str(message.get("text") or message.get("caption") or "").strip() or None
        sender = message.get("from") or {}
        message_id = (
            str(message.get("message_id"))
            if message.get("message_id") is not None
            else None
        )
        thread_id = (
            str(message.get("message_thread_id"))
            if message.get("message_thread_id") is not None
            else message_id
        )
        attachments: list[ConversationAttachmentRef] = []
        document = message.get("document")
        if isinstance(document, dict):
            attachments.append(
                ConversationAttachmentRef(
                    external_id=document.get("file_id"),
                    name=document.get("file_name"),
                    media_type=document.get("mime_type"),
                )
            )
        photos = message.get("photo") or []
        if photos and isinstance(photos[-1], dict):
            photo = photos[-1]
            attachments.append(
                ConversationAttachmentRef(
                    external_id=photo.get("file_id"),
                    name="photo",
                    media_type="image/jpeg",
                )
            )
        mentions: list[str] = []
        for entity in message.get("entities") or []:
            if not isinstance(entity, dict):
                continue
            user = entity.get("user") or {}
            if entity.get("type") == "text_mention" and user.get("id") is not None:
                mentions.append(str(user["id"]))
        command = None
        if text and text.startswith("/"):
            command = text.split(maxsplit=1)[0].split("@", 1)[0]
        normalized_event_id = (
            event_id
            or (
                str(payload.get("update_id"))
                if payload.get("update_id") is not None
                else None
            )
            or message_id
            or self._fallback_event_id(payload)
        )
        return ConversationFact(
            event_id=str(normalized_event_id),
            event_kind=(
                ConversationEventKind.EDIT
                if is_edit
                else ConversationEventKind.MESSAGE
            ),
            address=ConversationAddress(
                provider_type=self.provider_type,
                provider_instance=provider_instance,
                external_conversation_id=str(chat_id),
                external_thread_id=thread_id,
            ),
            message_id=message_id,
            external_name=(
                chat.get("title")
                or chat.get("username")
                or str(chat_id)
            ),
            sender=ConversationSenderRef(
                external_id=(
                    str(sender.get("id"))
                    if sender.get("id") is not None
                    else None
                ),
                display_name=(
                    sender.get("username")
                    or sender.get("first_name")
                ),
                is_service=bool(sender.get("is_bot")),
            ),
            text=text,
            attachments=tuple(attachments),
            mentions=tuple(dict.fromkeys(mentions)),
            command=command,
            occurred_at=(
                float(message["date"])
                if message.get("date") is not None
                else None
            ),
            provider_metadata={
                "chat_type": str(chat.get("type") or ""),
            },
        )
