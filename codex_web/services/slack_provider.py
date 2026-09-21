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
        if self.backfill_store is not None:
            def persist_rate_limit(state):
                state.cooldown_until = self.cooldown_until
                state.rate_limit_failures = self.rate_limit_failures
                return state
            self.backfill_store.update(persist_rate_limit)
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
        recent = getattr(self.telemetry, "recent", None)
        if callable(recent):
            events = recent(max(1, limit))
        else:
            events_file = self.telemetry.events_file
            if not events_file.exists():
                return set()
            max_bytes = 4 * 1024 * 1024
            with events_file.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                end_offset = handle.tell()
                start_offset = max(0, end_offset - max_bytes)
                handle.seek(start_offset)
                raw = handle.read(end_offset - start_offset)
            lines = raw.splitlines()
            if start_offset > 0 and lines:
                lines = lines[1:]
            events = []
            for line in lines[-max(1, limit):]:
                with contextlib.suppress(Exception):
                    events.append(
                        json.loads(line.decode("utf-8", errors="replace"))
                    )
        return {
            str(event["message_id"])
            for event in events
            if isinstance(event, dict)
            and event.get("provider") == "slack"
            and event.get("message_id")
        }

    def _slack_connections(self) -> dict[str, BotConnection]:
        return {
            connection.id: connection
            for connection in self.connections.load_connections()
            if connection.provider == "slack"
            and self._credential_identity(connection, "bot_token")
        }

    def _bindings_for_projects(
        self,
        project_ids: set[str] | None,
    ) -> list[Any]:
        if project_ids is not None and callable(
            getattr(self.bindings, "for_project", None)
        ):
            values: dict[str, Any] = {}
            for project_id in sorted(project_ids):
                for binding in self.bindings.for_project(
                    "slack",
                    project_id,
                ):
                    values[binding.id] = binding
            return list(values.values())
        return [
            binding
            for binding in self.bindings.load_bindings()
            if binding.provider == "slack"
            and (
                project_ids is None
                or binding.project_id in project_ids
            )
        ]

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
        for binding in self._bindings_for_projects(
            allowed_project_ids
        ):
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
        return [
            pairs[key]
            for key in sorted(pairs)
        ]

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
        bindings = self._bindings_for_projects(
            allowed_project_ids
        )
        exact = all(
            callable(getattr(self.targets, name, None))
            for name in (
                "active_reply_target_for_binding",
                "reply_target_for_binding",
                "delivery_target_for_binding",
            )
        )
        pairs: dict[
            tuple[str, str, str],
            tuple[BotConnection, str, str],
        ] = {}
        if exact:
            for binding in bindings:
                connection = connections.get(
                    binding.connection_id or ""
                )
                if connection is None:
                    continue
                for getter in (
                    self.targets.active_reply_target_for_binding,
                    self.targets.reply_target_for_binding,
                    self.targets.delivery_target_for_binding,
                ):
                    target = getter(binding)
                    if target is None:
                        continue
                    thread_ts = (
                        target.external_thread_id
                        or target.message_id
                    )
                    if not thread_ts:
                        continue
                    key = (
                        connection.id,
                        target.external_conversation_id,
                        str(thread_ts),
                    )
                    pairs[key] = (
                        connection,
                        target.external_conversation_id,
                        str(thread_ts),
                    )
            return [pairs[key] for key in sorted(pairs)]

        # Compatibility-only fallback for older direct service consumers.
        # Production composition supplies exact keyed target lookups above.
        binding_map = {
            (
                binding.provider,
                binding.thread_id,
                binding.external_conversation_id,
            ): binding
            for binding in bindings
            if binding.connection_id
        }
        targets: list[Any] = []
        targets.extend(self.targets.load_reply_targets().values())
        targets.extend(self.targets.load_delivery_targets().values())
        targets.extend(
            active.reply_target
            for active in self.targets.load_active_turns().values()
            if active.reply_target
        )
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
            connection = (
                connections.get(binding.connection_id or "")
                if binding
                else None
            )
            if connection is None:
                continue
            key = (
                connection.id,
                target.external_conversation_id,
                str(thread_ts),
            )
            pairs[key] = (
                connection,
                target.external_conversation_id,
                str(thread_ts),
            )
        return [pairs[key] for key in sorted(pairs)]

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
                limit=self.batch_limit(),
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

    @staticmethod
    def _backfill_work_key(
        kind: str,
        connection: BotConnection,
        channel_id: str,
        thread_ts: str | None = None,
    ) -> str:
        suffix = f":{thread_ts}" if thread_ts else ""
        return (
            f"{connection.project_id}:{connection.id}:"
            f"{kind}:{channel_id}{suffix}"
        )

    def _cycle_work(
        self,
        channels: list[tuple[BotConnection, str]],
        thread_targets: list[tuple[BotConnection, str, str]],
    ) -> list[tuple[str, str, BotConnection, str, str | None]]:
        work = [
            (
                self._backfill_work_key(
                    "history",
                    connection,
                    channel_id,
                ),
                "history",
                connection,
                channel_id,
                None,
            )
            for connection, channel_id in channels
        ]
        work.extend(
            (
                self._backfill_work_key(
                    "replies",
                    connection,
                    channel_id,
                    thread_ts,
                ),
                "replies",
                connection,
                channel_id,
                thread_ts,
            )
            for connection, channel_id, thread_ts in thread_targets
        )
        return sorted(work, key=lambda item: item[0])

    def _rotate_work(
        self,
        work: list[tuple[str, str, BotConnection, str, str | None]],
        cursor: str | None,
    ) -> list[tuple[str, str, BotConnection, str, str | None]]:
        if not work or not cursor:
            return work
        split = next(
            (
                index
                for index, item in enumerate(work)
                if item[0] > cursor
            ),
            len(work),
        )
        return work[split:] + work[:split]

    def _mark_cycle_start(self, target_count: int) -> None:
        if self.backfill_store is None:
            return
        now = time.time()

        def apply(state):
            state.last_start_at = now
            state.target_count = int(target_count)
            state.current_cursor = None
            return state

        self.backfill_store.update(apply)

    def _mark_cycle_complete(
        self,
        *,
        started_at: float,
        processed: int,
        skipped: int,
        errors: int,
    ) -> None:
        if self.backfill_store is None:
            return
        now = time.time()

        def apply(state):
            state.last_completion_at = now
            state.last_duration_seconds = max(
                0.0,
                now - started_at,
            )
            state.last_processed = int(processed)
            state.last_skipped = int(skipped)
            state.last_errors = int(errors)
            state.cooldown_until = self.cooldown_until or None
            state.rate_limit_failures = self.rate_limit_failures
            return state

        self.backfill_store.update(apply)

    def _mark_coalesced_cycle(self) -> None:
        if self.backfill_store is None:
            return

        def apply(state):
            state.coalesced_cycles += 1
            return state

        self.backfill_store.update(apply)

    def _checkpoint_work(
        self,
        key: str,
        *,
        watermark: float,
        processed: int,
        skipped: int,
        errors: int,
    ) -> None:
        if self.backfill_store is None:
            return
        self.backfill_store.checkpoint(
            key,
            watermark=watermark,
            completed_at=time.time(),
            processed=processed,
            skipped=skipped,
            errors=errors,
        )

    async def run_backfill_cycle(self) -> None:
        if self.backfill_lock.locked():
            await asyncio.to_thread(self._mark_coalesced_cycle)
            self.telemetry.append(
                {
                    "type": "slack_backfill_cycle_coalesced",
                    "provider": "slack",
                }
            )
            return

        async with self.backfill_lock:
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

            allowed = (
                set(eligible)
                if self.reconciliation_gates is None
                else set(started)
            )
            if not allowed:
                return

            recent_message_ids, channels, thread_targets = (
                await asyncio.gather(
                    asyncio.to_thread(
                        self._recent_inbound_message_ids
                    ),
                    asyncio.to_thread(self._channels, allowed),
                    asyncio.to_thread(
                        self._thread_targets,
                        allowed,
                    ),
                )
            )
            work = self._cycle_work(channels, thread_targets)
            persisted = (
                await asyncio.to_thread(self.backfill_store.load)
                if self.backfill_store is not None
                else None
            )
            work = self._rotate_work(
                work,
                (
                    persisted.last_completed_cursor
                    if persisted is not None
                    else None
                ),
            )
            work = work[: self.max_provider_calls_per_cycle()]
            cycle_started = time.time()
            await asyncio.to_thread(
                self._mark_cycle_start,
                len(channels) + len(thread_targets),
            )
            processed_total = 0
            skipped_total = 0
            errors_total = 0
            base_oldest = max(
                0.0,
                cycle_started - self.window_seconds(),
            )

            try:
                for (
                    key,
                    kind,
                    connection,
                    channel_id,
                    thread_ts,
                ) in work:
                    if (
                        time.time() - cycle_started
                        >= self.max_cycle_seconds()
                    ):
                        break
                    checkpoint = (
                        persisted.checkpoints.get(key)
                        if persisted is not None
                        else None
                    )
                    oldest_value = max(
                        base_oldest,
                        checkpoint.watermark
                        if checkpoint is not None
                        else 0.0,
                    )
                    oldest = f"{oldest_value:.6f}"
                    if kind == "replies" and thread_ts is not None:
                        thread_key = (
                            connection.id,
                            channel_id,
                            thread_ts,
                        )
                        if thread_key in self.bad_threads:
                            skipped_total += 1
                            await asyncio.to_thread(
                                self._checkpoint_work,
                                key,
                                watermark=oldest_value,
                                processed=0,
                                skipped=1,
                                errors=0,
                            )
                            continue
                        response = await self._replies(
                            connection,
                            channel_id,
                            thread_ts,
                            oldest,
                        )
                        failure_type = "slack_thread_backfill_failed"
                    else:
                        response = await self._history(
                            connection,
                            channel_id,
                            oldest,
                        )
                        failure_type = "slack_backfill_failed"

                    if not response.get("ok"):
                        errors_total += 1
                        if (
                            kind == "replies"
                            and thread_ts is not None
                            and response.get("error")
                            in {
                                "thread_not_found",
                                "channel_not_found",
                                "not_in_channel",
                            }
                        ):
                            self.bad_threads.add(
                                (
                                    connection.id,
                                    channel_id,
                                    thread_ts,
                                )
                            )
                        context = {
                            "connection_id": connection.id,
                            "external_conversation_id": channel_id,
                        }
                        if thread_ts is not None:
                            context["external_thread_id"] = thread_ts
                        rate_limited = self._record_failure(
                            failure_type,
                            response,
                            context,
                        )
                        await asyncio.to_thread(
                            self._checkpoint_work,
                            key,
                            watermark=oldest_value,
                            processed=0,
                            skipped=0,
                            errors=1,
                        )
                        if rate_limited:
                            break
                        continue

                    self.rate_limit_failures = 0
                    messages = response.get("messages") or []
                    processed = 0
                    skipped = 0
                    watermark = oldest_value
                    for event in reversed(messages):
                        if not isinstance(event, dict):
                            skipped += 1
                            continue
                        message_id = str(
                            event.get("ts") or ""
                        ).strip()
                        with contextlib.suppress(ValueError):
                            watermark = max(
                                watermark,
                                float(message_id),
                            )
                        if (
                            kind == "replies"
                            and thread_ts is not None
                            and message_id == thread_ts
                        ):
                            skipped += 1
                            continue
                        dispatched = (
                            await self._dispatch_backfill_message(
                                connection,
                                channel_id,
                                event,
                                recent_message_ids=recent_message_ids,
                                fallback_thread_ts=(
                                    thread_ts
                                    if kind == "replies"
                                    else None
                                ),
                            )
                        )
                        if dispatched:
                            processed += 1
                        else:
                            skipped += 1
                    processed_total += processed
                    skipped_total += skipped
                    await asyncio.to_thread(
                        self._checkpoint_work,
                        key,
                        watermark=watermark,
                        processed=processed,
                        skipped=skipped,
                        errors=0,
                    )
            finally:
                await asyncio.to_thread(
                    self._mark_cycle_complete,
                    started_at=cycle_started,
                    processed=processed_total,
                    skipped=skipped_total,
                    errors=errors_total,
                )
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
    ) -> bool:
        message_id = str(event.get("ts") or "").strip()
        if (
            not message_id
            or message_id in self.seen
            or message_id in recent_message_ids
        ):
            return False
        if event.get("bot_id") or event.get("subtype") in {
            "bot_message",
            "message_deleted",
        }:
            self.seen.add(message_id)
            return False
        text = self.presentation.strip_slack_mentions(
            event.get("text") or ""
        )
        if not text:
            self.seen.add(message_id)
            return False
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
        return True

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
