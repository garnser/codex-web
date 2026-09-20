from __future__ import annotations

import contextlib
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any


class StaticAssetVersionService:
    """Generate stable cache-busting versions outside the legacy runtime."""

    def __init__(self, static_dir: Path, repository_root: Path) -> None:
        self.static_dir = static_dir
        self.repository_root = repository_root

    def version(self) -> str:
        mtimes = [
            path.stat().st_mtime
            for path in (
                self.static_dir / "index.html",
                self.static_dir / "app.js",
                self.static_dir / "styles.css",
            )
            if path.exists()
        ]
        mtime_version = str(int(max(mtimes) if mtimes else time.time()))
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


class RuntimeHealthService:
    """Evaluate daemon/provider health from explicit runtime collaborators."""

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
    ) -> None:
        self.codex = codex
        self.bot_runtime = bot_runtime
        self.telemetry = telemetry
        self.load_bindings = load_bindings
        self.terminal_failures = terminal_failures
        self.terminal_recovery_tasks = terminal_recovery_tasks
        self.terminal_failure_window_seconds = terminal_failure_window_seconds
        self.load_queues = load_queues
        self.slack_provider_health = slack_provider_health
        self.gitlab_sync_status = gitlab_sync_status

    def health(self) -> dict[str, Any]:
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

        bound_thread_ids = {
            binding.thread_id for binding in self.load_bindings()
        }
        failure_window = float(self.terminal_failure_window_seconds())
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

        queues = self.load_queues()
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

        recent_delivery_failures = [
            event
            for event in self.telemetry.recent(120)
            if now - float(event.get("created_at") or 0) < 300
            and isinstance(event.get("delivery"), dict)
            and event["delivery"].get("sent") is False
        ]
        if len(recent_delivery_failures) >= 3:
            problems.append(
                f"{len(recent_delivery_failures)} outbound bot deliveries "
                "failed in the last 300s"
            )

        slack_health = self.slack_provider_health()
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

        gitlab = self.gitlab_sync_status()
        failures = int(gitlab.get("consecutive_failures") or 0)
        if failures >= 2:
            problems.append(
                f"GitLab sync failed {failures} consecutive times"
            )

        return {
            "ok": not problems,
            "problems": problems,
            "codexReady": self.codex.ready.is_set(),
            "codexPid": self.codex.proc.pid if self.codex.proc else None,
            "runtimeConnections": len(self.bot_runtime.tasks),
            "runtimeStatus": list(runtime_status.values()),
            "terminalFailureThreads": len(terminal_failures),
            "terminalRecoveryThreads": len(self.terminal_recovery_tasks),
            "staleQueueThreads": stale_queues,
            "recentDeliveryFailures": len(recent_delivery_failures),
            "slackBackfillCooldownRemainingSeconds": (
                slack_backfill_cooldown
            ),
            "gitlabSyncConsecutiveFailures": failures,
            "gitlabSyncLastError": gitlab.get("last_error"),
            "gitlabSyncLastErrorAt": gitlab.get("last_error_at") or None,
            "gitlabSyncLastSuccessAt": (
                gitlab.get("last_success_at") or None
            ),
        }


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
        self.work_item_public = work_item_public
        self.recent_events = recent_events
        self.bot_routing_metrics = bot_routing_metrics or (lambda: {})
        self.bot_binding_index_status = (
            bot_binding_index_status or (lambda: {})
        )

    def snapshot(self, project_id: str | None = None) -> dict[str, Any]:
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
                key: target.model_dump()
                for key, target in self.load_reply_targets().items()
                if not project_id or target.thread_id in binding_threads
            },
            "deliveryTargets": {
                key: target.model_dump()
                for key, target in self.load_delivery_targets().items()
                if not project_id or target.thread_id in binding_threads
            },
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
