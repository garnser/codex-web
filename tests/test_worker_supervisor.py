from __future__ import annotations

import asyncio
import unittest

from fastapi import FastAPI

from codex_web.runtime.workers import WorkerSupervisor, install_worker_supervisor


class _Codex:
    def __init__(self) -> None:
        self.ready = asyncio.Event()
        self.started = 0
        self.stopped = 0

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1


class _BotRuntime:
    def __init__(self) -> None:
        self.synced = 0
        self.stopped = 0

    async def sync(self) -> None:
        self.synced += 1

    async def stop(self) -> None:
        self.stopped += 1


class _Host:
    def __init__(self) -> None:
        self.IS_SHUTTING_DOWN = False
        self.codex = _Codex()
        self.bot_runtime = _BotRuntime()
        self.ACTIONABLE_OWNER_CONTINUITY_TASKS = {}
        self.HANDOFF_CONTINUITY_TASKS = {}
        self.notifications: list[str] = []
        self.events: list[dict] = []
        self.bootstrap_calls = 0
        self.recovery_calls = 0
        self.cycle_calls: list[str] = []
        for _, global_name, _ in WorkerSupervisor.WORKERS:
            setattr(self, global_name, None)

    async def startup(self) -> None:
        raise AssertionError("legacy startup should have been removed")

    async def shutdown(self) -> None:
        raise AssertionError("legacy shutdown should have been removed")

    def _load_projects(self) -> list:
        return []

    def _compact_turn_queues(self) -> None:
        pass

    def _dedupe_bot_integrations(self) -> None:
        pass

    def _autonomy_enabled(self) -> bool:
        return False

    async def _restore_bot_thread_names(self) -> None:
        self.bootstrap_calls += 1

    async def _resume_active_threads_after_startup(self) -> None:
        self.bootstrap_calls += 1

    def _sd_notify(self, message: str) -> bool:
        self.notifications.append(message)
        return True

    def _schedule_native_recovery_cycles(self) -> None:
        self.recovery_calls += 1

    def _append_bot_event(self, event: dict) -> None:
        self.events.append(event)

    def _truncate_text(self, value: str, limit: int) -> str:
        return value[:limit]

    def _watchdog_interval(self) -> float:
        return 3600

    def _daemon_health(self) -> dict:
        return {"ok": True, "problems": []}

    def _support_servicedesk_sweep_interval(self) -> float:
        return 3600

    def _gitlab_api_token(self) -> str:
        return "token"

    async def _run_support_servicedesk_sweep_once(self) -> dict:
        self.cycle_calls.append("support_servicedesk")
        return {"ok": True, "checked": 0, "results": []}

    def _owner_work_watchdog_interval(self) -> float:
        return 3600

    async def _run_owner_work_watchdog_cycle(self) -> None:
        self.cycle_calls.append("owner_work")

    def _release_gate_watchdog_interval(self) -> float:
        return 3600

    async def _run_release_gate_watchdog_cycle(self) -> None:
        self.cycle_calls.append("release_gate")

    def _work_item_sla_watchdog_interval(self) -> float:
        return 3600

    async def _run_work_item_sla_cycle(self) -> None:
        self.cycle_calls.append("work_item_sla")

    def _orchestrator_watchdog_interval(self) -> float:
        return 3600

    async def _run_orchestrator_watchdog_cycle(self) -> None:
        self.cycle_calls.append("orchestrator")

    def _split_brain_watchdog_interval(self) -> float:
        return 3600

    async def _run_split_brain_watchdog_cycle(self) -> None:
        self.cycle_calls.append("split_brain")

    def _queue_recovery_interval_seconds(self) -> float:
        return 3600

    def _load_turn_queues(self) -> dict:
        return {"thread-1": []}

    def _thread_is_active(self, thread_id: str) -> bool:
        return False

    def _release_stale_active_turn(self, thread_id: str, reason: str) -> None:
        self.cycle_calls.append("release_stale")

    def _schedule_queue_drain(self, thread_id: str) -> None:
        self.cycle_calls.append("queue_recovery")

    def _slack_backfill_interval_seconds(self) -> float:
        return 3600

    def _slack_backfill_cooldown_remaining_seconds(self) -> float:
        return 0

    async def _run_slack_backfill_cycle(self) -> None:
        self.cycle_calls.append("slack_backfill")


class WorkerSupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_and_stop_manage_all_long_lived_workers(self) -> None:
        host = _Host()
        supervisor = WorkerSupervisor(host)

        await supervisor.start()
        await asyncio.sleep(0)

        self.assertTrue(supervisor.started)
        self.assertEqual(set(supervisor.tasks), {name for name, _, _ in WorkerSupervisor.WORKERS})
        self.assertEqual(host.codex.started, 1)
        self.assertEqual(host.bot_runtime.synced, 1)
        self.assertEqual(host.recovery_calls, 1)
        self.assertIn("owner_work", host.cycle_calls)
        self.assertIn("queue_recovery", host.cycle_calls)
        self.assertIn("slack_backfill", host.cycle_calls)
        for name, global_name, _ in WorkerSupervisor.WORKERS:
            self.assertIs(getattr(host, global_name), supervisor.tasks[name])
            self.assertFalse(supervisor.tasks[name].done())

        await supervisor.stop()

        self.assertFalse(supervisor.started)
        self.assertEqual(supervisor.tasks, {})
        self.assertEqual(host.bot_runtime.stopped, 1)
        self.assertEqual(host.codex.stopped, 1)
        for _, global_name, _ in WorkerSupervisor.WORKERS:
            self.assertIsNone(getattr(host, global_name))

    async def test_install_replaces_legacy_handlers_idempotently(self) -> None:
        host = _Host()
        app = FastAPI()
        app.router.on_startup.append(host.startup)
        app.router.on_shutdown.append(host.shutdown)

        first = install_worker_supervisor(app, host)
        second = install_worker_supervisor(app, host)

        self.assertIs(app.state.worker_supervisor, second)
        self.assertIsNot(first, second)
        self.assertEqual(len(app.router.on_startup), 1)
        self.assertEqual(len(app.router.on_shutdown), 1)
        self.assertIs(getattr(app.router.on_startup[0], "__self__", None), second)
        self.assertIs(getattr(app.router.on_shutdown[0], "__self__", None), second)

    async def test_periodic_cycle_failures_are_isolated_and_reported(self) -> None:
        host = _Host()
        supervisor = WorkerSupervisor(host)

        calls = 0

        async def failing_cycle() -> None:
            nonlocal calls
            calls += 1
            raise RuntimeError("boom")

        host._run_owner_work_watchdog_cycle = failing_cycle
        task = asyncio.create_task(supervisor._owner_work_watchdog_loop())
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        self.assertEqual(calls, 1)
        self.assertEqual(host.events[-1]["type"], "owner_work_watchdog_failed")


if __name__ == "__main__":
    unittest.main()
