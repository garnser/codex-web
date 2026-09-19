from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from codex_web.coordination import (
    CoordinationBackend,
    CoordinationFenceError,
    CoordinationLease,
)


@dataclass(slots=True)
class ResponsibilityState:
    name: str
    lease: CoordinationLease | None = None
    last_error: str | None = None


class ReplicatedOwnershipService:
    """Lease/fencing facade for singleton control-plane responsibilities."""

    def __init__(
        self,
        backend: CoordinationBackend,
        *,
        instance_id: str,
        lease_seconds: float = 30.0,
        clock=time.time,
    ) -> None:
        self.backend = backend
        self.instance_id = str(instance_id)
        self.lease_seconds = max(5.0, float(lease_seconds))
        self.clock = clock
        self._states: dict[str, ResponsibilityState] = {}

    @staticmethod
    def _key(name: str) -> str:
        normalized = str(name or "").strip()
        if not normalized:
            raise ValueError("responsibility name must not be empty")
        return f"control-plane:{normalized}"

    def acquire(self, name: str) -> CoordinationLease | None:
        key = self._key(name)
        state = self._states.setdefault(
            name,
            ResponsibilityState(name=name),
        )
        now = float(self.clock())
        lease = state.lease
        try:
            if (
                lease is not None
                and lease.owner_id == self.instance_id
                and lease.expires_at > now
            ):
                renewed = self.backend.renew(
                    key,
                    self.instance_id,
                    lease.fencing_token,
                    lease_seconds=self.lease_seconds,
                    now=now,
                )
                state.lease = renewed
                state.last_error = None
                return renewed
            acquired = self.backend.acquire(
                key,
                self.instance_id,
                lease_seconds=self.lease_seconds,
                now=now,
            )
            state.lease = acquired
            state.last_error = None
            return acquired
        except CoordinationFenceError as exc:
            state.lease = None
            state.last_error = type(exc).__name__
            return None
        except Exception as exc:
            state.lease = None
            state.last_error = type(exc).__name__
            return None

    def owns(self, name: str) -> bool:
        lease = self.acquire(name)
        return lease is not None and lease.owner_id == self.instance_id

    def require_fence(self, name: str) -> CoordinationLease:
        state = self._states.get(name)
        if state is None or state.lease is None:
            raise CoordinationFenceError("responsibility is not owned")
        return self.backend.require_fence(
            self._key(name),
            self.instance_id,
            state.lease.fencing_token,
            now=float(self.clock()),
        )

    def release(self, name: str) -> bool:
        state = self._states.get(name)
        if state is None or state.lease is None:
            return False
        lease = state.lease
        try:
            released = self.backend.release(
                self._key(name),
                self.instance_id,
                lease.fencing_token,
                now=float(self.clock()),
            )
        finally:
            state.lease = None
        return released

    async def run_if_owner(
        self,
        name: str,
        operation: Callable[[], Awaitable[Any]],
    ) -> bool:
        if not self.owns(name):
            return False
        await operation()
        self.require_fence(name)
        return True

    def status(self) -> dict[str, Any]:
        now = float(self.clock())
        rows = {}
        for name, state in sorted(self._states.items()):
            lease = state.lease
            rows[name] = {
                "owned": bool(
                    lease
                    and lease.owner_id == self.instance_id
                    and lease.expires_at > now
                ),
                "fencingToken": lease.fencing_token if lease else None,
                "expiresAt": lease.expires_at if lease else None,
                "lastError": state.last_error,
            }
        return {
            "instanceId": self.instance_id,
            "backendId": self.backend.backend_id,
            "shared": self.backend.shared,
            "leaseSeconds": self.lease_seconds,
            "responsibilities": rows,
        }

    def release_all(self) -> None:
        for name in list(self._states):
            try:
                self.release(name)
            except Exception:
                continue
