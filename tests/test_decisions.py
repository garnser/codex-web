from __future__ import annotations

import re
import tempfile
import time
import unittest
from pathlib import Path

from codex_web.approval_requests import (
    ApprovalDecisionOutcome,
    ApprovalDecisionSubmit,
    ApprovalRequestStatus,
    ApprovalRequirement,
)
from codex_web.artifact_evidence import (
    EvidenceCreate,
    EvidenceResult,
    EvidenceType,
)
from codex_web.decisions import (
    DecisionApprovalRequest,
    DecisionBudget,
    DecisionCreate,
    DecisionDeliberationLimits,
    DecisionDeliberationRound,
    DecisionDissent,
    DecisionEvidenceInput,
    DecisionEvidenceKind,
    DecisionImportance,
    DecisionOption,
    DecisionParticipant,
    DecisionParticipantAnalysis,
    DecisionRecommendation,
    DecisionStatus,
    DecisionSupersedeRequest,
)
from codex_web.identity import (
    AuthenticationAssurance,
    HumanIdentity,
    Membership,
    MembershipRole,
    PrincipalKind,
    TenantScope,
)
from codex_web.metrics import (
    MetricAggregation,
    MetricDefinitionCreate,
    MetricFreshness,
    MetricObservationCreate,
    MetricSnapshotRequest,
    MetricValueType,
)
from codex_web.model_gateway import (
    MODEL_CLASS_STRATEGIC,
    ModelDefinitionUpsert,
    ModelProviderResult,
    ModelProviderUpsert,
    ModelProviderUsage,
    PromptTemplateUpsert,
)
from codex_web.services.approval_requests import ApprovalRequestService
from codex_web.services.artifact_evidence import ArtifactEvidenceService
from codex_web.services.canonical_events import (
    CanonicalEventBus,
    CanonicalEventIngestionService,
)
from codex_web.services.decision_deliberation import (
    DecisionDeliberationError,
    DecisionDeliberationService,
)
from codex_web.services.decisions import (
    DecisionService,
    DecisionStateError,
    DecisionValidationError,
)
from codex_web.services.identity import IdentityService
from codex_web.services.metrics import MetricService
from codex_web.services.model_gateway import ModelGatewayService
from codex_web.storage.approval_requests import ApprovalRequestStore
from codex_web.storage.artifact_evidence import ArtifactEvidenceStore
from codex_web.storage.canonical_events import CanonicalEventStore
from codex_web.storage.decisions import DecisionStore
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.metrics import MetricStore
from codex_web.storage.model_gateway import ModelGatewayStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class DecisionFakeAdapter:
    adapter_type = "decision-fake"

    def __init__(self) -> None:
        self.requests = []

    async def invoke(self, provider, model, request, *, credential):
        del provider, model, credential
        self.requests.append(request)
        if request.purpose == "decision-role-analysis":
            match = re.search(
                r"participant_id must be exactly '([^']+)'",
                request.system_prompt,
            )
            participant_id = match.group(1) if match else "unknown"
            text = (
                "{"
                f'"participant_id":"{participant_id}",'
                '"preferred_option_id":"option-a",'
                '"analysis":"The option is viable but evidence freshness must be respected.",'
                '"pros":["bounded"],'
                '"cons":["migration effort"],'
                '"risks":["stale evidence"],'
                '"uncertainty":["metric evidence may not be fresh"]'
                "}"
            )
        else:
            text = (
                "{"
                '"option_id":"option-a",'
                '"rationale":"Option A is the minimum sufficient bounded choice.",'
                '"confidence":0.72,'
                '"uncertainty":["stale or missing metrics require follow-up"],'
                '"dissent":[]'
                "}"
            )
        return ModelProviderResult(
            text=text,
            usage=ModelProviderUsage(input_tokens=100, output_tokens=50),
            provider_request_id=f"decision-provider-{len(self.requests)}",
        )


class DecisionDomainTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.sqlite = SQLiteStateStore(root / "state.sqlite3")

        self.identity_store = IdentityStateStore(self.sqlite)
        self.identity = IdentityService(self.identity_store)
        self.identity.bootstrap_local()
        self.requester = self._human_actor("decision-owner", MembershipRole.ADMIN)
        self.approver = self._human_actor("decision-approver", MembershipRole.APPROVER)

        event_store = CanonicalEventStore(self.sqlite)
        event_bus = CanonicalEventBus(event_store)
        self.events = CanonicalEventIngestionService(event_bus)

        self.approvals = ApprovalRequestService(
            ApprovalRequestStore(self.sqlite),
            self.identity,
            self.events,
        )
        self.metrics = MetricService(MetricStore(self.sqlite))
        self.artifacts = ArtifactEvidenceService(ArtifactEvidenceStore(self.sqlite))
        self.decision_store = DecisionStore(self.sqlite)
        self.decisions = DecisionService(
            self.decision_store,
            self.approvals,
            self.metrics,
            self.artifacts,
            self.events,
        )

        self.gateway = ModelGatewayService(ModelGatewayStore(self.sqlite))
        self.adapter = DecisionFakeAdapter()
        self.gateway.register_adapter(self.adapter)
        self.gateway.upsert_template(
            PromptTemplateUpsert(
                template_id="generic.system",
                version="1.0",
                content="{{ instructions }}",
            ),
            actor=self.requester,
        )
        self.gateway.upsert_provider(
            ModelProviderUpsert(
                id="decision-provider",
                adapter_type=self.adapter.adapter_type,
                display_name="Decision fake provider",
                credential_required=False,
            ),
            actor=self.requester,
        )
        self.gateway.upsert_model(
            ModelDefinitionUpsert(
                id="decision-model",
                provider_id="decision-provider",
                concrete_model="decision-test-model",
                model_version="1",
                model_classes=(MODEL_CLASS_STRATEGIC,),
                capabilities=("text", "reasoning"),
                context_window_tokens=64000,
                max_output_tokens=8192,
                input_price_per_million_usd=1.0,
                output_price_per_million_usd=2.0,
            ),
            actor=self.requester,
        )
        self.deliberation = DecisionDeliberationService(
            self.decisions,
            self.gateway,
        )

        self.evidence = self.artifacts.create_evidence(
            EvidenceCreate(
                evidence_type=EvidenceType.REVIEW,
                result=EvidenceResult.PASS,
                source="test",
                summary="Architecture review completed.",
            ),
            actor=self.requester,
        )

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    def _human_actor(self, identity_id: str, role: MembershipRole):
        def seed(state):
            if not any(item.id == identity_id for item in state.humans):
                state.humans.append(
                    HumanIdentity(id=identity_id, display_name=identity_id)
                )
            return state

        self.identity_store.update(seed)
        self.identity.add_membership(
            Membership(
                identity_id=identity_id,
                principal_kind=PrincipalKind.HUMAN,
                organization_id="local",
                workspace_id="default",
                roles=[role],
            )
        )
        credentials = self.identity.create_session(
            identity_id=identity_id,
            scope=TenantScope(),
            assurance=AuthenticationAssurance.MFA,
        )
        return self.identity.authenticate_session(
            credentials.session_token,
            touch=False,
        ).actor

    def _participant_rows(self):
        return (
            DecisionParticipant(
                id="participant-risk",
                role="risk",
                perspective="Identify operational and governance failure modes.",
            ),
            DecisionParticipant(
                id="participant-operator",
                role="operator",
                perspective="Assess implementation and operational practicality.",
            ),
        )

    def _options(self):
        return (
            DecisionOption(
                id="option-a",
                title="Bounded rollout",
                description="Ship the bounded canonical implementation.",
                pros=("small blast radius",),
                cons=("some migration work",),
                risks=("integration defects",),
            ),
            DecisionOption(
                id="option-b",
                title="Defer",
                description="Defer the implementation.",
                pros=("no immediate change",),
                cons=("capability gap remains",),
                risks=("decision state remains chat-only",),
            ),
        )

    def _metric_snapshots(self):
        scope = self.requester.tenant
        metric = self.metrics.create_definition(
            MetricDefinitionCreate(
                key=f"decision-latency-{time.time_ns()}",
                name="Decision latency",
                description="Decision processing latency.",
                owner_identity_id=self.requester.identity_id,
                unit="ms",
                value_type=MetricValueType.NUMBER,
                aggregation=MetricAggregation.LAST,
                freshness_seconds=10,
                source_requirements=("monitor",),
            ),
            scope=scope,
            actor_id=self.requester.identity_id,
        )
        self.metrics.ingest(
            metric.id,
            MetricObservationCreate(
                value=250,
                observed_at=time.time() - 120,
                source="monitor",
                idempotency_key=f"stale:{metric.id}",
            ),
            scope=scope,
            actor_id="collector",
        )
        stale = self.metrics.capture_snapshot(
            metric.id,
            MetricSnapshotRequest(),
            scope=scope,
            actor_id=self.requester.identity_id,
        )

        missing_metric = self.metrics.create_definition(
            MetricDefinitionCreate(
                key=f"decision-errors-{time.time_ns()}",
                name="Decision errors",
                description="Decision processing error rate.",
                owner_identity_id=self.requester.identity_id,
                unit="percent",
                value_type=MetricValueType.NUMBER,
                aggregation=MetricAggregation.LAST,
                freshness_seconds=10,
            ),
            scope=scope,
            actor_id=self.requester.identity_id,
        )
        missing = self.metrics.capture_snapshot(
            missing_metric.id,
            MetricSnapshotRequest(),
            scope=scope,
            actor_id=self.requester.identity_id,
        )
        return metric, stale, missing_metric, missing

    async def _create_decision(
        self,
        *,
        evidence=(),
        max_calls: int = 3,
        max_rounds: int = 2,
    ):
        return await self.decisions.create(
            DecisionCreate(
                title="Choose rollout strategy",
                question="Which bounded rollout strategy should we use?",
                participants=self._participant_rows(),
                evidence=evidence,
                assumptions=("Current provider contracts remain available.",),
                constraints=("No direct provider-side mutation from Decision.",),
                options=self._options(),
                importance=DecisionImportance.HIGH,
                budget=DecisionBudget(
                    max_input_tokens=24000,
                    max_output_tokens=6000,
                    max_model_calls=max_calls,
                    max_cost_usd=5.0,
                ),
                limits=DecisionDeliberationLimits(
                    max_participants=2,
                    max_rounds=max_rounds,
                ),
            ),
            actor=self.requester,
        )

    async def test_metric_and_evidence_references_are_exact_and_nonfresh_state_is_explicit(self) -> None:
        metric, stale, missing_metric, missing = self._metric_snapshots()
        decision = await self._create_decision(
            evidence=(
                DecisionEvidenceInput(
                    kind=DecisionEvidenceKind.EVIDENCE,
                    evidence_id=self.evidence.id,
                ),
                DecisionEvidenceInput(
                    kind=DecisionEvidenceKind.METRIC_SNAPSHOT,
                    metric_id=metric.id,
                    metric_snapshot_id=stale.id,
                ),
                DecisionEvidenceInput(
                    kind=DecisionEvidenceKind.METRIC_SNAPSHOT,
                    metric_id=missing_metric.id,
                    metric_snapshot_id=missing.id,
                ),
            )
        )

        by_kind = [item for item in decision.evidence if item.kind == DecisionEvidenceKind.METRIC_SNAPSHOT]
        self.assertEqual(
            {item.metric_freshness for item in by_kind},
            {MetricFreshness.STALE, MetricFreshness.MISSING},
        )
        stale_ref = next(item for item in by_kind if item.metric_snapshot_id == stale.id)
        self.assertEqual(stale_ref.observation_ids, stale.observation_ids)
        self.assertEqual(stale_ref.metric_revision, stale.metric_revision)
        self.assertEqual(
            next(item for item in decision.evidence if item.kind == DecisionEvidenceKind.EVIDENCE).evidence_id,
            self.evidence.id,
        )

        with self.assertRaises(DecisionValidationError):
            await self._create_decision(
                evidence=(
                    DecisionEvidenceInput(
                        kind=DecisionEvidenceKind.METRIC_SNAPSHOT,
                        metric_id=metric.id,
                        metric_snapshot_id="missing-snapshot",
                    ),
                )
            )

    async def test_bounded_parallel_deliberation_records_one_analysis_per_role_and_one_synthesis(self) -> None:
        decision = await self._create_decision(max_calls=3, max_rounds=2)
        updated = await self.deliberation.deliberate(
            decision.id,
            actor=self.requester,
        )

        self.assertEqual(updated.status, DecisionStatus.ANALYSIS)
        self.assertEqual(len(updated.deliberation_rounds), 1)
        round_record = updated.deliberation_rounds[0]
        self.assertEqual(
            {item.participant_id for item in round_record.analyses},
            {"participant-risk", "participant-operator"},
        )
        self.assertEqual(updated.recommendation.option_id, "option-a")
        self.assertEqual(
            [item.purpose for item in self.adapter.requests].count("decision-role-analysis"),
            2,
        )
        self.assertEqual(
            [item.purpose for item in self.adapter.requests].count("decision-synthesis"),
            1,
        )
        usage = self.gateway.decision_usage(decision.id, actor=self.requester)
        self.assertEqual(usage.calls, 3)

        with self.assertRaisesRegex(
            DecisionDeliberationError,
            "model-call budget",
        ):
            await self.deliberation.deliberate(
                decision.id,
                actor=self.requester,
            )

    async def test_approval_is_canonical_and_consumption_atomically_finalizes_decision(self) -> None:
        decision = await self._create_decision(max_calls=3, max_rounds=1)
        analyzed = await self.deliberation.deliberate(
            decision.id,
            actor=self.requester,
        )
        awaiting = await self.decisions.request_approval(
            analyzed.id,
            DecisionApprovalRequest(
                reason="Review bounded rollout recommendation.",
                requirement=ApprovalRequirement(
                    quorum=1,
                    required_assurance=AuthenticationAssurance.MFA,
                    membership_roles=(MembershipRole.APPROVER,),
                    allow_self_approval=False,
                ),
            ),
            actor=self.requester,
        )
        self.assertEqual(awaiting.status, DecisionStatus.AWAITING_APPROVAL)
        approval = self.approvals.get(
            awaiting.approval_request_id,
            actor=self.requester,
        )
        self.assertEqual(approval.target.object_type, "decision")
        self.assertEqual(approval.target.object_id, awaiting.id)
        self.assertEqual(approval.target.target_version, str(awaiting.revision))
        self.assertEqual(approval.target.target_digest, awaiting.approval_digest())

        approved_request = await self.approvals.decide(
            approval.id,
            ApprovalDecisionSubmit(
                outcome=ApprovalDecisionOutcome.APPROVE,
                reason="Approved after review.",
                idempotency_key="decision-approve-1",
            ),
            actor=self.approver,
        )
        self.assertEqual(approved_request.status, ApprovalRequestStatus.APPROVED)

        approved = await self.decisions.finalize_approval(
            awaiting.id,
            actor=self.requester,
        )
        self.assertEqual(approved.status, DecisionStatus.APPROVED)
        self.assertEqual(approved.final_decision.option_id, "option-a")
        consumed = self.approvals.get(approval.id, actor=self.requester)
        self.assertEqual(consumed.status, ApprovalRequestStatus.CONSUMED)
        self.assertEqual(
            consumed.resulting_operation_reference,
            f"decision:{approved.id}:approved:r{approved.revision}",
        )

        revisions = self.decisions.revisions(approved.id, actor=self.requester)
        self.assertEqual([item.revision for item in revisions], list(range(1, approved.revision + 1)))
        events = self.decisions.events(approved.id, actor=self.requester)
        self.assertIn("decision.approved", {item.event_type for item in events})

    async def test_rejection_and_supersession_use_durable_states_without_side_effects(self) -> None:
        first = await self._create_decision(max_calls=3, max_rounds=1)
        analyzed = await self.deliberation.deliberate(
            first.id,
            actor=self.requester,
        )
        awaiting = await self.decisions.request_approval(
            analyzed.id,
            DecisionApprovalRequest(
                reason="Review decision.",
                requirement=ApprovalRequirement(
                    quorum=1,
                    membership_roles=(MembershipRole.APPROVER,),
                    allow_self_approval=False,
                ),
            ),
            actor=self.requester,
        )
        await self.approvals.decide(
            awaiting.approval_request_id,
            ApprovalDecisionSubmit(
                outcome=ApprovalDecisionOutcome.REJECT,
                reason="Risk is not accepted.",
                idempotency_key="decision-reject-1",
            ),
            actor=self.approver,
        )
        rejected = await self.decisions.finalize_approval(
            awaiting.id,
            actor=self.requester,
        )
        self.assertEqual(rejected.status, DecisionStatus.REJECTED)
        self.assertIsNone(rejected.final_decision)

        replacement = await self._create_decision(max_calls=3, max_rounds=1)
        superseded = await self.decisions.supersede(
            rejected.id,
            DecisionSupersedeRequest(
                replacement_decision_id=replacement.id,
                reason="A replacement Decision now owns the question.",
            ),
            actor=self.requester,
        )
        self.assertEqual(superseded.status, DecisionStatus.SUPERSEDED)
        self.assertEqual(superseded.superseded_by_decision_id, replacement.id)

    async def test_model_generated_round_cannot_approve_or_create_external_actions(self) -> None:
        decision = await self._create_decision(max_calls=3, max_rounds=1)
        analyzed = await self.deliberation.deliberate(
            decision.id,
            actor=self.requester,
        )
        self.assertEqual(analyzed.status, DecisionStatus.ANALYSIS)
        self.assertIsNone(analyzed.approval_request_id)
        self.assertIsNone(analyzed.final_decision)
        self.assertEqual(analyzed.post_execution_reviews, ())


if __name__ == "__main__":
    unittest.main()
