from __future__ import annotations

import inspect
import threading

from typing import Any, Callable

from codex_web.compatibility import ContractSpec, MigrationRegistry
from codex_web.identity import IdentityState
from codex_web.storage.sqlite_state import SQLiteStateStore


IDENTITY_STATE_CONTRACT = ContractSpec("identity-state", "1.4", ("1.0", "1.1", "1.2", "1.3", "1.4"))
IDENTITY_STATE_MIGRATIONS = MigrationRegistry("identity-state")
IDENTITY_STATE_MIGRATIONS.register(
    "0.0",
    "1.0",
    lambda payload: {
        "schema_version": "1.0",
        **{key: value for key, value in payload.items() if key != "schema_version"},
    },
)

IDENTITY_STATE_MIGRATIONS.register(
    "1.3",
    "1.4",
    lambda payload: {
        **payload,
        "schema_version": "1.4",
        "authentication_policies": payload.get("authentication_policies", []),
        "authentication_policy_events": payload.get("authentication_policy_events", []),
    },
)
IDENTITY_STATE_MIGRATIONS.register(
    "1.0",
    "1.1",
    lambda payload: {
        **payload,
        "schema_version": "1.1",
        "memberships": [
            {
                **dict(item),
                "updated_at": dict(item).get("updated_at"),
                "updated_by": dict(item).get("updated_by"),
                "revoked_by": dict(item).get("revoked_by"),
            }
            for item in payload.get("memberships", [])
        ],
    },
)


class IdentityStateStore:
    namespace = "identity_state"

    def __init__(self, store: SQLiteStateStore) -> None:
        self.store = store

    @staticmethod
    def _empty() -> dict[str, Any]:
        return IdentityState().model_dump(mode="json")

    def _decode(self, payload: Any) -> IdentityState:
        if payload is None:
            return IdentityState()
        if not isinstance(payload, dict):
            raise ValueError("identity state must be an object")
        version = str(payload.get("schema_version") or "0.0")
        if version != IDENTITY_STATE_CONTRACT.current:
            payload = IDENTITY_STATE_MIGRATIONS.migrate(
                payload,
                from_version=version,
                to_version=IDENTITY_STATE_CONTRACT.current,
            )
        IDENTITY_STATE_CONTRACT.require(payload.get("schema_version", ""))
        return IdentityState.model_validate(payload)

    def load(self) -> IdentityState:
        return self._decode(self.store.get(self.namespace))

    def can_observe_reads(self) -> bool:
        return (type(self) is IdentityStateStore
                and all(getattr(getattr(self, name, None), "__self__", None) is self
                        and getattr(getattr(self, name, None), "__func__", None) is method
                        for name, method in _CANONICAL_IDENTITY_STORE_READS.items()))

    def observe_reads(self, callback):
        """Offer one lazy, expiring canonical catalog read to a sync owner."""
        if not _CANONICAL_IDENTITY_STORE_READS["can_observe_reads"](self):
            return callback(None)
        observation = _IdentityStateReadObservation(self)
        try:
            result = callback(observation)
            if inspect.isawaitable(result) or inspect.isgenerator(result) or inspect.isasyncgen(result):
                if inspect.iscoroutine(result) or inspect.isgenerator(result):
                    result.close()
                raise ValueError("Identity read observation callback must be synchronous")
            return result
        finally:
            observation.close()

    def save(self, state: IdentityState) -> IdentityState:
        payload = state.model_dump(mode="json")
        self.store.put(self.namespace, payload)
        return state.model_copy(deep=True)

    def update(self, updater: Callable[[IdentityState], IdentityState]) -> IdentityState:
        def apply(raw: Any) -> dict[str, Any]:
            current = self._decode(raw)
            updated = updater(current)
            return updated.model_dump(mode="json")

        payload = self.store.update(
            self.namespace,
            apply,
            default=self._empty(),
        )
        return self._decode(payload)

IDENTITY_STATE_MIGRATIONS.register(
    "1.1",
    "1.2",
    lambda payload: {
        **payload,
        "schema_version": "1.2",
        "memberships": [
            {
                **dict(item),
                "created_by": dict(item).get("created_by"),
            }
            for item in payload.get("memberships", [])
        ],
    },
)


IDENTITY_STATE_MIGRATIONS.register(
    "1.2",
    "1.3",
    lambda payload: {
        **payload,
        "schema_version": "1.3",
        "service_tokens": [
            {
                **dict(item),
                "created_by": dict(item).get("created_by"),
                "rotated_at": dict(item).get("rotated_at"),
                "rotated_by": dict(item).get("rotated_by"),
                "revoked_by": dict(item).get("revoked_by"),
            }
            for item in payload.get("service_tokens", [])
        ],
    },
)


class _IdentityStateReadObservation:
    """A lazy full-envelope observation, never an authorization decision."""

    def __init__(self, owner):
        self._owner = owner
        self._backend = owner.store
        self._thread = threading.get_ident()
        self._active = True
        self._state = None

    def require_owner(self, owner):
        if (not self._active or owner is not self._owner
                or owner.store is not self._backend
                or threading.get_ident() != self._thread
                or not _CANONICAL_IDENTITY_STORE_READS["can_observe_reads"](owner)):
            raise ValueError("Identity read observation is foreign, closed or on another thread")

    def load(self, owner):
        self.require_owner(owner)
        if self._state is None:
            # Keep full canonical migration/validation, including unrelated rows.
            # Loading is delayed until the original first identity lookup.
            self._state = owner.load()
        return self._state.model_copy(deep=True)

    def close(self):
        self._active = False
        self._state = None


_CANONICAL_IDENTITY_STORE_READS = {name: getattr(IdentityStateStore, name)
    for name in ("load", "_decode", "can_observe_reads", "observe_reads")}
