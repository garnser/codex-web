from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from codex_web.conversation_channels import (
    ConversationAddress,
    ConversationAttachmentRef,
    ConversationChannelCapability,
    ConversationEventKind,
    ConversationFact,
    ConversationSenderRef,
)


class TeamsConversationChannelProvider:
    """Normalize Microsoft Bot Framework/Teams activity payloads."""

    provider_type = "teams"

    def capabilities(self) -> frozenset[ConversationChannelCapability]:
        return frozenset(
            {
                ConversationChannelCapability.INBOUND_MESSAGES,
                ConversationChannelCapability.THREADS,
                ConversationChannelCapability.ATTACHMENTS,
                ConversationChannelCapability.MENTIONS,
                ConversationChannelCapability.EDITS,
                ConversationChannelCapability.DELETES,
                ConversationChannelCapability.REACTIONS,
            }
        )

    @staticmethod
    def _fallback_event_id(payload: dict[str, Any]) -> str:
        encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        return "teams-" + hashlib.sha256(encoded).hexdigest()[:32]

    @staticmethod
    def _timestamp(value: Any) -> float | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(
                str(value).replace("Z", "+00:00")
            ).timestamp()
        except ValueError:
            return None

    def normalize(
        self,
        payload: dict[str, Any],
        *,
        provider_instance: str,
        event_id: str | None = None,
    ) -> ConversationFact | None:
        activity_type = str(payload.get("type") or "")
        kind_map = {
            "message": ConversationEventKind.MESSAGE,
            "messageUpdate": ConversationEventKind.EDIT,
            "messageDelete": ConversationEventKind.DELETE,
            "messageReaction": ConversationEventKind.REACTION,
        }
        kind = kind_map.get(activity_type)
        if kind is None:
            return None
        conversation = payload.get("conversation") or {}
        conversation_id = conversation.get("id")
        if not conversation_id:
            return None
        sender = payload.get("from") or {}
        text = str(payload.get("text") or "").strip() or None
        attachments = tuple(
            ConversationAttachmentRef(
                external_id=str(index),
                name=item.get("name"),
                media_type=item.get("contentType"),
                external_url=item.get("contentUrl"),
            )
            for index, item in enumerate(payload.get("attachments") or [])
            if isinstance(item, dict)
        )
        mentions: list[str] = []
        for entity in payload.get("entities") or []:
            if not isinstance(entity, dict) or entity.get("type") != "mention":
                continue
            mentioned = entity.get("mentioned") or {}
            if mentioned.get("id"):
                mentions.append(str(mentioned["id"]))
        message_id = str(payload.get("id") or "").strip() or None
        thread_id = (
            str(payload.get("replyToId") or "").strip()
            or message_id
        )
        normalized_event_id = (
            event_id
            or message_id
            or self._fallback_event_id(payload)
        )
        return ConversationFact(
            event_id=str(normalized_event_id),
            event_kind=kind,
            address=ConversationAddress(
                provider_type=self.provider_type,
                provider_instance=provider_instance,
                external_conversation_id=str(conversation_id),
                external_thread_id=thread_id,
            ),
            message_id=message_id,
            external_name=conversation.get("name") or str(conversation_id),
            sender=ConversationSenderRef(
                external_id=(
                    str(sender.get("id"))
                    if sender.get("id") is not None
                    else None
                ),
                display_name=sender.get("name"),
                is_service=sender.get("role") == "bot",
            ),
            text=text,
            attachments=attachments,
            mentions=tuple(dict.fromkeys(mentions)),
            occurred_at=self._timestamp(payload.get("timestamp")),
            provider_metadata={
                "channel_id": str(payload.get("channelId") or ""),
                "provider_event_type": activity_type,
            },
        )
