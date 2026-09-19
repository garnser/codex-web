from __future__ import annotations

import time
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class CoordinationLease(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    key: str = Field(min_length=1, max_length=500)
    owner_id: str = Field(min_length=1, max_length=500)
    fencing_token: int = Field(ge=1)
    acquired_at: float
    expires_at: float
    renewed_at: float | None = None

    def active(self, now: float) -> bool:
        return self.expires_at > now


class CoordinationHealth(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    backend_id: str
    healthy: bool
    shared: bool
    active_leases: int = Field(ge=0)
    expired_leases: int = Field(ge=0)
    reason: str | None = None
    checked_at: float = Field(default_factory=time.time)


class CoordinationError(RuntimeError):
    pass


class CoordinationLeaseHeldError(CoordinationError):
    pass


class CoordinationFenceError(CoordinationError):
    pass


@runtime_checkable
class CoordinationBackend(Protocol):
    backend_id: str
    shared: bool

    def acquire(
        self,
        key: str,
        owner_id: str,
        *,
        lease_seconds: float,
        now: float | None = None,
    ) -> CoordinationLease | None: ...

    def renew(
        self,
        key: str,
        owner_id: str,
        fencing_token: int,
        *,
        lease_seconds: float,
        now: float | None = None,
    ) -> CoordinationLease: ...

    def release(
        self,
        key: str,
        owner_id: str,
        fencing_token: int,
        *,
        now: float | None = None,
    ) -> bool: ...

    def current(
        self,
        key: str,
        *,
        now: float | None = None,
    ) -> CoordinationLease | None: ...

    def require_fence(
        self,
        key: str,
        owner_id: str,
        fencing_token: int,
        *,
        now: float | None = None,
    ) -> CoordinationLease: ...

    def health(self, *, now: float | None = None) -> CoordinationHealth: ...
