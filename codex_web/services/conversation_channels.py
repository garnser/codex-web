from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from codex_web.canonical_events import CanonicalEventType
from codex_web.conversation_channels import (
    ConversationChannelCapability,
    ConversationChannelError,
    ConversationChannelProvider,
    ConversationChannelUnsupportedError,
    ConversationEventKind,
    ConversationFact,
)
from codex_web.models import BotInboundMessage
from codex_web.services.canonical_events import CanonicalEventIngestionService


class ConversationChannelRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, ConversationChannelProvider] = {}

    def register(self, provider: ConversationChannelProvider) -> None:
        provider_type = str(provider.provider_type).strip().casefold()
        if not provider_type:
            raise ValueError("conversation channel provider type is required")
        existing = self._providers.get(provider_type)
        if existing is not None and existing is not provider:
            raise ValueError(
                f"conversation channel provider already registered: {provider_type}"
            )
        self._providers[provider_type] = provider

    def get(self, provider_type: str) -> ConversationChannelProvider:
        key = str(provider_type).strip().casefold()
        try:
            return self._providers[key]
        except KeyError as exc:
            raise ConversationChannelError(
                f"conversation channel provider is not registered: {provider_type}"
            ) from exc

    def require(
        self,
        provider_type: str,
        capability: ConversationChannelCapability,
    ) -> ConversationChannelProvider:
        provider = self.get(provider_type)
        if capability not in provider.capabilities():
            raise ConversationChannelUnsupportedError(
                f"{provider_type} does not support {capability.value}"
            )
        return provider

    def capabilities(self) -> dict[str, tuple[str, ...]]:
        return {
            key: tuple(sorted(item.value for item in provider.capabilities()))
            for key, provider in sorted(self._providers.items())
        }


@dataclass(frozen=True, slots=True)
class ConversationChannelDelivery:
    fact: ConversationFact
    inserted: bool


class ConversationChannelService:
    """Normalize inbound provider facts and persist the canonical event first."""

    def __init__(
        self,
        registry: ConversationChannelRegistry,
        canonical_events: CanonicalEventIngestionService,
    ) -> None:
        self.registry = registry
        self.canonical_events = canonical_events

    async def normalize_and_ingest(
        self,
        provider_type: str,
        payload: dict[str, Any],
        *,
        provider_instance: str,
        event_id: str | None = None,
        project_id: str | None = None,
        tenant_id: str | None = None,
        workspace_id: str | None = None,
    ) -> ConversationChannelDelivery | None:
        provider = self.registry.require(
            provider_type,
            ConversationChannelCapability.INBOUND_MESSAGES,
        )
        fact = provider.normalize(
            payload,
            provider_instance=provider_instance,
            event_id=event_id,
        )
        if fact is None:
            return None
        identity = {
            "provider_type": fact.address.provider_type,
            "provider_instance": fact.address.provider_instance,
            "external_conversation_id": fact.address.external_conversation_id,
            "external_thread_id": fact.address.external_thread_id,
            "event_kind": fact.event_kind.value,
            "event_id": fact.event_id,
            "message_id": fact.message_id,
        }
        idempotency_key = json.dumps(
            identity,
            sort_keys=True,
            separators=(",", ":"),
        )
        delivery = await self.canonical_events.ingest(
            event_type=CanonicalEventType.CONVERSATION,
            source=(
                f"conversation-channel:{fact.address.provider_type}:"
                f"{fact.address.provider_instance}"
            ),
            idempotency_key=idempotency_key,
            payload={
                "project_id": project_id,
                "identity": identity,
                "external_name": fact.external_name,
                "sender": fact.sender.model_dump(mode="json"),
                "text": fact.text,
                "attachments": [
                    item.model_dump(mode="json") for item in fact.attachments
                ],
                "mentions": list(fact.mentions),
                "command": fact.command,
                "provider_metadata": dict(fact.provider_metadata),
            },
            occurred_at=fact.occurred_at,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
        )
        return ConversationChannelDelivery(fact=fact, inserted=delivery.inserted)

    @staticmethod
    def to_bot_inbound(
        fact: ConversationFact,
        *,
        connection_id: str | None = None,
        project_id: str | None = None,
    ) -> BotInboundMessage:
        if fact.event_kind not in {
            ConversationEventKind.MESSAGE,
            ConversationEventKind.EDIT,
        }:
            raise ConversationChannelUnsupportedError(
                f"{fact.event_kind.value} is canonical-only and not routable as a turn"
            )
        return BotInboundMessage(
            provider=fact.address.provider_type,
            external_conversation_id=fact.address.external_conversation_id,
            connection_id=connection_id,
            external_name=(
                fact.external_name or fact.address.external_conversation_id
            ),
            sender_id=fact.sender.external_id,
            sender_name=fact.sender.display_name,
            text=fact.text or "",
            project_id=project_id,
            external_thread_id=fact.address.external_thread_id,
            message_id=fact.message_id,
        )
