from __future__ import annotations

import time
import uuid
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from codex_web.compatibility import CanonicalEventEnvelope


class TransportDeliveryStatus(StrEnum):
    PUBLISHED = "published"
    ACKNOWLEDGED = "acknowledged"
    RETRY = "retry"
    DEAD_LETTER = "dead_letter"


class EventTransportCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    durable: bool = False
    acknowledgement: bool = True
    negative_acknowledgement: bool = True
    dead_letter: bool = True
    consumer_groups: bool = False
    ordering: str = "best_effort"


class EventTransportHealth(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    backend_id: str
    healthy: bool
    degraded: bool = False
    reason: str | None = None
    checked_at: float = Field(default_factory=time.time)


class TransportDelivery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: f"transport-{uuid.uuid4().hex}")
    backend_id: str
    canonical_event_id: str
    transport_message_id: str | None = None
    attempt: int = Field(default=1, ge=1)
    status: TransportDeliveryStatus = TransportDeliveryStatus.PUBLISHED
    published_at: float = Field(default_factory=time.time)
    acknowledged_at: float | None = None
    reason: str | None = None
    event: CanonicalEventEnvelope


@runtime_checkable
class EventTransport(Protocol):
    backend_id: str
    capabilities: EventTransportCapabilities

    async def publish(
        self,
        event: CanonicalEventEnvelope,
        *,
        attempt: int = 1,
    ) -> TransportDelivery: ...

    async def consume(
        self,
        consumer_id: str,
        *,
        limit: int = 10,
        timeout_seconds: float = 1.0,
    ) -> tuple[TransportDelivery, ...]: ...

    async def acknowledge(
        self,
        delivery: TransportDelivery,
        *,
        consumer_id: str,
    ) -> TransportDelivery: ...

    async def negative_acknowledge(
        self,
        delivery: TransportDelivery,
        *,
        consumer_id: str,
        reason: str,
        retry: bool = True,
    ) -> TransportDelivery: ...

    async def health(self) -> EventTransportHealth: ...


class EventTransportError(RuntimeError):
    pass
