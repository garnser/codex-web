from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import random
import time
from collections import Counter, deque
from dataclasses import dataclass
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

from codex_web.integrations.slack_client import SlackClient
from codex_web.integrations.telegram_client import TelegramClient
from codex_web.models import BotConnection, BotInboundMessage
from codex_web.services.bot_binding_selection import BotBindingSelectionService
from codex_web.services.bot_connections import BotConnectionService
from codex_web.services.bot_delivery import BotDeliveryService
from codex_web.services.bot_presentation import BotPresentationService
from codex_web.services.bot_routing import BotRoutingService
from codex_web.services.bot_runtime_telemetry import BotRuntimeTelemetry
from codex_web.services.secrets import SecretBroker


@dataclass(slots=True)
class SlackPayloadWork:
    payload: dict[str, Any]
    enqueued_at: float
    ordering_key: str
    dedupe_key: str | None = None


class SlackSocketLifecycleError(RuntimeError):
    failure_class = "transport_error"


class SlackSocketCleanClose(SlackSocketLifecycleError):
    failure_class = "clean_close"


class BotRuntime:
    """Own long-lived Slack/Telegram connection lifecycle."""

    def __init__(
        self,
        *,
        connections: BotConnectionService,
        bindings: BotBindingSelectionService,
        presentation: BotPresentationService,
        telemetry: BotRuntimeTelemetry,
        routing: BotRoutingService,
        delivery: BotDeliveryService,
        publish_event,
        slack_client: SlackClient | None = None,
        telegram_client: TelegramClient | None = None,
        secret_broker: SecretBroker | None = None,
        ownership: Any | None = None,
    ) -> None:
        self.connections = connections
        self.bindings = bindings
        self.presentation = presentation
        self.telemetry = telemetry
        self.routing = routing
        self.delivery = delivery
        self.publish_event = publish_event
        self.slack = slack_client or SlackClient()
        self.telegram = telegram_client or TelegramClient()
        self.secret_broker = secret_broker
        self.ownership = ownership
        self.conversation_channels: Any | None = None
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.fingerprints: dict[str, tuple[Any, ...]] = {}
        self.slack_payload_queues: dict[
            str,
            list[asyncio.Queue[SlackPayloadWork]],
        ] = {}
        self.slack_payload_workers: dict[
            str,
            list[asyncio.Task[None]],
        ] = {}
        self.slack_payload_seen: dict[str, set[str]] = {}
        self.slack_payload_seen_order: dict[str, deque[str]] = {}
        self.slack_payload_stats: dict[str, dict[str, Any]] = {}
        self.slack_reconnect_counts: dict[str, int] = {}
        self.slack_reconnect_failures: dict[str, int] = {}
        self.lock = asyncio.Lock()

    async def sync(self) -> None:
        async with self.lock:
            if (
                self.ownership is not None
                and not self.ownership.owns("bot-runtime")
            ):
                for connection_id in list(self.tasks):
                    await self._stop_locked(connection_id)
                return
            connections = self.connections.load_connections()
            desired: dict[str, tuple[Any, ...]] = {}
            for connection in connections:
                fingerprint = self._fingerprint(connection)
                if not fingerprint:
                    continue
                desired[connection.id] = fingerprint
                if (
                    self.fingerprints.get(connection.id) == fingerprint
                    and connection.id in self.tasks
                ):
                    continue
                await self._stop_locked(connection.id)
                self.tasks[connection.id] = asyncio.create_task(
                    self._run_connection(connection),
                    name=(
                        f"bot-runtime-{connection.provider}-{connection.id}"
                    ),
                )
                self.fingerprints[connection.id] = fingerprint
            for connection_id in list(self.tasks):
                if connection_id not in desired:
                    await self._stop_locked(connection_id)

    async def stop(self) -> None:
        async with self.lock:
            for connection_id in list(self.tasks):
                await self._stop_locked(connection_id)

    async def _stop_locked(self, connection_id: str) -> None:
        task = self.tasks.pop(connection_id, None)
        self.fingerprints.pop(connection_id, None)
        current = self.telemetry.status.get(connection_id)
        if current:
            self.telemetry.status[connection_id] = {
                **current,
                "status": "stopped",
                "updatedAt": time.time(),
            }
        if not task:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    @staticmethod
    def slack_payload_worker_count() -> int:
        try:
            value = int(
                os.environ.get("CODEX_WEB_SLACK_PAYLOAD_WORKERS") or "4"
            )
        except ValueError:
            value = 4
        return max(1, min(value, 32))

    @staticmethod
    def slack_payload_queue_size() -> int:
        try:
            value = int(
                os.environ.get(
                    "CODEX_WEB_SLACK_PAYLOAD_QUEUE_PER_WORKER"
                )
                or "256"
            )
        except ValueError:
            value = 256
        return max(1, min(value, 10000))

    @staticmethod
    def slack_payload_dedupe_limit() -> int:
        try:
            value = int(
                os.environ.get("CODEX_WEB_SLACK_PAYLOAD_DEDUPE_LIMIT")
                or "50000"
            )
        except ValueError:
            value = 50000
        return max(1000, min(value, 200000))

    @staticmethod
    def slack_payload_drain_timeout() -> float:
        try:
            value = float(
                os.environ.get(
                    "CODEX_WEB_SLACK_PAYLOAD_DRAIN_TIMEOUT_SECONDS"
                )
                or "5"
            )
        except ValueError:
            value = 5.0
        return max(0.0, min(value, 60.0))

    @staticmethod
    def slack_socket_ping_interval() -> float:
        try:
            value = float(
                os.environ.get(
                    "CODEX_WEB_SLACK_SOCKET_PING_INTERVAL_SECONDS"
                )
                or "20"
            )
        except ValueError:
            value = 20.0
        return max(5.0, min(value, 120.0))

    @staticmethod
    def slack_socket_ping_timeout() -> float:
        try:
            value = float(
                os.environ.get(
                    "CODEX_WEB_SLACK_SOCKET_PING_TIMEOUT_SECONDS"
                )
                or "10"
            )
        except ValueError:
            value = 10.0
        return max(2.0, min(value, 60.0))

    @staticmethod
    def slack_socket_open_timeout() -> float:
        try:
            value = float(
                os.environ.get(
                    "CODEX_WEB_SLACK_SOCKET_OPEN_TIMEOUT_SECONDS"
                )
                or "10"
            )
        except ValueError:
            value = 10.0
        return max(2.0, min(value, 60.0))

    @staticmethod
    def slack_reconnect_min_seconds() -> float:
        try:
            value = float(
                os.environ.get(
                    "CODEX_WEB_SLACK_RECONNECT_MIN_SECONDS"
                )
                or "2"
            )
        except ValueError:
            value = 2.0
        return max(0.1, min(value, 60.0))

    @classmethod
    def slack_reconnect_max_seconds(cls) -> float:
        try:
            value = float(
                os.environ.get(
                    "CODEX_WEB_SLACK_RECONNECT_MAX_SECONDS"
                )
                or "60"
            )
        except ValueError:
            value = 60.0
        return max(
            cls.slack_reconnect_min_seconds(),
            min(value, 600.0),
        )

    @staticmethod
    def slack_reconnect_jitter_ratio() -> float:
        try:
            value = float(
                os.environ.get(
                    "CODEX_WEB_SLACK_RECONNECT_JITTER_RATIO"
                )
                or "0.2"
            )
        except ValueError:
            value = 0.2
        return max(0.0, min(value, 0.5))

    @staticmethod
    def slack_reconnect_stable_reset_seconds() -> float:
        try:
            value = float(
                os.environ.get(
                    "CODEX_WEB_SLACK_RECONNECT_STABLE_RESET_SECONDS"
                )
                or "60"
            )
        except ValueError:
            value = 60.0
        return max(5.0, min(value, 3600.0))

    @classmethod
    def slack_reconnect_delay(cls, failures: int) -> float:
        minimum = cls.slack_reconnect_min_seconds()
        maximum = cls.slack_reconnect_max_seconds()
        base = min(
            maximum,
            minimum * (2 ** max(0, min(int(failures) - 1, 10))),
        )
        jitter = base * cls.slack_reconnect_jitter_ratio()
        return min(
            maximum,
            base + random.uniform(0.0, jitter),
        )

    @staticmethod
    def slack_socket_failure_class(exc: Exception) -> str:
        if isinstance(exc, SlackSocketLifecycleError):
            return exc.failure_class
        text = str(exc).casefold()
        if any(
            marker in text
            for marker in (
                "invalid_auth",
                "not_authed",
                "account_inactive",
                "token_revoked",
                "missing slack_app_token",
                "missing bot_token",
            )
        ):
            return "authentication_configuration"
        if (
            "opening handshake" in text
            or "open timeout" in text
            or "timed out during opening handshake" in text
        ):
            return "handshake_timeout"
        if (
            "keepalive ping timeout" in text
            or "ping timeout" in text
            or "pong timeout" in text
        ):
            return "keepalive_timeout"
        if (
            "no close frame received" in text
            or "connection reset" in text
            or "connectionreseterror" in text
            or "eof" in text
        ):
            return "transport_reset"
        if isinstance(exc, ConnectionClosed):
            rcvd = getattr(exc, "rcvd", None)
            code = getattr(rcvd, "code", None)
            if code in {1000, 1001}:
                return "clean_close"
            return "transport_reset"
        if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
            return "handshake_timeout"
        return "transport_error"

    @staticmethod
    def slack_socket_failure_is_transport(
        failure_class: str,
    ) -> bool:
        return failure_class in {
            "handshake_timeout",
            "keepalive_timeout",
            "clean_close",
            "transport_reset",
            "transport_error",
        }

    @staticmethod
    def _slack_payload_ordering_key(
        connection: BotConnection,
        payload: dict[str, Any],
    ) -> str:
        event = payload.get("event") or {}
        if not isinstance(event, dict):
            event = {}
        item = event.get("item") or {}
        if not isinstance(item, dict):
            item = {}
        channel_payload = payload.get("channel") or {}
        if not isinstance(channel_payload, dict):
            channel_payload = {}
        container = payload.get("container") or {}
        if not isinstance(container, dict):
            container = {}
        channel = (
            event.get("channel")
            or item.get("channel")
            or channel_payload.get("id")
            or container.get("channel_id")
            or connection.default_external_conversation_id
            or "__connection__"
        )
        return f"{connection.id}:{channel}"

    @staticmethod
    def _slack_payload_dedupe_key(
        envelope_id: str | None,
        payload: dict[str, Any],
    ) -> str | None:
        event = payload.get("event") or {}
        if not isinstance(event, dict):
            event = {}
        event_identity = (
            payload.get("event_id")
            or event.get("client_msg_id")
            or event.get("event_ts")
            or event.get("ts")
        )
        if event_identity:
            return f"event:{event_identity}"
        trigger_id = payload.get("trigger_id")
        if trigger_id:
            return f"trigger:{trigger_id}"
        if envelope_id:
            return f"envelope:{envelope_id}"
        return None

    @staticmethod
    def _slack_payload_shard(
        ordering_key: str,
        shard_count: int,
    ) -> int:
        digest = hashlib.sha256(ordering_key.encode()).digest()
        return int.from_bytes(digest[:8], "big") % shard_count

    def _remember_slack_payload_key(
        self,
        connection_id: str,
        key: str | None,
    ) -> bool:
        if not key:
            return True
        seen = self.slack_payload_seen.setdefault(connection_id, set())
        if key in seen:
            return False
        order = self.slack_payload_seen_order.setdefault(
            connection_id,
            deque(),
        )
        seen.add(key)
        order.append(key)
        limit = self.slack_payload_dedupe_limit()
        while len(order) > limit:
            expired = order.popleft()
            seen.discard(expired)
        return True

    def _start_slack_payload_workers(
        self,
        connection: BotConnection,
    ) -> None:
        existing = self.slack_payload_workers.get(connection.id)
        if existing and any(not task.done() for task in existing):
            return
        shard_count = self.slack_payload_worker_count()
        queues = [
            asyncio.Queue(maxsize=self.slack_payload_queue_size())
            for _ in range(shard_count)
        ]
        workers = [
            asyncio.create_task(
                self._slack_payload_worker(
                    connection,
                    shard_index,
                    queues[shard_index],
                ),
                name=(
                    f"slack-payload-worker-{connection.id}-{shard_index}"
                ),
            )
            for shard_index in range(shard_count)
        ]
        self.slack_payload_queues[connection.id] = queues
        self.slack_payload_workers[connection.id] = workers
        self.slack_payload_stats.setdefault(
            connection.id,
            {
                "accepted": 0,
                "deduped": 0,
                "rejected": 0,
                "processed": 0,
                "failed": 0,
                "cancelled": 0,
                "processingLatencySeconds": 0.0,
                "processingWorkers": 0,
                "lastOverloadAt": None,
            },
        )
        self._update_slack_payload_status(connection)

    def _update_slack_payload_status(
        self,
        connection: BotConnection,
    ) -> dict[str, Any]:
        queues = self.slack_payload_queues.get(connection.id, [])
        workers = self.slack_payload_workers.get(connection.id, [])
        queued_items = [
            item
            for queue in queues
            for item in list(queue._queue)
        ]
        now = time.time()
        per_channel = Counter(
            item.ordering_key for item in queued_items
        )
        stats = self.slack_payload_stats.setdefault(connection.id, {})
        snapshot = {
            **stats,
            "queueDepth": len(queued_items),
            "queueCapacity": sum(queue.maxsize for queue in queues),
            "oldestQueuedAgeSeconds": (
                max(
                    0.0,
                    now - min(
                        item.enqueued_at for item in queued_items
                    ),
                )
                if queued_items
                else 0.0
            ),
            "activeWorkers": int(stats.get("processingWorkers", 0)),
            "workerTasks": sum(
                1 for task in workers if not task.done()
            ),
            "workerLimit": len(workers),
            "overloaded": any(queue.full() for queue in queues),
            "perChannelBacklog": dict(
                per_channel.most_common(20)
            ),
            "dedupeEntries": len(
                self.slack_payload_seen.get(connection.id, set())
            ),
        }
        self.slack_payload_stats[connection.id] = snapshot
        self.telemetry.set_status(
            connection,
            "connected",
            slackPayloadQueueDepth=snapshot["queueDepth"],
            slackPayloadQueueCapacity=snapshot["queueCapacity"],
            slackPayloadOldestQueuedAgeSeconds=(
                snapshot["oldestQueuedAgeSeconds"]
            ),
            slackPayloadActiveWorkers=snapshot["activeWorkers"],
            slackPayloadOverloaded=snapshot["overloaded"],
            slackPayloadAccepted=snapshot.get("accepted", 0),
            slackPayloadDeduped=snapshot.get("deduped", 0),
            slackPayloadRejected=snapshot.get("rejected", 0),
            slackPayloadFailed=snapshot.get("failed", 0),
        )
        return dict(snapshot)

    def slack_payload_status(
        self,
        connection_id: str | None = None,
    ) -> dict[str, Any]:
        if connection_id is not None:
            return dict(
                self.slack_payload_stats.get(connection_id, {})
            )
        return {
            key: dict(value)
            for key, value in self.slack_payload_stats.items()
        }

    def _admit_slack_payload(
        self,
        connection: BotConnection,
        payload: dict[str, Any],
        *,
        envelope_id: str | None = None,
    ) -> str:
        self._start_slack_payload_workers(connection)
        dedupe_key = self._slack_payload_dedupe_key(
            envelope_id,
            payload,
        )
        seen = self.slack_payload_seen.setdefault(connection.id, set())
        if dedupe_key and dedupe_key in seen:
            stats = self.slack_payload_stats[connection.id]
            stats["deduped"] = int(stats.get("deduped", 0)) + 1
            self._update_slack_payload_status(connection)
            return "deduped"

        ordering_key = self._slack_payload_ordering_key(
            connection,
            payload,
        )
        queues = self.slack_payload_queues[connection.id]
        shard = self._slack_payload_shard(
            ordering_key,
            len(queues),
        )
        queue = queues[shard]
        if queue.full():
            stats = self.slack_payload_stats[connection.id]
            stats["rejected"] = int(stats.get("rejected", 0)) + 1
            stats["lastOverloadAt"] = time.time()
            self.telemetry.append(
                {
                    "type": "slack_payload_overload",
                    "provider": "slack",
                    "connection_id": connection.id,
                    "ordering_key": ordering_key,
                    "queue_depth": queue.qsize(),
                    "queue_capacity": queue.maxsize,
                }
            )
            self._update_slack_payload_status(connection)
            return "rejected"

        work = SlackPayloadWork(
            payload=dict(payload),
            enqueued_at=time.time(),
            ordering_key=ordering_key,
            dedupe_key=dedupe_key,
        )
        queue.put_nowait(work)
        self._remember_slack_payload_key(
            connection.id,
            dedupe_key,
        )
        stats = self.slack_payload_stats[connection.id]
        stats["accepted"] = int(stats.get("accepted", 0)) + 1
        self._update_slack_payload_status(connection)
        return "accepted"

    async def _slack_payload_worker(
        self,
        connection: BotConnection,
        shard_index: int,
        queue: asyncio.Queue[SlackPayloadWork],
    ) -> None:
        del shard_index
        while True:
            work = await queue.get()
            started = time.perf_counter()
            stats = self.slack_payload_stats[connection.id]
            stats["processingWorkers"] = (
                int(stats.get("processingWorkers", 0)) + 1
            )
            self._update_slack_payload_status(connection)
            try:
                await self._handle_slack_payload(
                    connection,
                    work.payload,
                )
                stats = self.slack_payload_stats[connection.id]
                stats["processed"] = (
                    int(stats.get("processed", 0)) + 1
                )
            except asyncio.CancelledError:
                stats = self.slack_payload_stats.get(
                    connection.id,
                    {},
                )
                stats["cancelled"] = (
                    int(stats.get("cancelled", 0)) + 1
                )
                raise
            except Exception as exc:
                stats = self.slack_payload_stats[connection.id]
                stats["failed"] = int(stats.get("failed", 0)) + 1
                self.telemetry.append(
                    {
                        "type": "slack_payload_worker_failed",
                        "provider": "slack",
                        "connection_id": connection.id,
                        "ordering_key": work.ordering_key,
                        "error": str(exc),
                    }
                )
            finally:
                elapsed = time.perf_counter() - started
                stats = self.slack_payload_stats.get(
                    connection.id,
                    {},
                )
                stats["processingLatencySeconds"] = elapsed
                stats["processingWorkers"] = max(
                    0,
                    int(stats.get("processingWorkers", 0)) - 1,
                )
                queue.task_done()
                self._update_slack_payload_status(connection)

    async def _stop_slack_payload_workers(
        self,
        connection: BotConnection,
    ) -> None:
        queues = self.slack_payload_queues.get(connection.id, [])
        workers = self.slack_payload_workers.get(connection.id, [])
        pending_before = sum(queue.qsize() for queue in queues)
        if queues and self.slack_payload_drain_timeout() > 0:
            try:
                await asyncio.wait_for(
                    asyncio.gather(
                        *(queue.join() for queue in queues)
                    ),
                    timeout=self.slack_payload_drain_timeout(),
                )
            except asyncio.TimeoutError:
                pass

        cancelled_items = 0
        for queue in queues:
            while True:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                else:
                    cancelled_items += 1
                    queue.task_done()

        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(
                *workers,
                return_exceptions=True,
            )

        if cancelled_items:
            stats = self.slack_payload_stats.setdefault(
                connection.id,
                {},
            )
            stats["cancelled"] = (
                int(stats.get("cancelled", 0)) + cancelled_items
            )
            self.telemetry.append(
                {
                    "type": "slack_payload_shutdown_cancelled",
                    "provider": "slack",
                    "connection_id": connection.id,
                    "pending_before": pending_before,
                    "cancelled": cancelled_items,
                }
            )
        self.slack_payload_queues.pop(connection.id, None)
        self.slack_payload_workers.pop(connection.id, None)
        self.slack_payload_seen.pop(connection.id, None)
        self.slack_payload_seen_order.pop(connection.id, None)
        stats = self.slack_payload_stats.setdefault(connection.id, {})
        stats.update(
            {
                "queueDepth": 0,
                "queueCapacity": 0,
                "oldestQueuedAgeSeconds": 0.0,
                "activeWorkers": 0,
                "workerTasks": 0,
                "workerLimit": 0,
                "processingWorkers": 0,
                "overloaded": False,
                "perChannelBacklog": {},
                "dedupeEntries": 0,
            }
        )

    @staticmethod
    def _credential_identity(
        connection: BotConnection,
        field: str,
    ) -> str | None:
        return getattr(connection, f"{field}_secret_id", None) or getattr(
            connection,
            field,
            None,
        )

    @classmethod
    def _fingerprint(
        cls,
        connection: BotConnection,
    ) -> tuple[Any, ...] | None:
        bot_token = cls._credential_identity(connection, "bot_token")
        app_token = cls._credential_identity(
            connection,
            "slack_app_token",
        )
        if connection.provider == "slack" and bot_token and app_token:
            return (
                "slack",
                bot_token,
                app_token,
                connection.updated_at,
            )
        if connection.provider == "telegram" and bot_token:
            return (
                "telegram",
                bot_token,
                connection.telegram_update_offset,
                connection.updated_at,
            )
        return None

    async def _with_credential(
        self,
        connection: BotConnection,
        field: str,
        operation: str,
        consumer: Any,
    ) -> Any:
        secret_id = getattr(connection, f"{field}_secret_id", None)
        if secret_id and self.secret_broker is not None:
            return await self.secret_broker.use_async(
                secret_id,
                actor=self.connections.runtime_actor(
                    connection.project_id
                ),
                operation=operation,
                consumer=consumer,
                context={
                    "connection_id": connection.id,
                    "provider": connection.provider,
                },
            )
        raw = getattr(connection, field, None)
        if raw:
            return await consumer(raw)
        raise RuntimeError(f"Bot connection is missing {field}")

    @staticmethod
    def is_transient_websocket_disconnect(exc: Exception) -> bool:
        text = str(exc).lower()
        return (
            isinstance(exc, ConnectionClosed)
            or "keepalive ping timeout" in text
            or "no close frame received" in text
        )

    async def _run_connection(self, connection: BotConnection) -> None:
        if connection.provider == "slack":
            self._start_slack_payload_workers(connection)
        try:
            while True:
                try:
                    self.telemetry.set_status(
                        connection,
                        "starting",
                        lastError=None,
                    )
                    if connection.provider == "slack":
                        await self._run_slack(connection)
                    elif connection.provider == "telegram":
                        await self._run_telegram(connection)
                    else:
                        return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if (
                        connection.provider == "slack"
                        and self.is_transient_websocket_disconnect(exc)
                    ):
                        self.telemetry.set_status(
                            connection,
                            "reconnecting",
                            lastDisconnect=str(exc),
                            lastDisconnectAt=time.time(),
                            lastError=None,
                        )
                        self.telemetry.append(
                            {
                                "type": "runtime_reconnect",
                                "provider": connection.provider,
                                "connection_id": connection.id,
                                "reason": str(exc),
                            }
                        )
                        await self.publish_event(
                            {
                                "type": "bot.runtime",
                                "provider": connection.provider,
                                "connectionId": connection.id,
                                "status": "reconnecting",
                                "reason": str(exc),
                            }
                        )
                        await asyncio.sleep(5)
                        continue
                    self.telemetry.set_status(
                        connection,
                        "error",
                        lastError=str(exc),
                        lastErrorAt=time.time(),
                    )
                    self.telemetry.append(
                        {
                            "type": "runtime_error",
                            "provider": connection.provider,
                            "connection_id": connection.id,
                            "error": str(exc),
                        }
                    )
                    await self.publish_event(
                        {
                            "type": "bot.runtime",
                            "provider": connection.provider,
                            "connectionId": connection.id,
                            "status": "error",
                            "error": str(exc),
                        }
                    )
                    await asyncio.sleep(10)
        finally:
            if connection.provider == "slack":
                await self._stop_slack_payload_workers(connection)

    async def _run_slack(self, connection: BotConnection) -> None:
        socket_url = await self._with_credential(
            connection,
            "slack_app_token",
            "slack.socket_url",
            self.slack.socket_url,
        )
        self.telemetry.set_status(
            connection,
            "connecting",
            lastError=None,
        )
        await self.publish_event(
            {
                "type": "bot.runtime",
                "provider": "slack",
                "connectionId": connection.id,
                "status": "connected",
            }
        )
        async with websockets.connect(
            socket_url,
            ping_interval=60,
            ping_timeout=None,
            close_timeout=5,
        ) as websocket:
            self.telemetry.set_status(
                connection,
                "connected",
                connectedAt=time.time(),
                lastError=None,
            )
            async for raw in websocket:
                envelope = json.loads(raw)
                envelope_id = str(
                    envelope.get("envelope_id") or ""
                ).strip() or None
                payload = envelope.get("payload") or {}
                if not isinstance(payload, dict):
                    payload = {}
                admission = self._admit_slack_payload(
                    connection,
                    payload,
                    envelope_id=envelope_id,
                )
                # Ack only after bounded admission succeeds, or when replay is
                # safely deduplicated. A saturated queue deliberately leaves
                # the envelope unacked so Slack can retry instead of losing a
                # user message after we reported success.
                if envelope_id and admission in {
                    "accepted",
                    "deduped",
                }:
                    await websocket.send(
                        json.dumps({"envelope_id": envelope_id})
                    )
                self.telemetry.set_status(
                    connection,
                    "connected",
                    lastEnvelopeAt=time.time(),
                    lastPayloadType=payload.get("type"),
                    lastPayloadAdmission=admission,
                )

    def _schedule_slack_payload(
        self,
        connection: BotConnection,
        payload: dict[str, Any],
    ) -> bool:
        return (
            self._admit_slack_payload(connection, payload)
            == "accepted"
        )

    async def _handle_slack_payload(
        self,
        connection: BotConnection,
        payload: dict[str, Any],
    ) -> None:
        try:
            if payload.get("type") == "block_actions":
                await self.delivery.handle_slack_interaction(
                    connection,
                    payload,
                )
                return
            event = payload.get("event") or {}
            if event:
                self.telemetry.set_status(
                    connection,
                    "connected",
                    lastEventAt=time.time(),
                    lastEventType=event.get("type"),
                )
            channel = (
                event.get("channel")
                or (
                    (event.get("item") or {}).get("channel")
                    if isinstance(event.get("item"), dict)
                    else event.get("channel")
                )
                or connection.default_external_conversation_id
            )
            if not channel:
                return
            # Same-channel ordering is already guaranteed by stable
            # shard assignment and one worker per shard. Avoid retaining a
            # lock object for every channel ever observed.
            async with contextlib.nullcontext():
                if self.conversation_channels is not None:
                    normalized_payload = dict(payload)
                    normalized_event = dict(event)
                    if normalized_event.get("text"):
                        normalized_event["text"] = (
                            self.presentation.strip_slack_mentions(
                                normalized_event.get("text") or ""
                            )
                        )
                    if (
                        normalized_event.get("subtype")
                        == "message_changed"
                        and isinstance(
                            normalized_event.get("message"),
                            dict,
                        )
                    ):
                        changed = dict(normalized_event["message"])
                        changed["text"] = (
                            self.presentation.strip_slack_mentions(
                                changed.get("text") or ""
                            )
                        )
                        normalized_event["message"] = changed
                    normalized_payload["event"] = normalized_event
                    receipts = (
                        await self.conversation_channels.ingest_raw(
                            "slack",
                            connection.id,
                            normalized_payload,
                            actor=self.connections.runtime_actor(
                                connection.project_id
                            ),
                            connection_id=connection.id,
                            project_id=connection.project_id,
                        )
                    )
                    if not receipts:
                        return
                    result = (
                        self.conversation_channels.legacy_routing_result(
                            receipts[0]
                        )
                    )
                    if (
                        not result.get("routed", True)
                        and not result.get("threadId")
                    ):
                        return
                else:
                    if event.get("type") not in {
                        "message",
                        "app_mention",
                    }:
                        return
                    if event.get("bot_id") or event.get("subtype") in {
                        "bot_message",
                        "message_deleted",
                    }:
                        return
                    text = self.presentation.strip_slack_mentions(
                        event.get("text") or ""
                    )
                    if not text:
                        return
                    result = await self.routing.handle_inbound(
                        BotInboundMessage(
                            provider="slack",
                            external_conversation_id=channel,
                            connection_id=connection.id,
                            external_name=(
                                connection.default_external_name
                                or channel
                            ),
                            sender_id=event.get("user"),
                            text=text,
                            project_id=connection.project_id,
                            external_thread_id=(
                                event.get("thread_ts")
                                or event.get("ts")
                            ),
                            message_id=event.get("ts"),
                        )
                    )
            if (
                result.get("ambiguous")
                and self._credential_identity(connection, "bot_token")
            ):
                binding = self.bindings.first_for_connection(
                    "slack",
                    channel,
                )

                async def send_ambiguous(token: str):
                    return await self.slack.post_message(
                        token,
                        channel,
                        self.presentation.ambiguous_route_message(
                            result.get("availablePrefixes") or []
                        ),
                        username=(
                            self.presentation.slack_reply_username(binding)
                            if binding
                            else None
                        ),
                        icon_emoji=(
                            self.presentation.slack_reply_icon(binding)
                            if binding
                            else None
                        ),
                        thread_ts=(
                            event.get("thread_ts") or event.get("ts")
                        ),
                    )

                await self._with_credential(
                    connection,
                    "bot_token",
                    "slack.post_message",
                    send_ambiguous,
                )
            elif (
                result.get("timedOut")
                and self._credential_identity(connection, "bot_token")
            ):
                binding = self.bindings.first_for_connection(
                    "slack",
                    channel,
                )

                async def send_timeout(token: str):
                    return await self.slack.post_message(
                        token,
                        channel,
                        (
                            "Codex is still busy starting that turn, "
                            "so I could not steer it yet."
                        ),
                        username=(
                            self.presentation.slack_reply_username(binding)
                            if binding
                            else None
                        ),
                        icon_emoji=(
                            self.presentation.slack_reply_icon(binding)
                            if binding
                            else None
                        ),
                        thread_ts=(
                            event.get("thread_ts") or event.get("ts")
                        ),
                    )

                await self._with_credential(
                    connection,
                    "bot_token",
                    "slack.post_message",
                    send_timeout,
                )
        except Exception as exc:
            event = payload.get("event") or {}
            channel = (
                event.get("channel")
                or (payload.get("channel") or {}).get("id")
                or connection.default_external_conversation_id
            )
            thread_ts = (
                event.get("thread_ts")
                or event.get("ts")
                or ((payload.get("message") or {}).get("ts"))
            )
            self.telemetry.set_status(
                connection,
                "connected",
                lastError=str(exc),
                lastErrorAt=time.time(),
            )
            self.telemetry.append(
                {
                    "type": "inbound_error",
                    "provider": "slack",
                    "connection_id": connection.id,
                    "external_conversation_id": channel,
                    "message_id": event.get("ts"),
                    "error": str(exc),
                }
            )
            if (
                channel
                and self._credential_identity(connection, "bot_token")
            ):
                binding = self.bindings.first_for_connection(
                    "slack",
                    channel,
                )

                async def send_error(token: str):
                    return await self.slack.post_message(
                        token,
                        channel,
                        (
                            "Codex could not handle that Slack message: "
                            f"{self.presentation.truncate_text(str(exc), 500)}"
                        ),
                        username=(
                            self.presentation.slack_reply_username(binding)
                            if binding
                            else None
                        ),
                        icon_emoji=(
                            self.presentation.slack_reply_icon(binding)
                            if binding
                            else None
                        ),
                        thread_ts=thread_ts,
                    )

                await self._with_credential(
                    connection,
                    "bot_token",
                    "slack.post_message",
                    send_error,
                )

    async def _run_telegram(
        self,
        connection: BotConnection,
    ) -> None:
        offset = connection.telegram_update_offset
        self.telemetry.set_status(
            connection,
            "polling",
            connectedAt=time.time(),
            lastError=None,
        )
        await self.publish_event(
            {
                "type": "bot.runtime",
                "provider": "telegram",
                "connectionId": connection.id,
                "status": "polling",
            }
        )
        while True:

            async def get_updates(token: str):
                return await self.telegram.get_updates(
                    token,
                    offset=offset,
                    timeout=0,
                )

            response = await self._with_credential(
                connection,
                "bot_token",
                "telegram.get_updates",
                get_updates,
            )
            if not response.get("ok"):
                raise RuntimeError(
                    f"Telegram getUpdates failed: {response}"
                )
            for update in response.get("result") or []:
                self.telemetry.set_status(
                    connection,
                    "polling",
                    lastEnvelopeAt=time.time(),
                    lastPayloadType="update",
                )
                update_id = update.get("update_id")
                if update_id is not None:
                    offset = int(update_id) + 1
                message_payload = (
                    update.get("message")
                    or update.get("edited_message")
                    or {}
                )
                if self.conversation_channels is not None:
                    await self.conversation_channels.ingest_raw(
                        "telegram",
                        connection.id,
                        update,
                        actor=self.connections.runtime_actor(
                            connection.project_id
                        ),
                        connection_id=connection.id,
                        project_id=connection.project_id,
                    )
                    continue
                text = (message_payload.get("text") or "").strip()
                chat = message_payload.get("chat") or {}
                chat_id = chat.get("id")
                if not text or chat_id is None:
                    continue
                sender = message_payload.get("from") or {}
                await self.routing.handle_inbound(
                    BotInboundMessage(
                        provider="telegram",
                        external_conversation_id=str(chat_id),
                        connection_id=connection.id,
                        external_name=(
                            chat.get("title")
                            or chat.get("username")
                            or str(chat_id)
                        ),
                        sender_id=(
                            str(sender.get("id"))
                            if sender.get("id") is not None
                            else None
                        ),
                        sender_name=(
                            sender.get("username")
                            or sender.get("first_name")
                        ),
                        text=text,
                        project_id=connection.project_id,
                        external_thread_id=(
                            str(
                                message_payload.get(
                                    "message_thread_id"
                                )
                            )
                            if message_payload.get(
                                "message_thread_id"
                            )
                            is not None
                            else None
                        ),
                        message_id=(
                            str(message_payload.get("message_id"))
                            if message_payload.get("message_id")
                            is not None
                            else None
                        ),
                    )
                )
            if offset is not None:
                self.connections.update(
                    connection.id,
                    telegram_update_offset=offset,
                )
            await asyncio.sleep(10)


def install_bot_runtime(
    app: Any,
    host: Any,
    *,
    connections=None,
    bindings=None,
    presentation=None,
    telemetry=None,
    routing=None,
    delivery=None,
    publish_event=None,
    slack_client: SlackClient | None = None,
    telegram_client: TelegramClient | None = None,
    ownership: Any | None = None,
) -> BotRuntime:
    existing = getattr(app.state, "bot_runtime", None)
    if isinstance(existing, BotRuntime):
        existing.ownership = ownership or existing.ownership
        host.bot_runtime = existing
        return existing

    runtime = BotRuntime(
        connections=connections or app.state.bot_connection_service,
        bindings=bindings or app.state.bot_binding_selection_service,
        presentation=presentation or app.state.bot_presentation_service,
        telemetry=telemetry or app.state.bot_runtime_telemetry,
        routing=routing or app.state.bot_routing_service,
        delivery=delivery or app.state.bot_delivery_service,
        publish_event=publish_event or host.hub.publish,
        slack_client=slack_client,
        telegram_client=telegram_client,
        secret_broker=getattr(app.state, "secret_broker", None),
        ownership=ownership,
    )
    host.bot_runtime = runtime
    host._is_transient_websocket_disconnect = (
        runtime.is_transient_websocket_disconnect
    )
    app.state.bot_runtime = runtime
    return runtime
