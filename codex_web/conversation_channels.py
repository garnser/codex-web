from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class ConversationChannelError(RuntimeError):
    pass


class ConversationChannelUnsupportedError(ConversationChannelError):
    pass


class ConversationChannelCapability(StrEnum):
    INBOUND_MESSAGES = "inbound_messages"
    THREADS = "threads"
    ATTACHMENTS = "attachments"
    MENTIONS = "mentions"
    COMMANDS = "commands"
    EDITS = "edits"
    DELETES = "deletes"
    REACTIONS = "reactions"


class ConversationEventKind(StrEnum):
    MESSAGE = "message"
    EDIT = "edit"
    DELETE = "delete"
    REACTION = "reaction"


class ConversationAddress(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    provider_type: str = Field(min_length=1)
    provider_instance: str = Field(min_length=1)
    external_conversation_id: str = Field(min_length=1)
    external_thread_id: str | None = None


class ConversationSenderRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    external_id: str | None = None
    display_name: str | None = None
    is_service: bool = False


class ConversationAttachmentRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    external_id: str | None = None
    name: str | None = None
    media_type: str | None = None
    external_url: str | None = None


class ConversationFact(BaseModel):
    """Provider-neutral inbound conversation fact.

    External identities are routing/provenance references only. They never grant
    tenant membership, authority, or ActionIntent permission.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(min_length=1)
    event_kind: ConversationEventKind
    address: ConversationAddress
    message_id: str | None = None
    external_name: str | None = None
    sender: ConversationSenderRef = Field(default_factory=ConversationSenderRef)
    text: str | None = None
    attachments: tuple[ConversationAttachmentRef, ...] = ()
    mentions: tuple[str, ...] = ()
    command: str | None = None
    occurred_at: float | None = None
    provider_metadata: dict[str, str | int | float | bool | None] = Field(
        default_factory=dict
    )


@runtime_checkable
class ConversationChannelProvider(Protocol):
    provider_type: str

    def capabilities(self) -> frozenset[ConversationChannelCapability]:
        ...

    def normalize(
        self,
        payload: dict[str, Any],
        *,
        provider_instance: str,
        event_id: str | None = None,
    ) -> ConversationFact | None:
        ...
