from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any


class StaticAssetVersionService:
    """Generate a stable cache-busting version once, outside hot paths."""

    def __init__(self, static_dir: Path, repository_root: Path) -> None:
        self.static_dir = static_dir
        self.repository_root = repository_root
        self._lock = threading.Lock()
        self._version = self._compute_version()

    def _compute_version(self) -> str:
        mtimes = [
            path.stat().st_mtime
            for path in (
                self.static_dir / "index.html",
                self.static_dir / "app.js",
                self.static_dir / "styles.css",
            )
            if path.exists()
        ]
        mtime_version = str(
            int(max(mtimes) if mtimes else time.time())
        )
        with contextlib.suppress(Exception):
            commit = subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=self.repository_root,
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
            if commit:
                return f"{commit}-{mtime_version}"
        return mtime_version

    def refresh(self) -> str:
        value = self._compute_version()
        with self._lock:
            self._version = value
        return value

    def version(self) -> str:
        with self._lock:
            return self._version


class RuntimeHealthService:
    """Cache expensive runtime health evaluation away from request hot paths."""

    def __init__(
        self,
        *,
        codex: Any,
        bot_runtime: Any,
        telemetry: Any,
        load_bindings: Callable[[], list[Any]],
        terminal_failures: dict[str, Any],
        terminal_recovery_tasks: dict[str, Any],
        terminal_failure_window_seconds: Callable[[], float],
        load_queues: Callable[[], dict[str, list[Any]]],
        slack_provider_health: Callable[[], dict[str, Any]],
        gitlab_sync_status: Callable[[], dict[str, Any]],
        execution_readiness: Callable[[], dict[str, Any]] | None = None,
        count_active_turns: Callable[[], int] | None = None,
        state_store_status: Callable[[], dict[str, Any]] | None = None,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.codex = codex
        self.bot_runtime = bot_runtime
        self.telemetry = telemetry
        self.load_bindings = load_bindings
        self.terminal_failures = terminal_failures
        self.terminal_recovery_tasks = terminal_recovery_tasks
        self.terminal_failure_window_seconds = (
            terminal_failure_window_seconds
        )
        self.load_queues = load_queues
        self.slack_provider_health = slack_provider_health
        self.gitlab_sync_status = gitlab_sync_status
        self.execution_readiness = execution_readiness or (lambda: {})
        self.count_active_turns = count_active_turns or (lambda: 0)
        self.state_store_status = state_store_status or (lambda: {})
        self.event_sink = event_sink

        self._snapshot_lock = threading.Lock()
        self._metrics_lock = threading.Lock()
        self._async_refresh_lock = asyncio.Lock()
        self._snapshot: dict[str, Any] | None = None
        self._generated_at: float | None = None
        self._last_refresh_started_at: float | None = None
        self._last_refresh_completed_at: float | None = None
        self._last_refresh_duration = 0.0
        self._last_refresh_error_class: str | None = None
        self._refresh_count = 0
        self._refresh_failures = 0
        self._loop_lag_seconds = 0.0
        self._max_loop_lag_seconds = 0.0
        self._operation_metrics: dict[str, dict[str, Any]] = {}

    @staticmethod
    def refresh_interval_seconds() -> float:
        try:
            value = float(
                os.environ.get(
                    "CODEX_WEB_RUNTIME_HEALTH_REFRESH_SECONDS"
                )
                or "5"
            )
        except ValueError:
            value = 5.0
        return max(1.0, min(value, 60.0))

    @staticmethod
    def cache_max_age_seconds() -> float:
        try:
            value = float(
                os.environ.get(
                    "CODEX_WEB_RUNTIME_HEALTH_CACHE_MAX_AGE_SECONDS"
                )
                or "20"
            )
        except ValueError:
            value = 20.0
        return max(2.0, min(value, 300.0))

    @staticmethod
    def slow_operation_seconds() -> float:
        try:
            value = float(
                os.environ.get(
                    "CODEX_WEB_RUNTIME_SLOW_OPERATION_SECONDS"
                )
                or "0.25"
            )
        except ValueError:
            value = 0.25
        return max(0.01, min(value, 30.0))

    @staticmethod
    def event_loop_lag_warning_seconds() -> float:
        try:
            value = float(
                os.environ.get(
                    "CODEX_WEB_EVENT_LOOP_LAG_WARNING_SECONDS"
                )
                or "0.25"
            )
        except ValueError:
            value = 0.25
        return max(0.01, min(value, 10.0))

    @staticmethod
    def _record_count(value: Any) -> int | None:
        if isinstance(value, dict):
            return len(value)
        if isinstance(value, (list, tuple, set)):
            return len(value)
        return None

    def _record_operation(
        self,
        name: str,
        *,
        elapsed: float,
        records: int | None,
        bytes_read: int | None,
        refresh_id: str,
        error_class: str | None = None,
    ) -> None:
        with self._metrics_lock:
            current = dict(self._operation_metrics.get(name, {}))
            current["calls"] = int(current.get("calls", 0)) + 1
            current["lastSeconds"] = elapsed
            current["maxSeconds"] = max(
                float(current.get("maxSeconds", 0.0)),
                elapsed,
            )
            current["lastRecords"] = records
            current["lastBytes"] = bytes_read
            current["lastRefreshId"] = refresh_id
            current["lastErrorClass"] = error_class
            if elapsed >= self.slow_operation_seconds():
                current["slowCalls"] = (
                    int(current.get("slowCalls", 0)) + 1
                )
            self._operation_metrics[name] = current

        if (
            elapsed >= self.slow_operation_seconds()
            and self.event_sink is not None
        ):
            self.event_sink(
                {
                    "type": "runtime_slow_operation",
                    "operation": name,
                    "duration_seconds": elapsed,
                    "records": records,
                    "bytes_read": bytes_read,
                    "refresh_id": refresh_id,
                    "error_class": error_class,
                }
            )

    def _profile(
        self,
        name: str,
        callback: Callable[[], Any],
        *,
        refresh_id: str,
        record_counter: Callable[[Any], int | None] | None = None,
        bytes_counter: Callable[[Any], int | None] | None = None,
    ) -> Any:
        started = time.perf_counter()
        error_class: str | None = None
        value: Any = None
        try:
            value = callback()
            return value
        except Exception as exc:
            error_class = type(exc).__name__
            raise
        finally:
            elapsed = max(0.0, time.perf_counter() - started)
            records = (
                record_counter(value)
                if record_counter is not None
                else self._record_count(value)
            )
            bytes_read = (
                bytes_counter(value)
                if bytes_counter is not None
                else None
            )
            self._record_operation(
                name,
                elapsed=elapsed,
                records=records,
                bytes_read=bytes_read,
                refresh_id=refresh_id,
                error_class=error_class,
            )

    def _evaluate(self, refresh_id: str) -> dict[str, Any]:
        now = time.time()
        problems: list[str] = []
        configured_runtime_ids = set(self.bot_runtime.fingerprints)
        running_runtime_ids = set(self.bot_runtime.tasks)

        if not self.codex.proc or self.codex.proc.poll() is not None:
            problems.append("codex app-server process is not running")
        elif not self.codex.ready.is_set():
            problems.append("codex app-server is not ready")

        missing_runtimes = configured_runtime_ids - running_runtime_ids
        if missing_runtimes:
            problems.append(
                "bot runtime task missing for: "
                + ", ".join(sorted(missing_runtimes))
            )

        runtime_status = self.telemetry.snapshot()
        for connection_id, task in self.bot_runtime.tasks.items():
            if task.done():
                problems.append(
                    f"bot runtime task stopped for: {connection_id}"
                )
                continue
            status = runtime_status.get(connection_id, {})
            if status.get("status") == "error":
                error_at = float(
                    status.get("lastErrorAt")
                    or status.get("updatedAt")
                    or now
                )
                if now - error_at > 120:
                    problems.append(
                        "bot runtime has been in error for "
                        f"{int(now - error_at)}s: {connection_id}"
                    )

        bindings = self._profile(
            "bindings.load",
            self.load_bindings,
            refresh_id=refresh_id,
        )
        bound_thread_ids = {
            binding.thread_id for binding in bindings
        }
        failure_window = float(
            self.terminal_failure_window_seconds()
        )
        terminal_failures = {
            thread_id: list(failures)
            for thread_id, failures in self.terminal_failures.items()
            if thread_id in bound_thread_ids
            and failures
            and now - failures[-1][0] < failure_window
        }
        if terminal_failures:
            problems.append(
                "terminal turn failures unresolved for "
                f"{len(terminal_failures)} bound thread(s)"
            )

        queues = self._profile(
            "turn_queues.load",
            self.load_queues,
            refresh_id=refresh_id,
            record_counter=lambda values: sum(
                len(items) for items in values.values()
            ),
        )
        queued_turn_count = sum(
            len(items) for items in queues.values()
        )
        stale_queues = {
            thread_id: len(items)
            for thread_id, items in queues.items()
            if items
            and now - min(item.created_at for item in items) > 900
        }
        if stale_queues:
            problems.append(
                "queued turns have waited over 900s for "
                f"{len(stale_queues)} thread(s)"
            )

        recent_events = self._profile(
            "telemetry.recent",
            lambda: self.telemetry.recent(120),
            refresh_id=refresh_id,
            bytes_counter=lambda _value: int(
                self.telemetry.recent_metrics().get(
                    "bytesRead",
                    0,
                )
            )
            if callable(
                getattr(self.telemetry, "recent_metrics", None)
            )
            else None,
        )
        recent_delivery_failures = [
            event
            for event in recent_events
            if now - float(event.get("created_at") or 0) < 300
            and isinstance(event.get("delivery"), dict)
            and event["delivery"].get("sent") is False
        ]
        if len(recent_delivery_failures) >= 3:
            problems.append(
                f"{len(recent_delivery_failures)} outbound bot deliveries "
                "failed in the last 300s"
            )

        slack_health = self._profile(
            "slack.health",
            self.slack_provider_health,
            refresh_id=refresh_id,
        )
        slack_backfill_cooldown = float(
            slack_health.get("cooldownRemainingSeconds") or 0.0
        )
        if (
            int(slack_health.get("rateLimitFailures") or 0) >= 2
            and slack_backfill_cooldown > 0
        ):
            problems.append(
                "Slack backfill rate limited for another "
                f"{int(slack_backfill_cooldown)}s"
            )

        execution_readiness = self._profile(
            "execution.readiness",
            self.execution_readiness,
            refresh_id=refresh_id,
        )
        if (
            execution_readiness
            and not execution_readiness.get("ready", False)
        ):
            problems.append(
                str(
                    execution_readiness.get("reason")
                    or "execution worker is not ready"
                )
            )

        gitlab = self._profile(
            "gitlab.health",
            self.gitlab_sync_status,
            refresh_id=refresh_id,
        )
        failures = int(gitlab.get("consecutive_failures") or 0)
        if failures >= 2:
            problems.append(
                f"GitLab sync failed {failures} consecutive times"
            )

        active_turn_count = int(
            self._profile(
                "active_turns.count",
                self.count_active_turns,
                refresh_id=refresh_id,
            )
        )
        state_status = self._profile(
            "state_store.status",
            self.state_store_status,
            refresh_id=refresh_id,
        )

        return {
            "ok": not problems,
            "problems": problems,
            "codexReady": self.codex.ready.is_set(),
            "codexPid": (
                self.codex.proc.pid if self.codex.proc else None
            ),
            "runtimeConnections": len(self.bot_runtime.tasks),
            "runtimeStatus": list(runtime_status.values()),
            "terminalFailureThreads": len(terminal_failures),
            "terminalRecoveryThreads": len(
                self.terminal_recovery_tasks
            ),
            "staleQueueThreads": stale_queues,
            "recentDeliveryFailures": len(
                recent_delivery_failures
            ),
            "slackBackfillCooldownRemainingSeconds": (
                slack_backfill_cooldown
            ),
            "gitlabSyncConsecutiveFailures": failures,
            "gitlabSyncLastError": gitlab.get("last_error"),
            "gitlabSyncLastErrorAt": (
                gitlab.get("last_error_at") or None
            ),
            "gitlabSyncLastSuccessAt": (
                gitlab.get("last_success_at") or None
            ),
            "executionReadiness": execution_readiness,
            "activeTurns": active_turn_count,
            "queuedTurns": queued_turn_count,
            "queueThreads": sum(
                1 for items in queues.values() if items
            ),
            "bindingCount": len(bindings),
            "stateStore": state_status,
            "refreshId": refresh_id,
        }

    def _commit_snapshot(
        self,
        snapshot: dict[str, Any],
        *,
        started_at: float,
        completed_at: float,
    ) -> None:
        with self._snapshot_lock:
            self._snapshot = snapshot
            self._generated_at = completed_at
            self._last_refresh_started_at = started_at
            self._last_refresh_completed_at = completed_at
            self._last_refresh_duration = max(
                0.0,
                completed_at - started_at,
            )
            self._last_refresh_error_class = None
            self._refresh_count += 1

    def refresh_sync(self) -> dict[str, Any]:
        started_at = time.time()
        refresh_id = f"health-{uuid.uuid4().hex}"
        try:
            snapshot = self._evaluate(refresh_id)
        except Exception as exc:
            completed_at = time.time()
            with self._snapshot_lock:
                self._last_refresh_started_at = started_at
                self._last_refresh_completed_at = completed_at
                self._last_refresh_duration = max(
                    0.0,
                    completed_at - started_at,
                )
                self._last_refresh_error_class = type(exc).__name__
                self._refresh_failures += 1
            return self.health()
        completed_at = time.time()
        self._commit_snapshot(
            snapshot,
            started_at=started_at,
            completed_at=completed_at,
        )
        return self.health()

    async def refresh(self) -> dict[str, Any]:
        async with self._async_refresh_lock:
            return await asyncio.to_thread(self.refresh_sync)

    def health(self) -> dict[str, Any]:
        now = time.time()
        with self._snapshot_lock:
            snapshot = (
                dict(self._snapshot)
                if self._snapshot is not None
                else None
            )
            generated_at = self._generated_at
            last_started = self._last_refresh_started_at
            last_completed = self._last_refresh_completed_at
            duration = self._last_refresh_duration
            error_class = self._last_refresh_error_class
            refresh_count = self._refresh_count
            refresh_failures = self._refresh_failures

        age = (
            max(0.0, now - generated_at)
            if generated_at is not None
            else None
        )
        stale = bool(
            generated_at is None
            or age is None
            or age > self.cache_max_age_seconds()
        )
        if snapshot is None:
            snapshot = {
                "ok": False,
                "problems": [
                    "runtime health snapshot has not been generated"
                ],
                "codexReady": self.codex.ready.is_set(),
                "codexPid": (
                    self.codex.proc.pid if self.codex.proc else None
                ),
                "runtimeConnections": len(
                    self.bot_runtime.tasks
                ),
                "runtimeStatus": list(
                    self.telemetry.snapshot().values()
                ),
                "terminalFailureThreads": 0,
                "terminalRecoveryThreads": len(
                    self.terminal_recovery_tasks
                ),
                "staleQueueThreads": {},
                "recentDeliveryFailures": 0,
                "slackBackfillCooldownRemainingSeconds": 0.0,
                "gitlabSyncConsecutiveFailures": 0,
                "gitlabSyncLastError": None,
                "gitlabSyncLastErrorAt": None,
                "gitlabSyncLastSuccessAt": None,
                "executionReadiness": {},
                "activeTurns": 0,
                "queuedTurns": 0,
                "queueThreads": 0,
                "bindingCount": 0,
                "stateStore": {},
                "refreshId": None,
            }
        evaluated_ok = bool(snapshot.get("ok"))
        problems = list(snapshot.get("problems") or [])
        if stale and "runtime health snapshot is stale" not in problems:
            problems.append("runtime health snapshot is stale")
        snapshot["evaluatedOk"] = evaluated_ok
        snapshot["ok"] = evaluated_ok and not stale
        snapshot["problems"] = problems
        snapshot["healthCache"] = {
            "generatedAt": generated_at,
            "ageSeconds": age,
            "maxAgeSeconds": self.cache_max_age_seconds(),
            "stale": stale,
            "refreshInProgress": self._async_refresh_lock.locked(),
            "lastRefreshStartedAt": last_started,
            "lastRefreshCompletedAt": last_completed,
            "lastRefreshDurationSeconds": duration,
            "lastRefreshErrorClass": error_class,
            "refreshCount": refresh_count,
            "refreshFailures": refresh_failures,
        }
        snapshot["eventLoopLagSeconds"] = self._loop_lag_seconds
        snapshot["maxEventLoopLagSeconds"] = (
            self._max_loop_lag_seconds
        )
        return snapshot

    def metrics(self) -> dict[str, Any]:
        with self._metrics_lock:
            operations = {
                key: dict(value)
                for key, value in self._operation_metrics.items()
            }
        return {
            "operations": operations,
            "eventLoopLagSeconds": self._loop_lag_seconds,
            "maxEventLoopLagSeconds": (
                self._max_loop_lag_seconds
            ),
            "refreshIntervalSeconds": (
                self.refresh_interval_seconds()
            ),
            "cacheMaxAgeSeconds": self.cache_max_age_seconds(),
        }

    async def run_forever(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            await self.refresh()
            interval = self.refresh_interval_seconds()
            target = loop.time() + interval
            await asyncio.sleep(interval)
            lag = max(0.0, loop.time() - target)
            self._loop_lag_seconds = lag
            self._max_loop_lag_seconds = max(
                self._max_loop_lag_seconds,
                lag,
            )
            if (
                lag >= self.event_loop_lag_warning_seconds()
                and self.event_sink is not None
            ):
                await asyncio.to_thread(
                    self.event_sink,
                    {
                        "type": "runtime_event_loop_lag",
                        "lag_seconds": lag,
                        "threshold_seconds": (
                            self.event_loop_lag_warning_seconds()
                        ),
                    },
                )


class RuntimeDiagnosticsService:
    """Build the privileged diagnostics snapshot without exposing secrets."""

    def __init__(
        self,
        *,
        version: Callable[[], str],
        health: Callable[[], dict[str, Any]],
        codex: Any,
        bot_runtime: Any,
        telemetry: Any,
        runtime_policy: Any,
        supervisor: Any,
        thread_message_limit: Callable[[], int],
        slack_provider_health: Callable[[], dict[str, Any]],
        project_lookup: Callable[[str], Any],
        load_projects: Callable[[], list[Any]],
        load_thread_index: Callable[[], list[Any]],
        load_active_turns: Callable[[], dict[str, Any]],
        load_queues: Callable[[], dict[str, list[Any]]],
        queued_turn_public: Callable[[Any], dict[str, Any]],
        queue_tasks: dict[str, Any],
        load_connections: Callable[[], list[Any]],
        connection_public: Callable[[Any], dict[str, Any]],
        load_bindings: Callable[[], list[Any]],
        binding_public: Callable[[Any], dict[str, Any]],
        load_agent_presence: Callable[[], Any],
        agent_presence_public: Callable[[Any], dict[str, Any]],
        load_reply_targets: Callable[[], dict[str, Any]],
        load_delivery_targets: Callable[[], dict[str, Any]],
        load_work_item_states: Callable[[], dict[str, Any]],
        count_reply_targets: Callable[[], int] | None = None,
        count_delivery_targets: Callable[[], int] | None = None,
        page_reply_targets_raw: Callable[..., tuple[dict[str, Any], str | None]] | None = None,
        page_delivery_targets_raw: Callable[..., tuple[dict[str, Any], str | None]] | None = None,
        work_item_public: Callable[[Any], dict[str, Any]],
        recent_events: Callable[[int], list[dict[str, Any]]],
        bot_routing_metrics: Callable[[], dict[str, Any]] | None = None,
        bot_binding_index_status: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.version = version
        self.health = health
        self.codex = codex
        self.bot_runtime = bot_runtime
        self.telemetry = telemetry
        self.runtime_policy = runtime_policy
        self.supervisor = supervisor
        self.thread_message_limit = thread_message_limit
        self.slack_provider_health = slack_provider_health
        self.project_lookup = project_lookup
        self.load_projects = load_projects
        self.load_thread_index = load_thread_index
        self.load_active_turns = load_active_turns
        self.load_queues = load_queues
        self.queued_turn_public = queued_turn_public
        self.queue_tasks = queue_tasks
        self.load_connections = load_connections
        self.connection_public = connection_public
        self.load_bindings = load_bindings
        self.binding_public = binding_public
        self.load_agent_presence = load_agent_presence
        self.agent_presence_public = agent_presence_public
        self.load_reply_targets = load_reply_targets
        self.load_delivery_targets = load_delivery_targets
        self.load_work_item_states = load_work_item_states
        self.count_reply_targets = count_reply_targets
        self.count_delivery_targets = count_delivery_targets
        self.page_reply_targets_raw = page_reply_targets_raw
        self.page_delivery_targets_raw = page_delivery_targets_raw
        self._diagnostics_metrics_lock = threading.Lock()
        self._diagnostics_metrics = {
            "snapshots": 0,
            "targetPages": 0,
            "targetRecordsScanned": 0,
            "targetRecordsSerialized": 0,
            "truncatedSnapshots": 0,
            "lastSnapshotSeconds": 0.0,
            "maxSnapshotSeconds": 0.0,
        }
        self.work_item_public = work_item_public
        self.recent_events = recent_events
        self.bot_routing_metrics = bot_routing_metrics or (lambda: {})
        self.bot_binding_index_status = (
            bot_binding_index_status or (lambda: {})
        )

    TARGET_SAMPLE_LIMIT = 25
    TARGET_PAGE_LIMIT = 100
    TARGET_SCAN_BUDGET = 2000

    def _metric(self, name: str, amount: int | float = 1) -> None:
        with self._diagnostics_metrics_lock:
            if name in {"lastSnapshotSeconds", "maxSnapshotSeconds"}:
                self._diagnostics_metrics[name] = float(amount)
            else:
                self._diagnostics_metrics[name] = (
                    int(self._diagnostics_metrics.get(name, 0))
                    + int(amount)
                )

    def metrics(self) -> dict[str, Any]:
        with self._diagnostics_metrics_lock:
            return dict(self._diagnostics_metrics)

    @staticmethod
    def _target_public(key: str, raw: Any) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        # BotReplyTarget carries routing coordinates only. Keep diagnostics
        # output compact and explicitly omit unknown/provider-specific fields.
        return {
            "key": key,
            "thread_id": raw.get("thread_id"),
            "provider": raw.get("provider"),
            "external_conversation_id": raw.get(
                "external_conversation_id"
            ),
            "external_thread_id": raw.get("external_thread_id"),
            "message_id": raw.get("message_id"),
            "updated_at": raw.get("updated_at"),
        }

    def _target_page(
        self,
        kind: str,
        *,
        project_id: str | None = None,
        after: str | None = None,
        limit: int = TARGET_SAMPLE_LIMIT,
        scan_budget: int = TARGET_SCAN_BUDGET,
        binding_threads: set[str] | None = None,
    ) -> dict[str, Any]:
        normalized = str(kind).strip().casefold()
        if normalized not in {"reply", "delivery"}:
            raise ValueError("target kind must be 'reply' or 'delivery'")
        page_raw = (
            self.page_reply_targets_raw
            if normalized == "reply"
            else self.page_delivery_targets_raw
        )
        count = (
            self.count_reply_targets
            if normalized == "reply"
            else self.count_delivery_targets
        )
        fallback = (
            self.load_reply_targets
            if normalized == "reply"
            else self.load_delivery_targets
        )
        page_size = max(1, min(int(limit), self.TARGET_PAGE_LIMIT))
        budget = max(page_size, min(int(scan_budget), 10_000))
        allowed_threads = binding_threads
        if project_id is not None and allowed_threads is None:
            allowed_threads = set()

        if page_raw is None:
            values = fallback()
            rows = []
            for key in sorted(values):
                target = values[key]
                raw = (
                    target.model_dump(mode="json")
                    if hasattr(target, "model_dump")
                    else target
                )
                if (
                    allowed_threads is not None
                    and isinstance(raw, dict)
                    and raw.get("thread_id") not in allowed_threads
                ):
                    continue
                public = self._target_public(key, raw)
                if public is not None:
                    rows.append(public)
                if len(rows) >= page_size:
                    break
            return {
                "kind": normalized,
                "items": rows,
                "nextCursor": None,
                "hasMore": len(values) > len(rows),
                "truncated": len(values) > len(rows),
                "globalTotalCount": len(values),
                "scopeCountExact": project_id is None,
                "scanned": len(values),
                "serialized": len(rows),
                "compatibilityFallback": True,
            }

        selected: list[dict[str, Any]] = []
        cursor = after
        scanned = 0
        has_more = False
        while scanned < budget and len(selected) < page_size:
            request_size = min(250, budget - scanned)
            raw_page, backend_cursor = page_raw(
                after=cursor,
                limit=request_size,
            )
            if not raw_page:
                cursor = None
                break
            batch_items = list(raw_page.items())
            processed_in_batch = 0
            for key, raw in batch_items:
                processed_in_batch += 1
                cursor = key
                scanned += 1
                if (
                    allowed_threads is not None
                    and (
                        not isinstance(raw, dict)
                        or raw.get("thread_id") not in allowed_threads
                    )
                ):
                    if scanned >= budget:
                        break
                    continue
                public = self._target_public(key, raw)
                if public is not None:
                    selected.append(public)
                if len(selected) >= page_size or scanned >= budget:
                    break
            has_more = bool(
                backend_cursor
                or processed_in_batch < len(batch_items)
            )
            if len(selected) >= page_size or scanned >= budget:
                break
            if not backend_cursor:
                cursor = None
                has_more = False
                break
            cursor = backend_cursor

        global_count = int(count()) if count is not None else None
        truncated = bool(has_more or (cursor is not None and scanned >= budget))
        self._metric("targetPages")
        self._metric("targetRecordsScanned", scanned)
        self._metric("targetRecordsSerialized", len(selected))
        return {
            "kind": normalized,
            "items": selected,
            "nextCursor": cursor if truncated else None,
            "hasMore": truncated,
            "truncated": truncated,
            "globalTotalCount": global_count,
            "scopeCountExact": project_id is None,
            "scanned": scanned,
            "serialized": len(selected),
            "scanBudget": budget,
            "compatibilityFallback": False,
        }

    def target_page(
        self,
        kind: str,
        *,
        project_id: str | None = None,
        after: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        binding_threads: set[str] | None = None
        if project_id:
            self.project_lookup(project_id)
            binding_threads = {
                binding.thread_id
                for binding in self.load_bindings()
                if binding.project_id == project_id
            }
        return self._target_page(
            kind,
            project_id=project_id,
            after=after,
            limit=limit,
            binding_threads=binding_threads,
        )

    def snapshot(self, project_id: str | None = None) -> dict[str, Any]:
        started = time.perf_counter()
        bindings = self.load_bindings()
        if project_id:
            self.project_lookup(project_id)
            bindings = [
                binding
                for binding in bindings
                if binding.project_id == project_id
            ]
        queues = self.load_queues()
        active_turns = self.load_active_turns()
        slack_health = self.slack_provider_health()
        task_status = self.supervisor.task_status()
        binding_threads = {binding.thread_id for binding in bindings}

        def running(name: str) -> bool:
            return bool(task_status.get(name, {}).get("running"))

        reply_page = self._target_page(
            "reply",
            project_id=project_id,
            limit=self.TARGET_SAMPLE_LIMIT,
            binding_threads=binding_threads if project_id else None,
        )
        delivery_page = self._target_page(
            "delivery",
            project_id=project_id,
            limit=self.TARGET_SAMPLE_LIMIT,
            binding_threads=binding_threads if project_id else None,
        )
        truncated = bool(
            reply_page["truncated"] or delivery_page["truncated"]
        )
        elapsed = time.perf_counter() - started
        self._metric("snapshots")
        if truncated:
            self._metric("truncatedSnapshots")
        self._metric("lastSnapshotSeconds", elapsed)
        with self._diagnostics_metrics_lock:
            self._diagnostics_metrics["maxSnapshotSeconds"] = max(
                float(
                    self._diagnostics_metrics.get(
                        "maxSnapshotSeconds",
                        0.0,
                    )
                ),
                elapsed,
            )

        return {
            "generatedAt": time.time(),
            "version": self.version(),
            "status": {
                "ok": self.codex.ready.is_set(),
                "pid": (
                    self.codex.proc.pid if self.codex.proc else None
                ),
                "error": (
                    None
                    if self.codex.ready.is_set()
                    else self.codex.last_error
                ),
                "pendingApprovals": len(self.codex.pending_approvals),
                "activeTurns": len(active_turns),
                "queuedTurns": sum(
                    len(items) for items in queues.values()
                ),
                "ownerWorkWatchdogIntervalSeconds": (
                    self.runtime_policy.owner_work_watchdog_interval()
                ),
                "ownerWorkWatchdogRunning": running("owner-work"),
                "releaseGateWatchdogIntervalSeconds": (
                    self.runtime_policy.release_gate_watchdog_interval()
                ),
                "releaseGateWatchdogRunning": running("release-gate"),
                "workItemSlaWatchdogIntervalSeconds": (
                    self.runtime_policy.work_item_sla_watchdog_interval()
                ),
                "workItemSlaWatchdogRunning": running("work-item-sla"),
                "orchestratorWatchdogIntervalSeconds": (
                    self.runtime_policy.orchestrator_watchdog_interval()
                ),
                "orchestratorWatchdogRunning": running("orchestrator"),
                "splitBrainWatchdogIntervalSeconds": (
                    self.runtime_policy.split_brain_watchdog_interval()
                ),
                "splitBrainWatchdogRunning": running("split-brain"),
                "threadMessageLimit": self.thread_message_limit(),
                "slackBackfillIntervalSeconds": float(
                    slack_health.get("intervalSeconds") or 0.0
                ),
                "slackBackfillRunning": bool(
                    slack_health.get("running")
                ),
                "slackBackfillCooldownRemainingSeconds": float(
                    slack_health.get("cooldownRemainingSeconds") or 0.0
                ),
                "slackBackfillCooldownUntil": slack_health.get(
                    "cooldownUntil"
                ),
            },
            "health": self.health(),
            "projects": [
                project.model_dump() for project in self.load_projects()
            ],
            "threadIndex": [
                thread.model_dump() for thread in self.load_thread_index()
            ],
            "activeTurns": [
                active.model_dump() for active in active_turns.values()
            ],
            "queues": {
                thread_id: [
                    self.queued_turn_public(queued) for queued in items
                ]
                for thread_id, items in queues.items()
                if not project_id
                or any(
                    queued.project_id == project_id for queued in items
                )
            },
            "queueTasks": {
                thread_id: {
                    "done": task.done(),
                    "cancelled": task.cancelled(),
                }
                for thread_id, task in self.queue_tasks.items()
            },
            "connections": [
                {
                    **self.connection_public(connection),
                    "runtime": self.telemetry.snapshot().get(connection.id),
                    "runtimeTaskRunning": (
                        connection.id in self.bot_runtime.tasks
                        and not self.bot_runtime.tasks[
                            connection.id
                        ].done()
                    ),
                }
                for connection in self.load_connections()
                if not project_id
                or connection.project_id == project_id
            ],
            "bindings": [
                self.binding_public(binding) for binding in bindings
            ],
            "agentChannelPresence": self.agent_presence_public(
                self.load_agent_presence()
            ),
            "replyTargets": {
                item["key"]: {
                    key: value
                    for key, value in item.items()
                    if key != "key"
                }
                for item in reply_page["items"]
            },
            "deliveryTargets": {
                item["key"]: {
                    key: value
                    for key, value in item.items()
                    if key != "key"
                }
                for item in delivery_page["items"]
            },
            "targetMetadata": {
                "sampleLimit": self.TARGET_SAMPLE_LIMIT,
                "reply": {
                    key: value
                    for key, value in reply_page.items()
                    if key != "items"
                },
                "delivery": {
                    key: value
                    for key, value in delivery_page.items()
                    if key != "items"
                },
            },
            "diagnosticsMetrics": self.metrics(),
            "workItemStates": [
                self.work_item_public(state)
                for state in sorted(
                    self.load_work_item_states().values(),
                    key=lambda item: item.updated_at,
                    reverse=True,
                )[:200]
                if not project_id or state.project_id == project_id
            ],
            "botRouting": {
                "targets": self.bot_routing_metrics(),
                "bindings": self.bot_binding_index_status(),
            },
            "recentBotEvents": self.recent_events(80),
        }
