from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from codex_web.api.metrics import build_metrics_router
from codex_web.goals import (
    GoalCriterionKind,
    GoalCriterionOperator,
    GoalSuccessCriterion,
)
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
    TenantScope,
)
from codex_web.metrics import (
    MetricAggregation,
    MetricDefinitionCreate,
    MetricDefinitionUpdate,
    MetricFreshness,
    MetricObservationCreate,
    MetricSnapshotRequest,
    MetricValueType,
)
from codex_web.services.metrics import (
    MetricConflictError,
    MetricNotFoundError,
    MetricService,
    MetricValidationError,
)
from codex_web.storage.metrics import MetricStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class MetricServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.service = MetricService(MetricStore(sqlite))
        self.scope = TenantScope(organization_id="org-a", workspace_id="ws-a")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _definition(self, **overrides):
        data = {
            "key": "deploy_pass_rate",
            "name": "Deploy pass rate",
            "description": "Percent of deployment checks that passed.",
            "owner_identity_id": "owner-a",
            "unit": "percent",
            "value_type": MetricValueType.NUMBER,
            "aggregation": MetricAggregation.AVERAGE,
            "window_seconds": 300,
            "freshness_seconds": 60,
            "source_requirements": ("ci",),
        }
        data.update(overrides)
        return self.service.create_definition(
            MetricDefinitionCreate(**data),
            scope=self.scope,
            actor_id="admin",
        )

    def test_ingestion_validates_unit_type_source_and_is_idempotent(self) -> None:
        metric = self._definition()

        first = self.service.ingest(
            metric.id,
            MetricObservationCreate(
                value=90,
                source="ci",
                idempotency_key="ci:run-1",
            ),
            scope=self.scope,
            actor_id="collector",
        )
        retry = self.service.ingest(
            metric.id,
            MetricObservationCreate(
                value=90,
                source="ci",
                idempotency_key="ci:run-1",
            ),
            scope=self.scope,
            actor_id="collector",
        )
        self.assertEqual(retry.id, first.id)

        with self.assertRaises(MetricConflictError):
            self.service.ingest(
                metric.id,
                MetricObservationCreate(
                    value=91,
                    source="ci",
                    idempotency_key="ci:run-1",
                ),
                scope=self.scope,
                actor_id="collector",
            )

        with self.assertRaises(MetricValidationError):
            self.service.ingest(
                metric.id,
                MetricObservationCreate(
                    value=90,
                    unit="seconds",
                    source="ci",
                    idempotency_key="ci:bad-unit",
                ),
                scope=self.scope,
                actor_id="collector",
            )

        with self.assertRaises(MetricValidationError):
            self.service.ingest(
                metric.id,
                MetricObservationCreate(
                    value=90,
                    source="dashboard",
                    idempotency_key="dashboard:1",
                ),
                scope=self.scope,
                actor_id="collector",
            )

        integer_metric = self._definition(
            key="deploy_count",
            name="Deploy count",
            description="Count of deployments.",
            unit="count",
            value_type=MetricValueType.INTEGER,
            aggregation=MetricAggregation.SUM,
            source_requirements=(),
        )
        with self.assertRaises(MetricValidationError):
            self.service.ingest(
                integer_metric.id,
                MetricObservationCreate(
                    value=1.5,
                    source="ci",
                    idempotency_key="ci:float-count",
                ),
                scope=self.scope,
                actor_id="collector",
            )

    def test_window_aggregation_and_freshness_are_deterministic(self) -> None:
        metric = self._definition()
        now = time.time()
        for key, value, observed_at in (
            ("ci:1", 80, now - 40),
            ("ci:2", 100, now - 20),
        ):
            self.service.ingest(
                metric.id,
                MetricObservationCreate(
                    value=value,
                    observed_at=observed_at,
                    source="ci",
                    idempotency_key=key,
                ),
                scope=self.scope,
                actor_id="collector",
            )

        current = self.service.evaluate(metric.id, scope=self.scope, at=now)
        self.assertEqual(current.value, 90)
        self.assertEqual(current.freshness, MetricFreshness.FRESH)
        self.assertEqual(len(current.observation_ids), 2)

        stale = self.service.evaluate(metric.id, scope=self.scope, at=now + 100)
        self.assertEqual(stale.value, 90)
        self.assertEqual(stale.freshness, MetricFreshness.STALE)

        self.service.ingest(
            metric.id,
            MetricObservationCreate(
                value=95,
                observed_at=now + 105,
                source="ci",
                partial=True,
                idempotency_key="ci:partial",
            ),
            scope=self.scope,
            actor_id="collector",
        )
        partial = self.service.evaluate(metric.id, scope=self.scope, at=now + 110)
        self.assertEqual(partial.freshness, MetricFreshness.PARTIAL)

        missing_metric = self._definition(
            key="latency",
            name="Latency",
            description="Request latency.",
            unit="ms",
            aggregation=MetricAggregation.LAST,
            source_requirements=(),
        )
        missing = self.service.evaluate(
            missing_metric.id,
            scope=self.scope,
            at=now,
        )
        self.assertEqual(missing.freshness, MetricFreshness.MISSING)
        self.assertIsNone(missing.value)

    def test_definition_revision_requires_new_observation_before_current_value_returns(self) -> None:
        metric = self._definition(aggregation=MetricAggregation.LAST)
        now = time.time()
        old = self.service.ingest(
            metric.id,
            MetricObservationCreate(
                value=70,
                observed_at=now,
                source="ci",
                idempotency_key="ci:revision-1",
            ),
            scope=self.scope,
            actor_id="collector",
        )
        self.assertEqual(old.metric_revision, 1)

        updated = self.service.update_definition(
            metric.id,
            MetricDefinitionUpdate(
                description="Revised metric semantics.",
                reason="change metric definition",
            ),
            scope=self.scope,
            actor_id="admin",
        )
        self.assertEqual(updated.revision, 2)
        missing = self.service.evaluate(
            metric.id,
            scope=self.scope,
            at=now + 1,
        )
        self.assertEqual(missing.freshness, MetricFreshness.MISSING)
        self.assertIsNone(missing.value)
        self.assertEqual(missing.observation_ids, ())

        current = self.service.ingest(
            metric.id,
            MetricObservationCreate(
                value=75,
                observed_at=now + 2,
                source="ci",
                idempotency_key="ci:revision-2",
            ),
            scope=self.scope,
            actor_id="collector",
        )
        self.assertEqual(current.metric_revision, 2)
        evaluated = self.service.evaluate(
            metric.id,
            scope=self.scope,
            at=now + 3,
        )
        self.assertEqual(evaluated.value, 75)
        self.assertEqual(evaluated.observation_ids, (current.id,))

    def test_snapshot_can_pin_historical_as_of_freshness(self) -> None:
        metric = self._definition(
            aggregation=MetricAggregation.LAST,
            freshness_seconds=30,
        )
        observation = self.service.ingest(
            metric.id,
            MetricObservationCreate(
                value=88,
                observed_at=100.0,
                source="ci",
                idempotency_key="ci:historical",
            ),
            scope=self.scope,
            actor_id="collector",
        )
        snapshot = self.service.capture_snapshot(
            metric.id,
            MetricSnapshotRequest(
                window_start=90.0,
                window_end=100.0,
                evaluated_at=100.0,
            ),
            scope=self.scope,
            actor_id="decision-service",
        )
        self.assertEqual(snapshot.freshness, MetricFreshness.FRESH)
        self.assertEqual(snapshot.observation_ids, (observation.id,))
        self.assertEqual(snapshot.window_start, 90.0)
        self.assertEqual(snapshot.window_end, 100.0)
        self.assertEqual(snapshot.captured_at, 100.0)

    def test_snapshot_is_immutable_and_tenant_scoped(self) -> None:
        metric = self._definition(aggregation=MetricAggregation.LAST)
        now = time.time()
        first = self.service.ingest(
            metric.id,
            MetricObservationCreate(
                value=70,
                observed_at=now,
                source="ci",
                idempotency_key="ci:before",
            ),
            scope=self.scope,
            actor_id="collector",
        )
        snapshot = self.service.capture_snapshot(
            metric.id,
            MetricSnapshotRequest(),
            scope=self.scope,
            actor_id="decision-service",
        )
        self.assertEqual(snapshot.value, 70)
        self.assertEqual(snapshot.observation_ids, (first.id,))

        self.service.ingest(
            metric.id,
            MetricObservationCreate(
                value=99,
                observed_at=now + 10,
                source="ci",
                idempotency_key="ci:after",
            ),
            scope=self.scope,
            actor_id="collector",
        )
        current = self.service.evaluate(
            metric.id,
            scope=self.scope,
            at=now + 11,
        )
        persisted = self.service.get_snapshot(
            metric.id,
            snapshot.id,
            scope=self.scope,
        )
        self.assertEqual(current.value, 99)
        self.assertEqual(persisted.value, 70)
        self.assertEqual(persisted.observation_ids, (first.id,))

        with self.assertRaises(MetricNotFoundError):
            self.service.get_snapshot(
                metric.id,
                snapshot.id,
                scope=TenantScope(
                    organization_id="org-b",
                    workspace_id="ws-b",
                ),
            )

    def test_goal_metric_binding_supports_canonical_snapshot_provenance(self) -> None:
        metric = self._definition(aggregation=MetricAggregation.LAST)
        self.service.ingest(
            metric.id,
            MetricObservationCreate(
                value=96,
                source="ci",
                idempotency_key="ci:goal",
            ),
            scope=self.scope,
            actor_id="collector",
        )
        snapshot = self.service.capture_snapshot(
            metric.id,
            MetricSnapshotRequest(),
            scope=self.scope,
            actor_id="goal-service",
        )

        criterion = GoalSuccessCriterion(
            description="Deployment checks meet target",
            kind=GoalCriterionKind.METRIC,
            metric_id=metric.id,
            metric_snapshot_id=snapshot.id,
            metric_window_seconds=300,
            operator=GoalCriterionOperator.GTE,
            target_value=90,
            unit="percent",
        )
        self.assertEqual(criterion.metric_id, metric.id)
        self.assertEqual(criterion.metric_snapshot_id, snapshot.id)

        with self.assertRaises(ValueError):
            GoalSuccessCriterion(
                description="Invalid snapshot binding",
                kind=GoalCriterionKind.METRIC,
                metric_snapshot_id=snapshot.id,
                operator=GoalCriterionOperator.GTE,
                target_value=90,
            )


class MetricApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.service = MetricService(MetricStore(sqlite))
        self.actor = AuthenticationActor(
            identity_id="admin",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="org-a",
            workspace_id="ws-a",
            roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.PRIMARY,
        )
        app = FastAPI()

        @app.middleware("http")
        async def inject_actor(request: Request, call_next):
            request.state.identity_actor = self.actor
            return await call_next(request)

        app.include_router(build_metrics_router(self.service))
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        self.temp.cleanup()

    @staticmethod
    def _metric_body():
        return {
            "key": "availability",
            "name": "Availability",
            "description": "Observed service availability.",
            "owner_identity_id": "owner-a",
            "unit": "percent",
            "value_type": "number",
            "aggregation": "last",
            "freshness_seconds": 120,
            "source_requirements": ["monitor"],
        }

    def test_read_is_allowed_at_primary_but_mutation_requires_step_up(self) -> None:
        self.assertEqual(self.client.get("/api/metrics").status_code, 200)
        denied = self.client.post("/api/metrics", json=self._metric_body())
        self.assertEqual(denied.status_code, 403)
        self.assertIn("mfa", denied.json()["detail"].lower())

    def test_mfa_admin_can_define_ingest_query_and_snapshot(self) -> None:
        self.actor = self.actor.model_copy(
            update={"assurance": AuthenticationAssurance.MFA}
        )
        created = self.client.post("/api/metrics", json=self._metric_body())
        self.assertEqual(created.status_code, 200)
        metric_id = created.json()["item"]["id"]

        ingested = self.client.post(
            f"/api/metrics/{metric_id}/observations",
            json={
                "value": 99.95,
                "source": "monitor",
                "idempotency_key": "monitor:availability:1",
                "external_record_ref": "monitoring://availability/1",
            },
        )
        current = self.client.get(f"/api/metrics/{metric_id}/current")
        snapshot = self.client.post(
            f"/api/metrics/{metric_id}/snapshots",
            json={},
        )
        history = self.client.get(
            f"/api/metrics/{metric_id}/observations",
            params={"limit": 10},
        )

        self.assertEqual(ingested.status_code, 200)
        self.assertEqual(current.status_code, 200)
        self.assertEqual(current.json()["item"]["freshness"], "fresh")
        self.assertEqual(snapshot.status_code, 200)
        self.assertEqual(snapshot.json()["item"]["value"], 99.95)
        self.assertEqual(history.status_code, 200)
        self.assertEqual(history.json()["count"], 1)

    def test_metrics_admin_service_scope_supports_automation(self) -> None:
        self.actor = AuthenticationActor(
            identity_id="metrics-collector",
            principal_kind=PrincipalKind.SERVICE,
            organization_id="org-a",
            workspace_id="ws-a",
            assurance=AuthenticationAssurance.SERVICE_TOKEN,
            service_scopes=("metrics:admin",),
        )

        created = self.client.post("/api/metrics", json=self._metric_body())
        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json()["item"]["created_by"], "metrics-collector")


if __name__ == "__main__":
    unittest.main()
