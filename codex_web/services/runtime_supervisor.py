from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Awaitable, Callable
from typing import Any


def _noop(*_args: Any, **_kwargs: Any) -> None:
    return None


async def _async_noop(*_args: Any, **_kwargs: Any) -> None:
    return None


class _HostPolicy:
    """Compatibility adapter used only by direct legacy-style consumers."""

    def __init__(self, host: Any) -> None:
        self.host = host

    def autonomy_enabled(self) -> bool:
        return bool(getattr(self.host, "_autonomy_enabled", lambda: False)())

    def owner_work_watchdog_interval(self) -> float:
        return float(
            getattr(
                self.host,
                "_owner_work_watchdog_interval",
                lambda: 0.0,
            )()
        )

    def release_gate_watchdog_interval(self) -> float:
        return float(
            getattr(
                self.host,
                "_release_gate_watchdog_interval",
                lambda: 0.0,
            )()
        )

    def work_item_sla_watchdog_interval(self) -> float:
        return float(
            getattr(
                self.host,
                "_work_item_sla_watchdog_interval",
                lambda: 0.0,
            )()
        )

    def orchestrator_watchdog_interval(self) -> float:
        return float(
            getattr(
                self.host,
                "_orchestrator_watchdog_interval",
                lambda: 0.0,
            )()
        )

    def split_brain_watchdog_interval(self) -> float:
        return float(
            getattr(
                self.host,
                "_split_brain_watchdog_interval",
                lambda: 0.0,
            )()
        )


class _HostAutonomy:
    def __init__(self, host: Any) -> None:
        self.run_owner_work_cycle = getattr(
            host,
            "_run_owner_work_watchdog_cycle",
            _async_noop,
        )
        self.run_release_gate_cycle = getattr(
            host,
            "_run_release_gate_watchdog_cycle",
            _async_noop,
        )
        self.run_work_item_sla_cycle = getattr(
            host,
            "_run_work_item_sla_cycle",
            _async_noop,
        )
        self.run_orchestrator_cycle = getattr(
            host,
            "_run_orchestrator_watchdog_cycle",
            _async_noop,
        )
        self.run_split_brain_cycle = getattr(
            host,
            "_run_split_brain_watchdog_cycle",
            _async_noop,
        )


class _HostGitLab:
    def __init__(self, host: Any) -> None:
        self.support_servicedesk_sweep_interval = getattr(
            host,
            "_support_servicedesk_sweep_interval",
            lambda: 0.0,
        )
        self.api_token = getattr(host, "_gitlab_api_token", lambda: None)
        self.sweep_support_servicedesk = getattr(
            host,
            "_run_support_servicedesk_sweep_once",
            _async_noop,
        )


class _HostRecovery:
    def __init__(self, host: Any) -> None:
        self.host = host

    def set_shutting_down(self, value: bool) -> None:
        if hasattr(self.host, "IS_SHUTTING_DOWN"):
            self.host.IS_SHUTTING_DOWN = value

    def schedule(self, *, reason: str = "manual") -> bool:
        callback = getattr(
            self.host,
            "_schedule_native_recovery_cycles",
            None,
        )
        if callback is None:
            return False
        callback(reason=reason)
        return True

    async def stop(self) -> None:
        return None


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

    def __init__(
        self,
        app: Any,
        host: Any,
        *,
        policy: Any | None = None,
        autonomy: Any | None = None,
        gitlab: Any | None = None,
        native_recovery: Any | None = None,
        continuity: Any | None = None,
        codex: Any | None = None,
        bot_runtime: Any | None = None,
        runtime_health: Any | None = None,
        stale_turn_recovery: Any | None = None,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
        truncate_text: Callable[[str, int], str] | None = None,
        sd_notify: Callable[[str], Any] | None = None,
        daemon_health: Callable[[], dict[str, Any]] | None = None,
        load_projects: Callable[[], Any] | None = None,
        compact_turn_queues: Callable[[], Any] | None = None,
        dedupe_bot_integrations: Callable[[], Any] | None = None,
        restore_thread_names: Callable[[], Awaitable[None]] | None = None,
        resume_active_threads: Callable[[], Awaitable[None]] | None = None,
        load_turn_queues: Callable[[], dict[str, Any]] | None = None,
        thread_is_active: Callable[[str], bool] | None = None,
        release_stale_active_turn: Callable[[str, str], Any] | None = None,
        schedule_queue_drain: Callable[[str], Any] | None = None,
        flush_compatibility_state: Callable[[], Any] | None = None,
    ) -> None:
        self.app = app
        self.host = host
        self.policy = policy or _HostPolicy(host)
        self.autonomy = autonomy or _HostAutonomy(host)
        self.gitlab = gitlab or _HostGitLab(host)
        self.native_recovery = native_recovery or _HostRecovery(host)
        self.continuity = continuity
        self.codex = codex or getattr(host, "codex", None)
        self.bot_runtime = bot_runtime or getattr(host, "bot_runtime", None)
        self.runtime_health = runtime_health
        self.stale_turn_recovery = stale_turn_recovery
        self.event_sink = event_sink or getattr(
            host,
            "_append_bot_event",
            _noop,
        )
        self.truncate_text = truncate_text or getattr(
            host,
            "_truncate_text",
            lambda value, limit: value[:limit],
        )
        self.sd_notify = sd_notify or getattr(host, "_sd_notify", _noop)
        self.daemon_health = daemon_health or getattr(
            host,
            "_daemon_health",
            lambda: {"ok": True, "problems": []},
        )
        self.load_projects = load_projects or getattr(
            host,
            "_load_projects",
            lambda: [],
        )
        self.compact_turn_queues = compact_turn_queues or getattr(
            host,
            "_compact_turn_queues",
            _noop,
        )
        self.dedupe_bot_integrations = dedupe_bot_integrations or getattr(
            host,
            "_dedupe_bot_integrations",
            _noop,
        )
        self.restore_thread_names = restore_thread_names or getattr(
            host,
            "_restore_bot_thread_names",
            _async_noop,
        )
        self.resume_active_threads = resume_active_threads or getattr(
            host,
            "_resume_active_threads_after_startup",
            _async_noop,
        )
        self.load_turn_queues = load_turn_queues or getattr(
            host,
            "_load_turn_queues",
            lambda: {},
        )
        self.thread_is_active = thread_is_active or getattr(
            host,
            "_thread_is_active",
            lambda _thread_id: False,
        )
        self.release_stale_active_turn = (
            release_stale_active_turn
            or getattr(host, "_release_stale_active_turn", _noop)
        )
        self.schedule_queue_drain = schedule_queue_drain or getattr(
            host,
            "_schedule_queue_drain",
            _noop,
        )
        self.flush_compatibility_state = (
            flush_compatibility_state
            or getattr(host, "_flush_compatibility_state", _noop)
        )
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.startup_tasks: set[asyncio.Task[Any]] = set()
        self.started = False

    def _ownership(self) -> Any | None:
        return getattr(
            self.app.state,
            "replicated_ownership_service",
            None,
        )

    def _owns(self, responsibility: str) -> bool:
        ownership = self._ownership()
        return True if ownership is None else ownership.owns(responsibility)

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
            seconds = float(
                os.environ.get("CODEX_WEB_QUEUE_RECOVERY_SECONDS") or "30"
            )
        except ValueError:
            return 30.0
        if seconds <= 0:
            return 0.0
        return max(10.0, seconds)

    def _spawn(
        self,
        name: str,
        coroutine: Awaitable[None],
    ) -> asyncio.Task[None]:
        task = asyncio.create_task(coroutine, name=f"codex-web:{name}")
        self.tasks[name] = task
        legacy_attr = self.LEGACY_TASK_ATTRS.get(name)
        if legacy_attr:
            setattr(self.host, legacy_attr, task)
        return task

    def _spawn_startup_task(
        self,
        name: str,
        coroutine: Awaitable[Any],
    ) -> asyncio.Task[Any]:
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
                ownership = self._ownership()
                if responsibility is None or ownership is None:
                    await cycle()
                else:
                    await ownership.run_exclusive(responsibility, cycle)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.event_sink(
                    {"type": failure_event, "error": str(exc)}
                )
            await asyncio.sleep(interval)

    async def _systemd_watchdog_loop(self) -> None:
        interval = self.watchdog_interval()
        if interval <= 0:
            return
        while True:
            health = self.daemon_health()
            if health["ok"]:
                self.sd_notify("WATCHDOG=1\nSTATUS=codex-web healthy")
            else:
                self.sd_notify(
                    "WATCHDOG=1\nSTATUS=codex-web unhealthy: "
                    + "; ".join(health["problems"])
                )
            await asyncio.sleep(interval)

    async def _support_servicedesk_loop(self) -> None:
        interval = float(self.gitlab.support_servicedesk_sweep_interval())
        if interval <= 0 or not self.gitlab.api_token():
            return

        async def sweep() -> None:
            result = await self.gitlab.sweep_support_servicedesk()
            self.event_sink(
                {
                    "type": "support_servicedesk_sweep_completed",
                    **{
                        key: value
                        for key, value in result.items()
                        if key != "results"
                    },
                }
            )

        while True:
            try:
                ownership = self._ownership()
                if ownership is None:
                    await sweep()
                else:
                    await ownership.run_exclusive(
                        "support-servicedesk",
                        sweep,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.event_sink(
                    {
                        "type": "support_servicedesk_sweep_failed",
                        "error": self.truncate_text(str(exc), 500),
                    }
                )
            await asyncio.sleep(interval)

    async def _queue_recovery_loop(self) -> None:
        interval = self.queue_recovery_interval_seconds()
        if interval <= 0:
            return

        async def recover() -> None:
            if self.stale_turn_recovery is not None:
                self.stale_turn_recovery.schedule(
                    reason="queue-recovery",
                )
            for thread_id in self.load_turn_queues():
                if (
                    self.stale_turn_recovery is None
                    and self.thread_is_active(thread_id)
                ):
                    self.release_stale_active_turn(
                        thread_id,
                        "queue-recovery",
                    )
                if not self.thread_is_active(thread_id):
                    self.schedule_queue_drain(thread_id)

        while True:
            try:
                ownership = self._ownership()
                if ownership is None:
                    await recover()
                else:
                    await ownership.run_exclusive(
                        "queue-recovery",
                        recover,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.event_sink(
                    {"type": "queue_recovery_failed", "error": str(exc)}
                )
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
        self.native_recovery.set_shutting_down(False)
        if hasattr(self.host, "IS_SHUTTING_DOWN"):
            self.host.IS_SHUTTING_DOWN = False
        self.load_projects()
        self.compact_turn_queues()
        self.dedupe_bot_integrations()
        if self.codex is not None:
            try:
                await self.codex.start()
            except Exception:
                pass
        if (
            self.codex is not None
            and self.codex.ready.is_set()
            and self.policy.autonomy_enabled()
            and self._owns("startup-recovery")
        ):
            self._spawn_startup_task(
                "restore-thread-names",
                self.restore_thread_names(),
            )
            if self.stale_turn_recovery is None:
                self._spawn_startup_task(
                    "resume-active-threads",
                    self.resume_active_threads(),
                )

        if self.runtime_health is not None:
            self._spawn(
                "runtime-health-refresh",
                self.runtime_health.run_forever(),
            )

        telemetry = getattr(
            self.app.state,
            "bot_runtime_telemetry",
            None,
        )
        if telemetry is not None and callable(
            getattr(
                telemetry,
                "run_journal_maintenance_forever",
                None,
            )
        ):
            self._spawn(
                "event-journal-maintenance",
                telemetry.run_journal_maintenance_forever(),
            )

        self.sd_notify("READY=1\nSTATUS=codex-web started")
        if (
            self.stale_turn_recovery is not None
            and self._owns("startup-recovery")
        ):
            self.stale_turn_recovery.schedule(reason="startup")
        if self.bot_runtime is not None:
            self._spawn(
                "bot-runtime-owner",
                self._singleton_service_loop(
                    "bot-runtime",
                    self.bot_runtime.sync,
                    self.bot_runtime.stop,
                ),
            )
        self._spawn("systemd-watchdog", self._systemd_watchdog_loop())
        self._spawn("support-servicedesk", self._support_servicedesk_loop())
        self._spawn(
            "owner-work",
            self._cycle_loop(
                self.policy.owner_work_watchdog_interval,
                self.autonomy.run_owner_work_cycle,
                failure_event="owner_work_watchdog_failed",
                responsibility="owner-work",
            ),
        )
        self._spawn(
            "release-gate",
            self._cycle_loop(
                self.policy.release_gate_watchdog_interval,
                self.autonomy.run_release_gate_cycle,
                failure_event="release_gate_watchdog_failed",
                responsibility="release-gate",
            ),
        )
        self._spawn(
            "work-item-sla",
            self._cycle_loop(
                self.policy.work_item_sla_watchdog_interval,
                self.autonomy.run_work_item_sla_cycle,
                failure_event="work_item_sla_watchdog_failed",
                responsibility="work-item-sla",
            ),
        )
        self._spawn(
            "orchestrator",
            self._cycle_loop(
                self.policy.orchestrator_watchdog_interval,
                self.autonomy.run_orchestrator_cycle,
                failure_event="orchestrator_watchdog_failed",
                responsibility="orchestrator",
            ),
        )
        self._spawn(
            "split-brain",
            self._cycle_loop(
                self.policy.split_brain_watchdog_interval,
                self.autonomy.run_split_brain_cycle,
                failure_event="split_brain_watchdog_failed",
                responsibility="split-brain",
            ),
        )
        self._spawn("queue-recovery", self._queue_recovery_loop())

        goal_continuation = getattr(
            self.app.state,
            "goal_continuation_service",
            None,
        )
        if goal_continuation is not None:
            self._spawn(
                "goal-continuation-recovery",
                self._cycle_loop(
                    lambda: max(
                        1.0,
                        float(
                            os.environ.get(
                                "CODEX_WEB_GOAL_CONTINUATION_RECOVERY_SECONDS",
                                "5",
                            )
                        ),
                    ),
                    goal_continuation.recover_due,
                    failure_event="goal_continuation_recovery_failed",
                    responsibility="goal-continuation",
                ),
            )

        scheduler = getattr(self.app.state, "scheduler_service", None)
        if scheduler is not None:
            self._spawn("scheduler", scheduler.run_forever())

        event_transport = getattr(
            self.app.state,
            "event_transport_runtime",
            None,
        )
        if event_transport is not None:
            self._spawn(
                "event-transport",
                event_transport.run_forever(),
            )

        slack_provider = getattr(
            self.app.state,
            "slack_provider_service",
            None,
        )
        if slack_provider is not None:
            self._spawn(
                "slack-provider-owner",
                self._singleton_service_loop(
                    "bot-runtime",
                    slack_provider.start,
                    slack_provider.stop,
                ),
            )
        if self._owns("native-recovery"):
            self.native_recovery.schedule()

    async def stop(self) -> None:
        self.native_recovery.set_shutting_down(True)
        if hasattr(self.host, "IS_SHUTTING_DOWN"):
            self.host.IS_SHUTTING_DOWN = True
        self.sd_notify("STOPPING=1\nSTATUS=codex-web stopping")

        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
        for legacy_attr in self.LEGACY_TASK_ATTRS.values():
            setattr(self.host, legacy_attr, None)

        startup_tasks = list(self.startup_tasks)
        for task in startup_tasks:
            task.cancel()
        if startup_tasks:
            await asyncio.gather(*startup_tasks, return_exceptions=True)
        self.startup_tasks.clear()

        slack_provider = getattr(
            self.app.state,
            "slack_provider_service",
            None,
        )
        if slack_provider is not None:
            await slack_provider.stop()

        ownership = self._ownership()
        if ownership is not None:
            ownership.release_all()

        if self.continuity is not None:
            await self.continuity.stop()
        else:
            continuity_tasks = [
                *list(
                    getattr(
                        self.host,
                        "ACTIONABLE_OWNER_CONTINUITY_TASKS",
                        {},
                    ).values()
                ),
                *list(
                    getattr(
                        self.host,
                        "HANDOFF_CONTINUITY_TASKS",
                        {},
                    ).values()
                ),
            ]
            for task in continuity_tasks:
                task.cancel()
            if continuity_tasks:
                await asyncio.gather(
                    *continuity_tasks,
                    return_exceptions=True,
                )

        await self.native_recovery.stop()
        if self.stale_turn_recovery is not None:
            await self.stale_turn_recovery.stop()

        worker_sessions = getattr(
            self.app.state,
            "assignment_bound_codex_session_manager",
            None,
        )
        if worker_sessions is not None:
            await worker_sessions.stop_all()

        task_source_writeback = getattr(
            self.app.state,
            "task_source_writeback_service",
            None,
        )
        if task_source_writeback is not None:
            await task_source_writeback.stop()

        gitlab_sync_jobs = getattr(
            self.app.state,
            "gitlab_sync_job_service",
            None,
        )
        if gitlab_sync_jobs is not None:
            await gitlab_sync_jobs.stop()

        if self.bot_runtime is not None:
            await self.bot_runtime.stop()
        if self.codex is not None:
            await self.codex.stop()
        # Keyed operational writes deliberately defer large JSON compatibility
        # mirrors. A controlled shutdown is the checkpoint boundary required
        # before rollback to a JSON-reading release.
        try:
            self.flush_compatibility_state()
        except Exception as exc:
            self.event_sink(
                {
                    "type": "compatibility_state_checkpoint_failed",
                    "error": self.truncate_text(str(exc), 500),
                }
            )
        self.started = False


def _replace_lifecycle_handler(
    handlers: list[Any],
    legacy: Any,
    replacement: Any,
) -> None:
    replaced = False
    for index, handler in enumerate(list(handlers)):
        if handler is legacy:
            handlers[index] = replacement
            replaced = True
    if not replaced and replacement not in handlers:
        handlers.append(replacement)


def install_runtime_supervisor(
    app: Any,
    host: Any,
    **dependencies: Any,
) -> RuntimeSupervisor:
    existing = getattr(app.state, "runtime_supervisor", None)
    if isinstance(existing, RuntimeSupervisor) and existing.host is host:
        return existing

    service = RuntimeSupervisor(app, host, **dependencies)
    app.state.runtime_supervisor = service
    host._watchdog_interval = service.watchdog_interval
    host._queue_recovery_interval_seconds = (
        service.queue_recovery_interval_seconds
    )
    _replace_lifecycle_handler(
        app.router.on_startup,
        getattr(host, "startup", None),
        service.start,
    )
    _replace_lifecycle_handler(
        app.router.on_shutdown,
        getattr(host, "shutdown", None),
        service.stop,
    )
    return service
