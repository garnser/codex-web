from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from collections import deque
from typing import Any

from fastapi import Request

from codex_web.conversation_channels import ConversationEventKind
from codex_web.integrations.slack_client import SlackClient
from codex_web.models import BotConnection, BotInboundMessage
from codex_web.services.bot_routing import BotRoutingService
from codex_web.services.conversation_channels import ConversationChannelService


class SlackProviderService:
    """Own Slack HTTP ingress and missed-message backfill outside the legacy runtime."""

    def __init__(
        self,
        host: Any,
        *,
        slack_client: SlackClient,
        routing_service: BotRoutingService,
        channel_service: ConversationChannelService | None = None,
    ) -> None:
        self.host = host
        self.slack = slack_client
        self.routing = routing_service
        self.channels = channel_service
        self.task: asyncio.Task[None] | None = None
        self.seen: set[str] = set()
        self.bad_threads: set[tuple[str, str, str]] = set()
        self.cooldown_until = 0.0
        self.rate_limit_failures = 0

    def interval_seconds(self) -> float:
        try:
            seconds = float(os.environ.get("CODEX_WEB_SLACK_BACKFILL_INTERVAL_SECONDS") or "15")
        except ValueError:
            return 15.0
        return max(5.0, seconds)

    def window_seconds(self) -> float:
        try:
            seconds = float(os.environ.get("CODEX_WEB_SLACK_BACKFILL_WINDOW_SECONDS") or "300")
        except ValueError:
            return 300.0
        return max(30.0, min(seconds, 3600.0))

    def rate_limit_min_seconds(self) -> float:
        try:
            seconds = float(os.environ.get("CODEX_WEB_SLACK_BACKFILL_RATE_LIMIT_MIN_SECONDS") or "60")
        except ValueError:
            return 60.0
        return max(5.0, seconds)

    def rate_limit_max_seconds(self) -> float:
        try:
            seconds = float(os.environ.get("CODEX_WEB_SLACK_BACKFILL_RATE_LIMIT_MAX_SECONDS") or "900")
        except ValueError:
            return 900.0
        return max(self.rate_limit_min_seconds(), seconds)

    def cooldown_remaining_seconds(self) -> float:
        return max(0.0, self.cooldown_until - time.time())

    def health(self) -> dict[str, Any]:
        return {
            "intervalSeconds": self.interval_seconds(),
            "running": bool(self.task and not self.task.done()),
            "cooldownRemainingSeconds": self.cooldown_remaining_seconds(),
            "cooldownUntil": self.cooldown_until or None,
            "rateLimitFailures": self.rate_limit_failures,
        }

    async def start(self) -> None:
        if self.task and not self.task.done():
            return
        if self.interval_seconds() <= 0:
            return
        self.task = asyncio.create_task(self._run_loop(), name="slack-backfill")

    async def stop(self) -> None:
        task = self.task
        self.task = None
        if not task:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    def _is_rate_limited(self, response: dict[str, Any]) -> bool:
        return response.get("_http_status") == 429 or response.get("error") == "ratelimited"

    def _apply_rate_limit(self, response: dict[str, Any], context: dict[str, Any]) -> None:
        self.rate_limit_failures += 1
        minimum = self.rate_limit_min_seconds()
        maximum = self.rate_limit_max_seconds()
        retry_after = response.get("_retry_after")
        fallback = min(maximum, minimum * (2 ** min(self.rate_limit_failures - 1, 4)))
        delay = max(minimum, float(retry_after) if retry_after is not None else fallback)
        delay = min(maximum, delay)
        self.cooldown_until = max(self.cooldown_until, time.time() + delay)
        self.host._append_bot_event(
            {
                "type": "slack_backfill_cooldown_set",
                "provider": "slack",
                "delay_seconds": delay,
                "retry_after": retry_after,
                "failure_count": self.rate_limit_failures,
                **context,
            }
        )

    def _record_failure(self, event_type: str, response: dict[str, Any], context: dict[str, Any]) -> bool:
        self.host._append_bot_event(
            {
                "type": event_type,
                "provider": "slack",
                **context,
                "error": response.get("error") or str(response),
            }
        )
        if self._is_rate_limited(response):
            self._apply_rate_limit(response, context)
            return True
        return False

    def _recent_inbound_message_ids(self, limit: int = 2000) -> set[str]:
        events_file = self.host.BOTS_EVENTS_FILE
        if not events_file.exists():
            return set()
        with events_file.open(errors="replace") as handle:
            lines = deque(handle, maxlen=max(1, limit))
        message_ids: set[str] = set()
        for line in lines:
            with contextlib.suppress(Exception):
                event = json.loads(line)
                if event.get("provider") == "slack" and event.get("message_id"):
                    message_ids.add(str(event["message_id"]))
        return message_ids

    def _channels(self) -> list[tuple[BotConnection, str]]:
        connections = {
            connection.id: connection
            for connection in self.host._load_bot_connections()
            if connection.provider == "slack" and connection.bot_token
        }
        pairs: dict[tuple[str, str], tuple[BotConnection, str]] = {}
        for binding in self.host._load_bot_bindings():
            connection = connections.get(binding.connection_id or "")
            if connection and binding.external_conversation_id:
                pairs[(connection.id, binding.external_conversation_id)] = (
                    connection,
                    binding.external_conversation_id,
                )
        for connection in connections.values():
            if connection.default_external_conversation_id:
                pairs.setdefault(
                    (connection.id, connection.default_external_conversation_id),
                    (connection, connection.default_external_conversation_id),
                )
        return list(pairs.values())

    def _thread_targets(self) -> list[tuple[BotConnection, str, str]]:
        connections = {
            connection.id: connection
            for connection in self.host._load_bot_connections()
            if connection.provider == "slack" and connection.bot_token
        }
        bindings = {
            (binding.provider, binding.thread_id, binding.external_conversation_id): binding
            for binding in self.host._load_bot_bindings()
            if binding.provider == "slack" and binding.connection_id
        }
        all_bindings = self.host._load_bot_bindings()
        targets: list[Any] = []
        targets.extend(self.host._load_bot_reply_targets().values())
        targets.extend(self.host._load_bot_delivery_targets().values())
        targets.extend(active.reply_target for active in self.host._load_active_turns().values() if active.reply_target)
        pairs: dict[tuple[str, str, str], tuple[BotConnection, str, str]] = {}
        for target in targets:
            if target.provider != "slack" or not target.external_conversation_id:
                continue
            thread_ts = target.external_thread_id or target.message_id
            if not thread_ts:
                continue
            binding = bindings.get((target.provider, target.thread_id, target.external_conversation_id))
            if not binding:
                candidates = [
                    item
                    for item in all_bindings
                    if item.provider == "slack"
                    and item.thread_id == target.thread_id
                    and item.external_conversation_id == target.external_conversation_id
                    and item.connection_id
                ]
                binding = candidates[0] if candidates else None
            connection = connections.get(binding.connection_id or "") if binding else None
            if not connection:
                continue
            pairs[(connection.id, target.external_conversation_id, thread_ts)] = (
                connection,
                target.external_conversation_id,
                thread_ts,
            )
        return list(pairs.values())

    async def run_backfill_cycle(self) -> None:
        recent_message_ids = self._recent_inbound_message_ids()
        oldest = f"{max(0.0, time.time() - self.window_seconds()):.6f}"
        for connection, channel_id in self._channels():
            assert connection.bot_token
            response = await self.slack.history(
                connection.bot_token,
                channel_id,
                oldest=oldest,
                limit=50,
            )
            if not response.get("ok"):
                rate_limited = self._record_failure(
                    "slack_backfill_failed",
                    response,
                    {
                        "connection_id": connection.id,
                        "external_conversation_id": channel_id,
                    },
                )
                if rate_limited:
                    return
                continue
            self.rate_limit_failures = 0
            for event in reversed(response.get("messages") or []):
                await self._dispatch_backfill_message(
                    connection,
                    channel_id,
                    event,
                    recent_message_ids=recent_message_ids,
                )

        for connection, channel_id, thread_ts in self._thread_targets():
            thread_key = (connection.id, channel_id, thread_ts)
            if thread_key in self.bad_threads:
                continue
            assert connection.bot_token
            response = await self.slack.replies(
                connection.bot_token,
                channel_id,
                thread_ts,
                oldest=oldest,
                limit=50,
            )
            if not response.get("ok"):
                if response.get("error") in {"thread_not_found", "channel_not_found", "not_in_channel"}:
                    self.bad_threads.add(thread_key)
                rate_limited = self._record_failure(
                    "slack_thread_backfill_failed",
                    response,
                    {
                        "connection_id": connection.id,
                        "external_conversation_id": channel_id,
                        "external_thread_id": thread_ts,
                    },
                )
                if rate_limited:
                    return
                continue
            self.rate_limit_failures = 0
            for event in reversed(response.get("messages") or []):
                message_id = str(event.get("ts") or "").strip()
                if message_id == thread_ts:
                    continue
                await self._dispatch_backfill_message(
                    connection,
                    channel_id,
                    event,
                    recent_message_ids=recent_message_ids,
                    fallback_thread_ts=thread_ts,
                )

    def _connection_scope(
        self,
        connection: BotConnection | None,
    ) -> tuple[str | None, str | None]:
        if connection is None:
            return None, None
        try:
            project = self.host._project(connection.project_id)
        except Exception:
            return None, None
        return (
            getattr(project, "organization_id", None),
            getattr(project, "workspace_id", None),
        )

    async def _dispatch_backfill_message(
        self,
        connection: BotConnection,
        channel_id: str,
        event: dict[str, Any],
        *,
        recent_message_ids: set[str],
        fallback_thread_ts: str | None = None,
    ) -> None:
        message_id = str(event.get("ts") or "").strip()
        if not message_id or message_id in self.seen or message_id in recent_message_ids:
            return
        if event.get("bot_id") or event.get("subtype") == "bot_message":
            self.seen.add(message_id)
            return
        self.seen.add(message_id)

        if self.channels is not None:
            provider_payload = dict(event)
            provider_payload.setdefault("channel", channel_id)
            if fallback_thread_ts:
                provider_payload.setdefault("thread_ts", fallback_thread_ts)
            tenant_id, workspace_id = self._connection_scope(connection)
            delivery = await self.channels.normalize_and_ingest(
                "slack",
                provider_payload,
                provider_instance=connection.id,
                event_id=message_id,
                project_id=connection.project_id,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
            )
            if delivery is None or not delivery.inserted:
                return
            if delivery.fact.event_kind not in {
                ConversationEventKind.MESSAGE,
                ConversationEventKind.EDIT,
            }:
                return
            message = self.channels.to_bot_inbound(
                delivery.fact,
                connection_id=connection.id,
                project_id=connection.project_id,
            )
            text = self.host._strip_slack_mentions(message.text)
            if not text:
                return
            message = message.model_copy(
                update={
                    "text": text,
                    "external_name": (
                        connection.default_external_name or channel_id
                    ),
                }
            )
        else:
            if event.get("subtype") == "message_deleted":
                return
            text = self.host._strip_slack_mentions(event.get("text") or "")
            if not text:
                return
            message = BotInboundMessage(
                provider="slack",
                external_conversation_id=channel_id,
                connection_id=connection.id,
                external_name=connection.default_external_name or channel_id,
                sender_id=event.get("user"),
                text=text,
                project_id=connection.project_id,
                external_thread_id=(
                    event.get("thread_ts")
                    or fallback_thread_ts
                    or message_id
                ),
                message_id=message_id,
            )

        result = await self.routing.handle_inbound(message)
        event_type = (
            "slack_thread_backfill_dispatched"
            if fallback_thread_ts
            else "slack_backfill_dispatched"
        )
        payload: dict[str, Any] = {
            "type": event_type,
            "provider": "slack",
            "connection_id": connection.id,
            "external_conversation_id": channel_id,
            "message_id": message_id,
            "thread_id": result.get("threadId"),
            "queued": result.get("queued", False),
            "ok": result.get("ok", False),
        }
        if fallback_thread_ts:
            payload["external_thread_id"] = fallback_thread_ts
        self.host._append_bot_event(payload)

    async def _run_loop(self) -> None:
        interval = self.interval_seconds()
        if interval <= 0:
            return
        while True:
            cooldown = self.cooldown_remaining_seconds()
            if cooldown > 0:
                await asyncio.sleep(max(interval, cooldown))
                continue
            try:
                await self.run_backfill_cycle()
            except Exception as exc:
                self.host._append_bot_event({"type": "slack_backfill_loop_failed", "error": str(exc)})
            await asyncio.sleep(interval)

    async def handle_webhook(self, request: Request) -> dict[str, Any]:
        body = await request.body()
        self.host._verify_slack_signature(request, body)
        payload = json.loads(body or b"{}")

        if payload.get("type") == "url_verification":
            return {"challenge": payload.get("challenge")}
        if payload.get("type") != "event_callback":
            return {"ok": True, "ignored": True}

        event = payload.get("event") or {}
        channel = event.get("channel")
        if not channel:
            return {"ok": True, "ignored": True}
        connection = self.host._bot_connection_for_conversation("slack", channel)

        if self.channels is not None:
            tenant_id, workspace_id = self._connection_scope(connection)
            delivery = await self.channels.normalize_and_ingest(
                "slack",
                payload,
                provider_instance=connection.id if connection else "unbound",
                event_id=payload.get("event_id"),
                project_id=connection.project_id if connection else None,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
            )
            if delivery is None:
                return {"ok": True, "ignored": True}
            if not delivery.inserted:
                return {
                    "ok": True,
                    "accepted": False,
                    "duplicate": True,
                }
            if delivery.fact.event_kind not in {
                ConversationEventKind.MESSAGE,
                ConversationEventKind.EDIT,
            }:
                return {
                    "ok": True,
                    "accepted": False,
                    "canonicalOnly": True,
                }
            message = self.channels.to_bot_inbound(
                delivery.fact,
                connection_id=connection.id if connection else None,
                project_id=connection.project_id if connection else None,
            )
            text = self.host._strip_slack_mentions(message.text)
            if not text:
                return {
                    "ok": True,
                    "accepted": False,
                    "ignored": True,
                }
            message = message.model_copy(update={"text": text})
        else:
            if event.get("type") not in {"message", "app_mention"}:
                return {"ok": True, "ignored": True}
            if event.get("bot_id") or event.get("subtype") in {
                "bot_message",
                "message_deleted",
            }:
                return {"ok": True, "ignored": True}
            text = self.host._strip_slack_mentions(event.get("text") or "")
            if not text:
                return {"ok": True, "ignored": True}
            message = BotInboundMessage(
                provider="slack",
                external_conversation_id=channel,
                connection_id=connection.id if connection else None,
                external_name=channel,
                sender_id=event.get("user"),
                text=text,
                project_id=connection.project_id if connection else None,
                external_thread_id=event.get("thread_ts") or event.get("ts"),
                message_id=event.get("ts"),
            )

        result = await self.routing.handle_inbound(message)
        if result.get("ambiguous"):
            if self.channels is None:
                binding = self.host._first_binding_for_connection("slack", channel)
                legacy_connection = (
                    self.host._bot_connection(binding.connection_id)
                    if binding and binding.connection_id
                    else None
                )
                if legacy_connection and legacy_connection.bot_token:
                    await self.slack.post_message(
                        legacy_connection.bot_token,
                        channel,
                        self.host._ambiguous_route_message(
                            result["availablePrefixes"]
                        ),
                        username=(
                            self.host._slack_reply_username(binding)
                            if binding
                            else None
                        ),
                        icon_emoji=(
                            self.host._slack_reply_icon(binding)
                            if binding
                            else None
                        ),
                        thread_ts=event.get("thread_ts") or event.get("ts"),
                    )
            return {
                "ok": True,
                "accepted": False,
                "ambiguous": True,
                "availablePrefixes": result["availablePrefixes"],
            }
        if result.get("timedOut"):
            return {
                "ok": True,
                "accepted": False,
                "timedOut": True,
                "threadId": result.get("threadId"),
            }
        return {"ok": True, "accepted": True, "threadId": result["threadId"]}


def install_slack_provider_service(
    app: Any,
    host: Any,
    *,
    slack_client: SlackClient,
    routing_service: BotRoutingService,
    channel_service: ConversationChannelService | None = None,
) -> SlackProviderService:
    existing = getattr(app.state, "slack_provider_service", None)
    if isinstance(existing, SlackProviderService) and existing.host is host:
        service = existing
        if channel_service is not None:
            service.channels = channel_service
    else:
        service = SlackProviderService(
            host,
            slack_client=slack_client,
            routing_service=routing_service,
            channel_service=channel_service,
        )
        app.state.slack_provider_service = service

    # Compatibility entrypoints remain callable while their legacy definitions
    # are removed in the follow-up pruning pass.
    host._run_slack_backfill_cycle = service.run_backfill_cycle
    host._slack_backfill_loop = service._run_loop
    host._slack_backfill_interval_seconds = service.interval_seconds
    host._slack_backfill_cooldown_remaining_seconds = service.cooldown_remaining_seconds
    host._slack_backfill_thread_targets = service._thread_targets
    return service
