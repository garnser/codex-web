from __future__ import annotations

from typing import Any, Callable

from codex_web.action_intents import ActionIntentState
from codex_web.compatibility import ContractSpec, MigrationRegistry
from codex_web.storage.sqlite_state import SQLiteStateStore


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

    def __init__(self, store: SQLiteStateStore) -> None:
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

    def load(self) -> ActionIntentState:
        return self._decode(self.store.get(self.namespace))

    def update(self, updater: Callable[[ActionIntentState], ActionIntentState]) -> ActionIntentState:
        def apply(raw: Any) -> dict[str, Any]:
            state = self._decode(raw)
            return updater(state).model_dump(mode="json")

        payload = self.store.update(
            self.namespace,
            apply,
            default=ActionIntentState().model_dump(mode="json"),
        )
        return self._decode(payload)
