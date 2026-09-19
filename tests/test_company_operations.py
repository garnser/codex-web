from __future__ import annotations

import unittest
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from codex_web.action_intents import ActionIntentStatus
from codex_web.action_providers import ActionRiskClass
from codex_web.api.company_operations import build_company_operations_router
from codex_web.approval_requests import ApprovalRequestStatus
from codex_web.attention import AttentionSeverity, AttentionStatus
from codex_web.business_context import (
    BusinessEntityType,
    FactFreshness,
    FactSourceAuthority,
)
from codex_web.business_data_sources import (
    BusinessDataSourceCapability,
    BusinessDataSourceStatus,
)
from codex_web.company_operations import CompanyOperationsHealth
from codex_web.executive_roles import (
    ExecutiveActivationStatus,
    ExecutiveProposalStatus,
    ExecutiveTriggerKind,
)
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.provider_capacity import ProviderCapacityStatus
from codex_web.services.company_operations import CompanyOperationsService


class DumpNS(SimpleNamespace):
    def model_dump(self, mode=None):
        def dump(value):
            if isinstance(value, DumpNS):
                return {key: dump(item) for key, item in vars(value).items()}
            if isinstance(value, tuple):
                return [dump(item) for item in value]
            if isinstance(value, list):
                return [dump(item) for item in value]
            if isinstance(value, dict):
                return {key: dump(item) for key, item in value.items()}
            if hasattr(value, "value"):
                return value.value
            return value
        return dump(self)


class FakeContext:
    def __init__(self):
        self.entity = DumpNS(
            id="business-entity-1",
            name="Northstar",
            entity_type=BusinessEntityType.CUSTOMER,
            lifecycle=DumpNS(value="active"),
            classification=DumpNS(value="confidential"),
        )
        self.external = DumpNS(
            id="external-record-1",
            provider="crm",
            provider_instance="crm://prod",
            object_type="account",
            external_id="acct-1",
            lifecycle=DumpNS(value="active"),
            synced_at=100.0,
            external_url="https://example.invalid/account/acct-1",
        )
        self.selected = DumpNS(
            id="company-fact-1",
            value=1200.0,
            unit="usd",
            classification=DumpNS(value="confidential"),
            source=DumpNS(
                provider="crm",
                external_record_ref_id=self.external.id,
                authority=FactSourceAuthority.AUTHORITATIVE,
            ),
        )

    def list_entities(self, *, actor, include_inactive, limit, entity_type=None):
        return (self.entity,)

    def list_external_records(
        self,
        *,
        actor,
        include_inactive,
        limit,
        business_entity_id=None,
    ):
        return (self.external,)

    def list_facts(
        self,
        *,
        actor,
        include_inactive,
        limit,
        business_entity_id=None,
        key=None,
    ):
        return (
            DumpNS(
                id="company-fact-1",
                business_entity_id=self.entity.id,
                key="arr",
                value=1200.0,
                unit="usd",
                lifecycle=DumpNS(value="active"),
                classification=DumpNS(value="confidential"),
                source=self.selected.source,
            ),
        )

    def resolve_fact(self, entity_id, key, *, actor, at=None):
        return DumpNS(
            freshness=FactFreshness.FRESH,
            conflict=True,
            selected=self.selected,
            candidate_fact_ids=("company-fact-1", "company-fact-2"),
            conflict_fact_ids=("company-fact-1", "company-fact-2"),
            stale_fact_ids=(),
            revoked_source_fact_ids=(),
            reason="authoritative value selected; provider conflict retained",
        )


class FakeSources:
    def __init__(self):
        self.source = DumpNS(
            id="business-source-1",
            name="CRM Accounts",
            source_type="reference-crm",
            source_instance="crm://prod",
            provider_id="crm",
            extension_installation_id=None,
            object_type="account",
            entity_type=BusinessEntityType.CUSTOMER,
            status=BusinessDataSourceStatus.ACTIVE,
            capabilities=(
                BusinessDataSourceCapability.INCREMENTAL_SYNC,
                BusinessDataSourceCapability.EVENTS,
            ),
            credential_ref="secret://crm",
            cursor="42",
            checkpoint="42",
            last_success_at=95.0,
            last_error=None,
            last_error_at=None,
            projected_records=12,
            stale_events=2,
            duplicate_events=1,
        )

    def list(self, *, actor):
        return (self.source,)

    def drift(self, source_id, *, actor):
        return (
            {
                "business_entity_id": "business-entity-1",
                "external_record_ref_id": "external-record-1",
                "conflicts": [
                    {
                        "fact_key": "arr",
                        "selected_fact_id": "company-fact-1",
                        "conflict_fact_ids": ["company-fact-1", "company-fact-2"],
                    }
                ],
            },
        )


class FakeKpis:
    def list(self, *, actor):
        return (DumpNS(domain=DumpNS(value="revenue")),)

    def operating_view(self, *, actor, at=None):
        return DumpNS(
            current=False,
            blockers=("arr: partial",),
            items=(
                DumpNS(
                    kpi_id="business-kpi-arr",
                    name="ARR",
                    domain="revenue",
                    value=1200.0,
                    unit="usd",
                    currency="USD",
                    readiness="partial",
                    freshness="partial",
                    kpi_revision=2,
                    metric_revision=3,
                    readiness_reasons=("provider conflict",),
                ),
            ),
            evaluated_at=at,
        )


class FakeGoals:
    def list(self, *, scope):
        return (DumpNS(id="goal-1", status="active", title="Grow revenue"),)


class FakeDecisions:
    def list(self, *, actor):
        return (DumpNS(id="decision-1", status="approved", title="Pricing plan"),)


class FakeExecutives:
    def __init__(self):
        self.activation = DumpNS(
            id="executive-1",
            subject="Revenue review",
            request="Review governed revenue state",
            trigger_kind=ExecutiveTriggerKind.EVENT,
            trigger_ref="event-1",
            event_type="business_data.event",
            project_id=None,
            goal_ids=("goal-1",),
            decision_ids=("decision-1",),
            work_item_refs=(),
            evidence_ids=(),
            business_domains=("revenue",),
            status=ExecutiveActivationStatus.PLANNED,
            selections=(
                DumpNS(
                    role_id="cro",
                    score=50,
                    reasons=("business-domain:revenue",),
                    explicit=False,
                ),
            ),
            consultations=(
                DumpNS(
                    id="consult-1",
                    output=DumpNS(
                        recommendation="Review the measured ARR conflict.",
                        context_refs=("[business-kpi:arr@r2/metric:arr@r3]",),
                    ),
                ),
            ),
            context=DumpNS(
                business_entities=(
                    {
                        "id": "business-entity-1",
                        "name": "Northstar",
                        "lifecycle": "active",
                        "citation": "[business-entity:business-entity-1]",
                    },
                ),
                business_facts=(
                    {
                        "business_entity_id": "business-entity-1",
                        "key": "arr",
                        "freshness": "fresh",
                        "citation": "[company-fact-resolution:business-entity-1:arr]",
                        "selected": {
                            "id": "company-fact-1",
                            "value": 1200,
                            "source": {
                                "external_record_ref_id": "external-record-1"
                            },
                        },
                    },
                ),
                business_kpis=(
                    {
                        "id": "business-kpi-arr",
                        "name": "ARR",
                        "citation": "[business-kpi:arr@r2/metric:metric-arr@r3]",
                        "metric_id": "metric-arr",
                        "current": {
                            "value": 1200,
                            "readiness": "partial",
                        },
                    },
                ),
                business_context_denials=(),
            ),
            proposals=(
                DumpNS(
                    id="proposal-1",
                    role_id="cro",
                    kind=DumpNS(value="decision"),
                    title="Pricing review",
                    rationale="Canonical measured state requires a decision.",
                    payload={},
                    status=ExecutiveProposalStatus.MATERIALIZED,
                    authority_capability="executive.decision.materialize",
                    authority_reasons=("allowed",),
                    resulting_ref="decision:decision-1",
                    materialized_by="owner-a",
                    materialized_at=100.0,
                ),
            ),
            failure_reason=None,
        )

    def list(self, *, actor):
        return (self.activation,)

    def get(self, activation_id, *, actor):
        if activation_id != self.activation.id:
            raise KeyError(activation_id)
        return self.activation


class FakeAttention:
    def list(self, actor):
        return [
            DumpNS(
                id="attention-1",
                type="business_sync",
                severity=AttentionSeverity.HIGH,
                source=DumpNS(
                    object_type="business_data_source",
                    object_id="business-source-1",
                    event_id=None,
                ),
                reason="Provider conflict requires review",
                status=AttentionStatus.OPEN,
            )
        ]


class FakeCapacity:
    def get(self, provider_id, runtime_id, *, actor):
        return DumpNS(
            status=ProviderCapacityStatus.THROTTLED,
            reason="HTTP 429",
            retry_at=130.0,
            consecutive_failures=2,
        )


class FakeApprovals:
    def __init__(self):
        self.approval = DumpNS(
            id="approval-1",
            status=ApprovalRequestStatus.PENDING,
            reason="High-impact pricing action",
            target=DumpNS(
                operation="execute",
                object_type="decision",
                object_id="decision-1",
                resource_ids=("resource-billing",),
            ),
            target_fingerprint="fingerprint-1",
            resulting_operation_reference=None,
        )

    def list(self, actor):
        return [self.approval]


class FakeActions:
    def __init__(self):
        decision = DumpNS(
            outcome="allow",
            source="authority",
            reason="role grant",
            capabilities=(),
            definition_refs=(),
            role_ids=(),
            grant_ids=(),
            delegation_ids=(),
            expires_at=None,
            reasons=("test authority allows",),
            evaluated_at=100.0,
        )
        security = DumpNS(
            allowed=True,
            reasons=("bounded provider action",),
        )
        self.intent = DumpNS(
            id="action-intent-1",
            status=ActionIntentStatus.REQUIRES_RECONCILIATION,
            goal_id=None,
            decision_id="decision-1",
            work_item_ref=None,
            execution_id="action-exec-1",
            correlation_id="corr-1",
            causation_id="proposal-1",
            action_definition=DumpNS(
                title="Change billing plan",
                risk_class=ActionRiskClass.HIGH,
                required_authority=("billing.plan.change",),
                required_authority_level="execute",
                reversible=True,
            ),
            resource_ids=("resource-billing",),
            provider_type="billing",
            provider_instance="billing://prod",
            action_id="plan.change",
            authority_decision=decision,
            authority_recheck=None,
            policy_decision=decision,
            security_decision=security,
            verification_required=True,
            last_error="provider outcome unknown",
        )

    def list(self, actor, **kwargs):
        return [self.intent]

    def get(self, intent_id, actor):
        if intent_id != self.intent.id:
            raise KeyError(intent_id)
        return self.intent

    def history(self, intent_id, actor):
        return {
            "intent": self.intent.model_dump(mode="json"),
            "receipts": [
                {
                    "id": "receipt-1",
                    "outcome": "unknown",
                    "provider_external_id": "billing-change-42",
                    "correlation_id": "corr-1",
                    "result": None,
                }
            ],
            "verifications": [],
            "inbox": [],
        }


class FakeEvidence:
    def list_evidence(self, actor, **kwargs):
        return [
            DumpNS(
                id="evidence-1",
                result=DumpNS(value="pass"),
                summary="Billing system confirms final plan after reconciliation",
                external_id="billing-change-42",
                deep_link=None,
                execution_id="action-exec-1",
                work_item_ref=None,
            )
        ]


class FakeExtensions:
    def __init__(self, lifecycle="enabled", incompatible_reason=None):
        self.installation = DumpNS(
            id="extension-crm",
            manifest=DumpNS(
                id="com.example.crm",
                version="1.2.3",
                capabilities=DumpNS(
                    requested=("network.crm.read", "secrets.crm"),
                ),
            ),
            lifecycle=DumpNS(value=lifecycle),
            health_status=DumpNS(value="healthy"),
            configuration_record_ids=("config-crm",),
            incompatible_reason=incompatible_reason,
        )

    def get(self, installation_id, actor):
        if installation_id != self.installation.id:
            raise KeyError(installation_id)
        return self.installation

    def grants(self, installation_id, actor):
        self.get(installation_id, actor)
        return [
            DumpNS(
                capability="network.crm.read",
                active=True,
            ),
            DumpNS(
                capability="secrets.crm",
                active=False,
            ),
        ]


class CompanyOperationsTests(unittest.TestCase):
    def setUp(self):
        self.actor = AuthenticationActor(
            identity_id="owner-a",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="org-a",
            workspace_id="ws-a",
            roles=(MembershipRole.OWNER,),
            assurance=AuthenticationAssurance.MFA,
        )
        self.executives = FakeExecutives()
        self.actions = FakeActions()
        self.service = CompanyOperationsService(
            FakeContext(),
            FakeSources(),
            FakeKpis(),
            FakeGoals(),
            FakeDecisions(),
            self.executives,
            FakeAttention(),
            FakeCapacity(),
            FakeApprovals(),
            self.actions,
            FakeEvidence(),
            clock=lambda: 100.0,
        )

    def test_overview_keeps_canonical_provider_and_failure_state_distinct(self):
        overview = self.service.overview(actor=self.actor)
        self.assertEqual(
            overview.overall_health,
            CompanyOperationsHealth.DEGRADED,
        )
        self.assertEqual(overview.counts.business_entities, 1)
        self.assertEqual(overview.counts.external_records, 1)
        self.assertEqual(overview.counts.sources, 1)
        self.assertEqual(overview.business_domains, ("revenue",))

        fact = overview.fact_diagnostics[0]
        self.assertTrue(fact.conflict)
        self.assertEqual(fact.selected_fact_id, "company-fact-1")
        self.assertEqual(
            fact.external_record_ref_id,
            "external-record-1",
        )
        self.assertEqual(fact.provider, "crm")

        source = overview.sources[0]
        self.assertEqual(
            source.health,
            CompanyOperationsHealth.DEGRADED,
        )
        self.assertEqual(source.capacity_status, "throttled")
        self.assertEqual(source.conflict_count, 1)
        self.assertIn("HTTP 429", " ".join(source.issues))
        self.assertIn("arr: partial", overview.blockers)
        self.assertTrue(
            any("fact Northstar/arr: conflict" == item for item in overview.blockers)
        )

    def test_linked_extension_requested_and_active_grants_remain_distinct(self):
        context = FakeContext()
        sources = FakeSources()
        sources.source.extension_installation_id = "extension-crm"
        service = CompanyOperationsService(
            context,
            sources,
            FakeKpis(),
            FakeGoals(),
            FakeDecisions(),
            self.executives,
            FakeAttention(),
            FakeCapacity(),
            FakeApprovals(),
            self.actions,
            FakeEvidence(),
            FakeExtensions(),
            clock=lambda: 100.0,
        )
        source = service.overview(actor=self.actor).sources[0]
        self.assertEqual(source.extension_id, "com.example.crm")
        self.assertEqual(source.extension_version, "1.2.3")
        self.assertEqual(
            source.extension_requested_capabilities,
            ("network.crm.read", "secrets.crm"),
        )
        self.assertEqual(
            source.extension_granted_capabilities,
            ("network.crm.read",),
        )
        self.assertEqual(
            source.extension_configuration_record_ids,
            ("config-crm",),
        )

        blocked = CompanyOperationsService(
            context,
            sources,
            FakeKpis(),
            FakeGoals(),
            FakeDecisions(),
            self.executives,
            FakeAttention(),
            FakeCapacity(),
            FakeApprovals(),
            self.actions,
            FakeEvidence(),
            FakeExtensions(
                lifecycle="incompatible",
                incompatible_reason="host contract mismatch",
            ),
            clock=lambda: 100.0,
        ).overview(actor=self.actor).sources[0]
        self.assertEqual(blocked.health, CompanyOperationsHealth.BLOCKED)
        self.assertTrue(
            any("host contract mismatch" in item for item in blocked.issues)
        )

    def test_explain_executive_traces_governed_context_to_action_receipt_and_evidence(self):
        explained = self.service.explain_executive_activation(
            "executive-1",
            actor=self.actor,
        )
        kinds = [item.kind for item in explained.stages]
        for expected in (
            "trigger",
            "business_entity",
            "company_fact",
            "business_kpi",
            "executive_role",
            "executive_recommendation",
            "executive_proposal",
            "approval",
            "action_intent",
            "provider_receipt",
            "evidence",
        ):
            self.assertIn(expected, kinds)

        action = next(
            item for item in explained.stages if item.kind == "action_intent"
        )
        self.assertEqual(action.details["risk_class"], "high")
        self.assertEqual(
            action.details["resource_ids"],
            ["resource-billing"],
        )
        self.assertEqual(
            action.details["required_authority_level"],
            "execute",
        )
        self.assertIn(
            "action-intent-1: requires_reconciliation",
            explained.unresolved,
        )

    def test_action_explain_surfaces_unknown_provider_outcome_without_retrying(self):
        explained = self.service.explain_action_intent(
            "action-intent-1",
            actor=self.actor,
        )
        self.assertEqual(explained.subject_type, "action_intent")
        receipt = next(
            item for item in explained.stages if item.kind == "provider_receipt"
        )
        self.assertEqual(receipt.status, "unknown")
        self.assertTrue(
            any("reconciliation required" in item for item in explained.unresolved)
        )
        self.assertIn("provider outcome unknown", explained.unresolved)


class CompanyOperationsApiTests(unittest.TestCase):
    def test_overview_is_read_only_tenant_projection(self):
        actor = AuthenticationActor(
            identity_id="reader-a",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="org-a",
            workspace_id="ws-a",
            roles=(MembershipRole.MEMBER,),
            assurance=AuthenticationAssurance.PRIMARY,
        )
        service = CompanyOperationsTests()
        service.setUp()
        app = FastAPI()

        @app.middleware("http")
        async def inject_actor(request: Request, call_next):
            request.state.identity_actor = actor
            return await call_next(request)

        app.include_router(build_company_operations_router(service.service))
        client = TestClient(app)
        try:
            response = client.get("/api/company-operations/overview")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                response.json()["item"]["organization_id"],
                "org-a",
            )
            self.assertNotIn("mutate", response.json()["item"])
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
