from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.compatibility import ContractSpec


CONVERSATION_CHANNEL_CONTRACT = ContractSpec(
    "conversation-channel",
    "1.0",
    ("1.0",),
)


class ConversationChannelCapability(StrEnum):
    EVENTS = "events"
    HISTORY = "history"
    THREADS = "threads"
    ATTACHMENTS = "attachments"
    REFERENCES = "references"
    MENTIONS = "mentions"
    COMMANDS = "commands"
    REACTIONS = "reactions"
    EDITS = "edits"
    DELETES = "deletes"
    CURSORS = "cursors"


@dataclass(frozen=True, slots=True)
class ConversationChannelCapabilities:
    supported: frozenset[ConversationChannelCapability] = field(
        default_factory=frozenset
    )

    def supports(self, capability: ConversationChannelCapability) -> bool:
        return capability in self.supported

    def require(self, capability: ConversationChannelCapability) -> None:
        if capability not in self.supported:
            raise UnsupportedConversationChannelCapability(capability)


class UnsupportedConversationChannelCapability(RuntimeError):
    def __init__(self, capability: ConversationChannelCapability) -> None:
        self.capability = capability
        super().__init__(
            f"ConversationChannel does not support capability: {capability.value}"
        )


class ConversationEventKind(StrEnum):
    MESSAGE_CREATED = "message_created"
    MESSAGE_EDITED = "message_edited"
    MESSAGE_DELETED = "message_deleted"
    REACTION_ADDED = "reaction_added"
    REACTION_REMOVED = "reaction_removed"


class ConversationSenderKind(StrEnum):
    HUMAN = "human"
    SERVICE = "service"
    BOT = "bot"
    UNKNOWN = "unknown"


class ConversationProjectionOutcome(StrEnum):
    ROUTING = "routing"
    ROUTED = "routed"
    DUPLICATE = "duplicate"
    STALE = "stale"
    UPDATED = "updated"
    DELETED = "deleted"
    REACTION = "reaction"
    IGNORED = "ignored"
    REQUIRES_RECONCILIATION = "requires_reconciliation"
    FAILED = "failed"


class ExternalConversationRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    provider_type: str = Field(min_length=1, max_length=100)
    provider_instance: str = Field(min_length=1, max_length=1000)
    conversation_id: str = Field(min_length=1, max_length=1000)
    thread_id: str | None = Field(default=None, max_length=1000)
    display_name: str | None = Field(default=None, max_length=500)

    def stable_key(self) -> str:
        return "::".join(
            (
                self.provider_type.casefold(),
                self.provider_instance.casefold(),
                self.conversation_id,
                self.thread_id or "",
            )
        )


class ExternalMessageRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    conversation: ExternalConversationRef
    message_id: str = Field(min_length=1, max_length=1000)

    def stable_key(self) -> str:
        return f"{self.conversation.stable_key()}::{self.message_id}"


class ConversationSenderRef(BaseModel):
    """Untrusted provider identity metadata; never grants canonical authority."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    provider_user_id: str | None = Field(default=None, max_length=1000)
    display_name: str | None = Field(default=None, max_length=500)
    kind: ConversationSenderKind = ConversationSenderKind.UNKNOWN
    provider_claims: tuple[str, ...] = ()

    @model_validator(mode="after")
    def normalize_claims(self) -> "ConversationSenderRef":
        object.__setattr__(
            self,
            "provider_claims",
            tuple(
                dict.fromkeys(
                    value.strip()
                    for value in self.provider_claims
                    if value and value.strip()
                )
            ),
        )
        return self


class ConversationAttachmentRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    external_id: str = Field(min_length=1, max_length=1000)
    name: str | None = Field(default=None, max_length=500)
    media_type: str | None = Field(default=None, max_length=200)
    size_bytes: int | None = Field(default=None, ge=0)
    provider_url: str | None = Field(default=None, max_length=3000)
    reference_only: bool = True


class ConversationReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    kind: str = Field(min_length=1, max_length=100)
    value: str = Field(min_length=1, max_length=3000)
    label: str | None = Field(default=None, max_length=500)


class ConversationMention(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    kind: str = Field(min_length=1, max_length=100)
    external_id: str | None = Field(default=None, max_length=1000)
    text: str | None = Field(default=None, max_length=500)


class ConversationReaction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=200)
    sender: ConversationSenderRef | None = None


class ConversationChannelEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    contract_version: str = CONVERSATION_CHANNEL_CONTRACT.current
    delivery_id: str = Field(min_length=1, max_length=1000)
    event_kind: ConversationEventKind
    message: ExternalMessageRef
    sender: ConversationSenderRef = Field(default_factory=ConversationSenderRef)
    text: str | None = Field(default=None, max_length=32000)
    attachments: tuple[ConversationAttachmentRef, ...] = ()
    references: tuple[ConversationReference, ...] = ()
    mentions: tuple[ConversationMention, ...] = ()
    reactions: tuple[ConversationReaction, ...] = ()
    occurred_at: float = Field(default_factory=time.time)
    provider_sequence: int | None = Field(default=None, ge=0)
    provider_revision: str | None = Field(default=None, max_length=1000)
    provider_cursor: str | None = Field(default=None, max_length=2000)
    connection_id: str | None = Field(default=None, max_length=1000)
    project_id: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def validate_event(self) -> "ConversationChannelEvent":
        CONVERSATION_CHANNEL_CONTRACT.require(self.contract_version)
        if len(self.attachments) > 50:
            raise ValueError("conversation event exceeds 50 attachments")
        if len(self.references) > 100:
            raise ValueError("conversation event exceeds 100 references")
        if len(self.mentions) > 100:
            raise ValueError("conversation event exceeds 100 mentions")
        if len(self.reactions) > 100:
            raise ValueError("conversation event exceeds 100 reactions")
        if (
            self.event_kind
            in {ConversationEventKind.MESSAGE_CREATED, ConversationEventKind.MESSAGE_EDITED}
            and not (self.text or "").strip()
            and not self.attachments
            and not self.references
        ):
            raise ValueError("message create/edit event requires text, attachment, or reference")
        return self

    def semantic_fingerprint(self) -> str:
        payload = self.model_dump(mode="json", exclude={"delivery_id"})
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return digest


@dataclass(frozen=True, slots=True)
class ConversationChannelHistoryPage:
    events: tuple[ConversationChannelEvent, ...]
    next_cursor: str | None = None
    exhausted: bool = True

    def __post_init__(self) -> None:
        if not self.exhausted and not (self.next_cursor or "").strip():
            raise ValueError("non-exhausted conversation history requires next_cursor")


@runtime_checkable
class ConversationChannel(Protocol):
    provider_type: str
    provider_instance: str
    contract_version: str
    capabilities: ConversationChannelCapabilities

    async def normalize_events(
        self,
        payload: object,
        *,
        connection_id: str | None = None,
        project_id: str | None = None,
    ) -> tuple[ConversationChannelEvent, ...]:
        ...

    async def history(
        self,
        conversation: ExternalConversationRef,
        *,
        cursor: str | None = None,
        limit: int = 100,
    ) -> ConversationChannelHistoryPage:
        ...


class ConversationMessageState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    organization_id: str
    workspace_id: str
    message: ExternalMessageRef
    event_kind: ConversationEventKind
    occurred_at: float
    provider_sequence: int | None = None
    provider_revision: str | None = None
    provider_cursor: str | None = None
    deleted: bool = False
    last_canonical_event_id: str
    updated_at: float = Field(default_factory=time.time)


class ConversationProjectionReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canonical_event_id: str
    organization_id: str
    workspace_id: str
    message_key: str
    outcome: ConversationProjectionOutcome
    thread_id: str | None = None
    queued_id: str | None = None
    reason: str | None = None
    routing_result: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


class ConversationChannelState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = CONVERSATION_CHANNEL_CONTRACT.current
    messages: dict[str, ConversationMessageState] = Field(default_factory=dict)
    receipts: dict[str, ConversationProjectionReceipt] = Field(default_factory=dict)
