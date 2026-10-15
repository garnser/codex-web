from __future__ import annotations

from typing import Any, Callable

from codex_web.compatibility import MigrationRegistry
from codex_web.conversation_channels import (
    CONVERSATION_CHANNEL_STATE_CONTRACT,
    ConversationChannelState,
)
from codex_web.storage.state_store import StateStore


CONVERSATION_CHANNEL_MIGRATIONS = MigrationRegistry("conversation-channel")
CONVERSATION_CHANNEL_MIGRATIONS.register(
    "1.0", "2.0", lambda payload: {**payload, "schema_version": "2.0"},
)
def _migrate_protected_retention(payload):
    # Old pending inputs have no captured authority. Never infer permission to
    # replay them under the new reader's current connection/profile settings.
    receipts = {}
    for key, raw in (payload.get("receipts") or {}).items():
        row = dict(raw)
        if row.get("retained_event") is not None:
            row["connection_id"] = row["retained_event"].get("connection_id")
            row["project_id"] = row["retained_event"].get("project_id")
            if row.get("outcome") in {"pending", "routing"}:
                row["outcome"] = "requires_reconciliation"
                row["reason"] = "legacy retention has no exact authority/protection snapshot; governed migration required"
        receipts[key] = row
    return {**payload, "schema_version": "3.0", "receipts": receipts}

CONVERSATION_CHANNEL_MIGRATIONS.register("2.0", "3.0", _migrate_protected_retention)


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
            or "1.0"
        )
        if version != CONVERSATION_CHANNEL_STATE_CONTRACT.current:
            payload = CONVERSATION_CHANNEL_MIGRATIONS.migrate(
                payload,
                from_version=version,
                to_version=CONVERSATION_CHANNEL_STATE_CONTRACT.current,
            )
        CONVERSATION_CHANNEL_STATE_CONTRACT.require(
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
