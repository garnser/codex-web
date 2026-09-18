from __future__ import annotations

import json
import logging
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from codex_web.events import EventHub
from codex_web.observability import (
    CORRELATION_HEADER,
    JsonFormatter,
    RuntimeHealth,
    RuntimeMetrics,
    RuntimeTracer,
    correlated,
    current_correlation,
    install_observability,
    sanitize_telemetry,
)


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

    def test_metrics_allow_only_bounded_low_cardinality_labels(self) -> None:
        metrics = RuntimeMetrics()
        metrics.increment(
            "http.requests",
            labels={"method": "GET", "status_class": "2xx", "result": "ok"},
        )
        snapshot = metrics.snapshot()
        self.assertEqual(
            snapshot["counters"]["http.requests{method=GET,result=ok,status_class=2xx}"],
            1.0,
        )
        with self.assertRaisesRegex(ValueError, "unsafe/high-cardinality"):
            metrics.increment(
                "http.requests",
                labels={"workspace_id": "tenant-specific"},
            )

    def test_json_formatter_preserves_safe_structured_fields_and_redacts_secrets(self) -> None:
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
        record.structured = {
            "event": "test.problem",
            "thread_id": "thread-1",
            "api_token": "must-not-leak",
        }

        with correlated(correlation_id="corr-log"):
            payload = json.loads(formatter.format(record))

        self.assertEqual(payload["level"], "WARNING")
        self.assertEqual(payload["message"], "problem found")
        self.assertEqual(payload["event"], "test.problem")
        self.assertEqual(payload["thread_id"], "thread-1")
        self.assertEqual(payload["correlation_id"], "corr-log")
        self.assertEqual(payload["api_token"], "[REDACTED]")

    def test_secret_safe_telemetry_preserves_usage_counts(self) -> None:
        payload = sanitize_telemetry(
            {
                "authorization": "Bearer secret",
                "password": "hidden",
                "prompt": "sensitive model content",
                "access_token": "opaque-token",
                "input_tokens": 1200,
                "output_tokens": 400,
            }
        )
        self.assertEqual(payload["authorization"], "[REDACTED]")
        self.assertEqual(payload["password"], "[REDACTED]")
        self.assertEqual(payload["prompt"], "[REDACTED]")
        self.assertEqual(payload["access_token"], "[REDACTED]")
        self.assertEqual(payload["input_tokens"], 1200)
        self.assertEqual(payload["output_tokens"], 400)

    def test_install_is_idempotent_and_registers_observability_endpoints_once(self) -> None:
        app = FastAPI()
        host = _Host()

        first = install_observability(app, host)
        second = install_observability(app, host)
        paths = app.openapi()["paths"]

        self.assertIs(first, second)
        self.assertIs(app.state.runtime_metrics, first)
        self.assertIn("/api/metrics", paths)
        self.assertIn("/api/health", paths)
        self.assertIn("/api/traces/recent", paths)
        self.assertIn("/api/observability", paths)

    def test_http_correlation_is_propagated_and_observable_without_model_calls(self) -> None:
        app = FastAPI()
        host = _Host()
        install_observability(app, host)

        @app.get("/probe")
        async def probe() -> dict[str, bool]:
            return {"ok": True}

        with TestClient(app) as client:
            response = client.get(
                "/probe",
                headers={
                    CORRELATION_HEADER: "corr-http",
                    "x-causation-id": "cause-http",
                },
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers[CORRELATION_HEADER], "corr-http")

            health = client.get("/api/health").json()
            self.assertTrue(health["liveness"])
            self.assertTrue(health["readiness"])
            self.assertTrue(health["autonomousExecutionEligible"])

            snapshot = client.get("/api/observability").json()
            self.assertIn("metrics", snapshot)
            self.assertIn("health", snapshot)
            self.assertGreaterEqual(snapshot["traceCount"], 1)


class CorrelationAndHealthTests(unittest.TestCase):
    def test_correlation_context_propagates_and_nests(self) -> None:
        self.assertIsNone(current_correlation())
        with correlated(
            correlation_id="corr-1",
            causation_id="cause-1",
            workspace_id="workspace-a",
        ) as outer:
            self.assertEqual(outer.correlation_id, "corr-1")
            with correlated(work_item_ref="TASK-1") as inner:
                self.assertEqual(inner.correlation_id, "corr-1")
                self.assertEqual(inner.causation_id, "cause-1")
                self.assertEqual(inner.workspace_id, "workspace-a")
                self.assertEqual(inner.work_item_ref, "TASK-1")
        self.assertIsNone(current_correlation())

    def test_health_separates_liveness_readiness_and_autonomy_eligibility(self) -> None:
        health = RuntimeHealth()
        health.set_dependency(
            "task-source",
            "degraded",
            required_for_autonomy=True,
            reason="provider latency",
        )
        snapshot = health.snapshot()
        self.assertTrue(snapshot["liveness"])
        self.assertTrue(snapshot["readiness"])
        self.assertTrue(snapshot["degraded"])
        self.assertFalse(snapshot["autonomousExecutionEligible"])

        health.set_dependency(
            "database",
            "unhealthy",
            required_for_readiness=True,
            required_for_autonomy=True,
            reason="connection failed",
        )
        snapshot = health.snapshot()
        self.assertFalse(snapshot["readiness"])
        self.assertEqual(snapshot["status"], "unhealthy")

    def test_trace_spans_retain_correlation_and_redact_attributes(self) -> None:
        tracer = RuntimeTracer()
        with correlated(correlation_id="corr-span", causation_id="evt-1"):
            with tracer.span(
                "provider.execute",
                provider="reference",
                password="must-not-leak",
            ):
                pass

        items = tracer.snapshot()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["correlationId"], "corr-span")
        self.assertEqual(items[0]["causationId"], "evt-1")
        self.assertEqual(items[0]["attributes"]["password"], "[REDACTED]")


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
