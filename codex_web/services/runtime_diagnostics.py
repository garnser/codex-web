from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

from codex_web.models import BotBinding, QueuedTurn, WorkItemState
from codex_web.services.bot_binding_selection import BotBindingSelectionService
from codex_web.services.bot_connections import BotConnectionService
from codex_web.services.bot_presentation import BotPresentationService
from codex_web.services.bot_runtime_telemetry import BotRuntimeTelemetry
from codex_web.services.static_assets import StaticAssetVersionService


class RuntimeHealthService:
    """Build runtime health from explicit runtime/state owners."""

    def __init__(
        self,
        *,
        codex: Any,
        bot_runtime: Any,
        telemetry: BotRuntimeTelemetry,
        load_bindings: Callable[[], list[BotBinding]],
        load_queues: Callable[[], dict[str, list[QueuedTurn]]],
        terminal_failures: Mapping[str, Any],
        terminal_recovery_tasks: Mapping[str, Any],
        terminal_failure_window_seconds: Callable[[], float],
        slack_health: Callable[[], dict[str, Any]],
        gitlab_sync_health: Callable[[], dict[str, Any]],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.codex = codex
        self.bot_runtime = bot_runtime
        self.telemetry = telemetry
        self.load_bindings = load_bindings
        self.load_queues = load_queues
        self.terminal_failures = terminal_failures
        self.terminal_recovery_tasks = terminal_recovery_tasks
        self.terminal_failure_window_seconds = terminal_failure_window_seconds
        self.slack_health = slack_health
        self.gitlab_sync_health = gitlab_sync_health
        self.clock = clock

    def snapshot(self) -> dict[str, Any]:
        now = self.clock()
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
        terminal_failures = {
            thread_id: list(failures)
            for thread_id, failures in self.terminal_failures.items()
            if thread_id in bound_thread_ids
            and failures
            and now - failures[-1][0]
            < self.terminal_failure_window_seconds()
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
            for event in self.telemetry.recent_events(120)
            if now - float(event.get("created_at") or 0) < 300
            and isinstance(event.get("delivery"), dict)
            and event["delivery"].get("sent") is False
        ]
        if len(recent_delivery_failures) >= 3:
            problems.append(
                f"{len(recent_delivery_failures)} outbound bot deliveries "
                "failed in the last 300s"
            )

        slack = self.slack_health()
        slack_backfill_cooldown = float(
            slack.get("cooldownRemainingSeconds") or 0
        )
        if (
            int(slack.get("rateLimitFailures") or 0) >= 2
            and slack_backfill_cooldown > 0
        ):
            problems.append(
                "Slack backfill rate limited for another "
                f"{int(slack_backfill_cooldown)}s"
            )

        gitlab = self.gitlab_sync_health()
        consecutive_failures = int(
            gitlab.get("consecutiveFailures") or 0
        )
        if consecutive_failures >= 2:
            problems.append(
                f"GitLab sync failed {consecutive_failures} consecutive times"
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
            "gitlabSyncConsecutiveFailures": consecutive_failures,
            "gitlabSyncLastError": gitlab.get("lastError"),
            "gitlabSyncLastErrorAt": gitlab.get("lastErrorAt") or None,
            "gitlabSyncLastSuccessAt": gitlab.get("lastSuccessAt") or None,
        }


class RuntimeDiagnosticsService:
    """Project runtime state into operator diagnostics without secret material."""

    def __init__(
        self,
        *,
        assets: StaticAssetVersionService,
        health: RuntimeHealthService,
        codex: Any,
        bot_runtime: Any,
        telemetry: BotRuntimeTelemetry,
        load_projects: Callable[[], list[Any]],
        get_project: Callable[[str], Any],
        load_thread_index: Callable[[], list[Any]],
        load_active_turns: Callable[[], dict[str, Any]],
        load_queues: Callable[[], dict[str, list[QueuedTurn]]],
        queue_tasks: Mapping[str, Any],
        connections: BotConnectionService,
        bindings: BotBindingSelectionService,
        presentation: BotPresentationService,
        thread_is_active: Callable[[str], bool],
        queue_depth: Callable[[str], int],
        load_agent_presence: Callable[[], Any],
        load_reply_targets: Callable[[], dict[str, Any]],
        load_delivery_targets: Callable[[], dict[str, Any]],
        load_work_item_states: Callable[[], dict[str, WorkItemState]],
        work_item_public: Callable[[WorkItemState], dict[str, Any]],
        watchdog_status: Callable[[], dict[str, Any]],
        thread_message_limit: Callable[[], int],
        slack_health: Callable[[], dict[str, Any]],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.assets = assets
        self.health = health
        self.codex = codex
        self.bot_runtime = bot_runtime
        self.telemetry = telemetry
        self.load_projects = load_projects
        self.get_project = get_project
        self.load_thread_index = load_thread_index
        self.load_active_turns = load_active_turns
        self.load_queues = load_queues
        self.queue_tasks = queue_tasks
        self.connections = connections
        self.bindings = bindings
        self.presentation = presentation
        self.thread_is_active = thread_is_active
        self.queue_depth = queue_depth
        self.load_agent_presence = load_agent_presence
        self.load_reply_targets = load_reply_targets
        self.load_delivery_targets = load_delivery_targets
        self.load_work_item_states = load_work_item_states
        self.work_item_public = work_item_public
        self.watchdog_status = watchdog_status
        self.thread_message_limit = thread_message_limit
        self.slack_health = slack_health
        self.clock = clock

    @staticmethod
    def queued_turn_public(queued: QueuedTurn) -> dict[str, Any]:
        preview = queued.message.replace("\n", " ")
        if len(preview) > 180:
            preview = f"{preview[:180]}..."
        return {
            "id": queued.id,
            "threadId": queued.thread_id,
            "projectId": queued.project_id,
            "source": queued.source,
            "attempts": queued.attempts,
            "createdAt": queued.created_at,
            "messagePreview": preview,
            "replyTarget": (
                queued.reply_target.model_dump()
                if queued.reply_target
                else None
            ),
        }

    def binding_public(self, binding: BotBinding) -> dict[str, Any]:
        item = binding.model_dump()
        item["prefix"] = self.presentation.binding_prefix(binding)
        item["report_name"] = self.presentation.binding_report_name(binding)
        item["active"] = self.thread_is_active(binding.thread_id)
        item["queueDepth"] = self.queue_depth(binding.thread_id)
        if binding.provider == "slack":
            item["slack_icon"] = self.presentation.slack_reply_icon(binding)
            item["slack_username"] = (
                self.presentation.slack_reply_username(binding)
            )
        return item

    def work_item_stats(self) -> dict[str, int]:
        open_states = [
            state
            for state in self.load_work_item_states().values()
            if state.current_stage != "closed"
        ]
        return {
            "open_count": len(open_states),
            "blocked_count": sum(
                1
                for state in open_states
                if state.current_stage == "failed_with_action_owner"
            ),
            "pending_handoff_count": sum(
                1
                for state in open_states
                if state.handoff and state.handoff.status == "pending"
            ),
            "release_gate_count": sum(
                1 for state in open_states if state.release_gate
            ),
            "ready_for_validation_count": sum(
                1
                for state in open_states
                if state.current_stage == "ready_for_validation"
            ),
            "implementation_active_count": sum(
                1
                for state in open_states
                if state.current_stage == "implementation_active"
            ),
        }

    def active_turn_count(self) -> int:
        return len(self.load_active_turns())

    def queued_turn_count(self) -> int:
        return sum(len(items) for items in self.load_queues().values())

    def snapshot(self, project_id: str | None = None) -> dict[str, Any]:
        bindings = self.bindings.load_bindings()
        if project_id:
            self.get_project(project_id)
            bindings = [
                binding
                for binding in bindings
                if binding.project_id == project_id
            ]
        queues = self.load_queues()
        active_turns = self.load_active_turns()
        slack = self.slack_health()
        watchdogs = self.watchdog_status()
        scoped_thread_ids = {binding.thread_id for binding in bindings}

        return {
            "generatedAt": self.clock(),
            "version": self.assets.version(),
            "status": {
                "ok": self.codex.ready.is_set(),
                "pid": self.codex.proc.pid if self.codex.proc else None,
                "error": (
                    None
                    if self.codex.ready.is_set()
                    else self.codex.last_error
                ),
                "pendingApprovals": len(self.codex.pending_approvals),
                "activeTurns": len(active_turns),
                "queuedTurns": sum(len(items) for items in queues.values()),
                **watchdogs,
                "threadMessageLimit": self.thread_message_limit(),
                "slackBackfillIntervalSeconds": (
                    slack.get("intervalSeconds") or 0.0
                ),
                "slackBackfillRunning": bool(slack.get("running")),
                "slackBackfillCooldownRemainingSeconds": (
                    slack.get("cooldownRemainingSeconds") or 0.0
                ),
                "slackBackfillCooldownUntil": slack.get("cooldownUntil"),
            },
            "health": self.health.snapshot(),
            "projects": [
                project.model_dump()
                for project in self.load_projects()
            ],
            "threadIndex": [
                thread.model_dump()
                for thread in self.load_thread_index()
            ],
            "activeTurns": [
                active.model_dump()
                for active in active_turns.values()
            ],
            "queues": {
                thread_id: [
                    self.queued_turn_public(queued)
                    for queued in items
                ]
                for thread_id, items in queues.items()
                if not project_id
                or any(
                    queued.project_id == project_id
                    for queued in items
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
                    **self.connections.public(connection),
                    "runtime": self.telemetry.status.get(connection.id),
                    "runtimeTaskRunning": (
                        connection.id in self.bot_runtime.tasks
                        and not self.bot_runtime.tasks[
                            connection.id
                        ].done()
                    ),
                }
                for connection in self.connections.load_connections()
                if not project_id
                or connection.project_id == project_id
            ],
            "bindings": [
                self.binding_public(binding)
                for binding in bindings
            ],
            "agentChannelPresence": (
                self.load_agent_presence().model_dump()
            ),
            "replyTargets": {
                key: target.model_dump()
                for key, target in self.load_reply_targets().items()
                if not project_id
                or target.thread_id in scoped_thread_ids
            },
            "deliveryTargets": {
                key: target.model_dump()
                for key, target in self.load_delivery_targets().items()
                if not project_id
                or target.thread_id in scoped_thread_ids
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
            "recentBotEvents": self.telemetry.recent_events(),
        }
