from __future__ import annotations

import asyncio
import contextlib
from typing import Any, Awaitable


class WorkerSupervisor:
    """Own background worker startup/shutdown and periodic loop execution."""

    WORKERS: tuple[tuple[str, str, str], ...] = (
        ("watchdog", "WATCHDOG_TASK", "_watchdog_loop"),
        ("support_servicedesk", "SUPPORT_SERVICEDESK_SWEEP_TASK", "_support_servicedesk_sweep_loop"),
        ("owner_work", "OWNER_WORK_WATCHDOG_TASK", "_owner_work_watchdog_loop"),
        ("release_gate", "RELEASE_GATE_WATCHDOG_TASK", "_release_gate_watchdog_loop"),
        ("work_item_sla", "WORK_ITEM_SLA_TASK", "_work_item_sla_watchdog_loop"),
        ("orchestrator", "ORCHESTRATOR_WATCHDOG_TASK", "_orchestrator_watchdog_loop"),
        ("split_brain", "SPLIT_BRAIN_WATCHDOG_TASK", "_split_brain_watchdog_loop"),
        ("queue_recovery", "QUEUE_RECOVERY_TASK", "_queue_recovery_loop"),
        ("slack_backfill", "SLACK_BACKFILL_TASK", "_slack_backfill_loop"),
    )

    def __init__(self, host: Any) -> None:
        self.host = host
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.bootstrap_tasks: set[asyncio.Task[Any]] = set()
        self.started = False

    def _spawn_bootstrap(self, awaitable: Awaitable[Any]) -> None:
        task = asyncio.create_task(awaitable)
        self.bootstrap_tasks.add(task)
        task.add_done_callback(self.bootstrap_tasks.discard)

    def _start_worker(self, name: str, global_name: str, function_name: str) -> None:
        worker = getattr(self, function_name)
        task = asyncio.create_task(worker(), name=f"codex-web:{name}")
        self.tasks[name] = task
        # Preserve compatibility for diagnostics that still read these globals.
        setattr(self.host, global_name, task)

    async def start(self) -> None:
        if self.started:
            return
        self.started = True
        self.host.IS_SHUTTING_DOWN = False
        self.host._load_projects()
        self.host._compact_turn_queues()
        self.host._dedupe_bot_integrations()

        try:
            await self.host.codex.start()
        except Exception:
            # Keep the HTTP UI available so readiness/diagnostics can expose the
            # app-server failure instead of taking down the process.
            pass

        await self.host.bot_runtime.sync()
        if self.host.codex.ready.is_set() and self.host._autonomy_enabled():
            self._spawn_bootstrap(self.host._restore_bot_thread_names())
            self._spawn_bootstrap(self.host._resume_active_threads_after_startup())

        self.host._sd_notify("READY=1\nSTATUS=codex-web started")
        for worker in self.WORKERS:
            self._start_worker(*worker)
        self.host._schedule_native_recovery_cycles()

    async def stop(self) -> None:
        if not self.started:
            return
        self.started = False
        self.host.IS_SHUTTING_DOWN = True
        self.host._sd_notify("STOPPING=1\nSTATUS=codex-web stopping")

        managed = list(self.tasks.values()) + list(self.bootstrap_tasks)
        for task in managed:
            if not task.done():
                task.cancel()
        if managed:
            await asyncio.gather(*managed, return_exceptions=True)
        self.tasks.clear()
        self.bootstrap_tasks.clear()

        for _, global_name, _ in self.WORKERS:
            setattr(self.host, global_name, None)

        continuity_tasks = list(self.host.ACTIONABLE_OWNER_CONTINUITY_TASKS.values()) + list(
            self.host.HANDOFF_CONTINUITY_TASKS.values()
        )
        for task in continuity_tasks:
            if not task.done():
                task.cancel()
        if continuity_tasks:
            await asyncio.gather(*continuity_tasks, return_exceptions=True)
        self.host.ACTIONABLE_OWNER_CONTINUITY_TASKS.clear()
        self.host.HANDOFF_CONTINUITY_TASKS.clear()

        with contextlib.suppress(Exception):
            await self.host.bot_runtime.stop()
        await self.host.codex.stop()

    def status(self) -> dict[str, bool]:
        return {name: not task.done() for name, task in self.tasks.items()}

    async def _watchdog_loop(self) -> None:
        interval = self.host._watchdog_interval()
        if interval <= 0:
            return
        while True:
            health = self.host._daemon_health()
            if health["ok"]:
                self.host._sd_notify("WATCHDOG=1\nSTATUS=codex-web healthy")
            else:
                self.host._sd_notify(
                    "WATCHDOG=1\nSTATUS=codex-web unhealthy: " + "; ".join(health["problems"])
                )
            await asyncio.sleep(interval)

    async def _support_servicedesk_sweep_loop(self) -> None:
        interval = self.host._support_servicedesk_sweep_interval()
        if interval <= 0 or not self.host._gitlab_api_token():
            return
        while True:
            try:
                result = await self.host._run_support_servicedesk_sweep_once()
                self.host._append_bot_event(
                    {
                        "type": "support_servicedesk_sweep_completed",
                        **{key: value for key, value in result.items() if key != "results"},
                    }
                )
            except Exception as exc:
                self.host._append_bot_event(
                    {
                        "type": "support_servicedesk_sweep_failed",
                        "error": self.host._truncate_text(str(exc), 500),
                    }
                )
            await asyncio.sleep(interval)

    async def _owner_work_watchdog_loop(self) -> None:
        await self._simple_periodic_loop(
            self.host._owner_work_watchdog_interval,
            self.host._run_owner_work_watchdog_cycle,
            "owner_work_watchdog_failed",
        )

    async def _release_gate_watchdog_loop(self) -> None:
        await self._simple_periodic_loop(
            self.host._release_gate_watchdog_interval,
            self.host._run_release_gate_watchdog_cycle,
            "release_gate_watchdog_failed",
        )

    async def _work_item_sla_watchdog_loop(self) -> None:
        await self._simple_periodic_loop(
            self.host._work_item_sla_watchdog_interval,
            self.host._run_work_item_sla_cycle,
            "work_item_sla_watchdog_failed",
        )

    async def _orchestrator_watchdog_loop(self) -> None:
        await self._simple_periodic_loop(
            self.host._orchestrator_watchdog_interval,
            self.host._run_orchestrator_watchdog_cycle,
            "orchestrator_watchdog_failed",
        )

    async def _split_brain_watchdog_loop(self) -> None:
        await self._simple_periodic_loop(
            self.host._split_brain_watchdog_interval,
            self.host._run_split_brain_watchdog_cycle,
            "split_brain_watchdog_failed",
        )

    async def _simple_periodic_loop(
        self,
        interval_getter: Any,
        cycle: Any,
        failure_event: str,
    ) -> None:
        interval = interval_getter()
        if interval <= 0:
            return
        while True:
            try:
                await cycle()
            except Exception as exc:
                self.host._append_bot_event({"type": failure_event, "error": str(exc)})
            await asyncio.sleep(interval)

    async def _queue_recovery_loop(self) -> None:
        interval = self.host._queue_recovery_interval_seconds()
        if interval <= 0:
            return
        while True:
            try:
                for thread_id in self.host._load_turn_queues():
                    if self.host._thread_is_active(thread_id):
                        self.host._release_stale_active_turn(thread_id, "queue-recovery")
                    if not self.host._thread_is_active(thread_id):
                        self.host._schedule_queue_drain(thread_id)
            except Exception as exc:
                self.host._append_bot_event({"type": "queue_recovery_failed", "error": str(exc)})
            await asyncio.sleep(interval)

    async def _slack_backfill_loop(self) -> None:
        interval = self.host._slack_backfill_interval_seconds()
        if interval <= 0:
            return
        while True:
            cooldown = self.host._slack_backfill_cooldown_remaining_seconds()
            if cooldown > 0:
                await asyncio.sleep(max(interval, cooldown))
                continue
            try:
                await self.host._run_slack_backfill_cycle()
            except Exception as exc:
                self.host._append_bot_event({"type": "slack_backfill_loop_failed", "error": str(exc)})
            await asyncio.sleep(interval)


def _same_handler(left: Any, right: Any) -> bool:
    return (
        getattr(left, "__self__", None) is getattr(right, "__self__", None)
        and getattr(left, "__func__", left) is getattr(right, "__func__", right)
    )


def install_worker_supervisor(app: Any, host: Any) -> WorkerSupervisor:
    """Replace the legacy FastAPI lifecycle handlers with WorkerSupervisor."""

    previous = getattr(app.state, "worker_supervisor", None)
    startup_handlers = list(app.router.on_startup)
    shutdown_handlers = list(app.router.on_shutdown)

    def owned_by_previous(handler: Any) -> bool:
        return previous is not None and getattr(handler, "__self__", None) is previous

    app.router.on_startup[:] = [
        handler
        for handler in startup_handlers
        if not _same_handler(handler, host.startup) and not owned_by_previous(handler)
    ]
    app.router.on_shutdown[:] = [
        handler
        for handler in shutdown_handlers
        if not _same_handler(handler, host.shutdown) and not owned_by_previous(handler)
    ]

    supervisor = WorkerSupervisor(host)
    app.router.on_startup.append(supervisor.start)
    app.router.on_shutdown.append(supervisor.stop)
    app.state.worker_supervisor = supervisor
    return supervisor
