from __future__ import annotations

import asyncio
import inspect
import json
import time
import uuid
from collections import deque
from typing import Any

from codex_web.compatibility import CanonicalEventEnvelope
from codex_web.event_transport import (
    EventTransportCapabilities,
    EventTransportError,
    EventTransportHealth,
    TransportDelivery,
    TransportDeliveryStatus,
)


class InProcessEventTransport:
    """Bounded local transport for the default single-instance deployment."""

    backend_id = "in-process"
    capabilities = EventTransportCapabilities(
        durable=False,
        acknowledgement=True,
        negative_acknowledgement=True,
        dead_letter=True,
        consumer_groups=False,
        ordering="fifo",
    )

    def __init__(
        self,
        *,
        max_queue: int = 10000,
        max_attempts: int = 5,
    ) -> None:
        self.max_queue = max(1, int(max_queue))
        self.max_attempts = max(1, int(max_attempts))
        self._queue: asyncio.Queue[TransportDelivery] = asyncio.Queue(
            maxsize=self.max_queue
        )
        self._dead_letters: deque[TransportDelivery] = deque(maxlen=1000)

    async def publish(
        self,
        event: CanonicalEventEnvelope,
        *,
        attempt: int = 1,
    ) -> TransportDelivery:
        delivery = TransportDelivery(
            backend_id=self.backend_id,
            canonical_event_id=event.event_id,
            transport_message_id=f"local-{uuid.uuid4().hex}",
            attempt=attempt,
            event=event,
        )
        try:
            self._queue.put_nowait(delivery)
        except asyncio.QueueFull as exc:
            raise EventTransportError("in-process event transport queue is full") from exc
        return delivery

    async def consume(
        self,
        consumer_id: str,
        *,
        limit: int = 10,
        timeout_seconds: float = 1.0,
    ) -> tuple[TransportDelivery, ...]:
        del consumer_id
        limit = max(1, min(int(limit), 1000))
        rows: list[TransportDelivery] = []
        if self._queue.empty():
            try:
                rows.append(
                    await asyncio.wait_for(
                        self._queue.get(),
                        timeout=max(0.0, float(timeout_seconds)),
                    )
                )
            except asyncio.TimeoutError:
                return ()
        while len(rows) < limit and not self._queue.empty():
            rows.append(self._queue.get_nowait())
        return tuple(rows)

    async def acknowledge(
        self,
        delivery: TransportDelivery,
        *,
        consumer_id: str,
    ) -> TransportDelivery:
        del consumer_id
        return delivery.model_copy(
            update={
                "status": TransportDeliveryStatus.ACKNOWLEDGED,
                "acknowledged_at": time.time(),
            }
        )

    async def negative_acknowledge(
        self,
        delivery: TransportDelivery,
        *,
        consumer_id: str,
        reason: str,
        retry: bool = True,
    ) -> TransportDelivery:
        del consumer_id
        if retry and delivery.attempt < self.max_attempts:
            retried = await self.publish(
                delivery.event,
                attempt=delivery.attempt + 1,
            )
            return retried.model_copy(
                update={
                    "status": TransportDeliveryStatus.RETRY,
                    "reason": reason[:500],
                }
            )
        dead = delivery.model_copy(
            update={
                "status": TransportDeliveryStatus.DEAD_LETTER,
                "reason": reason[:500],
                "acknowledged_at": time.time(),
            }
        )
        self._dead_letters.append(dead)
        return dead

    async def health(self) -> EventTransportHealth:
        depth = self._queue.qsize()
        degraded = depth >= int(self.max_queue * 0.8)
        return EventTransportHealth(
            backend_id=self.backend_id,
            healthy=depth < self.max_queue,
            degraded=degraded,
            reason=(
                f"queue_depth={depth}/{self.max_queue}" if degraded else None
            ),
        )

    def dead_letters(self) -> tuple[TransportDelivery, ...]:
        return tuple(self._dead_letters)


class RedisStreamsEventTransport:
    """Redis Streams adapter without making redis-py a core dependency.

    Pass a redis.asyncio-compatible client. Canonical identity lives inside the
    message payload; Redis stream IDs remain transport metadata.
    """

    capabilities = EventTransportCapabilities(
        durable=True,
        acknowledgement=True,
        negative_acknowledgement=True,
        dead_letter=True,
        consumer_groups=True,
        ordering="stream",
    )

    def __init__(
        self,
        client: Any,
        *,
        stream: str = "codex-web:events",
        group: str = "codex-web",
        dead_letter_stream: str = "codex-web:events:dead",
        max_attempts: int = 5,
    ) -> None:
        self.client = client
        self.stream = stream
        self.group = group
        self.dead_letter_stream = dead_letter_stream
        self.max_attempts = max(1, int(max_attempts))
        self.backend_id = f"redis-streams:{stream}"
        self._group_ready = False

    async def _await(self, value: Any) -> Any:
        return await value if inspect.isawaitable(value) else value

    async def _ensure_group(self) -> None:
        if self._group_ready:
            return
        try:
            await self._await(
                self.client.xgroup_create(
                    self.stream,
                    self.group,
                    id="0",
                    mkstream=True,
                )
            )
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise EventTransportError(
                    f"redis consumer group setup failed: {type(exc).__name__}"
                ) from exc
        self._group_ready = True

    @staticmethod
    def _fields(
        event: CanonicalEventEnvelope,
        *,
        attempt: int,
    ) -> dict[str, str]:
        return {
            "canonical_event_id": event.event_id,
            "attempt": str(attempt),
            "event": json.dumps(
                event.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            ),
        }

    @staticmethod
    def _decode_message(message_id: Any, fields: Any, backend_id: str) -> TransportDelivery:
        decoded: dict[str, Any] = {}
        for key, value in dict(fields).items():
            if isinstance(key, bytes):
                key = key.decode("utf-8")
            if isinstance(value, bytes):
                value = value.decode("utf-8")
            decoded[str(key)] = value
        event = CanonicalEventEnvelope.model_validate(
            json.loads(str(decoded["event"]))
        )
        return TransportDelivery(
            backend_id=backend_id,
            canonical_event_id=event.event_id,
            transport_message_id=(
                message_id.decode("utf-8")
                if isinstance(message_id, bytes)
                else str(message_id)
            ),
            attempt=int(decoded.get("attempt") or 1),
            event=event,
        )

    async def publish(
        self,
        event: CanonicalEventEnvelope,
        *,
        attempt: int = 1,
    ) -> TransportDelivery:
        await self._ensure_group()
        try:
            message_id = await self._await(
                self.client.xadd(
                    self.stream,
                    self._fields(event, attempt=attempt),
                )
            )
        except Exception as exc:
            raise EventTransportError(
                f"redis stream publish failed: {type(exc).__name__}"
            ) from exc
        return TransportDelivery(
            backend_id=self.backend_id,
            canonical_event_id=event.event_id,
            transport_message_id=(
                message_id.decode("utf-8")
                if isinstance(message_id, bytes)
                else str(message_id)
            ),
            attempt=attempt,
            event=event,
        )

    async def consume(
        self,
        consumer_id: str,
        *,
        limit: int = 10,
        timeout_seconds: float = 1.0,
    ) -> tuple[TransportDelivery, ...]:
        await self._ensure_group()
        try:
            result = await self._await(
                self.client.xreadgroup(
                    self.group,
                    consumer_id,
                    {self.stream: ">"},
                    count=max(1, min(int(limit), 1000)),
                    block=max(0, int(float(timeout_seconds) * 1000)),
                )
            )
        except Exception as exc:
            raise EventTransportError(
                f"redis stream consume failed: {type(exc).__name__}"
            ) from exc
        rows: list[TransportDelivery] = []
        for _stream_name, messages in result or []:
            for message_id, fields in messages:
                rows.append(
                    self._decode_message(
                        message_id,
                        fields,
                        self.backend_id,
                    )
                )
        return tuple(rows)

    async def acknowledge(
        self,
        delivery: TransportDelivery,
        *,
        consumer_id: str,
    ) -> TransportDelivery:
        del consumer_id
        if not delivery.transport_message_id:
            raise EventTransportError("redis delivery has no transport message id")
        await self._await(
            self.client.xack(
                self.stream,
                self.group,
                delivery.transport_message_id,
            )
        )
        return delivery.model_copy(
            update={
                "status": TransportDeliveryStatus.ACKNOWLEDGED,
                "acknowledged_at": time.time(),
            }
        )

    async def negative_acknowledge(
        self,
        delivery: TransportDelivery,
        *,
        consumer_id: str,
        reason: str,
        retry: bool = True,
    ) -> TransportDelivery:
        del consumer_id
        if delivery.transport_message_id:
            await self._await(
                self.client.xack(
                    self.stream,
                    self.group,
                    delivery.transport_message_id,
                )
            )
        if retry and delivery.attempt < self.max_attempts:
            retried = await self.publish(
                delivery.event,
                attempt=delivery.attempt + 1,
            )
            return retried.model_copy(
                update={
                    "status": TransportDeliveryStatus.RETRY,
                    "reason": reason[:500],
                }
            )
        try:
            message_id = await self._await(
                self.client.xadd(
                    self.dead_letter_stream,
                    {
                        **self._fields(
                            delivery.event,
                            attempt=delivery.attempt,
                        ),
                        "reason": reason[:500],
                    },
                )
            )
        except Exception as exc:
            raise EventTransportError(
                f"redis dead-letter publish failed: {type(exc).__name__}"
            ) from exc
        return delivery.model_copy(
            update={
                "transport_message_id": (
                    message_id.decode("utf-8")
                    if isinstance(message_id, bytes)
                    else str(message_id)
                ),
                "status": TransportDeliveryStatus.DEAD_LETTER,
                "reason": reason[:500],
                "acknowledged_at": time.time(),
            }
        )

    async def health(self) -> EventTransportHealth:
        try:
            pong = await self._await(self.client.ping())
            healthy = bool(pong)
            return EventTransportHealth(
                backend_id=self.backend_id,
                healthy=healthy,
                degraded=not healthy,
                reason=None if healthy else "redis ping returned false",
            )
        except Exception as exc:
            return EventTransportHealth(
                backend_id=self.backend_id,
                healthy=False,
                degraded=True,
                reason=f"redis unavailable:{type(exc).__name__}",
            )


def build_event_transport(
    backend: str,
    *,
    redis_url: str | None = None,
    redis_stream: str = "codex-web:events",
    redis_group: str = "codex-web",
):
    normalized = str(backend or "in-process").strip().casefold()
    if normalized in {"none", "disabled", "direct"}:
        return None
    if normalized in {"in-process", "inprocess", "local"}:
        return InProcessEventTransport()
    if normalized in {"redis", "redis-streams", "redis_streams"}:
        if not redis_url:
            raise RuntimeError(
                "CODEX_WEB_REDIS_URL is required for Redis Streams event transport"
            )
        try:
            import redis.asyncio as redis_asyncio  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "Redis Streams transport requires the optional 'redis' package"
            ) from exc
        client = redis_asyncio.from_url(redis_url, decode_responses=True)
        return RedisStreamsEventTransport(
            client,
            stream=redis_stream,
            group=redis_group,
        )
    raise RuntimeError(f"unsupported EventTransport backend: {backend}")


class EventTransportRuntime:
    """Recover durable outbox and consume transport wakeups with bounded work."""

    def __init__(
        self,
        bus: Any,
        *,
        consumer_id: str,
        idle_seconds: float = 0.25,
        batch_size: int = 100,
    ) -> None:
        self.bus = bus
        self.consumer_id = consumer_id
        self.idle_seconds = max(0.05, float(idle_seconds))
        self.batch_size = max(1, min(int(batch_size), 1000))
        self.last_error: str | None = None

    async def run_forever(self) -> None:
        while True:
            try:
                outbox = await self.bus.dispatch_outbox_once(
                    limit=self.batch_size
                )
                incoming = await self.bus.consume_transport_once(
                    self.consumer_id,
                    limit=self.batch_size,
                    timeout_seconds=self.idle_seconds,
                )
                self.last_error = None
                if outbox["attempted"] == 0 and incoming["received"] == 0:
                    await asyncio.sleep(self.idle_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = type(exc).__name__
                await asyncio.sleep(self.idle_seconds)

    async def status(self) -> dict[str, Any]:
        health = await self.bus.transport_health()
        return {
            "consumerId": self.consumer_id,
            "transport": (
                health.model_dump(mode="json") if health is not None else None
            ),
            "outbox": self.bus.store.outbox_status(),
            "lastError": self.last_error,
        }
