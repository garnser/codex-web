from __future__ import annotations

import html
import re
from datetime import datetime
from typing import Any

from codex_web.conversation_channels import (
    CONVERSATION_CHANNEL_CONTRACT,
    ConversationAttachmentRef,
    ConversationChannelCapabilities,
    ConversationChannelCapability,
    ConversationChannelEvent,
    ConversationChannelHistoryPage,
    ConversationEventKind,
    ConversationMention,
    ConversationReaction,
    ConversationReference,
    ConversationSenderKind,
    ConversationSenderRef,
    ExternalConversationRef,
    ExternalMessageRef,
    UnsupportedConversationChannelCapability,
)


_URL_RE = re.compile(r"https?://[^\s<>]+")
_SLACK_MENTION_RE = re.compile(r"<@([A-Z0-9]+)>")
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _iso_timestamp(value: Any, default: float = 0.0) -> float:
    text = str(value or "").strip()
    if not text:
        return default
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return default


def _references_from_text(text: str) -> tuple[ConversationReference, ...]:
    return tuple(
        ConversationReference(kind="url", value=value)
        for value in dict.fromkeys(_URL_RE.findall(text or ""))
    )


class _ReadUnsupported:
    async def history(
        self,
        conversation: ExternalConversationRef,
        *,
        cursor: str | None = None,
        limit: int = 100,
    ) -> ConversationChannelHistoryPage:
        raise UnsupportedConversationChannelCapability(
            ConversationChannelCapability.HISTORY
        )


class SlackConversationChannel(_ReadUnsupported):
    provider_type = "slack"
    contract_version = CONVERSATION_CHANNEL_CONTRACT.current
    capabilities = ConversationChannelCapabilities(
        frozenset(
            {
                ConversationChannelCapability.EVENTS,
                ConversationChannelCapability.THREADS,
                ConversationChannelCapability.ATTACHMENTS,
                ConversationChannelCapability.REFERENCES,
                ConversationChannelCapability.MENTIONS,
                ConversationChannelCapability.COMMANDS,
                ConversationChannelCapability.REACTIONS,
                ConversationChannelCapability.EDITS,
                ConversationChannelCapability.DELETES,
                ConversationChannelCapability.CURSORS,
            }
        )
    )

    def __init__(self, provider_instance: str) -> None:
        self.provider_instance = provider_instance

    @staticmethod
    def _sender(event: dict[str, Any]) -> ConversationSenderRef:
        return ConversationSenderRef(
            provider_user_id=(
                str(event.get("user"))
                if event.get("user") is not None
                else str(event.get("bot_id"))
                if event.get("bot_id") is not None
                else None
            ),
            display_name=event.get("username"),
            kind=(
                ConversationSenderKind.BOT
                if event.get("bot_id")
                or event.get("subtype") == "bot_message"
                else ConversationSenderKind.HUMAN
                if event.get("user")
                else ConversationSenderKind.UNKNOWN
            ),
        )

    @staticmethod
    def _attachments(event: dict[str, Any]) -> tuple[ConversationAttachmentRef, ...]:
        rows: list[ConversationAttachmentRef] = []
        for item in event.get("files") or ():
            if not isinstance(item, dict) or not item.get("id"):
                continue
            rows.append(
                ConversationAttachmentRef(
                    external_id=str(item["id"]),
                    name=item.get("name") or item.get("title"),
                    media_type=item.get("mimetype"),
                    size_bytes=(
                        int(item["size"])
                        if item.get("size") is not None
                        else None
                    ),
                    provider_url=item.get("url_private") or item.get("permalink"),
                )
            )
        return tuple(rows)

    @staticmethod
    def _mentions(text: str) -> tuple[ConversationMention, ...]:
        rows = [
            ConversationMention(kind="user", external_id=value, text=f"<@{value}>")
            for value in dict.fromkeys(_SLACK_MENTION_RE.findall(text or ""))
        ]
        stripped = (text or "").lstrip()
        if stripped.startswith("/"):
            command = stripped.split(maxsplit=1)[0]
            rows.append(ConversationMention(kind="command", text=command))
        return tuple(rows)

    def _message_event(
        self,
        event: dict[str, Any],
        *,
        delivery_id: str,
        connection_id: str | None,
        project_id: str | None,
        event_kind: ConversationEventKind,
        provider_revision: str | None = None,
    ) -> ConversationChannelEvent | None:
        if event.get("bot_id") or event.get("subtype") == "bot_message":
            return None
        channel = str(event.get("channel") or "").strip()
        ts = str(event.get("ts") or event.get("event_ts") or "").strip()
        if not channel or not ts:
            return None
        text = str(event.get("text") or "").strip()
        thread = str(event.get("thread_ts") or ts).strip()
        occurred = 0.0
        ordering_timestamp = (
            provider_revision
            if event_kind == ConversationEventKind.MESSAGE_EDITED
            and provider_revision
            else ts
        )
        try:
            occurred = float(ordering_timestamp)
        except ValueError:
            pass
        return ConversationChannelEvent(
            delivery_id=delivery_id,
            event_kind=event_kind,
            message=ExternalMessageRef(
                conversation=ExternalConversationRef(
                    provider_type=self.provider_type,
                    provider_instance=self.provider_instance,
                    conversation_id=channel,
                    thread_id=thread,
                ),
                message_id=ts,
            ),
            sender=self._sender(event),
            text=text or None,
            attachments=self._attachments(event),
            references=_references_from_text(text),
            mentions=self._mentions(text),
            occurred_at=occurred,
            provider_revision=provider_revision or ts,
            provider_cursor=ts,
            connection_id=connection_id,
            project_id=project_id,
        )

    async def normalize_events(
        self,
        payload: object,
        *,
        connection_id: str | None = None,
        project_id: str | None = None,
    ) -> tuple[ConversationChannelEvent, ...]:
        if not isinstance(payload, dict):
            return ()
        outer = payload
        event = outer.get("event") if isinstance(outer.get("event"), dict) else outer
        if not isinstance(event, dict):
            return ()
        event_type = str(event.get("type") or "").strip()
        outer_id = str(
            outer.get("event_id")
            or outer.get("delivery_id")
            or ""
        ).strip()

        if event_type in {"reaction_added", "reaction_removed"}:
            item = event.get("item") or {}
            if not isinstance(item, dict):
                return ()
            channel = str(item.get("channel") or "").strip()
            message_id = str(item.get("ts") or "").strip()
            if not channel or not message_id:
                return ()
            kind = (
                ConversationEventKind.REACTION_ADDED
                if event_type == "reaction_added"
                else ConversationEventKind.REACTION_REMOVED
            )
            delivery = outer_id or (
                f"{event_type}:{channel}:{message_id}:"
                f"{event.get('reaction')}:{event.get('user')}:{event.get('event_ts')}"
            )
            return (
                ConversationChannelEvent(
                    delivery_id=delivery,
                    event_kind=kind,
                    message=ExternalMessageRef(
                        conversation=ExternalConversationRef(
                            provider_type=self.provider_type,
                            provider_instance=self.provider_instance,
                            conversation_id=channel,
                            thread_id=message_id,
                        ),
                        message_id=message_id,
                    ),
                    sender=ConversationSenderRef(
                        provider_user_id=(
                            str(event.get("user"))
                            if event.get("user") is not None
                            else None
                        ),
                        kind=ConversationSenderKind.HUMAN,
                    ),
                    reactions=(
                        ConversationReaction(
                            name=str(event.get("reaction") or "unknown"),
                        ),
                    ),
                    occurred_at=(
                        float(event.get("event_ts"))
                        if str(event.get("event_ts") or "").replace(".", "", 1).isdigit()
                        else 0.0
                    ),
                    provider_revision=str(event.get("event_ts") or "") or None,
                    connection_id=connection_id,
                    project_id=project_id,
                ),
            )

        if event_type not in {"message", "app_mention", ""}:
            return ()
        subtype = str(event.get("subtype") or "").strip()
        if subtype == "message_deleted":
            previous = event.get("previous_message") or {}
            if not isinstance(previous, dict):
                previous = {}
            channel = str(event.get("channel") or "").strip()
            deleted_ts = str(
                event.get("deleted_ts")
                or previous.get("ts")
                or event.get("event_ts")
                or ""
            ).strip()
            if not channel or not deleted_ts:
                return ()
            delivery = outer_id or f"message_deleted:{channel}:{deleted_ts}:{event.get('event_ts')}"
            return (
                ConversationChannelEvent(
                    delivery_id=delivery,
                    event_kind=ConversationEventKind.MESSAGE_DELETED,
                    message=ExternalMessageRef(
                        conversation=ExternalConversationRef(
                            provider_type=self.provider_type,
                            provider_instance=self.provider_instance,
                            conversation_id=channel,
                            thread_id=str(previous.get("thread_ts") or deleted_ts),
                        ),
                        message_id=deleted_ts,
                    ),
                    sender=self._sender(previous),
                    occurred_at=(
                        float(event.get("event_ts"))
                        if str(event.get("event_ts") or "").replace(".", "", 1).isdigit()
                        else 0.0
                    ),
                    provider_revision=str(event.get("event_ts") or deleted_ts),
                    connection_id=connection_id,
                    project_id=project_id,
                ),
            )
        if subtype == "message_changed":
            changed = event.get("message") or {}
            if not isinstance(changed, dict):
                return ()
            changed = {**changed, "channel": event.get("channel")}
            revision = str(
                (changed.get("edited") or {}).get("ts")
                if isinstance(changed.get("edited"), dict)
                else event.get("event_ts")
                or changed.get("ts")
            )
            delivery = outer_id or f"message_changed:{event.get('channel')}:{changed.get('ts')}:{revision}"
            normalized = self._message_event(
                changed,
                delivery_id=delivery,
                connection_id=connection_id,
                project_id=project_id,
                event_kind=ConversationEventKind.MESSAGE_EDITED,
                provider_revision=revision,
            )
            return (normalized,) if normalized is not None else ()
        if subtype and subtype not in {"thread_broadcast"}:
            return ()
        delivery = outer_id or (
            f"message:{event.get('channel')}:{event.get('ts')}:"
            f"{event.get('event_ts') or event.get('ts')}"
        )
        normalized = self._message_event(
            event,
            delivery_id=delivery,
            connection_id=connection_id,
            project_id=project_id,
            event_kind=ConversationEventKind.MESSAGE_CREATED,
        )
        return (normalized,) if normalized is not None else ()


class TelegramConversationChannel(_ReadUnsupported):
    provider_type = "telegram"
    contract_version = CONVERSATION_CHANNEL_CONTRACT.current
    capabilities = ConversationChannelCapabilities(
        frozenset(
            {
                ConversationChannelCapability.EVENTS,
                ConversationChannelCapability.THREADS,
                ConversationChannelCapability.ATTACHMENTS,
                ConversationChannelCapability.REFERENCES,
                ConversationChannelCapability.MENTIONS,
                ConversationChannelCapability.COMMANDS,
                ConversationChannelCapability.EDITS,
                ConversationChannelCapability.CURSORS,
            }
        )
    )

    def __init__(self, provider_instance: str) -> None:
        self.provider_instance = provider_instance

    @staticmethod
    def _attachments(message: dict[str, Any]) -> tuple[ConversationAttachmentRef, ...]:
        rows: list[ConversationAttachmentRef] = []
        document = message.get("document")
        if isinstance(document, dict) and document.get("file_id"):
            rows.append(
                ConversationAttachmentRef(
                    external_id=str(document["file_id"]),
                    name=document.get("file_name"),
                    media_type=document.get("mime_type"),
                    size_bytes=document.get("file_size"),
                )
            )
        photo = message.get("photo")
        if isinstance(photo, list) and photo:
            item = photo[-1]
            if isinstance(item, dict) and item.get("file_id"):
                rows.append(
                    ConversationAttachmentRef(
                        external_id=str(item["file_id"]),
                        name="photo",
                        media_type="image/*",
                        size_bytes=item.get("file_size"),
                    )
                )
        for key in ("video", "audio", "voice", "animation"):
            item = message.get(key)
            if isinstance(item, dict) and item.get("file_id"):
                rows.append(
                    ConversationAttachmentRef(
                        external_id=str(item["file_id"]),
                        name=item.get("file_name") or key,
                        media_type=item.get("mime_type"),
                        size_bytes=item.get("file_size"),
                    )
                )
        return tuple(rows)

    @staticmethod
    def _mentions(message: dict[str, Any], text: str) -> tuple[ConversationMention, ...]:
        rows: list[ConversationMention] = []
        for entity in message.get("entities") or ():
            if not isinstance(entity, dict):
                continue
            kind = str(entity.get("type") or "")
            if kind not in {"mention", "text_mention", "bot_command"}:
                continue
            offset = int(entity.get("offset") or 0)
            length = int(entity.get("length") or 0)
            value = text[offset : offset + length] if length > 0 else None
            user = entity.get("user") or {}
            external_id = (
                str(user.get("id"))
                if isinstance(user, dict) and user.get("id") is not None
                else None
            )
            rows.append(
                ConversationMention(
                    kind="command" if kind == "bot_command" else "user",
                    external_id=external_id,
                    text=value,
                )
            )
        return tuple(rows)

    async def normalize_events(
        self,
        payload: object,
        *,
        connection_id: str | None = None,
        project_id: str | None = None,
    ) -> tuple[ConversationChannelEvent, ...]:
        if not isinstance(payload, dict):
            return ()
        edited = isinstance(payload.get("edited_message"), dict)
        message = (
            payload.get("edited_message")
            if edited
            else payload.get("message")
        )
        if not isinstance(message, dict):
            return ()
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        if not isinstance(chat, dict) or chat.get("id") is None:
            return ()
        message_id = str(message.get("message_id") or "").strip()
        if not message_id:
            return ()
        chat_id = str(chat["id"])
        thread_id = str(
            message.get("message_thread_id")
            or message.get("reply_to_message", {}).get("message_id")
            if isinstance(message.get("reply_to_message"), dict)
            else message.get("message_thread_id")
            or message_id
        )
        text = str(message.get("text") or message.get("caption") or "").strip()
        update_id = str(payload.get("update_id") or "").strip()
        delivery = update_id or (
            f"{'edit' if edited else 'message'}:{chat_id}:{message_id}:"
            f"{message.get('edit_date') or message.get('date')}"
        )
        occurred_at = float(
            message.get("edit_date")
            or message.get("date")
            or 0.0
        )
        return (
            ConversationChannelEvent(
                delivery_id=delivery,
                event_kind=(
                    ConversationEventKind.MESSAGE_EDITED
                    if edited
                    else ConversationEventKind.MESSAGE_CREATED
                ),
                message=ExternalMessageRef(
                    conversation=ExternalConversationRef(
                        provider_type=self.provider_type,
                        provider_instance=self.provider_instance,
                        conversation_id=chat_id,
                        thread_id=thread_id,
                        display_name=(
                            chat.get("title")
                            or chat.get("username")
                            or chat_id
                        ),
                    ),
                    message_id=message_id,
                ),
                sender=ConversationSenderRef(
                    provider_user_id=(
                        str(sender.get("id"))
                        if isinstance(sender, dict)
                        and sender.get("id") is not None
                        else None
                    ),
                    display_name=(
                        sender.get("username")
                        or sender.get("first_name")
                        if isinstance(sender, dict)
                        else None
                    ),
                    kind=(
                        ConversationSenderKind.BOT
                        if isinstance(sender, dict) and sender.get("is_bot")
                        else ConversationSenderKind.HUMAN
                        if isinstance(sender, dict) and sender.get("id") is not None
                        else ConversationSenderKind.UNKNOWN
                    ),
                ),
                text=text or None,
                attachments=self._attachments(message),
                references=_references_from_text(text),
                mentions=self._mentions(message, text),
                occurred_at=occurred_at,
                provider_sequence=(
                    int(payload["update_id"])
                    if payload.get("update_id") is not None
                    else None
                ),
                provider_revision=str(
                    message.get("edit_date")
                    or message.get("date")
                    or message_id
                ),
                provider_cursor=(
                    str(int(payload["update_id"]) + 1)
                    if payload.get("update_id") is not None
                    else None
                ),
                connection_id=connection_id,
                project_id=project_id,
            ),
        )


class TeamsConversationChannel(_ReadUnsupported):
    """Microsoft Graph message/change-notification normalizer.

    Fetching Graph resources and outbound messages are intentionally outside this
    read/normalization adapter; credentials remain behind the provider/secret boundary.
    """

    provider_type = "teams"
    contract_version = CONVERSATION_CHANNEL_CONTRACT.current
    capabilities = ConversationChannelCapabilities(
        frozenset(
            {
                ConversationChannelCapability.EVENTS,
                ConversationChannelCapability.THREADS,
                ConversationChannelCapability.ATTACHMENTS,
                ConversationChannelCapability.REFERENCES,
                ConversationChannelCapability.MENTIONS,
                ConversationChannelCapability.EDITS,
                ConversationChannelCapability.DELETES,
            }
        )
    )

    def __init__(self, provider_instance: str) -> None:
        self.provider_instance = provider_instance

    @staticmethod
    def _plain_text(message: dict[str, Any]) -> str:
        body = message.get("body") or {}
        content = str(body.get("content") or "") if isinstance(body, dict) else ""
        return html.unescape(_HTML_TAG_RE.sub(" ", content)).strip()

    @staticmethod
    def _sender(message: dict[str, Any]) -> ConversationSenderRef:
        source = message.get("from") or {}
        if not isinstance(source, dict):
            return ConversationSenderRef()
        user = source.get("user")
        app = source.get("application")
        if isinstance(user, dict):
            return ConversationSenderRef(
                provider_user_id=(
                    str(user.get("id"))
                    if user.get("id") is not None
                    else None
                ),
                display_name=user.get("displayName"),
                kind=ConversationSenderKind.HUMAN,
                provider_claims=(
                    tuple(
                        value
                        for value in (
                            str(user.get("userIdentityType") or "").strip(),
                            str(user.get("tenantId") or "").strip(),
                        )
                        if value
                    )
                ),
            )
        if isinstance(app, dict):
            return ConversationSenderRef(
                provider_user_id=(
                    str(app.get("id"))
                    if app.get("id") is not None
                    else None
                ),
                display_name=app.get("displayName"),
                kind=ConversationSenderKind.SERVICE,
            )
        return ConversationSenderRef()

    @staticmethod
    def _attachments(message: dict[str, Any]) -> tuple[ConversationAttachmentRef, ...]:
        rows: list[ConversationAttachmentRef] = []
        for item in message.get("attachments") or ():
            if not isinstance(item, dict):
                continue
            external_id = str(
                item.get("id")
                or item.get("contentUrl")
                or item.get("name")
                or ""
            ).strip()
            if not external_id:
                continue
            rows.append(
                ConversationAttachmentRef(
                    external_id=external_id,
                    name=item.get("name"),
                    media_type=item.get("contentType"),
                    provider_url=item.get("contentUrl"),
                )
            )
        return tuple(rows)

    @staticmethod
    def _mentions(message: dict[str, Any]) -> tuple[ConversationMention, ...]:
        rows: list[ConversationMention] = []
        for item in message.get("mentions") or ():
            if not isinstance(item, dict):
                continue
            mentioned = item.get("mentioned") or {}
            user = mentioned.get("user") if isinstance(mentioned, dict) else {}
            rows.append(
                ConversationMention(
                    kind="user",
                    external_id=(
                        str(user.get("id"))
                        if isinstance(user, dict) and user.get("id") is not None
                        else None
                    ),
                    text=item.get("mentionText"),
                )
            )
        return tuple(rows)

    def _message(
        self,
        message: dict[str, Any],
        *,
        change_type: str,
        delivery_id: str,
        resource: str | None,
        connection_id: str | None,
        project_id: str | None,
    ) -> ConversationChannelEvent | None:
        message_id = str(message.get("id") or "").strip()
        chat_id = str(
            message.get("chatId")
            or message.get("channelIdentity", {}).get("channelId")
            if isinstance(message.get("channelIdentity"), dict)
            else message.get("chatId")
            or ""
        ).strip()
        if not message_id or not chat_id:
            return None
        reply_to = str(message.get("replyToId") or message_id)
        deleted_at = message.get("deletedDateTime")
        kind = (
            ConversationEventKind.MESSAGE_DELETED
            if change_type == "deleted" or deleted_at
            else ConversationEventKind.MESSAGE_EDITED
            if change_type in {"updated", "edit"}
            or message.get("lastModifiedDateTime")
            else ConversationEventKind.MESSAGE_CREATED
        )
        text = self._plain_text(message)
        references = list(_references_from_text(text))
        if resource:
            references.append(
                ConversationReference(
                    kind="graph-resource",
                    value=resource,
                )
            )
        occurred = _iso_timestamp(
            message.get("lastModifiedDateTime")
            or message.get("deletedDateTime")
            or message.get("createdDateTime"),
            default=0.0,
        )
        return ConversationChannelEvent(
            delivery_id=delivery_id,
            event_kind=kind,
            message=ExternalMessageRef(
                conversation=ExternalConversationRef(
                    provider_type=self.provider_type,
                    provider_instance=self.provider_instance,
                    conversation_id=chat_id,
                    thread_id=reply_to,
                ),
                message_id=message_id,
            ),
            sender=self._sender(message),
            text=text or None,
            attachments=self._attachments(message),
            references=tuple(references),
            mentions=self._mentions(message),
            occurred_at=occurred,
            provider_revision=str(
                message.get("lastModifiedDateTime")
                or message.get("@odata.etag")
                or message.get("createdDateTime")
                or message_id
            ),
            connection_id=connection_id,
            project_id=project_id,
        )

    async def normalize_events(
        self,
        payload: object,
        *,
        connection_id: str | None = None,
        project_id: str | None = None,
    ) -> tuple[ConversationChannelEvent, ...]:
        if not isinstance(payload, dict):
            return ()
        notifications = payload.get("value")
        rows = notifications if isinstance(notifications, list) else [payload]
        events: list[ConversationChannelEvent] = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            data = row.get("resourceData")
            message = data if isinstance(data, dict) else row
            change_type = str(row.get("changeType") or "created").casefold()
            resource = str(row.get("resource") or "").strip() or None
            delivery = str(
                row.get("id")
                or row.get("subscriptionId")
                or f"notification-{index}:{resource}:{message.get('id') if isinstance(message, dict) else ''}"
            )
            normalized = self._message(
                message,
                change_type=change_type,
                delivery_id=delivery,
                resource=resource,
                connection_id=connection_id,
                project_id=project_id,
            )
            if normalized is not None:
                events.append(normalized)
        return tuple(events)
