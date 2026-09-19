from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

from codex_web.canonical_events import CanonicalEventType
from codex_web.compatibility import ContractCompatibilityError
from codex_web.conversation_channels import (
    CONVERSATION_CHANNEL_CONTRACT,
    ConversationChannel,
    ConversationChannelCapabilities,
    ConversationChannelEvent,
    ConversationChannelState,
    ConversationEventKind,
    ConversationMessageState,
    ConversationProjectionOutcome,
    ConversationProjectionReceipt,
)
from codex_web.identity import AuthenticationActor
from codex_web.models import BotInboundMessage
from codex_web.services.canonical_events import CanonicalEventIngestionService
from codex_web.storage.conversation_channels import ConversationChannelStore


ConversationRouteHandler = Callable[[BotInboundMessage], Awaitable[dict[str, Any]]]
ConversationChannelFactory = Callable[[], ConversationChannel]
ConversationChannelProviderFactory = Callable[[str], ConversationChannel]
ConversationChannelAvailability = Callable[[], bool]


class ConversationChannelError(RuntimeError):
    pass


class ConversationChannelNotFoundError(ConversationChannelError):
    pass


class ConversationChannelConflictError(ConversationChannelError):
    pass


class ConversationChannelUnavailableError(ConversationChannelError):
    pass


class ConversationChannelValidationError(ConversationChannelError):
    pass


class ConversationChannelRegistry:
    """Code-owned adapter registry with an extension-lifecycle availability gate."""

    def __init__(self) -> None:
        self._entries: dict[
            tuple[str, str],
            tuple[
                ConversationChannelFactory,
                ConversationChannelAvailability | None,
                str | None,
            ],
        ] = {}
        self._provider_factories: dict[str, ConversationChannelProviderFactory] = {}

    @staticmethod
    def _key(provider_type: str, provider_instance: str) -> tuple[str, str]:
        provider = str(provider_type or "").strip().casefold()
        instance = str(provider_instance or "").strip().casefold()
        if not provider or not instance:
            raise ValueError("conversation channel provider and instance are required")
        return provider, instance

    def register(
        self,
        provider_type: str,
        provider_instance: str,
        factory: ConversationChannelFactory,
        *,
        available: ConversationChannelAvailability | None = None,
        installation_id: str | None = None,
    ) -> None:
        self._entries[self._key(provider_type, provider_instance)] = (
            factory,
            available,
            installation_id,
        )

    def register_provider(
        self,
        provider_type: str,
        factory: ConversationChannelProviderFactory,
    ) -> None:
        provider = str(provider_type or "").strip().casefold()
        if not provider:
            raise ValueError("conversation channel provider type is required")
        self._provider_factories[provider] = factory

    def unregister(self, provider_type: str, provider_instance: str) -> None:
        self._entries.pop(self._key(provider_type, provider_instance), None)

    def resolve(
        self,
        provider_type: str,
        provider_instance: str,
    ) -> ConversationChannel:
        key = self._key(provider_type, provider_instance)
        entry = self._entries.get(key)
        if entry is None:
            provider_factory = self._provider_factories.get(key[0])
            if provider_factory is None:
                raise ConversationChannelNotFoundError(
                    f"conversation channel adapter is not registered: {provider_type}/{provider_instance}"
                )
            adapter = provider_factory(provider_instance)
        else:
            factory, available, _installation_id = entry
            if available is not None and not available():
                raise ConversationChannelUnavailableError(
                    f"conversation channel adapter is not enabled: {provider_type}/{provider_instance}"
                )
            adapter = factory()
        try:
            CONVERSATION_CHANNEL_CONTRACT.require(adapter.contract_version)
        except (ContractCompatibilityError, ValueError) as exc:
            raise ConversationChannelValidationError(
                f"incompatible ConversationChannel contract version: {adapter.contract_version!r}"
            ) from exc
        if adapter.provider_type.casefold() != key[0]:
            raise ConversationChannelValidationError(
                "ConversationChannel provider_type does not match registry key"
            )
        if adapter.provider_instance.casefold() != key[1]:
            raise ConversationChannelValidationError(
                "ConversationChannel provider_instance does not match registry key"
            )
        return adapter

    def describe(self) -> tuple[dict[str, Any], ...]:
        rows: list[dict[str, Any]] = []
        for (provider, instance), (factory, available, installation_id) in sorted(
            self._entries.items()
        ):
            enabled = available() if available is not None else True
            capabilities: tuple[str, ...] = ()
            contract_version: str | None = None
            error: str | None = None
            if enabled:
                try:
                    adapter = factory()
                    capabilities = tuple(
                        sorted(item.value for item in adapter.capabilities.supported)
                    )
                    contract_version = adapter.contract_version
                except Exception as exc:
                    enabled = False
                    error = f"{type(exc).__name__}: {exc}"
            rows.append(
                {
                    "provider_type": provider,
                    "provider_instance": instance,
                    "enabled": enabled,
                    "installation_id": installation_id,
                    "contract_version": contract_version,
                    "capabilities": capabilities,
                    "error": error,
                }
            )
        return tuple(rows)


class ConversationChannelService:
    """Canonical, deterministic inbound channel normalization and routing boundary."""

    MAX_RECEIPTS = 20_000
    MAX_MESSAGES = 20_000

    def __init__(
        self,
        store: ConversationChannelStore,
        registry: ConversationChannelRegistry,
        canonical_events: CanonicalEventIngestionService,
        route_handler: ConversationRouteHandler,
        *,
        clock=time.time,
    ) -> None:
        self.store = store
        self.registry = registry
        self.canonical_events = canonical_events
        self.route_handler = route_handler
        self.clock = clock

    @staticmethod
    def _same_scope(item: Any, actor: AuthenticationActor) -> bool:
        return (
            item.organization_id == actor.organization_id
            and item.workspace_id == actor.workspace_id
        )

    @staticmethod
    def _event_payload(event: ConversationChannelEvent) -> dict[str, Any]:
        return event.model_dump(mode="json")

    @staticmethod
    def _message_text(event: ConversationChannelEvent) -> str:
        parts: list[str] = []
        if (event.text or "").strip():
            parts.append((event.text or "").strip())
        if event.attachments:
            attachments = ", ".join(
                item.name or item.external_id
                for item in event.attachments
            )
            parts.append(f"Attachments: {attachments}")
        if event.references and parts:
            refs = ", ".join(
                item.label or item.value
                for item in event.references
            )
            parts.append(f"References: {refs}")
        return "\n\n".join(parts).strip()

    @staticmethod
    def _routing_projection(result: dict[str, Any]) -> dict[str, str | int | float | bool | None]:
        allowed = (
            "ok",
            "queued",
            "ambiguous",
            "empty",
            "timedOut",
            "steered",
            "threadId",
            "queuedId",
            "queueDepth",
        )
        return {
            key: value
            for key in allowed
            if (value := result.get(key)) is None
            or isinstance(value, (str, int, float, bool))
        }

    @staticmethod
    def _is_stale(
        current: ConversationMessageState,
        event: ConversationChannelEvent,
    ) -> bool:
        if current.provider_sequence is not None:
            if event.provider_sequence is None:
                return True
            return event.provider_sequence <= current.provider_sequence
        if event.provider_sequence is not None:
            return False
        if event.occurred_at < current.occurred_at:
            return True
        if event.occurred_at > current.occurred_at:
            return False
        if (
            current.provider_revision is not None
            and event.provider_revision is not None
        ):
            return current.provider_revision == event.provider_revision
        return True

    def _claim(
        self,
        *,
        canonical_event_id: str,
        event: ConversationChannelEvent,
        actor: AuthenticationActor,
    ) -> tuple[ConversationProjectionReceipt, bool]:
        claimed: list[ConversationProjectionReceipt] = []
        created = False
        now = float(self.clock())

        def apply(state: ConversationChannelState) -> ConversationChannelState:
            nonlocal created
            existing = state.receipts.get(canonical_event_id)
            if existing is not None:
                claimed.append(existing)
                return state
            receipt = ConversationProjectionReceipt(
                canonical_event_id=canonical_event_id,
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                message_key=event.message.stable_key(),
                outcome=ConversationProjectionOutcome.ROUTING,
                created_at=now,
                updated_at=now,
            )
            state.receipts[canonical_event_id] = receipt
            claimed.append(receipt)
            created = True
            return state

        self.store.update(apply)
        return claimed[0], created

    def _finish(
        self,
        canonical_event_id: str,
        *,
        actor: AuthenticationActor,
        outcome: ConversationProjectionOutcome,
        thread_id: str | None = None,
        queued_id: str | None = None,
        reason: str | None = None,
        routing_result: dict[str, str | int | float | bool | None] | None = None,
        message_state: ConversationMessageState | None = None,
    ) -> ConversationProjectionReceipt:
        saved: list[ConversationProjectionReceipt] = []
        now = float(self.clock())

        def apply(state: ConversationChannelState) -> ConversationChannelState:
            current = state.receipts.get(canonical_event_id)
            if current is None or not self._same_scope(current, actor):
                raise ConversationChannelNotFoundError(
                    "conversation projection receipt not found"
                )
            updated = current.model_copy(
                update={
                    "outcome": outcome,
                    "thread_id": thread_id,
                    "queued_id": queued_id,
                    "reason": reason,
                    "routing_result": routing_result or {},
                    "updated_at": now,
                }
            )
            state.receipts[canonical_event_id] = updated
            if message_state is not None:
                state.messages[message_state.message.stable_key()] = message_state
            if len(state.receipts) > self.MAX_RECEIPTS:
                rows = sorted(
                    state.receipts.values(),
                    key=lambda item: (item.updated_at, item.canonical_event_id),
                    reverse=True,
                )[: self.MAX_RECEIPTS]
                state.receipts = {item.canonical_event_id: item for item in rows}
            if len(state.messages) > self.MAX_MESSAGES:
                rows = sorted(
                    state.messages.values(),
                    key=lambda item: (item.updated_at, item.last_canonical_event_id),
                    reverse=True,
                )[: self.MAX_MESSAGES]
                state.messages = {
                    item.message.stable_key(): item
                    for item in rows
                }
            saved.append(updated)
            return state

        self.store.update(apply)
        return saved[0]

    def _state_for_event(
        self,
        event: ConversationChannelEvent,
        *,
        actor: AuthenticationActor,
        canonical_event_id: str,
        deleted: bool = False,
    ) -> ConversationMessageState:
        return ConversationMessageState(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            message=event.message,
            event_kind=event.event_kind,
            occurred_at=event.occurred_at,
            provider_sequence=event.provider_sequence,
            provider_revision=event.provider_revision,
            provider_cursor=event.provider_cursor,
            deleted=deleted,
            last_canonical_event_id=canonical_event_id,
            updated_at=float(self.clock()),
        )

    async def ingest(
        self,
        event: ConversationChannelEvent,
        *,
        actor: AuthenticationActor,
    ) -> ConversationProjectionReceipt:
        provider = event.message.conversation.provider_type
        instance = event.message.conversation.provider_instance
        source = f"conversation-channel:{provider}:{instance}"
        delivery = await self.canonical_events.ingest(
            event_type=CanonicalEventType.CONVERSATION_CHANNEL,
            source=source,
            idempotency_key=event.delivery_id,
            occurred_at=event.occurred_at,
            tenant_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            payload=self._event_payload(event),
        )
        canonical_id = delivery.event.event_id
        receipt, claimed = self._claim(
            canonical_event_id=canonical_id,
            event=event,
            actor=actor,
        )
        if not claimed:
            if receipt.outcome == ConversationProjectionOutcome.ROUTING:
                return receipt.model_copy(
                    update={
                        "outcome": ConversationProjectionOutcome.REQUIRES_RECONCILIATION,
                        "reason": "canonical event committed with an unfinished routing claim",
                    }
                )
            return receipt.model_copy(
                update={
                    "outcome": ConversationProjectionOutcome.DUPLICATE,
                    "reason": f"duplicate delivery; original outcome={receipt.outcome.value}",
                }
            )

        current = self.store.load().messages.get(event.message.stable_key())
        if current is not None and self._is_stale(current, event):
            return self._finish(
                canonical_id,
                actor=actor,
                outcome=ConversationProjectionOutcome.STALE,
                reason="provider event is not newer than the canonical external-message position",
            )

        if event.event_kind == ConversationEventKind.MESSAGE_DELETED:
            return self._finish(
                canonical_id,
                actor=actor,
                outcome=ConversationProjectionOutcome.DELETED,
                message_state=self._state_for_event(
                    event,
                    actor=actor,
                    canonical_event_id=canonical_id,
                    deleted=True,
                ),
            )

        if event.event_kind == ConversationEventKind.MESSAGE_EDITED:
            return self._finish(
                canonical_id,
                actor=actor,
                outcome=ConversationProjectionOutcome.UPDATED,
                reason="edit recorded as provider conversation state; existing canonical work is not replayed",
                message_state=self._state_for_event(
                    event,
                    actor=actor,
                    canonical_event_id=canonical_id,
                ),
            )

        if event.event_kind in {
            ConversationEventKind.REACTION_ADDED,
            ConversationEventKind.REACTION_REMOVED,
        }:
            return self._finish(
                canonical_id,
                actor=actor,
                outcome=ConversationProjectionOutcome.REACTION,
                reason="reaction recorded without creating canonical work",
            )

        text = self._message_text(event)
        if not text:
            return self._finish(
                canonical_id,
                actor=actor,
                outcome=ConversationProjectionOutcome.IGNORED,
                reason="normalized message contains no routable text or references",
                message_state=self._state_for_event(
                    event,
                    actor=actor,
                    canonical_event_id=canonical_id,
                ),
            )

        message = BotInboundMessage(
            provider=provider,
            external_conversation_id=event.message.conversation.conversation_id,
            text=text,
            connection_id=event.connection_id,
            sender_id=event.sender.provider_user_id,
            sender_name=event.sender.display_name,
            project_id=event.project_id,
            external_name=event.message.conversation.display_name,
            external_thread_id=event.message.conversation.thread_id,
            message_id=event.message.message_id,
        )
        try:
            result = await self.route_handler(message)
        except Exception as exc:
            return self._finish(
                canonical_id,
                actor=actor,
                outcome=ConversationProjectionOutcome.REQUIRES_RECONCILIATION,
                reason=(
                    "routing claim is durable but downstream routing outcome is uncertain: "
                    f"{type(exc).__name__}: {exc}"
                )[:2000],
            )

        projection = self._routing_projection(result)
        return self._finish(
            canonical_id,
            actor=actor,
            outcome=ConversationProjectionOutcome.ROUTED,
            thread_id=(
                str(result.get("threadId"))
                if result.get("threadId") is not None
                else None
            ),
            queued_id=(
                str(result.get("queuedId"))
                if result.get("queuedId") is not None
                else None
            ),
            routing_result=projection,
            message_state=self._state_for_event(
                event,
                actor=actor,
                canonical_event_id=canonical_id,
            ),
        )

    @staticmethod
    def legacy_routing_result(
        receipt: ConversationProjectionReceipt,
    ) -> dict[str, Any]:
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

    async def ingest_raw(
        self,
        provider_type: str,
        provider_instance: str,
        payload: object,
        *,
        actor: AuthenticationActor,
        connection_id: str | None = None,
        project_id: str | None = None,
    ) -> tuple[ConversationProjectionReceipt, ...]:
        adapter = self.registry.resolve(provider_type, provider_instance)
        events = await adapter.normalize_events(
            payload,
            connection_id=connection_id,
            project_id=project_id,
        )
        return tuple(
            [
                await self.ingest(event, actor=actor)
                for event in events
            ]
        )

    async def ingest_bot_message(
        self,
        message: BotInboundMessage,
        *,
        actor: AuthenticationActor,
        provider_instance: str | None = None,
        occurred_at: float | None = None,
        provider_sequence: int | None = None,
        provider_revision: str | None = None,
    ) -> ConversationProjectionReceipt:
        from codex_web.conversation_channels import (
            ConversationChannelEvent,
            ConversationEventKind,
            ConversationSenderRef,
            ConversationSenderKind,
            ExternalConversationRef,
            ExternalMessageRef,
        )

        instance = (
            provider_instance
            or message.connection_id
            or f"{message.provider}:default"
        )
        message_id = (message.message_id or "").strip()
        if not message_id:
            material = json.dumps(
                {
                    "provider": message.provider,
                    "instance": instance,
                    "conversation": message.external_conversation_id,
                    "thread": message.external_thread_id,
                    "sender": message.sender_id,
                    "text": message.text,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            message_id = f"synthetic-{hashlib.sha256(material).hexdigest()[:32]}"
        event = ConversationChannelEvent(
            delivery_id=f"message:{message_id}",
            event_kind=ConversationEventKind.MESSAGE_CREATED,
            message=ExternalMessageRef(
                conversation=ExternalConversationRef(
                    provider_type=message.provider,
                    provider_instance=instance,
                    conversation_id=message.external_conversation_id,
                    thread_id=message.external_thread_id,
                    display_name=message.external_name,
                ),
                message_id=message_id,
            ),
            sender=ConversationSenderRef(
                provider_user_id=message.sender_id,
                display_name=message.sender_name,
                kind=ConversationSenderKind.UNKNOWN,
            ),
            text=message.text,
            occurred_at=(
                float(self.clock())
                if occurred_at is None
                else float(occurred_at)
            ),
            provider_sequence=provider_sequence,
            provider_revision=provider_revision,
            connection_id=message.connection_id,
            project_id=message.project_id,
        )
        return await self.ingest(event, actor=actor)

    def receipts(
        self,
        *,
        actor: AuthenticationActor,
        limit: int = 200,
    ) -> tuple[ConversationProjectionReceipt, ...]:
        if limit < 1 or limit > 1000:
            raise ConversationChannelValidationError(
                "conversation receipt limit must be between 1 and 1000"
            )
        rows = [
            item
            for item in self.store.load().receipts.values()
            if self._same_scope(item, actor)
        ]
        rows.sort(
            key=lambda item: (item.updated_at, item.canonical_event_id),
            reverse=True,
        )
        return tuple(rows[:limit])

    def message_states(
        self,
        *,
        actor: AuthenticationActor,
        provider_type: str | None = None,
        limit: int = 200,
    ) -> tuple[ConversationMessageState, ...]:
        if limit < 1 or limit > 1000:
            raise ConversationChannelValidationError(
                "conversation message limit must be between 1 and 1000"
            )
        rows = [
            item
            for item in self.store.load().messages.values()
            if self._same_scope(item, actor)
        ]
        if provider_type is not None:
            rows = [
                item
                for item in rows
                if item.message.conversation.provider_type.casefold()
                == provider_type.casefold()
            ]
        rows.sort(
            key=lambda item: (item.updated_at, item.last_canonical_event_id),
            reverse=True,
        )
        return tuple(rows[:limit])
