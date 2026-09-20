from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from contextlib import nullcontext
from typing import Any

from fastapi import WebSocket

from codex_web.observability import RuntimeMetrics, correlated, correlation_fields, log_event


EventListener = Callable[[dict[str, Any]], None]
logger = logging.getLogger(__name__)


class EventHub:
    def __init__(self, *, queue_size: int = 256) -> None:
        self._queue_size = queue_size
        self._clients: set[WebSocket] = set()
        self._queues: dict[WebSocket, asyncio.Queue[dict[str, Any]]] = {}
        self._senders: dict[WebSocket, asyncio.Task[None]] = {}
        self._listeners: set[EventListener] = set()
        self._metrics: RuntimeMetrics | None = None
        self._stream_id = uuid.uuid4().hex
        self._sequence = 0

    def configure_observability(self, metrics: RuntimeMetrics) -> None:
        self._metrics = metrics

    def stream_state(self) -> dict[str, Any]:
        return {
            "streamId": self._stream_id,
            "sequence": self._sequence,
        }

    def _envelope(self, event: dict[str, Any]) -> dict[str, Any]:
        self._sequence += 1
        return {
            **event,
            "eventStreamId": self._stream_id,
            "eventSequence": self._sequence,
            "eventPublishedAt": time.time(),
        }

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._clients.add(websocket)
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._queue_size)
        self._queues[websocket] = queue
        self._senders[websocket] = asyncio.create_task(
            self._sender(websocket, queue),
            name="eventhub-websocket-sender",
        )
        if self._metrics:
            self._metrics.increment("websocket.connections")

    def disconnect(self, websocket: WebSocket) -> None:
        self._clients.discard(websocket)
        self._queues.pop(websocket, None)
        sender = self._senders.pop(websocket, None)
        if sender is not None and sender is not asyncio.current_task():
            sender.cancel()

    def subscribe(self, listener: EventListener) -> None:
        self._listeners.add(listener)

    def unsubscribe(self, listener: EventListener) -> None:
        self._listeners.discard(listener)

    async def _sender(self, websocket: WebSocket, queue: asyncio.Queue[dict[str, Any]]) -> None:
        try:
            while True:
                event = await queue.get()
                try:
                    await websocket.send_json(event)
                finally:
                    queue.task_done()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._metrics:
                self._metrics.increment("websocket.send_failures")
            log_event(
                logger,
                logging.WARNING,
                "websocket.send_failed",
                "WebSocket event sender failed",
                error=str(exc),
            )
        finally:
            self._clients.discard(websocket)
            self._queues.pop(websocket, None)
            self._senders.pop(websocket, None)

    async def publish(self, event: dict[str, Any]) -> None:
        event = self._envelope(dict(event))
        for key, value in correlation_fields().items():
            event.setdefault(key, value)
        if self._metrics:
            self._metrics.increment("eventhub.events_published")

        # Runtime observers must never be able to break browser fan-out. They are
        # intentionally synchronous and should only update state/schedule work.
        event_context = (
            correlated(
                correlation_id=event.get("correlation_id"),
                causation_id=event.get("causation_id"),
                workspace_id=event.get("workspace_id"),
                work_item_ref=event.get("work_item_ref"),
                execution_id=event.get("execution_id"),
                action_intent_id=event.get("action_intent_id"),
            )
            if event.get("correlation_id")
            else nullcontext()
        )
        with event_context:
            for listener in list(self._listeners):
                try:
                    listener(event)
                except Exception as exc:
                    if self._metrics:
                        self._metrics.increment("eventhub.listener_failures")
                    log_event(
                        logger,
                        logging.ERROR,
                        "eventhub.listener_failed",
                        "EventHub listener failed",
                        event_type=event.get("type"),
                        listener=getattr(listener, "__qualname__", repr(listener)),
                        error=str(exc),
                    )

        dead: list[WebSocket] = []
        for websocket in list(self._clients):
            queue = self._queues.get(websocket)
            if queue is None:
                dead.append(websocket)
                continue
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                if self._metrics:
                    self._metrics.increment("websocket.queue_overflows")
                log_event(
                    logger,
                    logging.WARNING,
                    "websocket.queue_overflow",
                    "Dropping slow WebSocket client after event queue overflow",
                    event_type=event.get("type"),
                    queue_size=self._queue_size,
                )
                dead.append(websocket)
        for websocket in dead:
            self.disconnect(websocket)
