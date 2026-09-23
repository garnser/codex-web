from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from codex_web.attention import (
    ATTENTION_STATE_CONTRACT,
    AttentionItem,
    AttentionState,
    AttentionStatus,
)
from codex_web.compatibility import MigrationRegistry
from codex_web.storage.sqlite_state import SQLiteStateStore


ATTENTION_STATE_MIGRATIONS = MigrationRegistry("attention-state")
ATTENTION_STATE_MIGRATIONS.register(
    "0.0",
    "1.0",
    lambda payload: {
        "schema_version": "1.0",
        "items": dict(payload.get("items") or {}),
    },
)
ATTENTION_STATE_MIGRATIONS.register(
    "1.0",
    "1.1",
    lambda payload: {
        "schema_version": "1.1",
        "items": {
            key: {**dict(value), "project_id": dict(value).get("project_id")}
            for key, value in dict(payload.get("items") or {}).items()
        },
    },
)


class AttentionItemNotFoundError(KeyError):
    pass


class AttentionItemConflictError(RuntimeError):
    pass


class AttentionStore:
    namespace = "attention"

    def __init__(self, store: SQLiteStateStore) -> None:
        self.store = store

    def _decode(self, payload: Any) -> AttentionState:
        if payload is None:
            return AttentionState()
        if not isinstance(payload, dict):
            raise ValueError("attention state must be an object")
        version = str(payload.get("schema_version") or "0.0")
        if version != ATTENTION_STATE_CONTRACT.current:
            payload = ATTENTION_STATE_MIGRATIONS.migrate(
                payload,
                from_version=version,
                to_version=ATTENTION_STATE_CONTRACT.current,
            )
        ATTENTION_STATE_CONTRACT.require(payload.get("schema_version", ""))
        return AttentionState.model_validate(payload)

    def load(self) -> AttentionState:
        return self._decode(self.store.get(self.namespace))

    def list(self) -> list[AttentionItem]:
        return sorted(
            self.load().items.values(),
            key=lambda item: (-item.created_at, item.id),
        )

    def get(self, item_id: str) -> AttentionItem:
        item = self.load().items.get(item_id)
        if item is None:
            raise AttentionItemNotFoundError(item_id)
        return item

    def get_by_dedupe_key(self, dedupe_key: str) -> AttentionItem | None:
        return next(
            (item for item in self.load().items.values() if item.dedupe_key == dedupe_key),
            None,
        )

    def _update(
        self,
        updater: Callable[[AttentionState], AttentionState],
    ) -> AttentionState:
        payload = self.store.update(
            self.namespace,
            lambda raw: updater(self._decode(raw)).model_dump(mode="json"),
            default=AttentionState().model_dump(mode="json"),
        )
        return self._decode(payload)

    def upsert(self, item: AttentionItem) -> AttentionItem:
        result: dict[str, AttentionItem] = {}

        def apply(state: AttentionState) -> AttentionState:
            existing = next(
                (
                    current
                    for current in state.items.values()
                    if current.dedupe_key == item.dedupe_key
                ),
                None,
            )
            if existing is None:
                state.items[item.id] = item
                result["value"] = item
                return state

            merged = item.model_copy(
                update={
                    "id": existing.id,
                    "created_at": existing.created_at,
                    "created_by": existing.created_by,
                    "revision": existing.revision + 1,
                    "acknowledged_by_identity_id": existing.acknowledged_by_identity_id,
                    "acknowledged_at": existing.acknowledged_at,
                    "snoozed_until": existing.snoozed_until,
                    "resolved_by_identity_id": (
                        None
                        if item.status in {
                            AttentionStatus.OPEN,
                            AttentionStatus.ESCALATED,
                        }
                        else existing.resolved_by_identity_id
                    ),
                    "resolved_at": (
                        None
                        if item.status in {
                            AttentionStatus.OPEN,
                            AttentionStatus.ESCALATED,
                        }
                        else existing.resolved_at
                    ),
                    "resolution_reason": (
                        None
                        if item.status in {
                            AttentionStatus.OPEN,
                            AttentionStatus.ESCALATED,
                        }
                        else existing.resolution_reason
                    ),
                    "escalation_count": existing.escalation_count,
                    "escalation_schedule_id": (
                        item.escalation_schedule_id
                        or existing.escalation_schedule_id
                    ),
                }
            )
            state.items[existing.id] = merged
            result["value"] = merged
            return state

        self._update(apply)
        return result["value"]

    def transition(
        self,
        item_id: str,
        *,
        actor_id: str,
        transition: Callable[[AttentionItem], AttentionItem],
        now: float | None = None,
    ) -> AttentionItem:
        result: dict[str, AttentionItem] = {}
        timestamp = time.time() if now is None else float(now)

        def apply(state: AttentionState) -> AttentionState:
            current = state.items.get(item_id)
            if current is None:
                raise AttentionItemNotFoundError(item_id)
            updated = transition(current).model_copy(
                update={
                    "revision": current.revision + 1,
                    "updated_at": timestamp,
                    "updated_by": actor_id,
                }
            )
            state.items[item_id] = updated
            result["value"] = updated
            return state

        self._update(apply)
        return result["value"]
