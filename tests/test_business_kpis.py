from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from codex_web.api.business_kpis import build_business_kpis_router
from codex_web.business_context import (
    BusinessEntityCreate,
    BusinessEntityType,
    CompanyFactCreate,
    CompanyFactSource,
    FactQuality,
    FactSourceAuthority,
    FactValueType,
)
from codex_web.business_kpis import (
    BusinessKPIDefinitionCreate,
    BusinessKPIDefinitionUpdate,
    BusinessKPIDomain,
    BusinessKPIFormula,
    BusinessKPIFormulaKind,
    BusinessKPIReadiness,
    BusinessKPIFactTerm,
    BusinessKPITargetBindingCreate,
    BusinessKPITargetKind,
    BusinessKPITermAggregation,
)
from codex_web.data_governance import DataClassification
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.metrics import (
    MetricDirection,
    MetricFreshness,
    MetricThreshold,
    MetricThresholdOperator,
)
from codex_web.services.business_context import BusinessContextService
from codex_web.services.business_kpis import (
    BusinessKPINotFoundError,
    BusinessKPIService,
)
from codex_web.services.data_governance import DataGovernanceService
from codex_web.services.metrics import MetricService
from codex_web.storage.business_context import BusinessContextStore
from codex_web.storage.business_kpis import BusinessKPIStore
from codex_web.storage.data_governance import DataGovernanceStore
from codex_web.storage.metrics import MetricStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class MutableClock:
    def __init__(self, value: float) -> None:
        self.value = float(value)

    def __call__(self) -> float:
        return self.value


class FakeGoals:
    def __init__(self) -> None:
        self.ids = {"goal-growth"}

    def get(self, goal_id, *, scope):
        if goal_id not in self.ids:
            raise ValueError("goal not found")
        return type("Goal", (), {"id": goal_id})()


class FakeDecisions:
    def __init__(self) -> None:
        self.ids = {"decision-budget"}

    def get(self, decision_id, *, actor):
        if decision_id not in self.ids:
            raise ValueError("decision not found")
        return type("Decision", (), {"id": decision_id})()


class BusinessKPITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.state = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.clock = MutableClock(100.0)
        governance = DataGovernanceService(DataGovernanceStore(self.state))
        self.context = BusinessContextService(
            BusinessContextStore(self.state),
            governance=governance,
            clock=self.clock,
        )
        for object_type in (
            "business_entity",
            "external_record_ref",
            "company_fact",
        ):
            governance.register_action_handler(
                object_type,
                self.context.governance_action_handler,
            )
        self.metrics = MetricService(MetricStore(self.state))
        self.goals = FakeGoals()
        self.decisions = FakeDecisions()
        self.service = BusinessKPIService(
            BusinessKPIStore(self.state),
            self.metrics,
            self.context,
            self.goals,
            self.decisions,
            clock=self.clock,
        )
        self.admin = AuthenticationActor(
            identity_id="admin-a",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="org-a",
            workspace_id="ws-a",
            roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.MFA,
        )
        self.other = AuthenticationActor(
            identity_id="reader-b",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="org-b",
            workspace_id="ws-b",
            roles=(MembershipRole.MEMBER,),
            assurance=AuthenticationAssurance.PRIMARY,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def entity(self, name: str):
        return self.context.create_entity(
            BusinessEntityCreate(
                entity_type=BusinessEntityType.CUSTOMER,
                name=name,
                classification=DataClassification.CONFIDENTIAL,
            ),
            actor=self.admin,
        )

    def fact(
        self,
        entity_id: str,
        *,
        key: str,
        value: float,
        observed_at: float = 100.0,
        freshness_seconds: int = 3600,
        authority: FactSourceAuthority = FactSourceAuthority.AUTHORITATIVE,
        priority: int = 10,
        provider: str = "crm",
    ):
        return self.context.create_fact(
            CompanyFactCreate(
                business_entity_id=entity_id,
                key=key,
                value_type=FactValueType.NUMBER,
                value=value,
                unit="usd",
                source=CompanyFactSource(
                    source=f"{provider}.{key}",
                    provider=provider,
                    authority=authority,
                    priority=priority,
                    source_observed_at=observed_at,
                ),
                quality=FactQuality.VERIFIED,
                observed_at=observed_at,
                freshness_seconds=freshness_seconds,
                classification=DataClassification.CONFIDENTIAL,
            ),
            actor=self.admin,
        )

    @staticmethod
    def aggregate_formula(
        fact_key: str = "arr",
        *,
        scale: float = 1.0,
    ) -> BusinessKPIFormula:
        return BusinessKPIFormula(
            kind=BusinessKPIFormulaKind.AGGREGATE,
            terms=(
                BusinessKPIFactTerm(
                    alias="value",
                    fact_key=fact_key,
                    aggregation=BusinessKPITermAggregation.SUM,
                    entity_type=BusinessEntityType.CUSTOMER,
                ),
            ),
            left_alias="value",
            scale=scale,
        )

    def create_kpi(
        self,
        *,
        key: str = "arr",
        formula: BusinessKPIFormula | None = None,
    ):
        return self.service.create(
            BusinessKPIDefinitionCreate(
                key=key,
                name=key.upper(),
                description=f"Explicit {key} business KPI.",
                domain=BusinessKPIDomain.REVENUE,
                owner_identity_id="finance-owner",
                unit="usd",
                freshness_seconds=120,
                direction=MetricDirection.HIGHER_IS_BETTER,
                thresholds=(
                    MetricThreshold(
                        label="target",
                        operator=MetricThresholdOperator.GTE,
                        value=250,
                    ),
                ),
                formula=formula or self.aggregate_formula(),
                currency="usd",
            ),
            actor=self.admin,
        )

    def test_refresh_projects_fact_formula_into_canonical_metric_and_operating_view(self) -> None:
        a = self.entity("A")
        b = self.entity("B")
        self.fact(a.id, key="arr", value=100)
        self.fact(b.id, key="arr", value=200)
        kpi = self.create_kpi()

        refresh = self.service.refresh(kpi.id, actor=self.admin, at=100.0)
        self.assertEqual(refresh.value, 300.0)
        self.assertFalse(refresh.partial)
        self.assertIsNotNone(refresh.observation_id)

        current = self.metrics.evaluate(
            kpi.metric_id,
            scope=self.admin.tenant,
            at=100.0,
        )
        self.assertEqual(current.value, 300.0)
        self.assertEqual(current.freshness, MetricFreshness.FRESH)
        self.assertEqual(current.metric_revision, kpi.metric_revision)

        view = self.service.operating_view(actor=self.admin, at=100.0)
        self.assertTrue(view.current)
        item = view.items[0]
        self.assertEqual(item.readiness, BusinessKPIReadiness.CURRENT)
        self.assertEqual(item.value, 300.0)
        self.assertEqual(item.currency, "USD")
        self.assertEqual(item.thresholds[0].state.value, "met")
        self.assertEqual(item.thresholds[0].variance, 50.0)
        self.assertEqual(item.fact_keys, ("arr",))

    def test_trend_is_deterministic_from_current_metric_revision(self) -> None:
        entity = self.entity("A")
        first = self.fact(entity.id, key="arr", value=100)
        kpi = self.create_kpi()
        self.service.refresh(kpi.id, actor=self.admin, at=100.0)

        self.clock.value = 110.0
        self.context.supersede_fact(
            first.id,
            CompanyFactCreate(
                business_entity_id=entity.id,
                key="arr",
                value_type=FactValueType.NUMBER,
                value=125,
                unit="usd",
                source=CompanyFactSource(
                    source="crm.arr",
                    provider="crm",
                    authority=FactSourceAuthority.AUTHORITATIVE,
                    priority=10,
                    source_observed_at=110.0,
                ),
                quality=FactQuality.VERIFIED,
                observed_at=110.0,
                freshness_seconds=3600,
                classification=DataClassification.CONFIDENTIAL,
            ),
            actor=self.admin,
        )
        self.service.refresh(kpi.id, actor=self.admin, at=110.0)

        item = self.service.operating_view(
            actor=self.admin,
            at=110.0,
        ).items[0]
        self.assertEqual(item.value, 125.0)
        self.assertEqual(item.trend_delta, 25.0)
        self.assertEqual(item.trend_percent, 25.0)

    def test_stale_or_conflicting_company_facts_make_kpi_partial(self) -> None:
        stale_entity = self.entity("Stale")
        conflict_entity = self.entity("Conflict")
        self.fact(
            stale_entity.id,
            key="arr",
            value=100,
            observed_at=0,
            freshness_seconds=10,
        )
        self.fact(
            conflict_entity.id,
            key="arr",
            value=200,
            authority=FactSourceAuthority.AUTHORITATIVE,
            provider="crm",
        )
        self.fact(
            conflict_entity.id,
            key="arr",
            value=180,
            authority=FactSourceAuthority.SECONDARY,
            provider="billing",
        )
        kpi = self.create_kpi()

        refresh = self.service.refresh(kpi.id, actor=self.admin, at=100.0)
        self.assertEqual(refresh.value, 200.0)
        self.assertTrue(refresh.partial)
        self.assertTrue(
            any("stale" in finding for finding in refresh.findings)
        )
        self.assertTrue(
            any("conflicting provider values" in finding for finding in refresh.findings)
        )
        view = self.service.operating_view(actor=self.admin, at=100.0)
        self.assertFalse(view.current)
        self.assertEqual(
            view.items[0].readiness,
            BusinessKPIReadiness.PARTIAL,
        )
        self.assertEqual(
            view.items[0].thresholds[0].state.value,
            "unknown",
        )

    def test_ratio_formula_is_explicit_and_model_free(self) -> None:
        a = self.entity("A")
        b = self.entity("B")
        self.fact(a.id, key="won", value=1)
        self.fact(b.id, key="won", value=1)
        self.fact(a.id, key="opportunity", value=1)
        self.fact(b.id, key="opportunity", value=3)
        formula = BusinessKPIFormula(
            kind=BusinessKPIFormulaKind.RATIO,
            terms=(
                BusinessKPIFactTerm(
                    alias="won",
                    fact_key="won",
                    aggregation=BusinessKPITermAggregation.SUM,
                    entity_type=BusinessEntityType.CUSTOMER,
                ),
                BusinessKPIFactTerm(
                    alias="all",
                    fact_key="opportunity",
                    aggregation=BusinessKPITermAggregation.SUM,
                    entity_type=BusinessEntityType.CUSTOMER,
                ),
            ),
            left_alias="won",
            right_alias="all",
            scale=100,
        )
        kpi = self.create_kpi(key="conversion", formula=formula)
        refresh = self.service.refresh(kpi.id, actor=self.admin, at=100.0)
        self.assertEqual(refresh.value, 50.0)
        self.assertFalse(refresh.partial)

    def test_formula_revision_invalidates_old_metric_observation_until_refresh(self) -> None:
        entity = self.entity("A")
        self.fact(entity.id, key="arr", value=100)
        kpi = self.create_kpi()
        first = self.service.refresh(kpi.id, actor=self.admin, at=100.0)
        self.assertIsNotNone(first.observation_id)

        revised = self.service.update(
            kpi.id,
            BusinessKPIDefinitionUpdate(
                formula=self.aggregate_formula(scale=2.0),
                reason="switch to explicit doubled test formula",
            ),
            actor=self.admin,
        )
        self.assertEqual(revised.revision, 2)
        self.assertEqual(revised.metric_revision, 2)

        before_refresh = self.service.operating_view(
            actor=self.admin,
            at=101.0,
        ).items[0]
        self.assertEqual(before_refresh.readiness, BusinessKPIReadiness.MISSING)
        self.assertIsNone(before_refresh.value)

        updated = self.service.refresh(
            revised.id,
            actor=self.admin,
            at=101.0,
        )
        self.assertEqual(updated.value, 200.0)
        after_refresh = self.service.operating_view(
            actor=self.admin,
            at=101.0,
        ).items[0]
        self.assertEqual(after_refresh.readiness, BusinessKPIReadiness.CURRENT)
        self.assertEqual(after_refresh.metric_revision, 2)
        self.assertEqual(after_refresh.value, 200.0)
        self.assertEqual(len(self.service.revisions(kpi.id, actor=self.admin)), 2)

    def test_goal_decision_bindings_and_operating_snapshot_pin_exact_metric_state(self) -> None:
        entity = self.entity("A")
        self.fact(entity.id, key="arr", value=300)
        kpi = self.create_kpi()
        refresh = self.service.refresh(kpi.id, actor=self.admin, at=100.0)

        goal_binding = self.service.bind(
            BusinessKPITargetBindingCreate(
                kpi_id=kpi.id,
                target_kind=BusinessKPITargetKind.GOAL,
                target_id="goal-growth",
                purpose="Growth target",
            ),
            actor=self.admin,
        )
        decision_binding = self.service.bind(
            BusinessKPITargetBindingCreate(
                kpi_id=kpi.id,
                target_kind=BusinessKPITargetKind.DECISION,
                target_id="decision-budget",
                purpose="Budget evidence",
            ),
            actor=self.admin,
        )
        self.assertNotEqual(goal_binding.id, decision_binding.id)

        snapshot = self.service.capture_operating_snapshot(actor=self.admin)
        self.assertEqual(len(snapshot.items), 1)
        item = snapshot.items[0]
        self.assertEqual(item.kpi_revision, 1)
        self.assertEqual(item.metric_revision, 1)
        self.assertEqual(item.goal_ids, ("goal-growth",))
        self.assertEqual(item.decision_ids, ("decision-budget",))
        self.assertEqual(item.observation_ids, (refresh.observation_id,))
        self.assertTrue(item.metric_snapshot_id.startswith("metric-snapshot-"))

        goal_rows = self.service.target_snapshot(
            BusinessKPITargetKind.GOAL,
            "goal-growth",
            actor=self.admin,
        )
        decision_rows = self.service.target_snapshot(
            BusinessKPITargetKind.DECISION,
            "decision-budget",
            actor=self.admin,
        )
        self.assertEqual(goal_rows[0].metric_snapshot_id, item.metric_snapshot_id)
        self.assertEqual(decision_rows[0].metric_snapshot_id, item.metric_snapshot_id)

    def test_cross_tenant_business_kpi_state_is_isolated(self) -> None:
        entity = self.entity("A")
        self.fact(entity.id, key="arr", value=100)
        kpi = self.create_kpi()
        self.assertEqual(self.service.list(actor=self.other), ())
        with self.assertRaises(BusinessKPINotFoundError):
            self.service.get(kpi.id, actor=self.other)


class BusinessKPIApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        state = SQLiteStateStore(Path(self.temp.name) / "api.sqlite3")
        governance = DataGovernanceService(DataGovernanceStore(state))
        context = BusinessContextService(
            BusinessContextStore(state),
            governance=governance,
        )
        metrics = MetricService(MetricStore(state))
        service = BusinessKPIService(
            BusinessKPIStore(state),
            metrics,
            context,
            FakeGoals(),
            FakeDecisions(),
        )
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

        app.include_router(build_business_kpis_router(service))
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        self.temp.cleanup()

    @staticmethod
    def body():
        return {
            "key": "arr",
            "name": "ARR",
            "description": "Explicit recurring revenue KPI.",
            "domain": "revenue",
            "owner_identity_id": "finance-owner",
            "unit": "usd",
            "freshness_seconds": 3600,
            "direction": "higher_is_better",
            "currency": "USD",
            "formula": {
                "kind": "aggregate",
                "terms": [
                    {
                        "alias": "arr",
                        "fact_key": "arr",
                        "aggregation": "sum",
                        "entity_type": "customer"
                    }
                ],
                "left_alias": "arr",
                "scale": 1
            }
        }

    def test_read_operating_view_is_available_but_configuration_requires_mfa(self) -> None:
        view = self.client.get("/api/business-kpis/operating-view")
        denied = self.client.post(
            "/api/business-kpis",
            json=self.body(),
        )
        self.assertEqual(view.status_code, 200)
        self.assertEqual(denied.status_code, 403)
        self.assertIn("mfa", denied.json()["detail"].lower())

    def test_business_kpi_admin_service_scope_can_configure(self) -> None:
        self.actor = AuthenticationActor(
            identity_id="kpi-sync",
            principal_kind=PrincipalKind.SERVICE,
            organization_id="org-a",
            workspace_id="ws-a",
            assurance=AuthenticationAssurance.SERVICE_TOKEN,
            service_scopes=("business-kpis:admin",),
        )
        created = self.client.post(
            "/api/business-kpis",
            json=self.body(),
        )
        self.assertEqual(created.status_code, 200)
        item = created.json()["item"]
        self.assertEqual(item["metric_revision"], 1)
        self.assertTrue(item["metric_id"].startswith("metric-"))

        listed = self.client.get("/api/business-kpis")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["count"], 1)


if __name__ == "__main__":
    unittest.main()
