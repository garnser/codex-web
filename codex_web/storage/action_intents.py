from __future__ import annotations

import hashlib
import json

from typing import Any, Callable
from urllib.parse import quote

from codex_web.action_intents import ActionIntent, ActionIntentState, ActionIntentStatus
from codex_web.compatibility import ContractSpec, MigrationRegistry
from codex_web.storage.state_store import StateStore


ACTION_INTENT_STATE_CONTRACT = ContractSpec("action-intent-state", "1.2", ("1.2",))
ACTION_INTENT_STATE_MIGRATIONS = MigrationRegistry("action-intent-state")
ACTION_INTENT_STATE_MIGRATIONS.register(
    "0.0",
    "1.0",
    lambda payload: {
        "schema_version": "1.0",
        **{key: value for key, value in payload.items() if key != "schema_version"},
    },
)


def _security_migration(payload: dict[str, Any]) -> dict[str, Any]:
    migrated = dict(payload)
    intents: list[dict[str, Any]] = []
    for raw in migrated.get("intents", []) or []:
        item = dict(raw)
        definition = dict(item.get("action_definition") or {})
        authority = dict(item.get("authority_decision") or {})
        policy = dict(item.get("policy_decision") or {})
        resource_ids = list(item.get("resource_ids") or [])
        item.setdefault(
            "security_policy",
            {
                "sandbox": "workspace-write",
                "network": {
                    "enabled": False,
                    "allowed_schemes": ["https"],
                    "allowed_hosts": [],
                    "allowed_ports": [443],
                    "allowed_private_cidrs": [],
                    "max_redirects": 0,
                },
                "filesystem": {
                    "allowed_read_roots": [],
                    "allowed_write_roots": [],
                    "allow_symlink_escape": False,
                },
                "process": {
                    "allow_process_execution": True,
                    "allowed_executables": [],
                    "allow_shell": False,
                },
                "require_digest_for_executable_artifacts": True,
            },
        )
        item.setdefault(
            "security_decision",
            {
                "outcome": "deny",
                "source": "security:migration",
                "risk_class": str(definition.get("risk_class") or "medium"),
                "resource_ids": resource_ids,
                "sandbox": "workspace-write",
                "network_enabled": False,
                "authority_source": str(authority.get("source") or "unknown"),
                "policy_source": str(policy.get("source") or "unknown"),
                "reasons": [
                    "migrated pre-security-boundary intent requires re-evaluation"
                ],
            },
        )
        intents.append(item)
    migrated["intents"] = intents
    migrated["schema_version"] = "1.1"
    return migrated


ACTION_INTENT_STATE_MIGRATIONS.register("1.0", "1.1", _security_migration)
ACTION_INTENT_STATE_MIGRATIONS.register(
    "1.1",
    "1.2",
    lambda payload: {
        **payload,
        "schema_version": "1.2",
        "intents": [
            {**dict(item), "failure": dict(item).get("failure")}
            for item in payload.get("intents", [])
        ],
    },
)


class ActionIntentStore:
    namespace = "action_intents"

    def __init__(self, store: StateStore) -> None:
        self.store = store

    def _decode(self, payload: Any) -> ActionIntentState:
        if payload is None:
            return ActionIntentState()
        if not isinstance(payload, dict):
            raise ValueError("action intent state must be an object")
        version = str(payload.get("schema_version") or "0.0")
        if version != ACTION_INTENT_STATE_CONTRACT.current:
            payload = ACTION_INTENT_STATE_MIGRATIONS.migrate(
                payload,
                from_version=version,
                to_version=ACTION_INTENT_STATE_CONTRACT.current,
            )
        ACTION_INTENT_STATE_CONTRACT.require(payload.get("schema_version", ""))
        return ActionIntentState.model_validate(payload)

    records_namespace = "action_intents.records.v1"
    _meta_key = "meta"
    _fields = ("intents", "receipts", "verifications", "inbox")

    @staticmethod
    def _scope(organization_id: str, workspace_id: str) -> str:
        return f"{quote(organization_id, safe='')}:{quote(workspace_id, safe='')}:"

    @classmethod
    def _idempotency_key(cls, organization_id, workspace_id, binding_id, action_id, key):
        identity = json.dumps([organization_id, workspace_id, binding_id, action_id, key])
        return "idempotency:" + hashlib.sha256(identity.encode()).hexdigest()

    def find_idempotent(self, organization_id, workspace_id, binding_id, action_id, key):
        self._ensure_records()
        alias = self.store.record_get(self.records_namespace, self._idempotency_key(
            organization_id, workspace_id, binding_id, action_id, key,
        ))
        return self.get(alias["id"]) if alias is not None else None

    def insert(self, intent: ActionIntent) -> ActionIntent:
        self._ensure_records()
        alias_key = self._idempotency_key(intent.organization_id, intent.workspace_id,
                                          intent.binding_id, intent.action_id, intent.idempotency_key)
        result = []
        def apply(current):
            alias = current[alias_key]
            if alias is not None and intent.status != ActionIntentStatus.CANCELLED:
                result.append(alias["id"])
                return {}
            result.append(intent.id)
            return self._records(ActionIntentState(intents=[intent]))
        self.store.record_mutate(self.records_namespace, (alias_key,), apply)
        if result[0] == intent.id:
            return intent
        existing = self.get(result[0])
        if existing is None:
            raise ValueError("action idempotency alias points to missing intent")
        return existing

    @classmethod
    def _indexes(cls, item: ActionIntent) -> dict[str, Any]:
        scope = cls._scope(item.organization_id, item.workspace_id)
        indexes = {}
        if item.status != ActionIntentStatus.CANCELLED:
            indexes[cls._idempotency_key(item.organization_id, item.workspace_id,
                                        item.binding_id, item.action_id, item.idempotency_key)] = {"id": item.id}
        if item.status == ActionIntentStatus.PENDING:
            indexes[f"pending:{scope}{item.created_at:024.9f}:{item.id}"] = {"id": item.id}
        if item.status in {ActionIntentStatus.CLAIMED, ActionIntentStatus.EXECUTING} and item.lease:
            indexes[f"lease:{scope}{item.lease.expires_at:020.6f}:{item.id}"] = {
                "id": item.id, "expires_at": item.lease.expires_at,
            }
        return indexes

    @classmethod
    def _records(cls, state: ActionIntentState) -> dict[str, Any]:
        records = {}
        for field in cls._fields:
            for item in getattr(state, field):
                # Histories are ordered and scoped by their parent intent.
                if field == "intents":
                    key = f"intents:{item.id}"
                    records.update(cls._indexes(item))
                else:
                    timestamp = getattr(item, "received_at", getattr(item, "verified_at", 0.0))
                    key = f"{field}:{quote(item.intent_id or '', safe='')}:{timestamp:020.6f}:{item.id}"
                records[key] = item.model_dump(mode="json")
        return records

    def _ensure_records(self) -> None:
        meta = self.store.record_get(self.records_namespace, self._meta_key)
        if meta is not None:
            if meta != {"schema_version": "1.0"}:
                raise ValueError("unsupported action intent records schema")
            return
        # Upgrade both namespaces under the shared transaction lock. The old
        # document is a rollback checkpoint, never an active second authority.
        def migrate(documents):
            records = documents[self.records_namespace]
            if self._meta_key not in records:
                records = self._records(self._decode(documents[self.namespace]))
                records[self._meta_key] = {"schema_version": "1.0"}
            return {self.records_namespace: records, self.namespace: documents[self.namespace]}
        self.store.update_many(
            {self.namespace: ActionIntentState().model_dump(mode="json"), self.records_namespace: {}},
            migrate,
        )

    def _state(self, records: dict[str, Any]) -> ActionIntentState:
        return self._decode({
            "schema_version": ACTION_INTENT_STATE_CONTRACT.current,
            **{field: [raw for key, raw in sorted(records.items())
                       if key.startswith(field + ":") and raw is not None]
               for field in self._fields},
        })

    def get(self, intent_id: str) -> ActionIntent | None:
        self._ensure_records()
        raw = self.store.record_get(self.records_namespace, f"intents:{intent_id}")
        return ActionIntent.model_validate(raw) if raw is not None else None

    def _pages(self, prefix: str):
        after = None
        while True:
            page, after = self.store.record_page(
                self.records_namespace, key_prefix=prefix, after=after, limit=100,
            )
            yield from page.items()
            if after is None:
                return

    def indexed_intents(self, kind: str, *, organization_id: str | None = None,
                        workspace_id: str | None = None, now: float | None = None):
        self._ensure_records()
        prefix = kind + ":"
        if organization_id is not None and workspace_id is not None:
            prefix += self._scope(organization_id, workspace_id)
        for _, alias in self._pages(prefix):
            if kind == "lease" and now is not None and alias["expires_at"] > now:
                continue
            item = self.get(alias["id"])
            if item is not None:
                yield item

    def load(self, *, intent_id: str | None = None) -> ActionIntentState:
        self._ensure_records()
        if intent_id is None:
            return self._state(self.store.record_items(self.records_namespace))
        records = {f"intents:{intent_id}": self.store.record_get(
            self.records_namespace, f"intents:{intent_id}",
        )}
        for field in self._fields[1:]:
            records.update(self._pages(f"{field}:{quote(intent_id, safe='')}:"))
        return self._state(records)

    def flush_legacy_mirror(self) -> None:
        self.store.put(self.namespace, self.load().model_dump(mode="json"))

    def update(self, updater: Callable[[ActionIntentState], ActionIntentState], *,
               intent_ids: tuple[str, ...] | None = None) -> ActionIntentState:
        self._ensure_records()
        # Compatibility/bulk callers can still transform a complete snapshot;
        # execution hot paths explicitly select only their target intent rows.
        keys = (tuple(self.store.record_items(self.records_namespace)) if intent_ids is None
                else tuple(f"intents:{item_id}" for item_id in intent_ids))
        result = []
        def apply(current):
            state = self._state(current)
            before = self._records(state)
            updated = updater(state)
            after = self._records(updated)
            changes = {key: raw for key, raw in after.items() if before.get(key) != raw}
            changes.update({key: None for key in before.keys() - after.keys()})
            result.append(updated)
            return changes
        self.store.record_mutate(self.records_namespace, keys, apply)
        return result[0]
