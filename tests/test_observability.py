from __future__ import annotations

import json
import logging
import unittest

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from codex_web.events import EventHub
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.observability import (
    CORRELATION_HEADER,
    JsonFormatter,
    RuntimeHealth,
    RuntimeLogBuffer,
    RuntimeMetrics,
    RuntimeTracer,
    correlated,
    current_correlation,
    install_observability,
    log_event,
    sanitize_telemetry,
)


class _Host:
    def __init__(self) -> None:
        self.hub = EventHub()


class RuntimeLogBufferTests(unittest.TestCase):
    def test_buffer_is_tenant_scoped_redacted_and_omits_freeform_message(self) -> None:
        buffer = RuntimeLogBuffer(max_entries=10, retention_seconds=300)
        record = logging.LogRecord(
            "codex_web.test",
            logging.WARNING,
            __file__,
            1,
            "freeform secret should never be returned",
            (),
            None,
        )
        record.structured = {
            "event": "action.failed",
            "api_token": "must-not-leak",
            "provider": "reference",
        }

        with correlated(
            correlation_id="corr-log-a",
            causation_id="cause-log-a",
            tenant_id="org-a",
            workspace_id="ws-a",
            work_item_ref="group/app#1",
        ):
            buffer.emit(record)

        own, truncated = buffer.query(
            organization_id="org-a",
            workspace_id="ws-a",
            window_seconds=300,
            limit=10,
        )
        foreign, _ = buffer.query(
            organization_id="org-b",
            workspace_id="ws-b",
            window_seconds=300,
            limit=10,
        )

        self.assertFalse(truncated)
        self.assertEqual(len(own), 1)
        self.assertEqual(foreign, [])
        self.assertEqual(own[0]["event"], "action.failed")
        self.assertEqual(own[0]["correlationId"], "corr-log-a")
        self.assertEqual(own[0]["fields"]["api_token"], "[REDACTED]")
        self.assertEqual(own[0]["fields"]["provider"], "reference")
        self.assertEqual(own[0]["classification"], "internal")
        self.assertTrue(own[0]["telemetryOnly"])
        self.assertNotIn("message", own[0])
        self.assertNotIn("exception", own[0])

    def test_buffer_query_filters_and_bounds_results_deterministically(self) -> None:
        buffer = RuntimeLogBuffer(max_entries=10, retention_seconds=300)
        logger = logging.getLogger("codex_web.buffer-test")
        for index, event in enumerate(("first", "second", "third")):
            record = logging.LogRecord(
                logger.name,
                logging.INFO if index < 2 else logging.ERROR,
                __file__,
                index + 1,
                "not returned",
                (),
                None,
            )
            record.structured = {"event": event, "sequence": index}
            with correlated(
                correlation_id="corr-shared" if index < 2 else "corr-other",
                tenant_id="org-a",
                workspace_id="ws-a",
            ):
                buffer.emit(record)

        rows, truncated = buffer.query(
            organization_id="org-a",
            workspace_id="ws-a",
            window_seconds=300,
            limit=1,
            correlation_id="corr-shared",
        )
        self.assertTrue(truncated)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event"], "second")

        error_rows, _ = buffer.query(
            organization_id="org-a",
            workspace_id="ws-a",
            window_seconds=300,
            limit=10,
            level="ERROR",
            event="third",
        )
        self.assertEqual([item["event"] for item in error_rows], ["third"])


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
        self.assertIn("/api/logs/recent", paths)
        self.assertIn("/api/observability", paths)

    def test_http_correlation_is_propagated_and_observable_without_model_calls(self) -> None:
        app = FastAPI()
        host = _Host()
        install_observability(app, host)
        actor = AuthenticationActor(
            identity_id="operator",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="org-a",
            workspace_id="ws-a",
            roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.PRIMARY,
        )

        @app.middleware("http")
        async def inject_actor(request: Request, call_next):
            request.state.identity_actor = actor
            return await call_next(request)

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

    def test_observability_endpoints_require_admin_or_scoped_service(self) -> None:
        app = FastAPI()
        host = _Host()
        install_observability(app, host)
        current = {
            "actor": AuthenticationActor(
                identity_id="member",
                principal_kind=PrincipalKind.HUMAN,
                organization_id="org-a",
                workspace_id="ws-a",
                roles=(MembershipRole.MEMBER,),
                assurance=AuthenticationAssurance.PRIMARY,
            )
        }

        @app.middleware("http")
        async def inject_actor(request: Request, call_next):
            request.state.identity_actor = current["actor"]
            return await call_next(request)

        with TestClient(app) as client:
            for path in (
                "/api/metrics",
                "/api/health",
                "/api/traces/recent",
                "/api/logs/recent",
                "/api/observability",
            ):
                self.assertEqual(client.get(path).status_code, 403)

            current["actor"] = current["actor"].model_copy(
                update={"roles": (MembershipRole.ADMIN,)}
            )
            self.assertEqual(client.get("/api/observability").status_code, 200)

            current["actor"] = AuthenticationActor(
                identity_id="observer-service",
                principal_kind=PrincipalKind.SERVICE,
                organization_id="org-a",
                workspace_id="ws-a",
                assurance=AuthenticationAssurance.SERVICE_TOKEN,
                service_scopes=(),
            )
            denied = client.get("/api/metrics")
            self.assertEqual(denied.status_code, 403)
            self.assertIn("observability:read", denied.json()["detail"])

            current["actor"] = current["actor"].model_copy(
                update={"service_scopes": ("observability:read",)}
            )
            self.assertEqual(client.get("/api/metrics").status_code, 200)


class RuntimeLogApiTests(unittest.TestCase):
    def test_log_query_enforces_scope_redaction_filters_and_bounds(self) -> None:
        app = FastAPI()
        host = _Host()
        install_observability(app, host)
        app.state.runtime_log_buffer.clear()
        current = {
            "actor": AuthenticationActor(
                identity_id="operator",
                principal_kind=PrincipalKind.HUMAN,
                organization_id="org-a",
                workspace_id="ws-a",
                roles=(MembershipRole.ADMIN,),
                assurance=AuthenticationAssurance.PRIMARY,
            )
        }

        @app.middleware("http")
        async def inject_actor(request: Request, call_next):
            request.state.identity_actor = current["actor"]
            return await call_next(request)

        logger = logging.getLogger("codex_web.api-log-test")
        with correlated(
            correlation_id="corr-api",
            causation_id="cause-api",
            tenant_id="org-a",
            workspace_id="ws-a",
            execution_id="exec-1",
            action_intent_id="intent-1",
        ):
            log_event(
                logger,
                logging.ERROR,
                "provider.failed",
                "freeform provider failure with secret-looking text",
                provider="reference",
                access_token="must-not-leak",
            )
        with correlated(
            correlation_id="corr-foreign",
            tenant_id="org-b",
            workspace_id="ws-b",
        ):
            log_event(
                logger,
                logging.INFO,
                "foreign.event",
                "foreign tenant event",
                provider="other",
            )

        with TestClient(app) as client:
            response = client.get(
                "/api/logs/recent",
                params={
                    "correlation_id": "corr-api",
                    "level": "error",
                    "limit": 1,
                    "window_seconds": 900,
                },
            )
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload["count"], 1)
            self.assertEqual(payload["classification"], "internal")
            self.assertEqual(payload["retentionSeconds"], 3600)
            self.assertEqual(payload["maxResultBytes"], 64 * 1024)
            self.assertTrue(payload["telemetryOnly"])
            item = payload["items"][0]
            self.assertEqual(item["event"], "provider.failed")
            self.assertEqual(item["executionId"], "exec-1")
            self.assertEqual(item["actionIntentId"], "intent-1")
            self.assertEqual(item["fields"]["access_token"], "[REDACTED]")
            self.assertNotIn("message", item)
            self.assertNotIn("foreign.event", json.dumps(payload))

            self.assertEqual(
                client.get("/api/logs/recent", params={"limit": 101}).status_code,
                422,
            )
            self.assertEqual(
                client.get(
                    "/api/logs/recent",
                    params={"window_seconds": 86401},
                ).status_code,
                422,
            )

            current["actor"] = current["actor"].model_copy(
                update={"roles": (MembershipRole.MEMBER,)}
            )
            self.assertEqual(client.get("/api/logs/recent").status_code, 403)

            current["actor"] = AuthenticationActor(
                identity_id="observer-service",
                principal_kind=PrincipalKind.SERVICE,
                organization_id="org-a",
                workspace_id="ws-a",
                assurance=AuthenticationAssurance.SERVICE_TOKEN,
                service_scopes=("observability:read",),
            )
            self.assertEqual(client.get("/api/logs/recent").status_code, 200)


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
    async def test_publish_inherits_correlation_into_canonical_event_fanout(self) -> None:
        hub = EventHub()
        seen: list[dict] = []
        hub.subscribe(seen.append)

        with correlated(
            correlation_id="corr-event",
            causation_id="request-1",
            workspace_id="workspace-a",
            work_item_ref="TASK-9",
        ):
            await hub.publish({"type": "work.updated"})

        self.assertEqual(seen[0]["correlation_id"], "corr-event")
        self.assertEqual(seen[0]["causation_id"], "request-1")
        self.assertEqual(seen[0]["workspace_id"], "workspace-a")
        self.assertEqual(seen[0]["work_item_ref"], "TASK-9")

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
