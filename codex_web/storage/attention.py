from __future__ import annotations

import json
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
    record_namespace = "attention-items"
    index_namespace = "attention-index"
    index_version = 1

    def __init__(self, store: SQLiteStateStore) -> None:
        self.store = store

    @staticmethod
    def _index_key(*parts: str) -> str:
        return json.dumps(list(parts), separators=(",", ":"))

    @classmethod
    def _empty_index(cls) -> dict[str, Any]:
        return {
            "version": cls.index_version,
            "dedupe": {},
            "tenant": {},
            "status": {},
            "severity": {},
            "project": {},
            "type": {},
            "owner": {},
            "recipient_identity": {},
            "recipient_team": {},
            "unassigned": {},
        }

    @classmethod
    def _build_index(cls, items) -> dict[str, Any]:
        index = cls._empty_index()
        ordered = sorted(items, key=lambda item: (-item.created_at, item.id))

        def append(bucket: str, key: str, item_id: str) -> None:
            index[bucket].setdefault(key, []).append(item_id)

        for item in ordered:
            tenant = cls._index_key(item.organization_id, item.workspace_id)
            append("tenant", tenant, item.id)
            append("status", cls._index_key(tenant, item.status.value), item.id)
            append("severity", cls._index_key(tenant, item.severity.value), item.id)
            append("type", cls._index_key(tenant, item.type), item.id)
            if item.project_id:
                append("project", cls._index_key(tenant, item.project_id), item.id)
            if item.owner_identity_id:
                append("owner", cls._index_key(tenant, item.owner_identity_id), item.id)
            for identity_id in item.recipient_identity_ids:
                append(
                    "recipient_identity",
                    cls._index_key(tenant, identity_id),
                    item.id,
                )
            for team_id in item.recipient_team_ids:
                append(
                    "recipient_team",
                    cls._index_key(tenant, team_id),
                    item.id,
                )
            if (
                item.owner_identity_id is None
                and not item.recipient_identity_ids
                and not item.recipient_team_ids
            ):
                append("unassigned", tenant, item.id)
            index["dedupe"][
                cls._index_key(
                    item.organization_id,
                    item.workspace_id,
                    item.dedupe_key,
                )
            ] = item.id
        return index

    def _records_state(self, records: dict[str, Any]) -> AttentionState:
        return AttentionState(
            schema_version=ATTENTION_STATE_CONTRACT.current,
            items={
                item_id: AttentionItem.model_validate(payload)
                for item_id, payload in records.items()
            },
        )

    def _ensure_indexed(self) -> None:
        if self.store.record_collection_exists(self.record_namespace):
            records = self.store.record_items(self.record_namespace)
            raw_index = self.store.get(self.index_namespace)
            if (
                not isinstance(raw_index, dict)
                or raw_index.get("version") != self.index_version
            ):
                state = self._records_state(records)
                self.store.put(
                    self.index_namespace,
                    self._build_index(state.items.values()),
                )
            if self.store.contains(self.namespace):
                self.store.delete(self.namespace)
            return

        legacy = self._decode(self.store.get(self.namespace))
        records = {
            item_id: item.model_dump(mode="json")
            for item_id, item in legacy.items.items()
        }
        self.store.record_replace(self.record_namespace, records)
        self.store.put(
            self.index_namespace,
            self._build_index(legacy.items.values()),
        )
        if self.store.contains(self.namespace):
            self.store.delete(self.namespace)

    def _index(self) -> dict[str, Any]:
        self._ensure_indexed()
        raw = self.store.get(self.index_namespace)
        if not isinstance(raw, dict) or raw.get("version") != self.index_version:
            records = self.store.record_items(self.record_namespace)
            index = self._build_index(self._records_state(records).items.values())
            self.store.put(self.index_namespace, index)
            return index
        return raw

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
        self._ensure_indexed()
        return self._records_state(
            self.store.record_items(self.record_namespace)
        )

    def list(self) -> list[AttentionItem]:
        return sorted(
            self.load().items.values(),
            key=lambda item: (-item.created_at, item.id),
        )

    def get(self, item_id: str) -> AttentionItem:
        self._ensure_indexed()
        raw = self.store.record_get(self.record_namespace, item_id)
        if raw is None:
            raise AttentionItemNotFoundError(item_id)
        return AttentionItem.model_validate(raw)

    def get_by_dedupe_key(
        self,
        dedupe_key: str,
        *,
        organization_id: str | None = None,
        workspace_id: str | None = None,
    ) -> AttentionItem | None:
        if organization_id is None or workspace_id is None:
            return next(
                (item for item in self.list() if item.dedupe_key == dedupe_key),
                None,
            )
        index = self._index()
        item_id = index["dedupe"].get(
            self._index_key(organization_id, workspace_id, dedupe_key)
        )
        if not item_id:
            return None
        try:
            return self.get(item_id)
        except AttentionItemNotFoundError:
            return None

    def query_page(
        self,
        *,
        organization_id: str,
        workspace_id: str,
        identity_id: str,
        team_ids: tuple[str, ...] = (),
        is_admin: bool = False,
        limit: int = 50,
        cursor: int = 0,
        status: str | None = None,
        severity: str | None = None,
        project_id: str | None = None,
        item_type: str | None = None,
        assignee: str | None = None,
    ) -> tuple[list[AttentionItem], int | None, int]:
        index = self._index()
        tenant = self._index_key(organization_id, workspace_id)
        ordered = list(index["tenant"].get(tenant, ()))
        selected = set(ordered)

        def intersect(bucket: str, value: str) -> None:
            nonlocal selected
            selected &= set(
                index[bucket].get(self._index_key(tenant, value), ())
            )

        if not is_admin:
            visible = set(index["unassigned"].get(tenant, ()))
            visible.update(
                index["owner"].get(self._index_key(tenant, identity_id), ())
            )
            visible.update(
                index["recipient_identity"].get(
                    self._index_key(tenant, identity_id),
                    (),
                )
            )
            for team_id in team_ids:
                visible.update(
                    index["recipient_team"].get(
                        self._index_key(tenant, team_id),
                        (),
                    )
                )
            selected &= visible

        if status:
            if status == "active":
                active = set()
                for value in (
                    AttentionStatus.OPEN,
                    AttentionStatus.ACKNOWLEDGED,
                    AttentionStatus.SNOOZED,
                    AttentionStatus.ESCALATED,
                ):
                    active.update(
                        index["status"].get(
                            self._index_key(tenant, value.value),
                            (),
                        )
                    )
                selected &= active
            else:
                intersect("status", status)
        if severity:
            intersect("severity", severity)
        if project_id:
            intersect("project", project_id)
        if item_type:
            intersect("type", item_type)
        if assignee:
            assignee_id = identity_id if assignee == "me" else assignee
            assignee_ids = set(
                index["owner"].get(
                    self._index_key(tenant, assignee_id),
                    (),
                )
            )
            assignee_ids.update(
                index["recipient_identity"].get(
                    self._index_key(tenant, assignee_id),
                    (),
                )
            )
            selected &= assignee_ids

        matched = [item_id for item_id in ordered if item_id in selected]
        total = len(matched)
        offset = max(0, int(cursor))
        bounded_limit = max(1, min(int(limit), 100))
        page_ids = matched[offset:offset + bounded_limit]
        page: list[AttentionItem] = []
        for item_id in page_ids:
            raw = self.store.record_get(self.record_namespace, item_id)
            if raw is not None:
                page.append(AttentionItem.model_validate(raw))
        next_cursor = offset + len(page_ids)
        if next_cursor >= total:
            next_cursor = None
        return page, next_cursor, total

    def _update(
        self,
        updater: Callable[[AttentionState], AttentionState],
    ) -> AttentionState:
        self._ensure_indexed()

        def apply(documents: dict[str, Any]) -> dict[str, Any]:
            state = self._records_state(
                dict(documents[self.record_namespace] or {})
            )
            updated = updater(state)
            records = {
                item_id: item.model_dump(mode="json")
                for item_id, item in updated.items.items()
            }
            return {
                self.record_namespace: records,
                self.index_namespace: self._build_index(updated.items.values()),
            }

        documents = self.store.update_many(
            {
                self.record_namespace: {},
                self.index_namespace: self._empty_index(),
            },
            apply,
        )
        return self._records_state(
            dict(documents[self.record_namespace] or {})
        )

    def upsert(self, item: AttentionItem) -> AttentionItem:
        result: dict[str, AttentionItem] = {}

        def apply(state: AttentionState) -> AttentionState:
            existing = next(
                (
                    current
                    for current in state.items.values()
                    if current.dedupe_key == item.dedupe_key
                    and current.organization_id == item.organization_id
                    and current.workspace_id == item.workspace_id
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
