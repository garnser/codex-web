from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

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
        self.bootstrap_calls = 0
        self.recovery_calls = 0
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

    def __getattr__(self, name: str):
        if name in {function_name for _, _, function_name in WorkerSupervisor.WORKERS}:
            async def worker() -> None:
                await asyncio.Event().wait()
            return worker
        raise AttributeError(name)


class WorkerSupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_and_stop_manage_all_long_lived_workers(self) -> None:
        host = _Host()
        supervisor = WorkerSupervisor(host)

        await supervisor.start()

        self.assertTrue(supervisor.started)
        self.assertEqual(set(supervisor.tasks), {name for name, _, _ in WorkerSupervisor.WORKERS})
        self.assertEqual(host.codex.started, 1)
        self.assertEqual(host.bot_runtime.synced, 1)
        self.assertEqual(host.recovery_calls, 1)
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


if __name__ == "__main__":
    unittest.main()
