from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from codex_web.authority import AuthorityDecision, AuthorityDecisionOutcome
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
    BusinessKPIDomain,
    BusinessKPIFormula,
    BusinessKPIFormulaKind,
    BusinessKPIFactTerm,
    BusinessKPITargetBindingCreate,
    BusinessKPITargetKind,
    BusinessKPITermAggregation,
)
from codex_web.data_governance import DataClassification
from codex_web.executive_roles import (
    ExecutiveActivationCreate,
    ExecutiveActivationStatus,
    ExecutiveProposalStatus,
    ExecutiveReasoningBudget,
)
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.metrics import MetricDirection
from codex_web.services.business_context import BusinessContextService
from codex_web.services.business_kpis import BusinessKPIService
from codex_web.services.data_governance import DataGovernanceService
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.services.executive_management import (
    ExecutiveAuthorityError,
    ExecutiveConsultationError,
    ExecutiveManagementService,
)
from codex_web.services.executive_roles import install_executive_role_definitions
from codex_web.services.metrics import MetricService
from codex_web.storage.business_context import BusinessContextStore
from codex_web.storage.business_kpis import BusinessKPIStore
from codex_web.storage.data_governance import DataGovernanceStore
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.executive_activations import ExecutiveActivationStore
from codex_web.storage.metrics import MetricStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class MutableClock:
    def __init__(self, value: float) -> None:
        self.value = float(value)

    def __call__(self) -> float:
        return self.value


class FakeGoals:
    def __init__(self) -> None:
        self.ids = {"goal-revenue"}
        self.items = []
        self.created = []

    def get(self, goal_id, *, scope):
        if goal_id not in self.ids:
            raise ValueError("goal not found")
        return SimpleNamespace(id=goal_id)

    def snapshot(self, goal_id, *, scope):
        self.get(goal_id, scope=scope)
        return SimpleNamespace(
            model_dump=lambda mode=None: {
                "goal": {
                    "id": goal_id,
                    "title": "Grow recurring revenue",
                    "revision": 4,
                }
            }
        )

    def list(self, *, scope):
        return tuple(self.items)

    def create(
        self,
        payload,
        *,
        scope,
        actor_id,
        originating_executive_activation_id=None,
        originating_executive_proposal_id=None,
    ):
        self.created.append(payload)
        item = SimpleNamespace(
            id="goal-materialized",
            originating_executive_activation_id=originating_executive_activation_id,
            originating_executive_proposal_id=originating_executive_proposal_id,
        )
        self.items.append(item)
        return item


class FakeDecisions:
    def __init__(self) -> None:
        self.ids = {"decision-budget"}
        self.items = []
        self.created = []

    def get(self, decision_id, *, actor):
        if decision_id not in self.ids:
            raise ValueError("decision not found")
        return SimpleNamespace(
            id=decision_id,
            model_dump=lambda mode=None: {
                "id": decision_id,
                "title": "Budget decision",
                "revision": 3,
            },
        )

    def list(self, *, actor):
        return tuple(self.items)

    async def create(
        self,
        payload,
        *,
        actor,
        originating_executive_activation_id=None,
        originating_executive_proposal_id=None,
    ):
        self.created.append(payload)
        item = SimpleNamespace(
            id="decision-materialized",
            originating_executive_activation_id=originating_executive_activation_id,
            originating_executive_proposal_id=originating_executive_proposal_id,
        )
        self.items.append(item)
        return item


class FakeDecisionWork:
    def __init__(self) -> None:
        self.calls = []

    async def commit(self, decision_id, payload, *, actor):
        self.calls.append((decision_id, payload, actor))
        raise AssertionError("business Executive test must not bypass Decision approval/work gates")


class FakeWorkItems:
    state_machine = SimpleNamespace()

    def __init__(self) -> None:
        self.state_machine._work_item_state = lambda ref: (_ for _ in ()).throw(
            AssertionError(f"unexpected Work Item lookup: {ref}")
        )


class FakeWorkGraph:
    def snapshot(self, project_id, *, scope):
        raise AssertionError(f"unexpected WorkGraph lookup: {project_id}")


class FakeEvidence:
    def get_evidence(self, evidence_id, *, actor):
        raise AssertionError(f"unexpected Evidence lookup: {evidence_id}")


class FakeAuthority:
    def __init__(self, allow: bool = True) -> None:
        self.allow = allow
        self.requests = []

    def evaluate(self, request, *, actor):
        self.requests.append(request)
        return AuthorityDecision(
            outcome=(
                AuthorityDecisionOutcome.ALLOW
                if self.allow
                else AuthorityDecisionOutcome.DENY
            ),
            actor_identity_id=actor.identity_id,
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            request=request,
            reasons=(
                ("test authority allows",)
                if self.allow
                else ("test authority denies",)
            ),
        )


class BusinessGateway:
    MARKER = re.compile(
        r"\[(?:business-kpi|company-fact|company-fact-resolution|business-entity):[^\]]+\]"
    )

    def __init__(self) -> None:
        self.requests = []
        self.propose_decision = False
        self.cite_unknown = False

    async def invoke(self, request, *, actor):
        self.requests.append(request)
        if request.purpose == "executive-synthesis":
            payload = {
                "recommendation": "Use the bounded specialist recommendations.",
                "rationale": "Canonical business context was consulted.",
                "disagreement": [],
                "escalation_required": False,
                "escalation_reason": None,
            }
        else:
            content = request.messages[0].content
            refs = tuple(dict.fromkeys(self.MARKER.findall(content)))
            context_refs = (
                ["[business-kpi:invented@r9/metric:invented@r9]"]
                if self.cite_unknown
                else list(refs[:2])
            )
            proposals = []
            if self.propose_decision:
                proposals.append(
                    {
                        "kind": "decision",
                        "title": "Review revenue intervention",
                        "rationale": "A material business change needs a canonical Decision.",
                        "payload": {
                            "title": "Review revenue intervention",
                            "question": "Should we change the commercial operating plan?",
                            "initiator_identity_id": actor.identity_id,
                            "participants": [],
                            "options": [],
                        },
                    }
                )
            payload = {
                "summary": "Canonical business state was reviewed.",
                "recommendation": "Use the measured state and keep side effects canonical.",
                "risks": [],
                "assumptions": [],
                "disagreement": [],
                "context_refs": context_refs,
                "proposals": proposals,
            }
        return SimpleNamespace(
            text=json.dumps(payload),
            invocation=SimpleNamespace(id=f"invocation-{len(self.requests)}"),
        )


class ExecutiveBusinessContextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.state = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.clock = MutableClock(100.0)
        registry = DefinitionRegistryService(DefinitionRegistryStore(self.state))
        self.roles = install_executive_role_definitions(registry)
        self.governance = DataGovernanceService(DataGovernanceStore(self.state))
        self.business_context = BusinessContextService(
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
                self.business_context.governance_action_handler,
            )
        self.metrics = MetricService(MetricStore(self.state))
        self.goals = FakeGoals()
        self.decisions = FakeDecisions()
        self.business_kpis = BusinessKPIService(
            BusinessKPIStore(self.state),
            self.metrics,
            self.business_context,
            self.goals,
            self.decisions,
            clock=self.clock,
        )
        self.gateway = BusinessGateway()
        self.authority = FakeAuthority()
        self.service = ExecutiveManagementService(
            ExecutiveActivationStore(self.state),
            self.roles,
            self.gateway,
            self.authority,
            self.goals,
            self.decisions,
            FakeDecisionWork(),
            FakeWorkItems(),
            FakeWorkGraph(),
            FakeEvidence(),
            business_context=self.business_context,
            business_kpis=self.business_kpis,
            data_governance=self.governance,
            clock=self.clock,
        )
        self.actor = AuthenticationActor(
            identity_id="owner-a",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="local",
            workspace_id="default",
            roles=(MembershipRole.OWNER,),
            assurance=AuthenticationAssurance.MFA,
        )

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    def entity(self, name="Northstar", *, classification=DataClassification.CONFIDENTIAL):
        return self.business_context.create_entity(
            BusinessEntityCreate(
                entity_type=BusinessEntityType.CUSTOMER,
                name=name,
                classification=classification,
            ),
            actor=self.actor,
        )

    def fact(
        self,
        entity_id,
        *,
        value=100.0,
        provider="crm",
        authority=FactSourceAuthority.AUTHORITATIVE,
        priority=10,
        observed_at=100.0,
        freshness_seconds=300,
        classification=DataClassification.CONFIDENTIAL,
    ):
        return self.business_context.create_fact(
            CompanyFactCreate(
                business_entity_id=entity_id,
                key="arr",
                value_type=FactValueType.NUMBER,
                value=value,
                unit="usd",
                source=CompanyFactSource(
                    source=f"{provider}.arr",
                    provider=provider,
                    authority=authority,
                    priority=priority,
                    source_observed_at=observed_at,
                ),
                quality=FactQuality.VERIFIED,
                observed_at=observed_at,
                freshness_seconds=freshness_seconds,
                classification=classification,
            ),
            actor=self.actor,
        )

    def kpi(self, *, key="arr"):
        return self.business_kpis.create(
            BusinessKPIDefinitionCreate(
                key=key,
                name="Recurring revenue",
                description="Explicit recurring revenue from governed customer facts.",
                domain=BusinessKPIDomain.REVENUE,
                owner_identity_id="finance-owner",
                unit="usd",
                freshness_seconds=120,
                direction=MetricDirection.HIGHER_IS_BETTER,
                formula=BusinessKPIFormula(
                    kind=BusinessKPIFormulaKind.AGGREGATE,
                    terms=(
                        BusinessKPIFactTerm(
                            alias="arr",
                            fact_key="arr",
                            aggregation=BusinessKPITermAggregation.SUM,
                            entity_type=BusinessEntityType.CUSTOMER,
                        ),
                    ),
                    left_alias="arr",
                ),
                currency="USD",
            ),
            actor=self.actor,
        )

    def refresh_and_bind_goal(self, kpi):
        refresh = self.business_kpis.refresh(
            kpi.id,
            actor=self.actor,
            at=self.clock.value,
        )
        self.business_kpis.bind(
            BusinessKPITargetBindingCreate(
                kpi_id=kpi.id,
                target_kind=BusinessKPITargetKind.GOAL,
                target_id="goal-revenue",
                purpose="Executive revenue review",
            ),
            actor=self.actor,
        )
        return refresh

    async def test_goal_kpi_binding_routes_only_revenue_specialist_and_records_exact_provenance(self):
        entity = self.entity()
        fact = self.fact(entity.id, value=1200.0)
        kpi = self.kpi()
        refresh = self.refresh_and_bind_goal(kpi)
        self.assertIsNotNone(refresh.observation_id)

        activation = self.service.create(
            ExecutiveActivationCreate(
                subject="Measured operating review",
                request="Review the canonical Goal and its measured business state.",
                goal_ids=("goal-revenue",),
                max_roles=3,
                budget=ExecutiveReasoningBudget(
                    max_input_tokens=10000,
                    max_output_tokens=2000,
                    max_model_calls=1,
                    max_cost_usd=1,
                ),
            ),
            actor=self.actor,
        )

        self.assertEqual(
            [item.role_id for item in activation.selections],
            ["cro"],
        )
        self.assertIn(
            "business-domain:revenue",
            activation.selections[0].reasons,
        )
        self.assertEqual(activation.business_kpi_ids, (kpi.id,))
        self.assertEqual(activation.business_domains, ("revenue",))
        self.assertEqual(len(activation.context.business_kpis), 1)
        row = activation.context.business_kpis[0]
        self.assertEqual(row["kpi_revision"], 1)
        self.assertEqual(row["metric_revision"], 1)
        self.assertEqual(
            row["source_refresh"]["terms"][0]["fact_ids"],
            [fact.id],
        )
        self.assertEqual(row["current"]["readiness"], "current")

        completed = await self.service.consult(
            activation.id,
            actor=self.actor,
        )
        self.assertEqual(completed.status, ExecutiveActivationStatus.COMPLETED)
        consultation = completed.consultations[0]
        self.assertEqual(consultation.role_id, "cro")
        self.assertEqual(
            consultation.output.context_refs,
            (row["citation"],),
        )
        request = self.gateway.requests[0]
        self.assertIn(row["citation"], request.messages[0].content)
        self.assertIn("ActionIntent", request.system_prompt)
        self.assertNotIn("executive-role:cfo", [item.purpose for item in self.gateway.requests])

    async def test_stale_kpi_remains_visibly_stale_in_executive_context(self):
        entity = self.entity()
        self.fact(entity.id, value=100.0, freshness_seconds=1000)
        kpi = self.kpi()
        self.business_kpis.refresh(kpi.id, actor=self.actor, at=100.0)
        self.clock.value = 400.0

        activation = self.service.create(
            ExecutiveActivationCreate(
                subject="Measured revenue state",
                request="Review the configured KPI.",
                business_kpi_ids=(kpi.id,),
                max_roles=1,
            ),
            actor=self.actor,
        )
        row = activation.context.business_kpis[0]
        self.assertEqual(row["current"]["readiness"], "stale")
        self.assertEqual(row["current"]["freshness"], "stale")
        self.assertFalse(row["current"]["readiness"] == "current")

    async def test_conflicting_provider_facts_remain_partial_and_attributable(self):
        entity = self.entity()
        first = self.fact(entity.id, value=100.0, provider="crm")
        second = self.fact(
            entity.id,
            value=80.0,
            provider="billing",
            authority=FactSourceAuthority.SECONDARY,
            priority=5,
        )
        kpi = self.kpi()
        refresh = self.business_kpis.refresh(
            kpi.id,
            actor=self.actor,
            at=100.0,
        )
        self.assertTrue(refresh.partial)
        self.assertTrue(any("conflicting provider values" in x for x in refresh.findings))

        activation = self.service.create(
            ExecutiveActivationCreate(
                subject="Revenue conflict review",
                request="Review the governed KPI conflict.",
                business_entity_ids=(entity.id,),
                business_kpi_ids=(kpi.id,),
                max_roles=2,
            ),
            actor=self.actor,
        )
        kpi_row = activation.context.business_kpis[0]
        self.assertEqual(kpi_row["current"]["readiness"], "partial")
        self.assertTrue(kpi_row["source_refresh"]["partial"])
        fact_row = next(
            row
            for row in activation.context.business_facts
            if row["key"] == "arr"
        )
        self.assertTrue(fact_row["conflict"])
        self.assertEqual(
            set(fact_row["conflict_fact_ids"]),
            {first.id, second.id},
        )

    async def test_secret_source_fact_blocks_derived_kpi_from_model_context(self):
        entity = self.entity(classification=DataClassification.SECRET)
        self.fact(
            entity.id,
            value=500.0,
            classification=DataClassification.SECRET,
        )
        kpi = self.kpi()
        self.business_kpis.refresh(kpi.id, actor=self.actor, at=100.0)

        activation = self.service.create(
            ExecutiveActivationCreate(
                subject="Revenue review",
                request="Review the configured revenue KPI.",
                business_kpi_ids=(kpi.id,),
                max_roles=1,
            ),
            actor=self.actor,
        )
        self.assertEqual(activation.context.business_kpis, ())
        self.assertTrue(activation.context.business_context_denials)
        denial = activation.context.business_context_denials[0]
        self.assertEqual(denial["object_type"], "business_kpi")
        self.assertEqual(
            denial["reason"],
            "kpi_source_fact_denied_for_model_context",
        )

    async def test_customer_scope_is_bounded_to_revenue_and_customer_success_specialists(self):
        entity = self.entity()
        self.fact(entity.id, value=100.0)
        activation = self.service.create(
            ExecutiveActivationCreate(
                subject="Customer operating review",
                request="Review this governed customer account.",
                business_entity_ids=(entity.id,),
                max_roles=2,
                budget=ExecutiveReasoningBudget(
                    max_input_tokens=12000,
                    max_output_tokens=3000,
                    max_model_calls=3,
                    max_cost_usd=2,
                ),
            ),
            actor=self.actor,
        )
        selected = [item.role_id for item in activation.selections]
        self.assertEqual(set(selected), {"cro", "customer-success"})
        self.assertNotIn("cfo", selected)
        self.assertNotIn("cmo", selected)
        self.assertNotIn("cpo", selected)
        self.assertLessEqual(len(selected), 2)

    async def test_role_output_cannot_cite_business_context_it_was_not_given(self):
        entity = self.entity()
        self.fact(entity.id, value=100.0)
        kpi = self.kpi()
        self.business_kpis.refresh(kpi.id, actor=self.actor, at=100.0)
        self.gateway.cite_unknown = True
        activation = self.service.create(
            ExecutiveActivationCreate(
                subject="Revenue review",
                request="Review the configured KPI.",
                business_kpi_ids=(kpi.id,),
                max_roles=1,
            ),
            actor=self.actor,
        )
        with self.assertRaisesRegex(
            ExecutiveConsultationError,
            "unavailable canonical context",
        ):
            await self.service.consult(activation.id, actor=self.actor)

    async def test_authority_denial_keeps_business_recommendation_advisory(self):
        entity = self.entity()
        self.fact(entity.id, value=100.0)
        kpi = self.kpi()
        self.business_kpis.refresh(kpi.id, actor=self.actor, at=100.0)
        self.gateway.propose_decision = True
        self.authority.allow = False

        activation = self.service.create(
            ExecutiveActivationCreate(
                subject="Revenue intervention",
                request="Assess a material commercial plan change.",
                business_kpi_ids=(kpi.id,),
                max_roles=1,
            ),
            actor=self.actor,
        )
        completed = await self.service.consult(activation.id, actor=self.actor)
        proposal = completed.proposals[0]

        with self.assertRaises(ExecutiveAuthorityError):
            await self.service.materialize(
                completed.id,
                proposal.id,
                actor=self.actor,
            )
        self.assertEqual(self.decisions.created, [])
        stored = next(
            item
            for item in self.service.get(completed.id, actor=self.actor).proposals
            if item.id == proposal.id
        )
        self.assertEqual(stored.status, ExecutiveProposalStatus.PROPOSED)
        self.assertTrue(stored.authority_reasons)
        self.assertFalse(
            self.roles.catalog(
                organization_id=self.actor.organization_id,
                workspace_id=self.actor.workspace_id,
            ).role_map["cro"].authority.external_side_effects
        )


if __name__ == "__main__":
    unittest.main()
