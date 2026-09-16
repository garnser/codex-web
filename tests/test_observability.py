from __future__ import annotations

import json
import logging
import unittest

from fastapi import FastAPI

from codex_web.events import EventHub
from codex_web.observability import JsonFormatter, RuntimeMetrics, install_observability


class _Host:
    def __init__(self) -> None:
        self.hub = EventHub()


class RuntimeMetricsTests(unittest.TestCase):
    def test_counter_and_timer_snapshot(self) -> None:
        metrics = RuntimeMetrics()
        metrics.increment("requests")
        metrics.increment("requests", 2)
        metrics.observe("latency", 0.25)
        metrics.observe("latency", 0.75)

        snapshot = metrics.snapshot()

        self.assertEqual(snapshot["counters"]["requests"], 3)
        self.assertEqual(snapshot["timers"]["latency"]["count"], 2)
        self.assertEqual(snapshot["timers"]["latency"]["totalSeconds"], 1.0)
        self.assertEqual(snapshot["timers"]["latency"]["maxSeconds"], 0.75)
        self.assertEqual(snapshot["timers"]["latency"]["averageSeconds"], 0.5)

    def test_json_formatter_preserves_structured_fields(self) -> None:
        formatter = JsonFormatter()
        record = logging.LogRecord(
            "test",
            logging.WARNING,
            __file__,
            1,
            "problem %s",
            ("found",),
            None,
        )
        record.structured = {"event": "test.problem", "thread_id": "thread-1"}

        payload = json.loads(formatter.format(record))

        self.assertEqual(payload["level"], "WARNING")
        self.assertEqual(payload["message"], "problem found")
        self.assertEqual(payload["event"], "test.problem")
        self.assertEqual(payload["thread_id"], "thread-1")

    def test_install_is_idempotent_and_registers_metrics_endpoint_once(self) -> None:
        app = FastAPI()
        host = _Host()

        first = install_observability(app, host)
        second = install_observability(app, host)
        metric_paths = [path for path in app.openapi()["paths"] if path == "/api/metrics"]

        self.assertIs(first, second)
        self.assertIs(app.state.runtime_metrics, first)
        self.assertEqual(metric_paths, ["/api/metrics"])


class EventHubObservabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_listener_failure_is_counted_without_breaking_other_listeners(self) -> None:
        hub = EventHub()
        metrics = RuntimeMetrics()
        hub.configure_observability(metrics)
        seen: list[dict] = []

        def broken(event: dict) -> None:
            raise RuntimeError("boom")

        hub.subscribe(broken)
        hub.subscribe(seen.append)

        await hub.publish({"type": "test.event"})

        self.assertEqual(seen, [{"type": "test.event"}])
        counters = metrics.snapshot()["counters"]
        self.assertEqual(counters["eventhub.events_published"], 1)
        self.assertEqual(counters["eventhub.listener_failures"], 1)


if __name__ == "__main__":
    unittest.main()
