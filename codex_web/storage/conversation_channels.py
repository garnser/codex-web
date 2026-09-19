from __future__ import annotations

from typing import Any, Callable

from codex_web.compatibility import MigrationRegistry
from codex_web.conversation_channels import (
    CONVERSATION_CHANNEL_CONTRACT,
    ConversationChannelState,
)
from codex_web.storage.state_store import StateStore


CONVERSATION_CHANNEL_MIGRATIONS = MigrationRegistry("conversation-channel")


class ConversationChannelStore:
    namespace = "conversation_channels"

    def __init__(self, store: StateStore) -> None:
        self.store = store

    def _decode(self, payload: Any) -> ConversationChannelState:
        if payload is None:
            return ConversationChannelState()
        if not isinstance(payload, dict):
            raise ValueError("conversation channel state must be an object")
        version = str(
            payload.get("schema_version")
            or CONVERSATION_CHANNEL_CONTRACT.current
        )
        if version != CONVERSATION_CHANNEL_CONTRACT.current:
            payload = CONVERSATION_CHANNEL_MIGRATIONS.migrate(
                payload,
                from_version=version,
                to_version=CONVERSATION_CHANNEL_CONTRACT.current,
            )
        CONVERSATION_CHANNEL_CONTRACT.require(
            payload.get("schema_version", "")
        )
        return ConversationChannelState.model_validate(payload)

    def load(self) -> ConversationChannelState:
        return self._decode(self.store.get(self.namespace))

    def update(
        self,
        updater: Callable[[ConversationChannelState], ConversationChannelState],
    ) -> ConversationChannelState:
        raw = self.store.update(
            self.namespace,
            lambda current: updater(self._decode(current)).model_dump(mode="json"),
            default=ConversationChannelState().model_dump(mode="json"),
        )
        return self._decode(raw)
