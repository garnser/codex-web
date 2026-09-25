from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from codex_web.action_intents import ActionIntentCreate, ActionIntentStatus
from codex_web.action_providers import (
    ActionCapability,
    ActionDefinition,
    ActionRequest,
    ActionRiskClass,
)
from codex_web.autonomy import (
    AutonomyControl,
    AutonomyControlUpdate,
    AutonomyCycleOutcome,
    AutonomyObservation,
    AutonomyReasoningResult,
)
from codex_web.approval_requests import (
    ApprovalDecisionOutcome,
    ApprovalDecisionSubmit,
    ApprovalRequestStatus,
)
from codex_web.autonomy_policy import (
    AutonomyActionCharge,
    AutonomyBreakGlassPolicy,
    AutonomyBreakGlassRequest,
    AutonomyBudgetLimits,
    AutonomyExclusiveGoalScope,
    AutonomyLevel,
    AutonomyMaintenanceWindow,
    AutonomyPolicy,
    AutonomyQualificationEvidence,
    AutonomyQualificationGate,
    AutonomyScopeOverride,
)
from codex_web.compatibility import CanonicalEventEnvelope
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    HumanIdentity,
    Membership,
    MembershipRole,
    PrincipalKind,
    TenantScope,
)
from codex_web.resources import ResourceCreate, ResourceRisk, ResourceType
from codex_web.services.approval_requests import ApprovalRequestService
from codex_web.services.autonomy_controller import AutonomyController
from codex_web.services.autonomy_policy import AutonomyPolicyService
from codex_web.services.canonical_events import (
    CanonicalEventBus,
    CanonicalEventIngestionService,
)
from codex_web.services.identity import IdentityService
from codex_web.services.resources import ResourceCatalogService
from codex_web.storage.approval_requests import ApprovalRequestStore
from codex_web.storage.autonomy import AutonomyStateStore
from codex_web.storage.canonical_events import CanonicalEventStore
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.resource_catalog import ResourceCatalogStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class _Clock:
    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class _Authority:
    def role_ids_for_actor(self, actor, *, project_id=None, now=None):
        if project_id == "project-a":
            return ("release-manager",)
        return ()


class _Evidence:
    def __init__(self, rows=(), verifications=()) -> None:
        self.rows = list(rows)
        self.verifications = list(verifications)

    def list_evidence(self, actor, *, work_item_ref=None, include_inactive=True):
        return list(self.rows)

    def list_verifications(self, actor, *, work_item_ref=None):
        return list(self.verifications)


class _Execution:
    def __init__(self, definition: ActionDefinition) -> None:
        self.definition = definition
        self.prepared = []

    def resolve_contract(self, binding_id, request, *, actor):
        return (
            SimpleNamespace(id=binding_id),
            SimpleNamespace(),
            self.definition,
            request,
        )

    async def prepare(self, binding_id, request, *, actor):
        self.prepared.append((binding_id, request, actor))
        return SimpleNamespace()


class _Intents:
    def __init__(self, definition: ActionDefinition) -> None:
        self.execution = _Execution(definition)
        self.calls = []

    def create(self, payload, *, actor):
        self.calls.append((payload, actor))
        return SimpleNamespace(
            id=f"intent-{len(self.calls)}",
            status=ActionIntentStatus.PENDING,
        )


class _PolicyForController:
    def __init__(self, service: AutonomyPolicyService) -> None:
        self.service = service

    def __getattr__(self, name):
        return getattr(self.service, name)


class AutonomyPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.store = AutonomyStateStore(sqlite)
        self.resources = ResourceCatalogService(ResourceCatalogStore(sqlite))
        self.clock = _Clock(1_900_000_000.0)
        self.admin = AuthenticationActor(
            identity_id="admin",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="local",
            workspace_id="default",
            roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.LOCAL_TRUSTED,
        )
        self.actor = AuthenticationActor(
            identity_id="operator",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="local",
            workspace_id="default",
            roles=(MembershipRole.MEMBER,),
            assurance=AuthenticationAssurance.MFA,
        )
        self.prod = self.resources.create(
            ResourceCreate(
                resource_type=ResourceType.ENVIRONMENT,
                name="production",
                risk=ResourceRisk.CRITICAL,
            ),
            actor=self.admin,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def definition(
        *,
        risk: ActionRiskClass = ActionRiskClass.LOW,
        rollback: bool = True,
        verification: bool = True,
    ) -> ActionDefinition:
        return ActionDefinition(
            action_id="deploy.release",
            title="Deploy release",
            risk_class=risk,
            capabilities=ActionCapability(
                read=True,
                prepare=True,
                execute=True,
                rollback=rollback,
                verification=verification,
            ),
            reversible=rollback,
        )

    def service(self, policy: AutonomyPolicy) -> AutonomyPolicyService:
        self.store.set_control(
            AutonomyControl(policy=policy),
            actor_id="test",
        )
        return AutonomyPolicyService(
            self.store,
            authority=_Authority(),
            resources=self.resources,
            evidence=_Evidence(),
            clock=self.clock,
        )

    def request(
        self,
        *,
        resource_ids=(),
        project_id="project-a",
        goal_id=None,
        work_item_ref=None,
    ) -> ActionRequest:
        return ActionRequest(
            action_id="deploy.release",
            organization_id="local",
            workspace_id="default",
            project_id=project_id,
            goal_id=goal_id,
            work_item_ref=work_item_ref,
            resource_ids=tuple(resource_ids),
        )

    def test_role_project_action_override_is_effective_and_resource_risk_escalates(self):
        policy = AutonomyPolicy(
            level=AutonomyLevel.EXECUTE_LOW_RISK,
            required_qualification_gates=(),
            overrides=(
                AutonomyScopeOverride(
                    id="release-prod",
                    project_id="project-a",
                    role_id="release-manager",
                    action_id="deploy.release",
                    level=AutonomyLevel.EXECUTE_BROAD,
                    required_qualification_gates=(),
                ),
            ),
        )
        service = self.service(policy)

        effective, roles = service.effective(
            actor=self.actor,
            project_id="project-a",
            action_id="deploy.release",
        )
        self.assertEqual(effective.level, AutonomyLevel.EXECUTE_BROAD)
        self.assertEqual(effective.matched_override_ids, ("release-prod",))
        self.assertEqual(roles, ("release-manager",))

        decision = service.evaluate_action(
            self.definition(risk=ActionRiskClass.LOW),
            self.request(resource_ids=(self.prod.id,)),
            actor=self.actor,
        )
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.effective_risk, ActionRiskClass.CRITICAL)
        self.assertTrue(decision.approval_required)
        self.assertEqual(decision.approval_quorum, 2)
        self.assertTrue(decision.rollback_required)
        self.assertTrue(decision.verification_required)

    def test_low_risk_level_blocks_medium_execution(self):
        service = self.service(
            AutonomyPolicy(
                level=AutonomyLevel.EXECUTE_LOW_RISK,
                required_qualification_gates=(),
            )
        )
        decision = service.evaluate_action(
            self.definition(risk=ActionRiskClass.MEDIUM),
            self.request(resource_ids=()),
            actor=self.actor,
        )
        self.assertFalse(decision.allowed)
        self.assertIn("autonomy_level_blocks_execution", decision.reasons)

    def test_closed_maintenance_window_blocks_production_change(self):
        # 1900000000 is deterministic; selecting an impossible one-minute
        # weekly window makes the assertion independent of local machine TZ.
        service = self.service(
            AutonomyPolicy(
                level=AutonomyLevel.EXECUTE_BROAD,
                required_qualification_gates=(),
                maintenance_windows=(
                    AutonomyMaintenanceWindow(
                        timezone="UTC",
                        weekdays=((__import__("datetime").datetime.fromtimestamp(
                            self.clock(), __import__("datetime").timezone.utc
                        ).weekday() + 1) % 7,),
                        start_local="00:00",
                        end_local="00:01",
                    ),
                ),
            )
        )
        decision = service.evaluate_action(
            self.definition(risk=ActionRiskClass.HIGH),
            self.request(),
            actor=self.actor,
        )
        self.assertFalse(decision.allowed)
        self.assertIn("maintenance_window_closed", decision.reasons)

    def test_unproven_production_qualification_fails_closed(self):
        service = self.service(
            AutonomyPolicy(
                level=AutonomyLevel.EXECUTE_BROAD,
                required_qualification_gates=(
                    AutonomyQualificationGate.RECOVERY,
                ),
                qualifications=(
                    AutonomyQualificationEvidence(
                        gate=AutonomyQualificationGate.RECOVERY,
                        evidence_ids=("evidence-missing",),
                    ),
                ),
            )
        )
        decision = service.evaluate_action(
            self.definition(risk=ActionRiskClass.HIGH),
            self.request(),
            actor=self.actor,
        )
        self.assertFalse(decision.allowed)
        self.assertIn(
            "qualification:recovery:qualification_evidence_missing",
            decision.reasons,
        )

    def test_exclusive_goal_scope_allows_only_exact_canonical_scope(self):
        service = self.service(
            AutonomyPolicy(
                level=AutonomyLevel.EXECUTE_BOUNDED,
                required_qualification_gates=(),
                approval_required_risks=(),
                exclusive_goal_scope=AutonomyExclusiveGoalScope(
                    goal_id="goal-a",
                    goal_revision=7,
                    project_ids=("project-a",),
                    work_item_refs=("work-a", "work-b"),
                ),
            )
        )

        matched = service.evaluate_action(
            self.definition(),
            self.request(goal_id="goal-a", work_item_ref="work-a"),
            actor=self.actor,
        )
        self.assertTrue(matched.allowed)
        self.assertIn("exclusive_goal_scope:matched:goal-a", matched.reasons)

        cases = (
            (self.request(goal_id=None, work_item_ref="work-a"), "exclusive_goal_scope:goal_mismatch"),
            (self.request(goal_id="goal-b", work_item_ref="work-a"), "exclusive_goal_scope:goal_mismatch"),
            (
                self.request(project_id="project-b", goal_id="goal-a", work_item_ref="work-a"),
                "exclusive_goal_scope:project_mismatch",
            ),
            (
                self.request(goal_id="goal-a", work_item_ref="work-c"),
                "exclusive_goal_scope:work_item_mismatch",
            ),
        )
        for request, reason in cases:
            decision = service.evaluate_action(
                self.definition(),
                request,
                actor=self.actor,
            )
            self.assertFalse(decision.allowed)
            self.assertIn(reason, decision.reasons)

    def test_break_glass_cannot_widen_exclusive_goal_scope(self):
        service = self.service(
            AutonomyPolicy(
                level=AutonomyLevel.OBSERVE,
                required_qualification_gates=(),
                exclusive_goal_scope=AutonomyExclusiveGoalScope(
                    goal_id="goal-a",
                    goal_revision=7,
                    project_ids=("project-a",),
                ),
            )
        )
        service._active_break_glass = lambda **_kwargs: SimpleNamespace(id="break-glass-test")

        decision = service.evaluate_action(
            self.definition(),
            self.request(goal_id="goal-other"),
            actor=self.actor,
        )

        self.assertFalse(decision.allowed)
        self.assertIn("break_glass:autonomy_level_override", decision.reasons)
        self.assertIn("exclusive_goal_scope:goal_mismatch", decision.reasons)

    def test_budget_uses_policy_owned_action_charges(self):
        policy = AutonomyPolicy(
            level=AutonomyLevel.EXECUTE_BOUNDED,
            budget=AutonomyBudgetLimits(
                max_actions_per_cycle=2,
                max_model_tokens_per_cycle=100,
                max_model_cost_usd_per_cycle=1.0,
                max_monetary_impact_usd_per_cycle=50.0,
                max_cloud_spend_usd_per_cycle=20.0,
                max_production_changes_per_cycle=0,
            ),
            default_action_charge=AutonomyActionCharge(
                monetary_impact_usd=30.0,
                cloud_spend_usd=15.0,
            ),
            required_qualification_gates=(),
        )
        service = self.service(policy)
        definition = self.definition(risk=ActionRiskClass.LOW)
        decision = service.evaluate_action(
            definition,
            self.request(project_id=None),
            actor=self.actor,
        )
        result = AutonomyReasoningResult(
            model_input_tokens=60,
            model_output_tokens=50,
            model_cost_usd=0.5,
        )
        usage = service.cycle_budget_usage((decision, decision), result)
        violations = service.budget_violations(
            (decision, decision),
            usage,
            hard_action_limit=4,
        )
        self.assertIn("maximum_model_tokens_per_cycle_exceeded", violations)
        self.assertIn("maximum_monetary_impact_per_cycle_exceeded", violations)
        self.assertIn("maximum_cloud_spend_per_cycle_exceeded", violations)


class AutonomyLevelControllerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = AutonomyStateStore(
            SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        )
        self.actor = AuthenticationActor(
            identity_id="operator",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="org-a",
            workspace_id="ws-a",
            roles=(MembershipRole.MEMBER,),
            assurance=AuthenticationAssurance.MFA,
        )
        self.definition = ActionDefinition(
            action_id="deploy.release",
            title="Deploy",
            risk_class=ActionRiskClass.LOW,
            capabilities=ActionCapability(
                prepare=True,
                execute=True,
                rollback=True,
                verification=True,
            ),
            reversible=True,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def event(event_id="evt-1"):
        return CanonicalEventEnvelope(
            event_id=event_id,
            event_type="work.transition",
            occurred_at=1.0,
            source="test",
            correlation_id="corr",
            tenant_id="org-a",
            workspace_id="ws-a",
            payload={"project_id": "project-a"},
        )

    def controller(self, level: AutonomyLevel):
        self.store.set_control(
            AutonomyControl(
                cooldown_seconds=0,
                policy=AutonomyPolicy(
                    level=level,
                    required_qualification_gates=(),
                    approval_required_risks=(),
                ),
            ),
            actor_id="test",
        )
        policy = AutonomyPolicyService(self.store)
        intents = _Intents(self.definition)
        return AutonomyController(
            self.store,
            action_intents=intents,
            policy=_PolicyForController(policy),
        ), intents

    async def test_observe_never_invokes_reasoning(self):
        controller, _intents = self.controller(AutonomyLevel.OBSERVE)
        calls = 0

        async def reasoner(*_args):
            nonlocal calls
            calls += 1
            return AutonomyReasoningResult()

        cycle = await controller.process(
            self.event(),
            AutonomyObservation(
                deterministic_resolved=False,
                reasoning_score=1.0,
                reason="ambiguous",
            ),
            reasoner=reasoner,
            actor=self.actor,
        )
        self.assertEqual(cycle.outcome, AutonomyCycleOutcome.SKIPPED)
        self.assertEqual(cycle.reason, "autonomy_level_observe")
        self.assertEqual(calls, 0)

    async def test_recommend_reasons_but_never_prepares_or_queues(self):
        controller, intents = self.controller(AutonomyLevel.RECOMMEND)
        action = ActionIntentCreate(
            binding_id="binding-a",
            request=ActionRequest(
                action_id="deploy.release",
                organization_id="org-a",
                workspace_id="ws-a",
                project_id="project-a",
            ),
        )

        async def reasoner(*_args):
            return AutonomyReasoningResult(
                summary="recommend deployment",
                actions=(action,),
            )

        cycle = await controller.process(
            self.event("recommend"),
            AutonomyObservation(
                deterministic_resolved=False,
                reasoning_score=1.0,
                reason="needs recommendation",
            ),
            reasoner=reasoner,
            actor=self.actor,
        )
        self.assertEqual(cycle.outcome, AutonomyCycleOutcome.RECOMMENDED)
        self.assertEqual(intents.execution.prepared, [])
        self.assertEqual(intents.calls, [])

    async def test_prepare_runs_provider_preflight_but_never_queues_intent(self):
        controller, intents = self.controller(AutonomyLevel.PREPARE)
        action = ActionIntentCreate(
            binding_id="binding-a",
            request=ActionRequest(
                action_id="deploy.release",
                organization_id="org-a",
                workspace_id="ws-a",
                project_id="project-a",
            ),
        )

        async def reasoner(*_args):
            return AutonomyReasoningResult(actions=(action,))

        cycle = await controller.process(
            self.event("prepare"),
            AutonomyObservation(
                deterministic_resolved=False,
                reasoning_score=1.0,
                reason="prepare change",
            ),
            reasoner=reasoner,
            actor=self.actor,
        )
        self.assertEqual(cycle.outcome, AutonomyCycleOutcome.PREPARED)
        self.assertEqual(len(intents.execution.prepared), 1)
        self.assertEqual(intents.calls, [])


class CanonicalAutonomyApprovalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.autonomy_store = AutonomyStateStore(self.sqlite)
        self.identity_store = IdentityStateStore(self.sqlite)
        self.identity = IdentityService(self.identity_store)
        self.identity.bootstrap_local()
        event_store = CanonicalEventStore(self.sqlite)
        self.events = CanonicalEventIngestionService(CanonicalEventBus(event_store))
        self.approvals = ApprovalRequestService(
            ApprovalRequestStore(self.sqlite),
            self.identity,
            self.events,
        )
        self.requester = self._human_actor("requester", MembershipRole.MEMBER)
        self.approver_a = self._human_actor("approver-a", MembershipRole.APPROVER)
        self.approver_b = self._human_actor("approver-b", MembershipRole.APPROVER)

    def tearDown(self) -> None:
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

    def policy_service(self, policy: AutonomyPolicy) -> AutonomyPolicyService:
        self.autonomy_store.set_control(
            AutonomyControl(policy=policy),
            actor_id="test",
        )
        return AutonomyPolicyService(
            self.autonomy_store,
            approvals=self.approvals,
            clock=lambda: 2_000_000_000.0,
        )

    @staticmethod
    def critical_definition() -> ActionDefinition:
        return ActionDefinition(
            action_id="production.deploy",
            title="Production deploy",
            risk_class=ActionRiskClass.CRITICAL,
            capabilities=ActionCapability(
                prepare=True,
                execute=True,
                rollback=True,
                verification=True,
            ),
            reversible=True,
        )

    @staticmethod
    def action() -> ActionIntentCreate:
        return ActionIntentCreate(
            binding_id="deploy-provider",
            request=ActionRequest(
                action_id="production.deploy",
                organization_id="local",
                workspace_id="default",
                project_id="project-a",
                idempotency_key="deploy-r1",
            ),
        )

    async def test_critical_action_requires_distinct_human_canonical_quorum(self):
        service = self.policy_service(
            AutonomyPolicy(
                level=AutonomyLevel.EXECUTE_BROAD,
                required_qualification_gates=(),
            )
        )
        action = self.action()
        decision = service.evaluate_action(
            self.critical_definition(),
            action.request,
            actor=self.requester,
        )
        approval = await service.ensure_action_approval(
            action,
            decision,
            actor=self.requester,
        )
        self.assertIsNotNone(approval)
        self.assertEqual(approval.requirement.quorum, 2)
        self.assertTrue(approval.requirement.distinct_humans)
        self.assertFalse(approval.requirement.allow_self_approval)
        self.assertEqual(approval.status, ApprovalRequestStatus.PENDING)

        approval = await self.approvals.decide(
            approval.id,
            ApprovalDecisionSubmit(
                outcome=ApprovalDecisionOutcome.APPROVE,
                reason="verified release evidence",
                idempotency_key="approve-a",
            ),
            actor=self.approver_a,
        )
        self.assertEqual(
            approval.status,
            ApprovalRequestStatus.PARTIALLY_APPROVED,
        )
        approval = await self.approvals.decide(
            approval.id,
            ApprovalDecisionSubmit(
                outcome=ApprovalDecisionOutcome.APPROVE,
                reason="independent production review",
                idempotency_key="approve-b",
            ),
            actor=self.approver_b,
        )
        self.assertEqual(approval.status, ApprovalRequestStatus.APPROVED)

        consumed = await service.consume_action_approval(
            approval,
            action,
            actor=self.requester,
            cycle_id="cycle-a",
        )
        self.assertEqual(consumed.status, ApprovalRequestStatus.CONSUMED)

    async def test_break_glass_is_approval_backed_time_bounded_and_consumed(self):
        service = self.policy_service(
            AutonomyPolicy(
                level=AutonomyLevel.OBSERVE,
                required_qualification_gates=(),
                break_glass=AutonomyBreakGlassPolicy(
                    enabled=True,
                    quorum=2,
                    max_duration_seconds=600,
                ),
            )
        )
        approval = await service.request_break_glass(
            AutonomyBreakGlassRequest(
                project_id="project-a",
                reason="restore a critical production service",
            ),
            actor=self.requester,
        )
        self.assertEqual(approval.requirement.quorum, 2)
        self.assertFalse(approval.requirement.allow_self_approval)

        for index, approver in enumerate((self.approver_a, self.approver_b), start=1):
            approval = await self.approvals.decide(
                approval.id,
                ApprovalDecisionSubmit(
                    outcome=ApprovalDecisionOutcome.APPROVE,
                    reason="emergency authorization",
                    idempotency_key=f"break-glass-{index}",
                ),
                actor=approver,
            )
        self.assertEqual(approval.status, ApprovalRequestStatus.APPROVED)

        grant = await service.activate_break_glass(
            approval.id,
            actor=self.requester,
        )
        self.assertEqual(grant.approval_request_id, approval.id)
        self.assertLessEqual(
            grant.expires_at - grant.activated_at,
            600,
        )
        canonical = self.approvals.get(approval.id, actor=self.requester)
        self.assertEqual(canonical.status, ApprovalRequestStatus.CONSUMED)
        self.assertEqual(canonical.resulting_operation_reference, grant.id)


if __name__ == "__main__":
    unittest.main()
