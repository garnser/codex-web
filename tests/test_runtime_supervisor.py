from __future__ import annotations

import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from codex_web.services.runtime_supervisor import RuntimeSupervisor, install_runtime_supervisor


class RuntimeSupervisorInstallTests(unittest.TestCase):
    def test_runtime_timer_defaults_invalid_values_and_clamps_match_legacy_behavior(self) -> None:
        service = RuntimeSupervisor(SimpleNamespace(state=SimpleNamespace()), SimpleNamespace())

        with patch.dict(os.environ, {"WATCHDOG_USEC": ""}, clear=False):
            self.assertEqual(service.watchdog_interval(), 0.0)
        with patch.dict(os.environ, {"WATCHDOG_USEC": "bad"}, clear=False):
            self.assertEqual(service.watchdog_interval(), 0.0)
        with patch.dict(os.environ, {"WATCHDOG_USEC": "2"}, clear=False):
            self.assertEqual(service.watchdog_interval(), 5.0)
        with patch.dict(os.environ, {"WATCHDOG_USEC": "200000000"}, clear=False):
            self.assertEqual(service.watchdog_interval(), 30.0)
        with patch.dict(os.environ, {"WATCHDOG_USEC": "20000000"}, clear=False):
            self.assertEqual(service.watchdog_interval(), 10.0)

        with patch.dict(os.environ, {"CODEX_WEB_QUEUE_RECOVERY_SECONDS": ""}, clear=False):
            self.assertEqual(service.queue_recovery_interval_seconds(), 30.0)
        with patch.dict(os.environ, {"CODEX_WEB_QUEUE_RECOVERY_SECONDS": "bad"}, clear=False):
            self.assertEqual(service.queue_recovery_interval_seconds(), 30.0)
        with patch.dict(os.environ, {"CODEX_WEB_QUEUE_RECOVERY_SECONDS": "0"}, clear=False):
            self.assertEqual(service.queue_recovery_interval_seconds(), 0.0)
        with patch.dict(os.environ, {"CODEX_WEB_QUEUE_RECOVERY_SECONDS": "5"}, clear=False):
            self.assertEqual(service.queue_recovery_interval_seconds(), 10.0)
        with patch.dict(os.environ, {"CODEX_WEB_QUEUE_RECOVERY_SECONDS": "45"}, clear=False):
            self.assertEqual(service.queue_recovery_interval_seconds(), 45.0)

    def test_installer_replaces_legacy_lifecycle_handlers(self) -> None:
        async def legacy_startup() -> None:
            return None

        async def legacy_shutdown() -> None:
            return None

        host = SimpleNamespace(startup=legacy_startup, shutdown=legacy_shutdown)
        app = SimpleNamespace(
            state=SimpleNamespace(),
            router=SimpleNamespace(
                on_startup=[legacy_startup],
                on_shutdown=[legacy_shutdown],
            ),
        )

        service = install_runtime_supervisor(app, host)

        self.assertIs(app.state.runtime_supervisor, service)
        self.assertIs(app.router.on_startup[0].__self__, service)
        self.assertEqual(app.router.on_startup[0].__func__, RuntimeSupervisor.start)
        self.assertIs(app.router.on_shutdown[0].__self__, service)
        self.assertEqual(app.router.on_shutdown[0].__func__, RuntimeSupervisor.stop)
        self.assertIs(host._watchdog_interval.__self__, service)
        self.assertIs(host._queue_recovery_interval_seconds.__self__, service)

    def test_installer_adds_handlers_when_legacy_runtime_has_none(self) -> None:
        host = SimpleNamespace()
        app = SimpleNamespace(
            state=SimpleNamespace(),
            router=SimpleNamespace(on_startup=[], on_shutdown=[]),
        )

        service = install_runtime_supervisor(app, host)

        self.assertIs(app.state.runtime_supervisor, service)
        self.assertEqual(len(app.router.on_startup), 1)
        self.assertIs(app.router.on_startup[0].__self__, service)
        self.assertEqual(app.router.on_startup[0].__func__, RuntimeSupervisor.start)
        self.assertEqual(len(app.router.on_shutdown), 1)
        self.assertIs(app.router.on_shutdown[0].__self__, service)
        self.assertEqual(app.router.on_shutdown[0].__func__, RuntimeSupervisor.stop)

    def test_installer_is_idempotent_for_same_host(self) -> None:
        async def legacy_startup() -> None:
            return None

        async def legacy_shutdown() -> None:
            return None

        host = SimpleNamespace(startup=legacy_startup, shutdown=legacy_shutdown)
        app = SimpleNamespace(
            state=SimpleNamespace(),
            router=SimpleNamespace(
                on_startup=[legacy_startup],
                on_shutdown=[legacy_shutdown],
            ),
        )

        first = install_runtime_supervisor(app, host)
        second = install_runtime_supervisor(app, host)

        self.assertIs(first, second)
        self.assertEqual(len(app.router.on_startup), 1)
        self.assertEqual(len(app.router.on_shutdown), 1)


class RuntimeSupervisorAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_cycle_loop_records_failure_and_keeps_running_until_cancelled(self) -> None:
        events: list[dict[str, object]] = []
        calls = 0

        async def cycle() -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("boom")

        host = SimpleNamespace(_append_bot_event=events.append)
        app = SimpleNamespace(state=SimpleNamespace())
        service = RuntimeSupervisor(app, host)
        task = asyncio.create_task(
            service._cycle_loop(
                lambda: 0.001,
                cycle,
                failure_event="test_cycle_failed",
            )
        )
        await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertGreaterEqual(calls, 2)
        self.assertEqual(events[0]["type"], "test_cycle_failed")
        self.assertIn("boom", str(events[0]["error"]))

    async def test_stop_flushes_deferred_compatibility_state(self) -> None:
        calls: list[str] = []

        class Runtime:
            async def stop(self) -> None:
                return None

        class Codex:
            async def stop(self) -> None:
                return None

        host = SimpleNamespace(
            IS_SHUTTING_DOWN=False,
            _sd_notify=lambda _message: True,
            ACTIONABLE_OWNER_CONTINUITY_TASKS={},
            HANDOFF_CONTINUITY_TASKS={},
            bot_runtime=Runtime(),
            codex=Codex(),
        )
        app = SimpleNamespace(
            state=SimpleNamespace(slack_provider_service=None)
        )
        service = RuntimeSupervisor(
            app,
            host,
            flush_compatibility_state=lambda: calls.append("flushed"),
        )
        service.started = True

        await service.stop()

        self.assertEqual(calls, ["flushed"])

    async def test_stop_clears_legacy_task_slots_and_runtime_task_registry(self) -> None:
        async def sleeper() -> None:
            await asyncio.sleep(30)

        class Runtime:
            async def stop(self) -> None:
                return None

        class Codex:
            async def stop(self) -> None:
                return None

        host = SimpleNamespace(
            IS_SHUTTING_DOWN=False,
            _sd_notify=lambda _message: True,
            ACTIONABLE_OWNER_CONTINUITY_TASKS={},
            HANDOFF_CONTINUITY_TASKS={},
            bot_runtime=Runtime(),
            codex=Codex(),
        )
        for attr in RuntimeSupervisor.LEGACY_TASK_ATTRS.values():
            setattr(host, attr, None)
        app = SimpleNamespace(state=SimpleNamespace(slack_provider_service=None))
        service = RuntimeSupervisor(app, host)
        service.started = True
        service._spawn("owner-work", sleeper())

        await service.stop()

        self.assertTrue(host.IS_SHUTTING_DOWN)
        self.assertEqual(service.tasks, {})
        self.assertIsNone(host.OWNER_WORK_WATCHDOG_TASK)
        self.assertFalse(service.started)


if __name__ == "__main__":
    unittest.main()
