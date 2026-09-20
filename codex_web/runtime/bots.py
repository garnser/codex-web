from __future__ import annotations

import asyncio
import contextlib
import json
import time
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
        self.slack_payload_locks: dict[str, asyncio.Lock] = {}
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
                envelope_id = envelope.get("envelope_id")
                if envelope_id:
                    await websocket.send(
                        json.dumps({"envelope_id": envelope_id})
                    )
                payload = envelope.get("payload") or {}
                self.telemetry.set_status(
                    connection,
                    "connected",
                    lastEnvelopeAt=time.time(),
                    lastPayloadType=payload.get("type"),
                )
                self._schedule_slack_payload(connection, payload)

    def _schedule_slack_payload(
        self,
        connection: BotConnection,
        payload: dict[str, Any],
    ) -> None:
        task = asyncio.create_task(
            self._handle_slack_payload(connection, payload),
            name=f"slack-payload-{connection.id}",
        )

        def done_callback(completed: asyncio.Task[None]) -> None:
            try:
                completed.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
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
                        "error": str(exc),
                    }
                )

        task.add_done_callback(done_callback)

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
            lock_key = f"{connection.id}:{channel}"
            lock = self.slack_payload_locks.setdefault(
                lock_key,
                asyncio.Lock(),
            )
            async with lock:
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
