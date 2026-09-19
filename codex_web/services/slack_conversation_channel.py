from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from codex_web.conversation_channels import (
    ConversationAddress,
    ConversationAttachmentRef,
    ConversationChannelCapability,
    ConversationEventKind,
    ConversationFact,
    ConversationSenderRef,
)


_MENTION_RE = re.compile(r"<@([A-Z0-9]+)>")


class SlackConversationChannelProvider:
    provider_type = "slack"

    def capabilities(self) -> frozenset[ConversationChannelCapability]:
        return frozenset(
            {
                ConversationChannelCapability.INBOUND_MESSAGES,
                ConversationChannelCapability.THREADS,
                ConversationChannelCapability.ATTACHMENTS,
                ConversationChannelCapability.MENTIONS,
                ConversationChannelCapability.COMMANDS,
                ConversationChannelCapability.EDITS,
                ConversationChannelCapability.DELETES,
                ConversationChannelCapability.REACTIONS,
            }
        )

    @staticmethod
    def _fallback_event_id(payload: dict[str, Any]) -> str:
        encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        return "slack-" + hashlib.sha256(encoded).hexdigest()[:32]

    def normalize(
        self,
        payload: dict[str, Any],
        *,
        provider_instance: str,
        event_id: str | None = None,
    ) -> ConversationFact | None:
        envelope = payload
        event = dict(payload.get("event") or payload)
        event_type = str(event.get("type") or "")
        subtype = str(event.get("subtype") or "")

        if event.get("bot_id") or subtype == "bot_message":
            return None

        kind = ConversationEventKind.MESSAGE
        if subtype == "message_changed":
            event = dict(event.get("message") or {})
            kind = ConversationEventKind.EDIT
        elif subtype == "message_deleted":
            event = dict(event.get("previous_message") or event)
            kind = ConversationEventKind.DELETE
        elif event_type in {"reaction_added", "reaction_removed"}:
            kind = ConversationEventKind.REACTION
        elif event_type not in {"message", "app_mention"}:
            return None

        channel = (
            event.get("channel")
            or envelope.get("channel")
            or envelope.get("_channel")
        )
        if channel is None:
            return None
        conversation_id = str(channel)
        message_id = str(
            event.get("ts")
            or event.get("message_ts")
            or envelope.get("event_ts")
            or ""
        ).strip() or None
        thread_id = str(
            event.get("thread_ts")
            or event.get("item", {}).get("ts")
            or message_id
            or ""
        ).strip() or None
        text = str(event.get("text") or "").strip() or None
        sender_id = event.get("user")
        sender_name = event.get("username")
        files = event.get("files") or []
        attachments = tuple(
            ConversationAttachmentRef(
                external_id=str(item.get("id")) if item.get("id") else None,
                name=item.get("name") or item.get("title"),
                media_type=item.get("mimetype"),
                external_url=item.get("url_private") or item.get("permalink"),
            )
            for item in files
            if isinstance(item, dict)
        )
        mentions = tuple(dict.fromkeys(_MENTION_RE.findall(text or "")))
        command = (
            (text or "").split(maxsplit=1)[0]
            if text and text.startswith("/")
            else None
        )
        timestamp = event.get("ts") or envelope.get("event_time")
        try:
            occurred_at = float(timestamp) if timestamp is not None else None
        except (TypeError, ValueError):
            occurred_at = None
        normalized_event_id = (
            event_id
            or envelope.get("event_id")
            or event.get("client_msg_id")
            or message_id
            or self._fallback_event_id(payload)
        )
        metadata: dict[str, str | int | float | bool | None] = {}
        if event.get("reaction"):
            metadata["reaction"] = str(event["reaction"])
        if event_type:
            metadata["provider_event_type"] = event_type
        if subtype:
            metadata["provider_subtype"] = subtype
        return ConversationFact(
            event_id=str(normalized_event_id),
            event_kind=kind,
            address=ConversationAddress(
                provider_type=self.provider_type,
                provider_instance=provider_instance,
                external_conversation_id=conversation_id,
                external_thread_id=thread_id,
            ),
            message_id=message_id,
            external_name=conversation_id,
            sender=ConversationSenderRef(
                external_id=str(sender_id) if sender_id is not None else None,
                display_name=str(sender_name) if sender_name else None,
                is_service=False,
            ),
            text=text,
            attachments=attachments,
            mentions=mentions,
            command=command,
            occurred_at=occurred_at,
            provider_metadata=metadata,
        )
