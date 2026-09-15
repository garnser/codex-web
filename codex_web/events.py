from __future__ import annotations

import asyncio
from typing import Any

from fastapi import WebSocket


class EventHub:
    def __init__(self, *, queue_size: int = 256) -> None:
        self._queue_size = queue_size
        self._clients: set[WebSocket] = set()
        self._queues: dict[WebSocket, asyncio.Queue[dict[str, Any]]] = {}
        self._senders: dict[WebSocket, asyncio.Task[None]] = {}

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._clients.add(websocket)
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._queue_size)
        self._queues[websocket] = queue
        self._senders[websocket] = asyncio.create_task(self._sender(websocket, queue))

    def disconnect(self, websocket: WebSocket) -> None:
        self._clients.discard(websocket)
        self._queues.pop(websocket, None)
        sender = self._senders.pop(websocket, None)
        if sender is not None and sender is not asyncio.current_task():
            sender.cancel()

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
        except Exception:
            pass
        finally:
            self._clients.discard(websocket)
            self._queues.pop(websocket, None)
            self._senders.pop(websocket, None)

    async def publish(self, event: dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        for websocket in list(self._clients):
            queue = self._queues.get(websocket)
            if queue is None:
                dead.append(websocket)
                continue
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(websocket)
        for websocket in dead:
            self.disconnect(websocket)
