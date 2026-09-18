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
    RuntimeLogStore,
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
        self.assertIn("/api/logs", paths)
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
                "/api/logs",
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


class RuntimeStructuredLogTests(unittest.TestCase):
    def _record(self, *, created: float | None = None) -> logging.LogRecord:
        record = logging.LogRecord(
            "codex.test.logs",
            logging.WARNING,
            __file__,
            1,
            "free-form secret must-not-be-returned",
            (),
            None,
        )
        if created is not None:
            record.created = created
        record.structured = {
            "event": "worker.recovered",
            "operation": "recover",
            "provider": "reference",
            "resource_ids": ["resource-1"],
            "api_token": "raw-token-must-not-leak",
            "prompt": "raw-prompt-must-not-leak",
            "arbitrary_body": "not-on-allowlist",
        }
        return record

    def test_store_filters_by_tenant_and_returns_allowlisted_metadata_only(self) -> None:
        store = RuntimeLogStore(max_entries=10, max_age_seconds=3600)
        with correlated(
            correlation_id="corr-a",
            causation_id="cause-a",
            tenant_id="org-a",
            workspace_id="ws-a",
            work_item_ref="group/app#1",
            execution_id="exec-1",
            action_intent_id="intent-1",
        ):
            store.append(self._record())

        own = store.query(
            tenant_id="org-a",
            workspace_id="ws-a",
            correlation_id="corr-a",
        )
        other = store.query(
            tenant_id="org-b",
            workspace_id="ws-b",
        )

        self.assertEqual(len(own), 1)
        self.assertEqual(other, [])
        item = own[0]
        self.assertEqual(item["event"], "worker.recovered")
        self.assertEqual(item["correlationId"], "corr-a")
        self.assertEqual(item["causationId"], "cause-a")
        self.assertEqual(item["workItemRef"], "group/app#1")
        self.assertEqual(item["executionId"], "exec-1")
        self.assertEqual(item["actionIntentId"], "intent-1")
        self.assertEqual(item["fields"]["operation"], "recover")
        self.assertEqual(item["fields"]["resource_ids"], ["resource-1"])
        serialized = json.dumps(item)
        self.assertNotIn("free-form secret", serialized)
        self.assertNotIn("raw-token-must-not-leak", serialized)
        self.assertNotIn("raw-prompt-must-not-leak", serialized)
        self.assertNotIn("arbitrary_body", serialized)

    def test_store_excludes_unscoped_logs_and_enforces_query_bounds(self) -> None:
        store = RuntimeLogStore(max_entries=2, max_age_seconds=60)
        store.append(self._record())

        self.assertEqual(
            store.query(tenant_id="org-a", workspace_id="ws-a"),
            [],
        )
        with self.assertRaisesRegex(ValueError, "window_seconds"):
            store.query(
                tenant_id="org-a",
                workspace_id="ws-a",
                window_seconds=61,
            )
        with self.assertRaisesRegex(ValueError, "limit"):
            store.query(
                tenant_id="org-a",
                workspace_id="ws-a",
                limit=201,
            )

    def test_log_api_supports_deterministic_correlation_filtering(self) -> None:
        app = FastAPI()
        host = _Host()
        install_observability(app, host)
        actor = AuthenticationActor(
            identity_id="operator",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="org-log-api",
            workspace_id="ws-log-api",
            roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.PRIMARY,
        )

        @app.middleware("http")
        async def inject_actor(request: Request, call_next):
            request.state.identity_actor = actor
            return await call_next(request)

        with correlated(
            correlation_id="corr-log-api",
            tenant_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            work_item_ref="group/app#log",
        ):
            app.state.runtime_log_store.append(self._record())

        with TestClient(app) as client:
            response = client.get(
                "/api/logs",
                params={
                    "correlation_id": "corr-log-api",
                    "limit": 10,
                    "window_seconds": 60,
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["items"][0]["correlationId"], "corr-log-api")
        self.assertEqual(payload["classification"], "internal")
        self.assertEqual(payload["retention"]["storage"], "bounded_memory")
        self.assertLessEqual(payload["retention"]["maxEntries"], 500)
        self.assertIn("free-form messages", payload["payloadPolicy"])


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
