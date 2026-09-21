from __future__ import annotations

import asyncio
import os
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from codex_web.services.runtime_diagnostics import RuntimeHealthService


class _Telemetry:
    def __init__(self) -> None:
        self.status = {
            "connection-1": {
                "connectionId": "connection-1",
                "status": "connected",
            }
        }
        self.events = []
        self._metrics = {
            "bytesRead": 128,
            "validEvents": 0,
        }

    def snapshot(self):
        return {
            key: dict(value)
            for key, value in self.status.items()
        }

    def recent(self, _limit):
        return []

    def recent_metrics(self):
        return dict(self._metrics)


def _service(
    *,
    load_bindings=lambda: [],
    load_queues=lambda: {},
    telemetry=None,
    event_sink=None,
) -> RuntimeHealthService:
    ready = asyncio.Event()
    ready.set()
    codex = SimpleNamespace(
        proc=SimpleNamespace(pid=42, poll=lambda: None),
        ready=ready,
    )
    bot_runtime = SimpleNamespace(
        fingerprints={"connection-1": "fp"},
        tasks={
            "connection-1": SimpleNamespace(
                done=lambda: False
            )
        },
    )
    return RuntimeHealthService(
        codex=codex,
        bot_runtime=bot_runtime,
        telemetry=telemetry or _Telemetry(),
        load_bindings=load_bindings,
        terminal_failures={},
        terminal_recovery_tasks={},
        terminal_failure_window_seconds=lambda: 300.0,
        load_queues=load_queues,
        slack_provider_health=lambda: {
            "cooldownRemainingSeconds": 0.0,
            "rateLimitFailures": 0,
        },
        gitlab_sync_status=lambda: {
            "consecutive_failures": 0,
            "last_error": None,
            "last_error_at": 0.0,
            "last_success_at": 10.0,
        },
        execution_readiness=lambda: {
            "ready": True,
            "reason": "ready",
        },
        count_active_turns=lambda: 2,
        state_store_status=lambda: {
            "ok": True,
            "schemaVersion": 1,
            "supportedSchemaVersion": 1,
        },
        event_sink=event_sink,
    )


class RuntimeHealthHotPathTests(unittest.IsolatedAsyncioTestCase):
    async def test_cached_health_reads_never_call_expensive_loaders(self) -> None:
        calls = {"bindings": 0, "queues": 0}

        def bindings():
            calls["bindings"] += 1
            return []

        def queues():
            calls["queues"] += 1
            return {}

        service = _service(
            load_bindings=bindings,
            load_queues=queues,
        )

        cold = service.health()
        self.assertFalse(cold["ok"])
        self.assertTrue(cold["healthCache"]["stale"])
        self.assertEqual(calls, {"bindings": 0, "queues": 0})

        await service.refresh()
        self.assertEqual(calls, {"bindings": 1, "queues": 1})

        for _ in range(100):
            cached = service.health()
            self.assertTrue(cached["ok"])

        self.assertEqual(calls, {"bindings": 1, "queues": 1})

    async def test_concurrent_refreshes_coalesce_and_slow_loader_does_not_block_loop(self) -> None:
        calls = 0
        started = threading.Event()

        def slow_bindings():
            nonlocal calls
            calls += 1
            started.set()
            time.sleep(0.15)
            return []

        service = _service(load_bindings=slow_bindings)

        refreshes = [
            asyncio.create_task(service.refresh())
            for _ in range(3)
        ]
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.001)

        tick_started = time.perf_counter()
        await asyncio.sleep(0.01)
        tick_elapsed = time.perf_counter() - tick_started
        await asyncio.gather(*refreshes)

        self.assertLess(tick_elapsed, 0.08)
        self.assertEqual(calls, 1)
        self.assertEqual(
            service.health()["healthCache"]["refreshCount"],
            1,
        )

    async def test_cancelled_refresh_waiter_does_not_cancel_shared_worker_refresh(self) -> None:
        started = threading.Event()

        def slow_bindings():
            started.set()
            time.sleep(0.08)
            return []

        service = _service(load_bindings=slow_bindings)
        task = asyncio.create_task(service.refresh())
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.001)

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        await asyncio.sleep(0.12)
        snapshot = service.health()
        self.assertTrue(snapshot["ok"])
        self.assertEqual(
            snapshot["healthCache"]["refreshCount"],
            1,
        )
        self.assertFalse(
            snapshot["healthCache"]["refreshInProgress"]
        )

    async def test_failed_refresh_keeps_last_snapshot_but_marks_readiness_unhealthy(self) -> None:
        broken = False

        def queues():
            if broken:
                raise RuntimeError("malformed state")
            return {}

        service = _service(load_queues=queues)
        good = await service.refresh()
        self.assertTrue(good["ok"])

        broken = True
        failed = await service.refresh()

        self.assertFalse(failed["ok"])
        self.assertTrue(failed["evaluatedOk"])
        self.assertEqual(
            failed["healthCache"]["lastRefreshErrorClass"],
            "RuntimeError",
        )
        self.assertIn(
            "runtime health refresh failed: RuntimeError",
            failed["problems"],
        )
        self.assertEqual(
            failed["healthCache"]["refreshFailures"],
            1,
        )

    async def test_expired_snapshot_reports_stale_without_forcing_refresh(self) -> None:
        calls = 0

        def bindings():
            nonlocal calls
            calls += 1
            return []

        service = _service(load_bindings=bindings)
        await service.refresh()
        self.assertEqual(calls, 1)

        with service._snapshot_lock:
            service._generated_at = time.time() - 1000

        stale = service.health()

        self.assertFalse(stale["ok"])
        self.assertTrue(stale["healthCache"]["stale"])
        self.assertEqual(calls, 1)

    async def test_slow_refresh_operations_emit_correlation_metrics(self) -> None:
        events = []

        def slow_bindings():
            time.sleep(0.03)
            return [
                SimpleNamespace(thread_id="thread-1")
            ]

        service = _service(
            load_bindings=slow_bindings,
            event_sink=events.append,
        )
        with patch.dict(
            os.environ,
            {
                "CODEX_WEB_RUNTIME_SLOW_OPERATION_SECONDS": "0.01",
            },
            clear=False,
        ):
            snapshot = await service.refresh()

        metrics = snapshot["healthMetrics"]["operations"][
            "bindings.load"
        ]
        self.assertEqual(metrics["calls"], 1)
        self.assertEqual(metrics["lastRecords"], 1)
        self.assertGreaterEqual(metrics["lastSeconds"], 0.01)
        slow = [
            event
            for event in events
            if event.get("type") == "runtime_slow_operation"
            and event.get("operation") == "bindings.load"
        ]
        self.assertEqual(len(slow), 1)
        self.assertTrue(slow[0]["refresh_id"].startswith("health-"))


if __name__ == "__main__":
    unittest.main()
