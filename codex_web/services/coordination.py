from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from codex_web.coordination import (
    CoordinationFenceError,
    CoordinationHealth,
    CoordinationLease,
)
from codex_web.storage.state_store import StateStore


class CoordinationState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    leases: dict[str, CoordinationLease] = Field(default_factory=dict)
    last_fencing_tokens: dict[str, int] = Field(default_factory=dict)


class StateStoreCoordinationBackend:
    """Coordination backed by the same transactional store as canonical state.

    With SQLite this is a local/single-host coordination backend. With the
    PostgreSQL StateStore it is shared and fenced across control-plane instances.
    """

    namespace = "coordination"

    def __init__(
        self,
        store: StateStore,
        *,
        backend_id: str | None = None,
        clock=time.time,
    ) -> None:
        self.store = store
        self.clock = clock
        status = store.status()
        self.shared = bool(status.get("shared", False))
        self.backend_id = backend_id or (
            f"state-store:{status.get('backend') or 'unknown'}"
        )

    @staticmethod
    def _decode(raw: Any) -> CoordinationState:
        if raw is None:
            return CoordinationState()
        return CoordinationState.model_validate(raw)

    def _update(
        self,
        updater: Callable[[CoordinationState], CoordinationState],
    ) -> CoordinationState:
        raw = self.store.update(
            self.namespace,
            lambda value: updater(self._decode(value)).model_dump(mode="json"),
            default=CoordinationState().model_dump(mode="json"),
        )
        return self._decode(raw)

    def acquire(
        self,
        key: str,
        owner_id: str,
        *,
        lease_seconds: float,
        now: float | None = None,
    ) -> CoordinationLease | None:
        key = str(key or "").strip()
        owner_id = str(owner_id or "").strip()
        if not key or not owner_id:
            raise ValueError("coordination key and owner must not be empty")
        current_time = float(self.clock()) if now is None else float(now)
        lease_seconds = max(1.0, float(lease_seconds))
        result: list[CoordinationLease | None] = []

        def apply(state: CoordinationState) -> CoordinationState:
            current = state.leases.get(key)
            if current is not None and current.active(current_time):
                if current.owner_id != owner_id:
                    result.append(None)
                    return state
                renewed = current.model_copy(
                    update={
                        "expires_at": current_time + lease_seconds,
                        "renewed_at": current_time,
                    }
                )
                state.leases[key] = renewed
                result.append(renewed)
                return state

            last_token = max(
                state.last_fencing_tokens.get(key, 0),
                current.fencing_token if current is not None else 0,
            )
            lease = CoordinationLease(
                key=key,
                owner_id=owner_id,
                fencing_token=last_token + 1,
                acquired_at=current_time,
                expires_at=current_time + lease_seconds,
            )
            state.leases[key] = lease
            state.last_fencing_tokens[key] = lease.fencing_token
            result.append(lease)
            return state

        self._update(apply)
        return result[0]

    def renew(
        self,
        key: str,
        owner_id: str,
        fencing_token: int,
        *,
        lease_seconds: float,
        now: float | None = None,
    ) -> CoordinationLease:
        current_time = float(self.clock()) if now is None else float(now)
        result: list[CoordinationLease] = []

        def apply(state: CoordinationState) -> CoordinationState:
            current = state.leases.get(key)
            if (
                current is None
                or not current.active(current_time)
                or current.owner_id != owner_id
                or current.fencing_token != fencing_token
            ):
                raise CoordinationFenceError(
                    "coordination lease is expired, transferred, or fenced"
                )
            renewed = current.model_copy(
                update={
                    "expires_at": current_time + max(1.0, float(lease_seconds)),
                    "renewed_at": current_time,
                }
            )
            state.leases[key] = renewed
            result.append(renewed)
            return state

        self._update(apply)
        return result[0]

    def release(
        self,
        key: str,
        owner_id: str,
        fencing_token: int,
        *,
        now: float | None = None,
    ) -> bool:
        current_time = float(self.clock()) if now is None else float(now)
        released: list[bool] = []

        def apply(state: CoordinationState) -> CoordinationState:
            current = state.leases.get(key)
            if current is None:
                released.append(False)
                return state
            if (
                current.owner_id != owner_id
                or current.fencing_token != fencing_token
            ):
                raise CoordinationFenceError(
                    "stale owner cannot release a transferred lease"
                )
            state.last_fencing_tokens[key] = max(
                state.last_fencing_tokens.get(key, 0),
                current.fencing_token,
            )
            state.leases.pop(key, None)
            released.append(True)
            return state

        self._update(apply)
        return released[0]

    def current(
        self,
        key: str,
        *,
        now: float | None = None,
    ) -> CoordinationLease | None:
        current_time = float(self.clock()) if now is None else float(now)
        state = self._decode(self.store.get(self.namespace))
        lease = state.leases.get(key)
        if lease is None or not lease.active(current_time):
            return None
        return lease

    def require_fence(
        self,
        key: str,
        owner_id: str,
        fencing_token: int,
        *,
        now: float | None = None,
    ) -> CoordinationLease:
        lease = self.current(key, now=now)
        if (
            lease is None
            or lease.owner_id != owner_id
            or lease.fencing_token != fencing_token
        ):
            raise CoordinationFenceError(
                "stale coordination owner is fenced from protected mutation"
            )
        return lease

    def fenced_update(
        self,
        namespace: str,
        default: Any,
        updater: Callable[[Any], Any],
        *,
        lease_key: str,
        owner_id: str,
        fencing_token: int,
        now: float | None = None,
    ) -> Any:
        """Atomically validate a fence and mutate one canonical namespace."""

        current_time = float(self.clock()) if now is None else float(now)
        if namespace == self.namespace:
            raise ValueError("fenced_update target cannot be coordination namespace")
        result: list[Any] = []

        def apply(documents: dict[str, Any]) -> dict[str, Any]:
            coordination = self._decode(documents[self.namespace])
            lease = coordination.leases.get(lease_key)
            if (
                lease is None
                or not lease.active(current_time)
                or lease.owner_id != owner_id
                or lease.fencing_token != fencing_token
            ):
                raise CoordinationFenceError(
                    "stale coordination owner is fenced from protected mutation"
                )
            updated = updater(documents[namespace])
            result.append(updated)
            return {
                self.namespace: coordination.model_dump(mode="json"),
                namespace: updated,
            }

        self.store.update_many(
            {
                self.namespace: CoordinationState().model_dump(mode="json"),
                namespace: default,
            },
            apply,
        )
        return result[0]

    def health(self, *, now: float | None = None) -> CoordinationHealth:
        current_time = float(self.clock()) if now is None else float(now)
        state = self._decode(self.store.get(self.namespace))
        active = sum(item.active(current_time) for item in state.leases.values())
        expired = len(state.leases) - active
        status = self.store.status()
        return CoordinationHealth(
            backend_id=self.backend_id,
            healthy=bool(status.get("ok", True)),
            shared=self.shared,
            active_leases=active,
            expired_leases=expired,
            reason=None if status.get("ok", True) else "state store unhealthy",
        )
