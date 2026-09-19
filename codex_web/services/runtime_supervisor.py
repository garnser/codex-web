from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Awaitable, Callable
from typing import Any


class RuntimeSupervisor:
    """Own long-lived process tasks and application lifecycle coordination."""

    LEGACY_TASK_ATTRS = {
        "systemd-watchdog": "WATCHDOG_TASK",
        "support-servicedesk": "SUPPORT_SERVICEDESK_SWEEP_TASK",
        "owner-work": "OWNER_WORK_WATCHDOG_TASK",
        "release-gate": "RELEASE_GATE_WATCHDOG_TASK",
        "work-item-sla": "WORK_ITEM_SLA_TASK",
        "orchestrator": "ORCHESTRATOR_WATCHDOG_TASK",
        "split-brain": "SPLIT_BRAIN_WATCHDOG_TASK",
        "queue-recovery": "QUEUE_RECOVERY_TASK",
    }

    def __init__(self, app: Any, host: Any) -> None:
        self.app = app
        self.host = host
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.startup_tasks: set[asyncio.Task[Any]] = set()
        self.started = False

    def _ownership(self):
        return getattr(
            self.app.state,
            "replicated_ownership_service",
            None,
        )

    def _owns(self, responsibility: str) -> bool:
        ownership = self._ownership()
        if ownership is None:
            return True
        return ownership.owns(responsibility)

    def watchdog_interval(self) -> float:
        try:
            usec = int(os.environ.get("WATCHDOG_USEC") or "0")
        except ValueError:
            return 0.0
        if usec <= 0:
            return 0.0
        return max(5.0, min(30.0, usec / 2_000_000))

    def queue_recovery_interval_seconds(self) -> float:
        try:
            seconds = float(os.environ.get("CODEX_WEB_QUEUE_RECOVERY_SECONDS") or "30")
        except ValueError:
            return 30.0
        if seconds <= 0:
            return 0.0
        return max(10.0, seconds)

    def _spawn(self, name: str, coroutine: Awaitable[None]) -> asyncio.Task[None]:
        task = asyncio.create_task(coroutine, name=f"codex-web:{name}")
        self.tasks[name] = task
        legacy_attr = self.LEGACY_TASK_ATTRS.get(name)
        if legacy_attr:
            setattr(self.host, legacy_attr, task)
        return task

    def _spawn_startup_task(self, name: str, coroutine: Awaitable[Any]) -> asyncio.Task[Any]:
        task = asyncio.create_task(coroutine, name=f"codex-web:{name}")
        self.startup_tasks.add(task)
        task.add_done_callback(self.startup_tasks.discard)
        return task

    async def _cycle_loop(
        self,
        interval_getter: Callable[[], float],
        cycle: Callable[[], Awaitable[None]],
        *,
        failure_event: str,
        responsibility: str | None = None,
    ) -> None:
        interval = float(interval_getter())
        if interval <= 0:
            return
        while True:
            try:
                if responsibility is None or self._owns(responsibility):
                    await cycle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.host._append_bot_event({"type": failure_event, "error": str(exc)})
            await asyncio.sleep(interval)

    async def _systemd_watchdog_loop(self) -> None:
        h = self.host
        interval = float(h._watchdog_interval())
        if interval <= 0:
            return
        while True:
            health = h._daemon_health()
            if health["ok"]:
                h._sd_notify("WATCHDOG=1\nSTATUS=codex-web healthy")
            else:
                h._sd_notify(
                    "WATCHDOG=1\nSTATUS=codex-web unhealthy: "
                    + "; ".join(health["problems"])
                )
            await asyncio.sleep(interval)

    async def _support_servicedesk_loop(self) -> None:
        h = self.host
        interval = float(h._support_servicedesk_sweep_interval())
        if interval <= 0 or not h._gitlab_api_token():
            return
        while True:
            try:
                if not self._owns("support-servicedesk"):
                    await asyncio.sleep(interval)
                    continue
                result = await h._run_support_servicedesk_sweep_once()
                h._append_bot_event(
                    {
                        "type": "support_servicedesk_sweep_completed",
                        **{key: value for key, value in result.items() if key != "results"},
                    }
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                h._append_bot_event(
                    {
                        "type": "support_servicedesk_sweep_failed",
                        "error": h._truncate_text(str(exc), 500),
                    }
                )
            await asyncio.sleep(interval)

    async def _queue_recovery_loop(self) -> None:
        h = self.host
        interval = float(h._queue_recovery_interval_seconds())
        if interval <= 0:
            return
        while True:
            try:
                if not self._owns("queue-recovery"):
                    await asyncio.sleep(interval)
                    continue
                for thread_id in h._load_turn_queues():
                    if h._thread_is_active(thread_id):
                        h._release_stale_active_turn(thread_id, "queue-recovery")
                    if not h._thread_is_active(thread_id):
                        h._schedule_queue_drain(thread_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                h._append_bot_event({"type": "queue_recovery_failed", "error": str(exc)})
            await asyncio.sleep(interval)

    async def _singleton_service_loop(
        self,
        responsibility: str,
        start: Callable[[], Awaitable[None]],
        stop: Callable[[], Awaitable[None]],
    ) -> None:
        running = False
        ownership = self._ownership()
        interval = (
            max(1.0, float(ownership.lease_seconds) / 3.0)
            if ownership is not None
            else 5.0
        )
        try:
            while True:
                owned = self._owns(responsibility)
                if owned and not running:
                    await start()
                    running = True
                elif not owned and running:
                    await stop()
                    running = False
                await asyncio.sleep(interval)
        finally:
            if running:
                with contextlib.suppress(Exception):
                    await stop()

    def task_status(self) -> dict[str, dict[str, bool]]:
        return {
            name: {
                "running": not task.done(),
                "done": task.done(),
                "cancelled": task.cancelled(),
            }
            for name, task in self.tasks.items()
        }

    async def start(self) -> None:
        if self.started:
            return
        self.started = True
        h = self.host
        h.IS_SHUTTING_DOWN = False
        h._load_projects()
        h._compact_turn_queues()
        h._dedupe_bot_integrations()
        try:
            await h.codex.start()
        except Exception:
            # Keep the HTTP UI available so it can report app-server failures.
            pass
        await h.bot_runtime.sync()
        if (
            h.codex.ready.is_set()
            and h._autonomy_enabled()
            and self._owns("startup-recovery")
        ):
            self._spawn_startup_task(
                "restore-thread-names",
                h._restore_bot_thread_names(),
            )
            self._spawn_startup_task(
                "resume-active-threads",
                h._resume_active_threads_after_startup(),
            )

        h._sd_notify("READY=1\nSTATUS=codex-web started")
        self._spawn("systemd-watchdog", self._systemd_watchdog_loop())
        self._spawn("support-servicedesk", self._support_servicedesk_loop())
        self._spawn(
            "owner-work",
            self._cycle_loop(
                h._owner_work_watchdog_interval,
                h._run_owner_work_watchdog_cycle,
                failure_event="owner_work_watchdog_failed",
                responsibility="owner-work",
            ),
        )
        self._spawn(
            "release-gate",
            self._cycle_loop(
                h._release_gate_watchdog_interval,
                h._run_release_gate_watchdog_cycle,
                failure_event="release_gate_watchdog_failed",
                responsibility="release-gate",
            ),
        )
        self._spawn(
            "work-item-sla",
            self._cycle_loop(
                h._work_item_sla_watchdog_interval,
                h._run_work_item_sla_cycle,
                failure_event="work_item_sla_watchdog_failed",
                responsibility="work-item-sla",
            ),
        )
        self._spawn(
            "orchestrator",
            self._cycle_loop(
                h._orchestrator_watchdog_interval,
                h._run_orchestrator_watchdog_cycle,
                failure_event="orchestrator_watchdog_failed",
                responsibility="orchestrator",
            ),
        )
        self._spawn(
            "split-brain",
            self._cycle_loop(
                h._split_brain_watchdog_interval,
                h._run_split_brain_watchdog_cycle,
                failure_event="split_brain_watchdog_failed",
                responsibility="split-brain",
            ),
        )
        self._spawn("queue-recovery", self._queue_recovery_loop())

        scheduler_service = getattr(self.app.state, "scheduler_service", None)
        if scheduler_service is not None:
            self._spawn("scheduler", scheduler_service.run_forever())

        event_transport_runtime = getattr(
            self.app.state,
            "event_transport_runtime",
            None,
        )
        if event_transport_runtime is not None:
            self._spawn(
                "event-transport",
                event_transport_runtime.run_forever(),
            )

        slack_provider_service = getattr(
            self.app.state,
            "slack_provider_service",
            None,
        )
        if slack_provider_service is not None:
            self._spawn(
                "slack-provider-owner",
                self._singleton_service_loop(
                    "slack-provider",
                    slack_provider_service.start,
                    slack_provider_service.stop,
                ),
            )
        if self._owns("native-recovery"):
            h._schedule_native_recovery_cycles()

    async def stop(self) -> None:
        h = self.host
        h.IS_SHUTTING_DOWN = True
        h._sd_notify("STOPPING=1\nSTATUS=codex-web stopping")

        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
        for legacy_attr in self.LEGACY_TASK_ATTRS.values():
            setattr(h, legacy_attr, None)

        startup_tasks = list(self.startup_tasks)
        for task in startup_tasks:
            task.cancel()
        if startup_tasks:
            await asyncio.gather(*startup_tasks, return_exceptions=True)
        self.startup_tasks.clear()

        slack_provider_service = getattr(
            self.app.state,
            "slack_provider_service",
            None,
        )
        if slack_provider_service is not None:
            await slack_provider_service.stop()

        ownership = self._ownership()
        if ownership is not None:
            ownership.release_all()

        continuity_tasks = [
            *list(h.ACTIONABLE_OWNER_CONTINUITY_TASKS.values()),
            *list(h.HANDOFF_CONTINUITY_TASKS.values()),
        ]
        for task in continuity_tasks:
            task.cancel()
        if continuity_tasks:
            await asyncio.gather(*continuity_tasks, return_exceptions=True)
        h.ACTIONABLE_OWNER_CONTINUITY_TASKS.clear()
        h.HANDOFF_CONTINUITY_TASKS.clear()

        codex_worker_sessions = getattr(
            self.app.state,
            "assignment_bound_codex_session_manager",
            None,
        )
        if codex_worker_sessions is not None:
            await codex_worker_sessions.stop_all()

        await h.bot_runtime.stop()
        await h.codex.stop()
        self.started = False


def _replace_lifecycle_handler(handlers: list[Any], legacy: Any, replacement: Any) -> None:
    replaced = False
    for index, handler in enumerate(list(handlers)):
        if handler is legacy:
            handlers[index] = replacement
            replaced = True
    if not replaced and replacement not in handlers:
        handlers.append(replacement)


def install_runtime_supervisor(app: Any, host: Any) -> RuntimeSupervisor:
    existing = getattr(app.state, "runtime_supervisor", None)
    if isinstance(existing, RuntimeSupervisor) and existing.host is host:
        return existing

    service = RuntimeSupervisor(app, host)
    app.state.runtime_supervisor = service
    host._watchdog_interval = service.watchdog_interval
    host._queue_recovery_interval_seconds = service.queue_recovery_interval_seconds
    _replace_lifecycle_handler(app.router.on_startup, getattr(host, "startup", None), service.start)
    _replace_lifecycle_handler(app.router.on_shutdown, getattr(host, "shutdown", None), service.stop)
    return service
