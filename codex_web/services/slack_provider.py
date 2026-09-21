from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from collections import deque
from types import SimpleNamespace
from typing import Any

from fastapi import Request

from codex_web.integrations.slack_client import SlackClient
from codex_web.models import BotConnection, BotInboundMessage
from codex_web.services.bot_binding_selection import BotBindingSelectionService
from codex_web.services.bot_connections import BotConnectionService
from codex_web.services.bot_presentation import BotPresentationService
from codex_web.services.bot_routing import BotRoutingService
from codex_web.services.bot_runtime_telemetry import BotRuntimeTelemetry
from codex_web.services.bot_targets import BotTargetService
from codex_web.services.bot_webhook_security import BotWebhookSecurityService
from codex_web.services.conversation_channels import ConversationChannelService
from codex_web.services.secrets import SecretBroker
from codex_web.storage.slack_backfill import SlackBackfillStore


class SlackProviderService:
    """Own Slack HTTP ingress and missed-message backfill."""

    def __init__(
        self,
        *,
        slack_client: SlackClient,
        routing_service: BotRoutingService,
        connections: BotConnectionService,
        bindings: BotBindingSelectionService,
        targets: BotTargetService,
        presentation: BotPresentationService,
        telemetry: BotRuntimeTelemetry,
        webhook_security: BotWebhookSecurityService,
        secret_broker: SecretBroker | None = None,
        conversation_channels: ConversationChannelService | None = None,
        reconciliation_gates: Any | None = None,
        backfill_store: SlackBackfillStore | None = None,
    ) -> None:
        self.slack = slack_client
        self.routing = routing_service
        self.connections = connections
        self.bindings = bindings
        self.targets = targets
        self.presentation = presentation
        self.telemetry = telemetry
        self.webhook_security = webhook_security
        self.secret_broker = secret_broker
        self.conversation_channels = conversation_channels
        self.reconciliation_gates = reconciliation_gates
        self.backfill_store = backfill_store
        self.backfill_lock = asyncio.Lock()
        self.gate_states: dict[str, dict[str, Any]] = {}
        self.task: asyncio.Task[None] | None = None
        self.seen: set[str] = set()
        self.bad_threads: set[tuple[str, str, str]] = set()
        persisted = backfill_store.load() if backfill_store is not None else None
        self.cooldown_until = float(
            persisted.cooldown_until or 0.0
        ) if persisted is not None else 0.0
        self.rate_limit_failures = int(
            persisted.rate_limit_failures
        ) if persisted is not None else 0

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

    async def _with_credential(
        self,
        connection: BotConnection,
        field: str,
        operation: str,
        consumer,
    ):
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

    def interval_seconds(self) -> float:
        try:
            seconds = float(
                os.environ.get(
                    "CODEX_WEB_SLACK_BACKFILL_INTERVAL_SECONDS"
                )
                or "15"
            )
        except ValueError:
            return 15.0
        if seconds <= 0:
            return 0.0
        return max(5.0, seconds)

    def window_seconds(self) -> float:
        try:
            seconds = float(
                os.environ.get("CODEX_WEB_SLACK_BACKFILL_WINDOW_SECONDS")
                or "300"
            )
        except ValueError:
            return 300.0
        return max(30.0, min(seconds, 3600.0))

    def rate_limit_min_seconds(self) -> float:
        try:
            seconds = float(
                os.environ.get(
                    "CODEX_WEB_SLACK_BACKFILL_RATE_LIMIT_MIN_SECONDS"
                )
                or "60"
            )
        except ValueError:
            return 60.0
        return max(5.0, seconds)

    def rate_limit_max_seconds(self) -> float:
        try:
            seconds = float(
                os.environ.get(
                    "CODEX_WEB_SLACK_BACKFILL_RATE_LIMIT_MAX_SECONDS"
                )
                or "900"
            )
        except ValueError:
            return 900.0
        return max(self.rate_limit_min_seconds(), seconds)

    def cooldown_remaining_seconds(self) -> float:
        return max(0.0, self.cooldown_until - time.time())

    def max_provider_calls_per_cycle(self) -> int:
        try:
            value = int(
                os.environ.get(
                    "CODEX_WEB_SLACK_BACKFILL_MAX_CALLS_PER_CYCLE"
                )
                or "20"
            )
        except ValueError:
            value = 20
        return max(1, min(value, 200))

    def max_cycle_seconds(self) -> float:
        try:
            value = float(
                os.environ.get(
                    "CODEX_WEB_SLACK_BACKFILL_MAX_CYCLE_SECONDS"
                )
                or "5"
            )
        except ValueError:
            value = 5.0
        return max(0.1, min(value, 60.0))

    def batch_limit(self) -> int:
        try:
            value = int(
                os.environ.get(
                    "CODEX_WEB_SLACK_BACKFILL_BATCH_LIMIT"
                )
                or "50"
            )
        except ValueError:
            value = 50
        return max(1, min(value, 200))

    def health(self) -> dict[str, Any]:
        diagnostics = (
            self.backfill_store.diagnostics()
            if self.backfill_store is not None
            else {}
        )
        return {
            "intervalSeconds": self.interval_seconds(),
            "running": bool(self.task and not self.task.done()),
            "cycleActive": self.backfill_lock.locked(),
            "maxProviderCallsPerCycle": self.max_provider_calls_per_cycle(),
            "maxCycleSeconds": self.max_cycle_seconds(),
            "batchLimit": self.batch_limit(),
            "cooldownRemainingSeconds": self.cooldown_remaining_seconds(),
            "cooldownUntil": self.cooldown_until or None,
            "rateLimitFailures": self.rate_limit_failures,
            "gateStates": dict(self.gate_states),
            "checkpoint": diagnostics,
        }

    async def start(self) -> None:
        if self.task and not self.task.done():
            return
        if self.interval_seconds() <= 0:
            return
        self.task = asyncio.create_task(
            self._run_loop(),
            name="slack-backfill",
        )

    async def stop(self) -> None:
        task = self.task
        self.task = None
        if not task:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    @staticmethod
    def _is_rate_limited(response: dict[str, Any]) -> bool:
        return (
            response.get("_http_status") == 429
            or response.get("error") == "ratelimited"
        )

    def _apply_rate_limit(
        self,
        response: dict[str, Any],
        context: dict[str, Any],
    ) -> None:
        self.rate_limit_failures += 1
        minimum = self.rate_limit_min_seconds()
        maximum = self.rate_limit_max_seconds()
        retry_after = response.get("_retry_after")
        fallback = min(
            maximum,
            minimum * (2 ** min(self.rate_limit_failures - 1, 4)),
        )
        delay = max(
            minimum,
            float(retry_after)
            if retry_after is not None
            else fallback,
        )
        delay = min(maximum, delay)
        self.cooldown_until = max(
            self.cooldown_until,
            time.time() + delay,
        )
        self.telemetry.append(
            {
                "type": "slack_backfill_cooldown_set",
                "provider": "slack",
                "delay_seconds": delay,
                "retry_after": retry_after,
                "failure_count": self.rate_limit_failures,
                **context,
            }
        )

    def _record_failure(
        self,
        event_type: str,
        response: dict[str, Any],
        context: dict[str, Any],
    ) -> bool:
        self.telemetry.append(
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

    def _recent_inbound_message_ids(
        self,
        limit: int = 2000,
    ) -> set[str]:
        events_file = self.telemetry.events_file
        if not events_file.exists():
            return set()
        with events_file.open(errors="replace") as handle:
            lines = deque(handle, maxlen=max(1, limit))
        message_ids: set[str] = set()
        for line in lines:
            with contextlib.suppress(Exception):
                event = json.loads(line)
                if (
                    event.get("provider") == "slack"
                    and event.get("message_id")
                ):
                    message_ids.add(str(event["message_id"]))
        return message_ids

    def _slack_connections(self) -> dict[str, BotConnection]:
        return {
            connection.id: connection
            for connection in self.connections.load_connections()
            if connection.provider == "slack"
            and self._credential_identity(connection, "bot_token")
        }

    def _channels(
        self,
        allowed_project_ids: set[str] | None = None,
    ) -> list[tuple[BotConnection, str]]:
        connections = self._slack_connections()
        if allowed_project_ids is not None:
            connections = {
                key: value
                for key, value in connections.items()
                if value.project_id in allowed_project_ids
            }
        pairs: dict[
            tuple[str, str],
            tuple[BotConnection, str],
        ] = {}
        for binding in self.bindings.load_bindings():
            connection = connections.get(binding.connection_id or "")
            if connection and binding.external_conversation_id:
                pairs[
                    (connection.id, binding.external_conversation_id)
                ] = (
                    connection,
                    binding.external_conversation_id,
                )
        for connection in connections.values():
            if connection.default_external_conversation_id:
                pairs.setdefault(
                    (
                        connection.id,
                        connection.default_external_conversation_id,
                    ),
                    (
                        connection,
                        connection.default_external_conversation_id,
                    ),
                )
        return list(pairs.values())

    def _thread_targets(
        self,
        allowed_project_ids: set[str] | None = None,
    ) -> list[tuple[BotConnection, str, str]]:
        connections = self._slack_connections()
        if allowed_project_ids is not None:
            connections = {
                key: value
                for key, value in connections.items()
                if value.project_id in allowed_project_ids
            }
        all_bindings = self.bindings.load_bindings()
        binding_map = {
            (
                binding.provider,
                binding.thread_id,
                binding.external_conversation_id,
            ): binding
            for binding in all_bindings
            if binding.provider == "slack" and binding.connection_id
        }
        targets: list[Any] = []
        targets.extend(self.targets.load_reply_targets().values())
        targets.extend(self.targets.load_delivery_targets().values())
        targets.extend(
            active.reply_target
            for active in self.targets.load_active_turns().values()
            if active.reply_target
        )
        pairs: dict[
            tuple[str, str, str],
            tuple[BotConnection, str, str],
        ] = {}
        for target in targets:
            if (
                target.provider != "slack"
                or not target.external_conversation_id
            ):
                continue
            thread_ts = target.external_thread_id or target.message_id
            if not thread_ts:
                continue
            binding = binding_map.get(
                (
                    target.provider,
                    target.thread_id,
                    target.external_conversation_id,
                )
            )
            if not binding:
                candidates = [
                    item
                    for item in all_bindings
                    if item.provider == "slack"
                    and item.thread_id == target.thread_id
                    and item.external_conversation_id
                    == target.external_conversation_id
                    and item.connection_id
                ]
                binding = candidates[0] if candidates else None
            connection = (
                connections.get(binding.connection_id or "")
                if binding
                else None
            )
            if not connection:
                continue
            pairs[
                (
                    connection.id,
                    target.external_conversation_id,
                    thread_ts,
                )
            ] = (
                connection,
                target.external_conversation_id,
                thread_ts,
            )
        return list(pairs.values())

    async def _history(
        self,
        connection: BotConnection,
        channel_id: str,
        oldest: str,
    ) -> dict[str, Any]:
        async def operation(token: str):
            return await self.slack.history(
                token,
                channel_id,
                oldest=oldest,
                limit=50,
            )

        return await self._with_credential(
            connection,
            "bot_token",
            "slack.backfill.history",
            operation,
        )

    async def _replies(
        self,
        connection: BotConnection,
        channel_id: str,
        thread_ts: str,
        oldest: str,
    ) -> dict[str, Any]:
        async def operation(token: str):
            return await self.slack.replies(
                token,
                channel_id,
                thread_ts,
                oldest=oldest,
                limit=50,
            )

        return await self._with_credential(
            connection,
            "bot_token",
            "slack.backfill.replies",
            operation,
        )

    def _gate_decision(self, project_id: str):
        if self.reconciliation_gates is None:
            return None
        actor = self.connections.runtime_actor(project_id)
        decision = self.reconciliation_gates.decision(
            "slack-backfill",
            project_id,
            actor=actor,
        )
        self.gate_states[project_id] = decision.model_dump(mode="json")
        return decision

    async def _eligible_backfill_projects(self) -> dict[str, Any]:
        connections = await asyncio.to_thread(self._slack_connections)
        projects = sorted(
            {
                connection.project_id
                for connection in connections.values()
                if connection.project_id
            }
        )
        eligible: dict[str, Any] = {}
        for project_id in projects:
            if self.reconciliation_gates is None:
                eligible[project_id] = None
                continue
            decision = await asyncio.to_thread(
                self._gate_decision,
                project_id,
            )
            if decision.eligible:
                eligible[project_id] = self.connections.runtime_actor(
                    project_id
                )
        return eligible

    async def run_backfill_cycle(self) -> None:
        eligible = await self._eligible_backfill_projects()
        if not eligible:
            return

        started: dict[str, Any] = {}
        if self.reconciliation_gates is not None:
            for project_id, actor in eligible.items():
                try:
                    await asyncio.to_thread(
                        self.reconciliation_gates.record_start,
                        "slack-backfill",
                        project_id,
                        actor=actor,
                    )
                    started[project_id] = actor
                    await asyncio.to_thread(
                        self._gate_decision,
                        project_id,
                    )
                except Exception as exc:
                    self.gate_states[project_id] = {
                        "service_id": "slack-backfill",
                        "project_id": project_id,
                        "state": "paused",
                        "eligible": False,
                        "reason_code": "gate_start_conflict",
                        "reason": type(exc).__name__,
                    }

        allowed = set(eligible) if self.reconciliation_gates is None else set(started)
        if not allowed:
            return

        recent_message_ids = await asyncio.to_thread(
            self._recent_inbound_message_ids
        )
        channels = await asyncio.to_thread(self._channels, allowed)
        thread_targets = await asyncio.to_thread(
            self._thread_targets,
            allowed,
        )
        oldest = (
            f"{max(0.0, time.time() - self.window_seconds()):.6f}"
        )
        try:
            for connection, channel_id in channels:
                response = await self._history(
                    connection,
                    channel_id,
                    oldest,
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

            for connection, channel_id, thread_ts in thread_targets:
                thread_key = (connection.id, channel_id, thread_ts)
                if thread_key in self.bad_threads:
                    continue
                response = await self._replies(
                    connection,
                    channel_id,
                    thread_ts,
                    oldest,
                )
                if not response.get("ok"):
                    if response.get("error") in {
                        "thread_not_found",
                        "channel_not_found",
                        "not_in_channel",
                    }:
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
        finally:
            if self.reconciliation_gates is not None:
                for project_id, actor in started.items():
                    await asyncio.to_thread(
                        self.reconciliation_gates.record_completion,
                        "slack-backfill",
                        project_id,
                        actor=actor,
                    )
                    await asyncio.to_thread(
                        self._gate_decision,
                        project_id,
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
        if (
            not message_id
            or message_id in self.seen
            or message_id in recent_message_ids
        ):
            return
        if event.get("bot_id") or event.get("subtype") in {
            "bot_message",
            "message_deleted",
        }:
            self.seen.add(message_id)
            return
        text = self.presentation.strip_slack_mentions(
            event.get("text") or ""
        )
        if not text:
            self.seen.add(message_id)
            return
        self.seen.add(message_id)
        result = await self.routing.handle_inbound(
            BotInboundMessage(
                provider="slack",
                external_conversation_id=channel_id,
                connection_id=connection.id,
                external_name=(
                    connection.default_external_name or channel_id
                ),
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
        )
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
        self.telemetry.append(payload)

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
                self.telemetry.append(
                    {
                        "type": "slack_backfill_loop_failed",
                        "error": str(exc),
                    }
                )
            await asyncio.sleep(interval)

    async def handle_webhook(
        self,
        request: Request,
    ) -> dict[str, Any]:
        body = await request.body()
        await self.webhook_security.verify_slack(request, body)
        payload = json.loads(body or b"{}")

        if payload.get("type") == "url_verification":
            return {"challenge": payload.get("challenge")}
        if payload.get("type") != "event_callback":
            return {"ok": True, "ignored": True}

        event = payload.get("event") or {}
        if not isinstance(event, dict):
            return {"ok": True, "ignored": True}
        channel = (
            event.get("channel")
            or (
                (event.get("item") or {}).get("channel")
                if isinstance(event.get("item"), dict)
                else event.get("channel")
            )
        )
        if not channel:
            return {"ok": True, "ignored": True}

        connection = self.connections.for_conversation(
            "slack",
            str(channel),
        )
        project_id = connection.project_id if connection else "home"

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
                normalized_event.get("subtype") == "message_changed"
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
            receipts = await self.conversation_channels.ingest_raw(
                "slack",
                connection.id if connection else "slack:default",
                normalized_payload,
                actor=self.connections.runtime_actor(project_id),
                connection_id=connection.id if connection else None,
                project_id=project_id,
            )
            if not receipts:
                return {"ok": True, "ignored": True}
            result = self.conversation_channels.legacy_routing_result(
                receipts[0]
            )
            if (
                not result.get("routed", True)
                and not result.get("threadId")
            ):
                return {
                    "ok": True,
                    "accepted": False,
                    "conversationOutcome": result.get(
                        "conversationOutcome"
                    ),
                    "canonicalEventId": result.get(
                        "canonicalEventId"
                    ),
                }
        else:
            if event.get("type") not in {"message", "app_mention"}:
                return {"ok": True, "ignored": True}
            if event.get("bot_id") or event.get("subtype") in {
                "bot_message",
                "message_deleted",
            }:
                return {"ok": True, "ignored": True}
            text = self.presentation.strip_slack_mentions(
                event.get("text") or ""
            )
            if not text:
                return {"ok": True, "ignored": True}
            result = await self.routing.handle_inbound(
                BotInboundMessage(
                    provider="slack",
                    external_conversation_id=str(channel),
                    connection_id=(
                        connection.id if connection else None
                    ),
                    external_name=str(channel),
                    sender_id=event.get("user"),
                    text=text,
                    project_id=project_id,
                    external_thread_id=(
                        event.get("thread_ts") or event.get("ts")
                    ),
                    message_id=event.get("ts"),
                )
            )
        if result.get("ambiguous"):
            binding = self.bindings.first_for_connection(
                "slack",
                channel,
            )
            connection = (
                self.connections.get(binding.connection_id)
                if binding and binding.connection_id
                else None
            )
            if (
                connection
                and self._credential_identity(
                    connection,
                    "bot_token",
                )
            ):
                async def notify(token: str):
                    return await self.slack.post_message(
                        token,
                        channel,
                        self.presentation.ambiguous_route_message(
                            result["availablePrefixes"]
                        ),
                        username=(
                            self.presentation.slack_reply_username(
                                binding
                            )
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
                    "slack.post_ambiguous_route",
                    notify,
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
        return {
            "ok": True,
            "accepted": True,
            "threadId": result["threadId"],
        }


def install_slack_provider_service(
    app: Any,
    host: Any,
    *,
    slack_client: SlackClient,
    routing_service: BotRoutingService,
    connections=None,
    bindings=None,
    targets=None,
    presentation=None,
    telemetry=None,
    webhook_security=None,
    reconciliation_gates=None,
) -> SlackProviderService:
    existing = getattr(app.state, "slack_provider_service", None)
    if isinstance(existing, SlackProviderService):
        service = existing
        service.conversation_channels = getattr(
            app.state,
            "conversation_channel_service",
            service.conversation_channels,
        )
        service.reconciliation_gates = (
            reconciliation_gates
            or getattr(app.state, "reconciliation_gate_service", None)
            or service.reconciliation_gates
        )
    else:
        service = SlackProviderService(
            slack_client=slack_client,
            routing_service=routing_service,
            connections=connections or app.state.bot_connection_service,
            bindings=bindings or app.state.bot_binding_selection_service,
            targets=targets or app.state.bot_target_service,
            presentation=(
                presentation or app.state.bot_presentation_service
            ),
            telemetry=telemetry or app.state.bot_runtime_telemetry,
            webhook_security=(
                webhook_security
                or app.state.bot_webhook_security_service
            ),
            secret_broker=getattr(app.state, "secret_broker", None),
            conversation_channels=getattr(
                app.state,
                "conversation_channel_service",
                None,
            ),
            reconciliation_gates=(
                reconciliation_gates
                or getattr(app.state, "reconciliation_gate_service", None)
            ),
        )
        app.state.slack_provider_service = service

    host._run_slack_backfill_cycle = service.run_backfill_cycle
    host._slack_backfill_loop = service._run_loop
    host._slack_backfill_interval_seconds = service.interval_seconds
    host._slack_backfill_cooldown_remaining_seconds = (
        service.cooldown_remaining_seconds
    )
    def _compat_slack_backfill_thread_targets():
        compatibility = SlackProviderService(
            slack_client=service.slack,
            routing_service=service.routing,
            connections=SimpleNamespace(
                load_connections=host._load_bot_connections,
            ),
            bindings=SimpleNamespace(
                load_bindings=host._load_bot_bindings,
            ),
            targets=SimpleNamespace(
                load_reply_targets=host._load_bot_reply_targets,
                load_delivery_targets=host._load_bot_delivery_targets,
                load_active_turns=host._load_active_turns,
            ),
            presentation=service.presentation,
            telemetry=service.telemetry,
            webhook_security=service.webhook_security,
            secret_broker=service.secret_broker,
            conversation_channels=service.conversation_channels,
        )
        return compatibility._thread_targets()

    host._slack_backfill_thread_targets = (
        _compat_slack_backfill_thread_targets
    )
    return service
