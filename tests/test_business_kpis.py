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
    BUSINESS_KPI_TEMPLATES,
    BusinessKpiDefinitionCreate,
    BusinessKpiDefinitionUpdate,
    BusinessKpiDomain,
    BusinessKpiExpression,
    BusinessKpiExpressionKind,
    BusinessKpiMissingPolicy,
    BusinessKpiOperand,
    BusinessKpiOperandAggregation,
    BusinessKpiTarget,
)
from codex_web.data_governance import DataClassification
from codex_web.decisions import (
    DecisionCreate,
    DecisionEvidenceKind,
    DecisionOption,
    DecisionParticipant,
)
from codex_web.goals import GoalCreate
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.metrics import MetricDirection, MetricFreshness, MetricThresholdOperator
from codex_web.services.business_context import BusinessContextService
from codex_web.services.business_kpis import (
    BusinessKpiNotFoundError,
    BusinessKpiService,
    BusinessKpiValidationError,
)
from codex_web.services.data_governance import DataGovernanceService
from codex_web.services.decisions import DecisionService
from codex_web.services.goals import GoalService
from codex_web.services.metrics import MetricService
from codex_web.storage.business_context import BusinessContextStore
from codex_web.storage.business_kpis import BusinessKpiStore
from codex_web.storage.data_governance import DataGovernanceStore
from codex_web.storage.decisions import DecisionStore
from codex_web.storage.goals import GoalStore
from codex_web.storage.metrics import MetricStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class MutableClock:
    def __init__(self, value: float) -> None:
        self.value = float(value)

    def __call__(self) -> float:
        return self.value


def operand_expression(key: str) -> BusinessKpiExpression:
    return BusinessKpiExpression(
        kind=BusinessKpiExpressionKind.OPERAND,
        operand_key=key,
    )


def constant_expression(value: float) -> BusinessKpiExpression:
    return BusinessKpiExpression(
        kind=BusinessKpiExpressionKind.CONSTANT,
        constant=value,
    )


def binary_expression(
    kind: BusinessKpiExpressionKind,
    left: BusinessKpiExpression,
    right: BusinessKpiExpression,
) -> BusinessKpiExpression:
    return BusinessKpiExpression(kind=kind, left=left, right=right)


class BusinessKpiServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.clock = MutableClock(100.0)
        self.state = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.governance = DataGovernanceService(
            DataGovernanceStore(self.state)
        )
        self.context = BusinessContextService(
            BusinessContextStore(self.state),
            governance=self.governance,
            clock=self.clock,
        )
        for object_type in (
            "business_entity",
            "external_record_ref",
            "company_fact",
        ):
            self.governance.register_action_handler(
                object_type,
                self.context.governance_action_handler,
            )
        self.metrics = MetricService(MetricStore(self.state))
        self.goals = GoalService(
            GoalStore(self.state),
            projects=object(),
            work_graph=object(),
        )
        self.decisions = DecisionService(
            DecisionStore(self.state),
            approvals=None,
            metrics=self.metrics,
            artifact_evidence=None,
            canonical_events=None,
            goals=self.goals,
            clock=self.clock,
        )
        self.service = BusinessKpiService(
            BusinessKpiStore(self.state),
            self.metrics,
            self.context,
            goals=self.goals,
            decisions=self.decisions,
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

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    def entity(self, name: str, **facts: float):
        entity = self.context.create_entity(
            BusinessEntityCreate(
                entity_type=BusinessEntityType.CUSTOMER,
                name=name,
                classification=DataClassification.CONFIDENTIAL,
            ),
            actor=self.admin,
        )
        created = {}
        for key, value in facts.items():
            created[key] = self.context.create_fact(
                CompanyFactCreate(
                    business_entity_id=entity.id,
                    key=key,
                    value_type=FactValueType.NUMBER,
                    value=value,
                    unit="usd",
                    source=CompanyFactSource(
                        source=f"fixture.{key}",
                        provider="fixture",
                        authority=FactSourceAuthority.AUTHORITATIVE,
                        priority=10,
                    ),
                    quality=FactQuality.VERIFIED,
                    observed_at=self.clock.value,
                    freshness_seconds=3600,
                    classification=DataClassification.CONFIDENTIAL,
                ),
                actor=self.admin,
            )
        return entity, created

    def revenue_kpi(self, *, target: float | None = 150.0):
        return self.service.create(
            BusinessKpiDefinitionCreate(
                key="arr",
                name="Annual recurring revenue",
                description="Explicit recurring revenue sum.",
                owner_identity_id="finance-owner",
                domain=BusinessKpiDomain.REVENUE,
                template_key="recurring_revenue",
                unit="usd",
                currency="usd",
                direction=MetricDirection.HIGHER_IS_BETTER,
                freshness_seconds=3600,
                operands=(
                    BusinessKpiOperand(
                        key="revenue",
                        fact_key="arr",
                        aggregation=BusinessKpiOperandAggregation.SUM,
                        entity_types=(BusinessEntityType.CUSTOMER,),
                    ),
                ),
                expression=operand_expression("revenue"),
                target=(
                    BusinessKpiTarget(
                        label="ARR target",
                        operator=MetricThresholdOperator.GTE,
                        value=target,
                    )
                    if target is not None
                    else None
                ),
            ),
            actor=self.admin,
        )

    async def test_kpi_is_backed_by_canonical_metric_with_exact_fact_attribution(self) -> None:
        _a, _ = self.entity("A", arr=100.0)
        _b, _ = self.entity("B", arr=75.0)
        kpi = self.revenue_kpi(target=160.0)

        result = self.service.evaluate(kpi.id, actor=self.admin)

        self.assertEqual(result.value, 175.0)
        self.assertEqual(result.freshness, MetricFreshness.FRESH)
        self.assertEqual(result.target.passed, True)
        self.assertEqual(result.target.variance, 15.0)
        self.assertEqual(len(result.selected_fact_ids), 2)
        self.assertIsNotNone(result.observation_id)

        metric = self.metrics.get_definition(kpi.metric_id, scope=self.admin.tenant)
        self.assertEqual(metric.revision, 1)
        self.assertEqual(
            metric.source_requirements,
            (f"business-kpi:{kpi.id}:r1",),
        )
        observation = self.metrics.history(
            metric.id,
            scope=self.admin.tenant,
            limit=10,
        )[0]
        self.assertEqual(observation.id, result.observation_id)
        self.assertEqual(observation.metric_revision, 1)
        self.assertEqual(observation.source, f"business-kpi:{kpi.id}:r1")
        attribution = self.service.attribution(
            observation.id,
            actor=self.admin,
        )
        self.assertEqual(
            set(attribution.selected_fact_ids),
            set(result.selected_fact_ids),
        )
        self.assertEqual(attribution.kpi_revision, 1)
        self.assertEqual(attribution.metric_revision, 1)

    async def test_gross_margin_formula_is_deterministic_and_model_free(self) -> None:
        self.entity("A", revenue=100.0, direct_cost=40.0)
        self.entity("B", revenue=200.0, direct_cost=80.0)
        kpi = self.service.create(
            BusinessKpiDefinitionCreate(
                key="gross_margin",
                name="Gross margin",
                description="(revenue - direct cost) / revenue * 100",
                owner_identity_id="cfo",
                domain=BusinessKpiDomain.FINANCE,
                template_key="gross_margin",
                unit="percent",
                direction=MetricDirection.HIGHER_IS_BETTER,
                operands=(
                    BusinessKpiOperand(
                        key="revenue",
                        fact_key="revenue",
                        aggregation=BusinessKpiOperandAggregation.SUM,
                        entity_types=(BusinessEntityType.CUSTOMER,),
                    ),
                    BusinessKpiOperand(
                        key="cost",
                        fact_key="direct_cost",
                        aggregation=BusinessKpiOperandAggregation.SUM,
                        entity_types=(BusinessEntityType.CUSTOMER,),
                    ),
                ),
                expression=binary_expression(
                    BusinessKpiExpressionKind.MULTIPLY,
                    binary_expression(
                        BusinessKpiExpressionKind.DIVIDE,
                        binary_expression(
                            BusinessKpiExpressionKind.SUBTRACT,
                            operand_expression("revenue"),
                            operand_expression("cost"),
                        ),
                        operand_expression("revenue"),
                    ),
                    constant_expression(100.0),
                ),
            ),
            actor=self.admin,
        )

        result = self.service.evaluate(kpi.id, actor=self.admin)
        self.assertAlmostEqual(result.value, 60.0)
        self.assertEqual(result.freshness, MetricFreshness.FRESH)

    async def test_formula_revision_always_advances_underlying_metric_revision(self) -> None:
        self.entity("A", arr=100.0)
        kpi = self.revenue_kpi()
        metric_before = self.metrics.get_definition(
            kpi.metric_id,
            scope=self.admin.tenant,
        )

        updated = self.service.update(
            kpi.id,
            BusinessKpiDefinitionUpdate(
                expression=binary_expression(
                    BusinessKpiExpressionKind.MULTIPLY,
                    operand_expression("revenue"),
                    constant_expression(12.0),
                ),
                reason="switch from monthly to annualized formula",
            ),
            actor=self.admin,
        )
        metric_after = self.metrics.get_definition(
            kpi.metric_id,
            scope=self.admin.tenant,
        )

        self.assertEqual(updated.revision, 2)
        self.assertEqual(metric_before.revision, 1)
        self.assertEqual(metric_after.revision, 2)
        self.assertEqual(
            metric_after.source_requirements,
            (f"business-kpi:{kpi.id}:r2",),
        )
        result = self.service.evaluate(kpi.id, actor=self.admin)
        self.assertEqual(result.metric_revision, 2)
        self.assertEqual(result.value, 1200.0)

    async def test_conflicted_sources_are_partial_and_do_not_publish_normal_trend(self) -> None:
        entity, facts = self.entity("A", arr=100.0)
        kpi = self.revenue_kpi()
        first = self.service.evaluate(kpi.id, actor=self.admin)
        self.assertEqual(first.freshness, MetricFreshness.FRESH)

        self.clock.value = 110.0
        self.context.create_fact(
            CompanyFactCreate(
                business_entity_id=entity.id,
                key="arr",
                value_type=FactValueType.NUMBER,
                value=120.0,
                unit="usd",
                source=CompanyFactSource(
                    source="secondary.arr",
                    provider="secondary",
                    authority=FactSourceAuthority.SECONDARY,
                ),
                observed_at=self.clock.value,
                freshness_seconds=3600,
            ),
            actor=self.admin,
        )
        second = self.service.evaluate(kpi.id, actor=self.admin)

        self.assertEqual(second.freshness, MetricFreshness.PARTIAL)
        self.assertTrue(second.operand_attributions[0].conflict_fact_ids)
        self.assertIsNone(second.trend.absolute_delta)
        observation = self.metrics.history(
            kpi.metric_id,
            scope=self.admin.tenant,
            limit=1,
        )[0]
        self.assertTrue(observation.partial)

    async def test_stale_required_operand_blocks_number_instead_of_appearing_current(self) -> None:
        entity = self.context.create_entity(
            BusinessEntityCreate(
                entity_type=BusinessEntityType.CUSTOMER,
                name="Stale customer",
            ),
            actor=self.admin,
        )
        self.context.create_fact(
            CompanyFactCreate(
                business_entity_id=entity.id,
                key="arr",
                value_type=FactValueType.NUMBER,
                value=100.0,
                source=CompanyFactSource(source="fixture.arr"),
                observed_at=10.0,
                freshness_seconds=10,
            ),
            actor=self.admin,
        )
        kpi = self.revenue_kpi()

        result = self.service.evaluate(kpi.id, actor=self.admin, at=100.0)
        self.assertIsNone(result.value)
        self.assertIsNone(result.observation_id)
        self.assertEqual(result.freshness, MetricFreshness.STALE)
        self.assertEqual(
            self.metrics.history(kpi.metric_id, scope=self.admin.tenant),
            (),
        )

    async def test_trend_delta_uses_previous_complete_metric_observation(self) -> None:
        entity, facts = self.entity("A", arr=100.0)
        kpi = self.revenue_kpi()
        first = self.service.evaluate(kpi.id, actor=self.admin)
        self.clock.value = 120.0
        self.context.supersede_fact(
            facts["arr"].id,
            CompanyFactCreate(
                business_entity_id=entity.id,
                key="arr",
                value_type=FactValueType.NUMBER,
                value=125.0,
                unit="usd",
                source=CompanyFactSource(
                    source="fixture.arr",
                    authority=FactSourceAuthority.AUTHORITATIVE,
                    priority=10,
                ),
                observed_at=120.0,
                freshness_seconds=3600,
            ),
            actor=self.admin,
        )
        second = self.service.evaluate(kpi.id, actor=self.admin)

        self.assertEqual(first.value, 100.0)
        self.assertEqual(second.value, 125.0)
        self.assertEqual(second.trend.previous_observation_id, first.observation_id)
        self.assertEqual(second.trend.absolute_delta, 25.0)
        self.assertEqual(second.trend.percent_delta, 25.0)

    async def test_goal_binding_persists_exact_metric_snapshot_and_kpi_revision(self) -> None:
        self.entity("A", arr=100.0)
        kpi = self.revenue_kpi(target=90.0)
        goal = self.goals.create(
            GoalCreate(
                title="Grow ARR",
                description="Measured by canonical ARR KPI.",
                owner_identity_id="owner",
            ),
            scope=self.admin.tenant,
            actor_id=self.admin.identity_id,
        )

        bound = self.service.bind_goal(
            kpi.id,
            goal.id,
            actor=self.admin,
        )
        criterion = next(
            item
            for item in bound.success_criteria
            if item.id == f"goal-business-kpi-{kpi.id}"
        )
        self.assertEqual(criterion.metric_id, kpi.metric_id)
        self.assertIsNotNone(criterion.metric_snapshot_id)
        snapshot = self.metrics.get_snapshot(
            kpi.metric_id,
            criterion.metric_snapshot_id,
            scope=self.admin.tenant,
        )
        attribution = self.service.attribution(
            snapshot.observation_ids[0],
            actor=self.admin,
        )
        self.assertEqual(snapshot.metric_revision, 1)
        self.assertEqual(attribution.kpi_revision, 1)
        self.assertEqual(criterion.target_value, 90.0)

    async def test_decision_binding_uses_exact_metric_snapshot_evidence(self) -> None:
        self.entity("A", arr=100.0)
        kpi = self.revenue_kpi()
        decision = await self.decisions.create(
            DecisionCreate(
                title="Revenue plan",
                question="Which plan should we follow?",
                participants=(
                    DecisionParticipant(
                        id="finance",
                        role="CFO",
                        perspective="Financial outcomes",
                    ),
                ),
                options=(
                    DecisionOption(
                        id="a",
                        title="Plan A",
                        description="Conservative",
                    ),
                    DecisionOption(
                        id="b",
                        title="Plan B",
                        description="Aggressive",
                    ),
                ),
            ),
            actor=self.admin,
        )

        bound = await self.service.bind_decision(
            kpi.id,
            decision.id,
            actor=self.admin,
            summary="ARR operating state",
        )
        evidence = next(
            item
            for item in bound.evidence
            if item.kind == DecisionEvidenceKind.METRIC_SNAPSHOT
        )
        self.assertEqual(evidence.metric_id, kpi.metric_id)
        self.assertEqual(evidence.metric_revision, 1)
        self.assertEqual(evidence.metric_freshness, MetricFreshness.FRESH)
        self.assertEqual(evidence.observed_value, 100.0)
        self.assertEqual(len(evidence.observation_ids), 1)
        attribution = self.service.attribution(
            evidence.observation_ids[0],
            actor=self.admin,
        )
        self.assertEqual(attribution.kpi_revision, 1)

    async def test_operating_snapshot_contains_exact_provenance_and_bound_domain_links(self) -> None:
        self.entity("A", arr=100.0)
        kpi = self.revenue_kpi(target=90.0)
        goal = self.goals.create(
            GoalCreate(
                title="ARR goal",
                description="ARR goal",
                owner_identity_id="owner",
            ),
            scope=self.admin.tenant,
            actor_id=self.admin.identity_id,
        )
        self.service.bind_goal(kpi.id, goal.id, actor=self.admin)
        decision = await self.decisions.create(
            DecisionCreate(
                title="ARR decision",
                question="Do we invest?",
                participants=(
                    DecisionParticipant(
                        id="finance",
                        role="CFO",
                        perspective="Finance",
                    ),
                ),
                options=(
                    DecisionOption(id="a", title="A", description="A"),
                    DecisionOption(id="b", title="B", description="B"),
                ),
            ),
            actor=self.admin,
        )
        await self.service.bind_decision(
            kpi.id,
            decision.id,
            actor=self.admin,
        )

        operating = self.service.operating_snapshot(actor=self.admin)
        self.assertEqual(len(operating.items), 1)
        item = operating.items[0]
        self.assertEqual(item.kpi_revision, 1)
        self.assertEqual(item.metric_revision, 1)
        self.assertIsNotNone(item.metric_snapshot_id)
        self.assertEqual(item.goal_ids, (goal.id,))
        self.assertEqual(item.decision_ids, (decision.id,))
        self.assertEqual(len(item.selected_fact_ids), 1)
        self.assertEqual(
            self.service.operating_snapshots(actor=self.admin, limit=1)[0].id,
            operating.id,
        )

    async def test_templates_are_guidance_not_hidden_formulas(self) -> None:
        keys = {item.key for item in BUSINESS_KPI_TEMPLATES}
        self.assertIn("recurring_revenue", keys)
        self.assertIn("gross_margin", keys)
        self.assertIn("campaign_performance", keys)
        for template in BUSINESS_KPI_TEMPLATES:
            self.assertTrue(template.formula_guidance)
            self.assertFalse(hasattr(template, "expression"))

    async def test_cross_tenant_kpis_are_invisible(self) -> None:
        self.entity("A", arr=100.0)
        kpi = self.revenue_kpi()
        self.assertEqual(self.service.list(actor=self.other), ())
        with self.assertRaises(BusinessKpiNotFoundError):
            self.service.get(kpi.id, actor=self.other)


class BusinessKpiApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        state = SQLiteStateStore(Path(self.temp.name) / "api.sqlite3")
        governance = DataGovernanceService(DataGovernanceStore(state))
        context = BusinessContextService(
            BusinessContextStore(state),
            governance=governance,
        )
        for object_type in (
            "business_entity",
            "external_record_ref",
            "company_fact",
        ):
            governance.register_action_handler(
                object_type,
                context.governance_action_handler,
            )
        self.metrics = MetricService(MetricStore(state))
        self.service = BusinessKpiService(
            BusinessKpiStore(state),
            self.metrics,
            context,
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

        app.include_router(build_business_kpis_router(self.service))
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        self.temp.cleanup()

    @staticmethod
    def body():
        return {
            "key": "arr",
            "name": "ARR",
            "description": "Recurring revenue",
            "owner_identity_id": "finance",
            "domain": "revenue",
            "template_key": "recurring_revenue",
            "unit": "usd",
            "currency": "USD",
            "direction": "higher_is_better",
            "operands": [
                {
                    "key": "revenue",
                    "fact_key": "arr",
                    "aggregation": "sum",
                    "entity_types": ["customer"],
                }
            ],
            "expression": {
                "kind": "operand",
                "operand_key": "revenue",
            },
        }

    def test_reads_are_available_but_configuration_requires_mfa(self) -> None:
        self.assertEqual(
            self.client.get("/api/business-kpis/templates").status_code,
            200,
        )
        self.assertEqual(
            self.client.get("/api/business-kpis").status_code,
            200,
        )
        denied = self.client.post("/api/business-kpis", json=self.body())
        self.assertEqual(denied.status_code, 403)
        self.assertIn("mfa", denied.json()["detail"].lower())

    def test_business_kpi_admin_service_scope_can_create(self) -> None:
        self.actor = AuthenticationActor(
            identity_id="kpi-sync",
            principal_kind=PrincipalKind.SERVICE,
            organization_id="org-a",
            workspace_id="ws-a",
            assurance=AuthenticationAssurance.SERVICE_TOKEN,
            service_scopes=("business-kpi:admin",),
        )
        created = self.client.post("/api/business-kpis", json=self.body())
        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json()["item"]["created_by"], "kpi-sync")

        self.actor = self.actor.model_copy(
            update={"service_scopes": ("business-kpi:read",)}
        )
        denied = self.client.post(
            "/api/business-kpis",
            json={**self.body(), "key": "arr-2"},
        )
        self.assertEqual(denied.status_code, 403)


if __name__ == "__main__":
    unittest.main()
