from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from codex_web.action_intents import ActionDecisionOutcome, ActionDecisionSnapshot, ActionIntentCreate
from codex_web.action_providers import ActionDefinition, ActionRequest, ActionRiskClass
from codex_web.approval_requests import (
    ApprovalConsumeRequest,
    ApprovalRequest,
    ApprovalRequestCreate,
    ApprovalRequestStatus,
    ApprovalRequirement,
    ApprovalTarget,
)
from codex_web.artifact_evidence import EvidenceLifecycle, EvidenceResult, VerificationResult
from codex_web.autonomy import AutonomyControl, AutonomyPauseScope, AutonomyReasoningResult
from codex_web.autonomy_policy import (
    ACTION_RISK_RANK,
    AUTONOMY_LEVEL_RANK,
    AutonomyActionCharge,
    AutonomyActionDecision,
    AutonomyBreakGlassGrant,
    AutonomyBreakGlassRequest,
    AutonomyBudgetLimits,
    AutonomyCycleBudgetUsage,
    AutonomyLevel,
    AutonomyMaintenanceWindow,
    AutonomyPolicy,
    AutonomyQualificationEvidence,
    AutonomyQualificationGate,
    AutonomyQualificationOutcome,
    EffectiveAutonomyPolicy,
)
from codex_web.identity import AuthenticationActor, PrincipalKind, TenantScope
from codex_web.resources import ResourceRisk
from codex_web.services.approval_requests import ApprovalRequestService
from codex_web.services.artifact_evidence import ArtifactEvidenceService
from codex_web.services.authority_roles import AuthorityRoleService
from codex_web.services.resources import ResourceCatalogService
from codex_web.storage.autonomy import AutonomyStateStore


class AutonomyPolicyError(RuntimeError):
    pass


class AutonomyBreakGlassError(AutonomyPolicyError):
    pass


class AutonomyPolicyService:
    """Deterministic scoped policy evaluator for bounded autonomous side effects."""

    def __init__(
        self,
        store: AutonomyStateStore,
        *,
        authority: AuthorityRoleService | None = None,
        resources: ResourceCatalogService | None = None,
        evidence: ArtifactEvidenceService | None = None,
        approvals: ApprovalRequestService | None = None,
        clock=time.time,
    ) -> None:
        self.store = store
        self.authority = authority
        self.resources = resources
        self.evidence = evidence
        self.approvals = approvals
        self.clock = clock

    def policy(self) -> AutonomyPolicy:
        return self.store.load().control.policy

    @staticmethod
    def _merge_budget(
        current: AutonomyBudgetLimits,
        patch: Any,
    ) -> AutonomyBudgetLimits:
        updates = {
            key: value
            for key, value in patch.model_dump(mode="python").items()
            if value is not None
        }
        return current.model_copy(update=updates)

    def _role_ids(
        self,
        actor: AuthenticationActor,
        *,
        project_id: str | None,
        now: float,
    ) -> tuple[str, ...]:
        if self.authority is None:
            return ()
        return self.authority.role_ids_for_actor(
            actor,
            project_id=project_id,
            now=now,
        )

    @staticmethod
    def _override_matches(
        override,
        *,
        project_id: str | None,
        role_ids: tuple[str, ...],
        action_id: str | None,
    ) -> bool:
        if override.project_id is not None and override.project_id != project_id:
            return False
        if override.role_id is not None and override.role_id not in role_ids:
            return False
        if override.action_id is not None and override.action_id != action_id:
            return False
        return True

    def effective(
        self,
        *,
        actor: AuthenticationActor,
        project_id: str | None = None,
        action_id: str | None = None,
        now: float | None = None,
    ) -> tuple[EffectiveAutonomyPolicy, tuple[str, ...]]:
        current = float(self.clock()) if now is None else float(now)
        policy = self.policy()
        role_ids = self._role_ids(actor, project_id=project_id, now=current)

        level = policy.level
        budget = policy.budget
        charge = policy.default_action_charge
        production_change = charge.production_change
        approval_required_risks = policy.approval_required_risks
        approval_action_ids = policy.approval_action_ids
        approval_quorum = 0
        distinct_humans = policy.distinct_humans
        allow_self_approval = policy.allow_self_approval
        required_assurance = policy.required_assurance
        rollback_required_risks = policy.rollback_required_risks
        verification_required_risks = policy.verification_required_risks
        maintenance_windows = policy.maintenance_windows
        required_qualification_gates = policy.required_qualification_gates
        qualifications = policy.qualifications
        multi_instance = policy.multi_instance
        exclusive_goal_scope = policy.exclusive_goal_scope
        matched: list[str] = []

        candidates = [
            (index, item)
            for index, item in enumerate(policy.overrides)
            if self._override_matches(
                item,
                project_id=project_id,
                role_ids=role_ids,
                action_id=action_id,
            )
        ]
        candidates.sort(key=lambda row: (row[1].specificity, row[0]))
        for _index, item in candidates:
            matched.append(item.id)
            if item.level is not None:
                level = item.level
            budget = self._merge_budget(budget, item.budget)
            if item.action_charge is not None:
                charge = item.action_charge
                production_change = item.action_charge.production_change
            if item.production_change is not None:
                production_change = item.production_change
            if item.approval_required_risks is not None:
                approval_required_risks = item.approval_required_risks
            if item.approval_action_ids is not None:
                approval_action_ids = item.approval_action_ids
            if item.approval_quorum is not None:
                approval_quorum = item.approval_quorum
            if item.distinct_humans is not None:
                distinct_humans = item.distinct_humans
            if item.allow_self_approval is not None:
                allow_self_approval = item.allow_self_approval
            if item.required_assurance is not None:
                required_assurance = item.required_assurance
            if item.rollback_required_risks is not None:
                rollback_required_risks = item.rollback_required_risks
            if item.verification_required_risks is not None:
                verification_required_risks = item.verification_required_risks
            if item.maintenance_windows is not None:
                maintenance_windows = item.maintenance_windows
            if item.required_qualification_gates is not None:
                required_qualification_gates = item.required_qualification_gates
            if item.qualifications is not None:
                qualifications = item.qualifications
            if item.multi_instance is not None:
                multi_instance = item.multi_instance

        if multi_instance and AutonomyQualificationGate.REPLICATED_OWNERSHIP not in required_qualification_gates:
            required_qualification_gates = (
                *required_qualification_gates,
                AutonomyQualificationGate.REPLICATED_OWNERSHIP,
            )

        return (
            EffectiveAutonomyPolicy(
                policy_fingerprint=policy.fingerprint(),
                level=level,
                budget=budget,
                action_charge=charge,
                production_change=production_change,
                approval_required_risks=approval_required_risks,
                approval_action_ids=approval_action_ids,
                approval_quorum=approval_quorum,
                distinct_humans=distinct_humans,
                allow_self_approval=allow_self_approval,
                required_assurance=required_assurance,
                approval_expiry_seconds=policy.approval_expiry_seconds,
                rollback_required_risks=rollback_required_risks,
                verification_required_risks=verification_required_risks,
                require_preflight=policy.require_preflight,
                maintenance_windows=maintenance_windows,
                required_qualification_gates=required_qualification_gates,
                qualifications=qualifications,
                multi_instance=multi_instance,
                exclusive_goal_scope=exclusive_goal_scope,
                matched_override_ids=tuple(matched),
            ),
            role_ids,
        )

    @staticmethod
    def _risk_allowed(level: AutonomyLevel, risk: ActionRiskClass) -> bool:
        if level in {
            AutonomyLevel.OBSERVE,
            AutonomyLevel.RECOMMEND,
            AutonomyLevel.PREPARE,
        }:
            return False
        if level == AutonomyLevel.EXECUTE_LOW_RISK:
            return risk == ActionRiskClass.LOW
        if level == AutonomyLevel.EXECUTE_BOUNDED:
            return ACTION_RISK_RANK[risk] <= ACTION_RISK_RANK[ActionRiskClass.MEDIUM]
        return True

    def _resource_risk(
        self,
        request: ActionRequest,
        actor: AuthenticationActor,
    ) -> tuple[ActionRiskClass, tuple[str, ...]]:
        risk = ActionRiskClass.LOW
        reasons: list[str] = []
        if not request.resource_ids:
            return risk, ()
        if self.resources is None:
            return ActionRiskClass.CRITICAL, ("resource_catalog_unavailable",)
        for resource_id in request.resource_ids:
            resource = self.resources.get(resource_id, actor)
            observed = ActionRiskClass(ResourceRisk(resource.risk).value)
            if ACTION_RISK_RANK[observed] > ACTION_RISK_RANK[risk]:
                risk = observed
            reasons.append(f"resource:{resource.id}:risk:{observed.value}")
        return risk, tuple(reasons)

    @staticmethod
    def _window_open(
        window: AutonomyMaintenanceWindow,
        *,
        now: float,
    ) -> bool:
        local = datetime.fromtimestamp(now, ZoneInfo(window.timezone))
        start = datetime.combine(local.date(), datetime.fromisoformat(
            f"2000-01-01T{window.start_local}"
        ).time(), tzinfo=local.tzinfo)
        end = datetime.combine(local.date(), datetime.fromisoformat(
            f"2000-01-01T{window.end_local}"
        ).time(), tzinfo=local.tzinfo)
        if start.time() <= end.time():
            return local.weekday() in window.weekdays and start <= local <= end

        # Overnight window: the after-midnight segment belongs to the previous
        # configured weekday.
        if local.time() >= start.time():
            return local.weekday() in window.weekdays
        previous = (local.weekday() - 1) % 7
        return local.time() <= end.time() and previous in window.weekdays

    def _active_break_glass(
        self,
        *,
        actor: AuthenticationActor,
        project_id: str | None,
        policy_fingerprint: str,
        now: float,
    ) -> AutonomyBreakGlassGrant | None:
        grants = self.store.active_break_glass_grants(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            project_id=project_id,
            now=now,
        )
        matching = [
            item
            for item in grants
            if item.policy_fingerprint == policy_fingerprint
        ]
        if not matching:
            return None
        return max(matching, key=lambda item: (item.activated_at, item.id))

    def _qualification_outcomes(
        self,
        effective: EffectiveAutonomyPolicy,
        *,
        actor: AuthenticationActor,
        project_id: str | None,
        now: float,
    ) -> tuple[AutonomyQualificationOutcome, ...]:
        configured = {item.gate: item for item in effective.qualifications}
        evidence_rows = (
            {item.id: item for item in self.evidence.list_evidence(actor, include_inactive=True)}
            if self.evidence is not None
            else {}
        )
        verifications = (
            self.evidence.list_verifications(actor)
            if self.evidence is not None
            else []
        )
        outcomes: list[AutonomyQualificationOutcome] = []

        for gate in effective.required_qualification_gates:
            requirement = configured.get(gate)
            if requirement is None:
                outcomes.append(
                    AutonomyQualificationOutcome(
                        gate=gate,
                        satisfied=False,
                        reason="qualification_gate_unconfigured",
                    )
                )
                continue

            accepted_ids: list[str] = []
            verification_ids: list[str] = []
            failure: str | None = None
            for evidence_id in requirement.evidence_ids:
                row = evidence_rows.get(evidence_id)
                if row is None:
                    failure = "qualification_evidence_missing"
                    break
                if row.project_id not in {None, project_id}:
                    failure = "qualification_evidence_scope_mismatch"
                    break
                if row.lifecycle != EvidenceLifecycle.VALID:
                    failure = f"qualification_evidence_{row.lifecycle.value}"
                    break
                if row.result != EvidenceResult.PASS:
                    failure = f"qualification_evidence_result_{row.result.value}"
                    break
                if (
                    requirement.max_age_seconds is not None
                    and now - row.observed_at > requirement.max_age_seconds
                ):
                    failure = "qualification_evidence_stale"
                    break
                accepted_ids.append(row.id)
                if requirement.require_independent_verification:
                    matches = [
                        verification
                        for verification in verifications
                        if row.id in verification.evidence_ids
                        and verification.independent
                        and verification.result == VerificationResult.VERIFIED
                    ]
                    if not matches:
                        failure = "qualification_independent_verification_missing"
                        break
                    verification_ids.extend(item.id for item in matches)

            outcomes.append(
                AutonomyQualificationOutcome(
                    gate=gate,
                    satisfied=failure is None,
                    evidence_ids=tuple(accepted_ids),
                    verification_ids=tuple(dict.fromkeys(verification_ids)),
                    reason=failure,
                )
            )
        return tuple(outcomes)

    def evaluate_action(
        self,
        definition: ActionDefinition,
        request: ActionRequest,
        *,
        actor: AuthenticationActor,
        now: float | None = None,
    ) -> AutonomyActionDecision:
        current = float(self.clock()) if now is None else float(now)
        effective, role_ids = self.effective(
            actor=actor,
            project_id=request.project_id,
            action_id=request.action_id,
            now=current,
        )
        resource_risk, resource_reasons = self._resource_risk(request, actor)
        effective_risk = (
            resource_risk
            if ACTION_RISK_RANK[resource_risk] > ACTION_RISK_RANK[definition.risk_class]
            else definition.risk_class
        )
        production_change = (
            effective.production_change
            or ACTION_RISK_RANK[effective_risk] >= ACTION_RISK_RANK[ActionRiskClass.HIGH]
        )
        break_glass = self._active_break_glass(
            actor=actor,
            project_id=request.project_id,
            policy_fingerprint=effective.policy_fingerprint,
            now=current,
        )
        reasons = [
            f"level:{effective.level.value}",
            f"effective_risk:{effective_risk.value}",
            *resource_reasons,
        ]
        allowed = self._risk_allowed(effective.level, effective_risk)
        control = self.store.load().control
        scoped_pause = next(
            (
                item
                for item in control.scoped_pauses
                if item.active(current)
                and (
                    (
                        item.scope == AutonomyPauseScope.IDENTITY
                        and item.scope_id == actor.identity_id
                    )
                    or (
                        item.scope == AutonomyPauseScope.PROJECT
                        and item.scope_id == request.project_id
                    )
                    or (
                        item.scope == AutonomyPauseScope.RESOURCE
                        and item.scope_id in set(request.resource_ids)
                    )
                )
            ),
            None,
        )
        if scoped_pause is not None:
            allowed = False
            reasons.append(
                f"scoped_pause:{scoped_pause.scope.value}:{scoped_pause.scope_id}"
            )
        if not allowed:
            if break_glass is not None:
                allowed = True
                reasons.append("break_glass:autonomy_level_override")
            else:
                reasons.append("autonomy_level_blocks_execution")

        if production_change and effective.maintenance_windows:
            open_window = any(
                self._window_open(window, now=current)
                for window in effective.maintenance_windows
            )
            if not open_window:
                if break_glass is not None:
                    reasons.append("break_glass:maintenance_window_override")
                else:
                    allowed = False
                    reasons.append("maintenance_window_closed")

        qualification_outcomes: tuple[AutonomyQualificationOutcome, ...] = ()
        if (
            production_change
            and effective.level == AutonomyLevel.EXECUTE_BROAD
        ):
            qualification_outcomes = self._qualification_outcomes(
                effective,
                actor=actor,
                project_id=request.project_id,
                now=current,
            )
            failed = [item for item in qualification_outcomes if not item.satisfied]
            if failed:
                if break_glass is not None:
                    reasons.append("break_glass:qualification_override")
                else:
                    allowed = False
                    reasons.extend(
                        f"qualification:{item.gate.value}:{item.reason}"
                        for item in failed
                    )

        approval_required = (
            effective_risk in set(effective.approval_required_risks)
            or request.action_id in set(effective.approval_action_ids)
        )
        if effective.approval_quorum > 0:
            approval_quorum = effective.approval_quorum
        elif effective_risk == ActionRiskClass.CRITICAL:
            approval_quorum = self.policy().critical_risk_approval_quorum
        elif effective_risk == ActionRiskClass.HIGH:
            approval_quorum = self.policy().high_risk_approval_quorum
        else:
            approval_quorum = 1

        rollback_required = effective_risk in set(effective.rollback_required_risks)
        verification_required = effective_risk in set(effective.verification_required_risks)
        if rollback_required and (
            not definition.reversible or not definition.capabilities.rollback
        ):
            allowed = False
            reasons.append("required_rollback_capability_unavailable")
        if verification_required and not definition.capabilities.verification:
            allowed = False
            reasons.append("required_verification_capability_unavailable")
        if effective.require_preflight and not definition.capabilities.prepare:
            allowed = False
            reasons.append("required_preflight_capability_unavailable")

        exclusive = effective.exclusive_goal_scope
        if exclusive is not None:
            if request.goal_id != exclusive.goal_id:
                allowed = False
                reasons.append("exclusive_goal_scope:goal_mismatch")
            elif request.project_id not in set(exclusive.project_ids):
                allowed = False
                reasons.append("exclusive_goal_scope:project_mismatch")
            elif (
                exclusive.work_item_refs
                and request.work_item_ref not in set(exclusive.work_item_refs)
            ):
                allowed = False
                reasons.append("exclusive_goal_scope:work_item_mismatch")
            else:
                reasons.append(f"exclusive_goal_scope:matched:{exclusive.goal_id}")

        return AutonomyActionDecision(
            allowed=allowed,
            effective_level=effective.level,
            effective_risk=effective_risk,
            production_change=production_change,
            policy_fingerprint=effective.policy_fingerprint,
            matched_override_ids=effective.matched_override_ids,
            role_ids=role_ids,
            approval_required=approval_required,
            approval_quorum=approval_quorum if approval_required else 0,
            approval_expiry_seconds=(
                effective.approval_expiry_seconds if approval_required else 0.0
            ),
            required_assurance=effective.required_assurance,
            distinct_humans=effective.distinct_humans,
            allow_self_approval=effective.allow_self_approval,
            rollback_required=rollback_required,
            verification_required=verification_required,
            preflight_required=effective.require_preflight,
            budget=effective.budget,
            action_charge=effective.action_charge.model_copy(
                update={"production_change": production_change}
            ),
            qualification_outcomes=qualification_outcomes,
            break_glass_grant_id=break_glass.id if break_glass else None,
            reasons=tuple(reasons),
        )

    def action_policy_snapshot(
        self,
        decision: AutonomyActionDecision,
    ) -> ActionDecisionSnapshot:
        return ActionDecisionSnapshot(
            decision_id=f"autonomy-policy:{decision.policy_fingerprint[:24]}",
            outcome=(
                ActionDecisionOutcome.ALLOW
                if decision.allowed
                else ActionDecisionOutcome.DENY
            ),
            source="canonical:autonomy-policy",
            reason=decision.reasons[-1] if decision.reasons else None,
            role_ids=decision.role_ids,
            reasons=decision.reasons,
            evaluated_at=float(self.clock()),
        )

    @staticmethod
    def action_approval_target(payload: ActionIntentCreate) -> ApprovalTarget:
        canonical = {
            "binding_id": payload.binding_id,
            "request": payload.request.model_dump(mode="json"),
            "work_item_ref": payload.work_item_ref,
            "goal_id": payload.goal_id,
            "decision_id": payload.decision_id,
            "execution_id": payload.execution_id,
            "expected_evidence": [
                item.model_dump(mode="json") for item in payload.expected_evidence
            ],
            "verification_required": payload.verification_required,
            "rollback_required": payload.rollback_required,
            "timeout_seconds": payload.timeout_seconds,
            "retry_policy": payload.retry_policy.model_dump(mode="json"),
            "work_item_success": (
                payload.work_item_success.model_dump(mode="json")
                if payload.work_item_success is not None
                else None
            ),
        }
        digest = hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return ApprovalTarget(
            operation="autonomy.execute",
            object_type="action_request",
            object_id=f"{payload.binding_id}:{payload.request.action_id}",
            target_version=payload.request.idempotency_key,
            target_digest=digest,
            resource_ids=payload.request.resource_ids,
        )

    async def ensure_action_approval(
        self,
        payload: ActionIntentCreate,
        decision: AutonomyActionDecision,
        *,
        actor: AuthenticationActor,
    ) -> ApprovalRequest | None:
        if not decision.approval_required:
            return None
        if self.approvals is None:
            raise AutonomyPolicyError(
                "canonical ApprovalRequest service is unavailable for required approval"
            )
        target = self.action_approval_target(payload)
        candidates = [
            item
            for item in self.approvals.list(actor)
            if item.target_fingerprint == target.fingerprint()
            and item.project_id == payload.request.project_id
            and item.status
            in {
                ApprovalRequestStatus.PENDING,
                ApprovalRequestStatus.PARTIALLY_APPROVED,
                ApprovalRequestStatus.APPROVED,
            }
        ]
        candidates.sort(key=lambda item: (item.created_at, item.id), reverse=True)
        approved = next(
            (item for item in candidates if item.status == ApprovalRequestStatus.APPROVED),
            None,
        )
        if approved is not None:
            return approved
        if candidates:
            return candidates[0]

        request = ApprovalRequestCreate(
            target=target,
            project_id=payload.request.project_id,
            reason=(
                f"Autonomy policy requires approval before {payload.request.action_id} "
                f"({decision.effective_risk.value} risk, level {decision.effective_level.value})."
            ),
            policy_source=f"autonomy:{decision.policy_fingerprint}",
            authority_source="canonical:role-authority",
            requirement=ApprovalRequirement(
                quorum=decision.approval_quorum,
                required_assurance=decision.required_assurance,
                distinct_humans=decision.distinct_humans,
                allow_self_approval=decision.allow_self_approval,
            ),
            expires_at=float(self.clock()) + decision.approval_expiry_seconds,
        )
        return await self.approvals.create_for_identity(
            request,
            requester_identity_id=actor.identity_id,
            scope=TenantScope(
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
            ),
        )

    async def consume_action_approval(
        self,
        approval: ApprovalRequest,
        payload: ActionIntentCreate,
        *,
        actor: AuthenticationActor,
        cycle_id: str,
    ) -> ApprovalRequest:
        if self.approvals is None:
            raise AutonomyPolicyError("canonical ApprovalRequest service is unavailable")
        target = self.action_approval_target(payload)
        return await self.approvals.consume(
            approval.id,
            ApprovalConsumeRequest(
                target=target,
                idempotency_key=f"autonomy:{cycle_id}:{target.target_digest}",
                resulting_operation_reference=(
                    f"autonomy-action:{target.target_digest}"
                ),
            ),
            actor=actor,
        )

    def cycle_budget_usage(
        self,
        decisions: tuple[AutonomyActionDecision, ...],
        result: AutonomyReasoningResult,
    ) -> AutonomyCycleBudgetUsage:
        return AutonomyCycleBudgetUsage(
            actions=len(decisions),
            model_tokens=result.model_tokens,
            model_cost_usd=result.model_cost_usd,
            monetary_impact_usd=sum(
                item.action_charge.monetary_impact_usd for item in decisions
            ),
            cloud_spend_usd=sum(
                item.action_charge.cloud_spend_usd for item in decisions
            ),
            production_changes=sum(
                1 for item in decisions if item.action_charge.production_change
            ),
        )

    @staticmethod
    def budget_violations(
        decisions: tuple[AutonomyActionDecision, ...],
        usage: AutonomyCycleBudgetUsage,
        *,
        hard_action_limit: int,
        default_limits: AutonomyBudgetLimits | None = None,
    ) -> tuple[str, ...]:
        if decisions:
            limits = decisions[0].budget
            # Mixed action scopes fail against the strictest matched budget.
            values = [item.budget for item in decisions]
            limits = AutonomyBudgetLimits(
                max_actions_per_cycle=min(item.max_actions_per_cycle for item in values),
                max_model_tokens_per_cycle=min(item.max_model_tokens_per_cycle for item in values),
                max_model_cost_usd_per_cycle=min(item.max_model_cost_usd_per_cycle for item in values),
                max_monetary_impact_usd_per_cycle=min(item.max_monetary_impact_usd_per_cycle for item in values),
                max_cloud_spend_usd_per_cycle=min(item.max_cloud_spend_usd_per_cycle for item in values),
                max_production_changes_per_cycle=min(item.max_production_changes_per_cycle for item in values),
            )
        else:
            limits = default_limits or AutonomyBudgetLimits(
                max_actions_per_cycle=hard_action_limit
            )

        violations: list[str] = []
        if usage.actions > min(hard_action_limit, limits.max_actions_per_cycle):
            violations.append("maximum_actions_per_cycle_exceeded")
        if usage.model_tokens > limits.max_model_tokens_per_cycle:
            violations.append("maximum_model_tokens_per_cycle_exceeded")
        if usage.model_cost_usd > limits.max_model_cost_usd_per_cycle:
            violations.append("maximum_model_cost_per_cycle_exceeded")
        if usage.monetary_impact_usd > limits.max_monetary_impact_usd_per_cycle:
            violations.append("maximum_monetary_impact_per_cycle_exceeded")
        if usage.cloud_spend_usd > limits.max_cloud_spend_usd_per_cycle:
            violations.append("maximum_cloud_spend_per_cycle_exceeded")
        if usage.production_changes > limits.max_production_changes_per_cycle:
            violations.append("maximum_production_changes_per_cycle_exceeded")
        return tuple(violations)

    async def request_break_glass(
        self,
        payload: AutonomyBreakGlassRequest,
        *,
        actor: AuthenticationActor,
    ) -> ApprovalRequest:
        policy = self.policy()
        if not policy.break_glass.enabled:
            raise AutonomyBreakGlassError("break-glass is disabled by autonomy policy")
        if self.approvals is None:
            raise AutonomyBreakGlassError("canonical ApprovalRequest service is unavailable")
        if actor.principal_kind != PrincipalKind.HUMAN:
            raise AutonomyBreakGlassError("break-glass must be requested by a human identity")
        fingerprint = policy.fingerprint()
        target = ApprovalTarget(
            operation="autonomy.break_glass",
            object_type="autonomy_policy",
            object_id=payload.project_id or "workspace",
            target_version=fingerprint,
            target_digest=hashlib.sha256(
                f"{actor.organization_id}:{actor.workspace_id}:{payload.project_id or ''}:{fingerprint}".encode(
                    "utf-8"
                )
            ).hexdigest(),
        )
        return await self.approvals.create(
            ApprovalRequestCreate(
                target=target,
                project_id=payload.project_id,
                reason=payload.reason,
                policy_source=f"autonomy:{fingerprint}",
                authority_source="canonical:role-authority",
                requirement=ApprovalRequirement(
                    quorum=policy.break_glass.quorum,
                    required_assurance=policy.break_glass.required_assurance,
                    distinct_humans=True,
                    allow_self_approval=False,
                ),
                expires_at=float(self.clock()) + policy.break_glass.max_duration_seconds,
            ),
            requester=actor,
        )

    async def activate_break_glass(
        self,
        approval_request_id: str,
        *,
        actor: AuthenticationActor,
    ) -> AutonomyBreakGlassGrant:
        policy = self.policy()
        if not policy.break_glass.enabled:
            raise AutonomyBreakGlassError("break-glass is disabled by autonomy policy")
        if self.approvals is None:
            raise AutonomyBreakGlassError("canonical ApprovalRequest service is unavailable")
        if actor.principal_kind != PrincipalKind.HUMAN:
            raise AutonomyBreakGlassError("break-glass activation requires a human identity")
        approval = self.approvals.get(approval_request_id, actor=actor)
        if approval.status != ApprovalRequestStatus.APPROVED:
            raise AutonomyBreakGlassError("break-glass ApprovalRequest is not approved")
        if approval.target.operation != "autonomy.break_glass":
            raise AutonomyBreakGlassError("ApprovalRequest is not a break-glass authorization")
        fingerprint = policy.fingerprint()
        if approval.target.target_version != fingerprint:
            raise AutonomyBreakGlassError(
                "break-glass approval targets a stale autonomy policy revision"
            )
        now = float(self.clock())
        grant = AutonomyBreakGlassGrant(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            project_id=approval.project_id,
            approval_request_id=approval.id,
            policy_fingerprint=fingerprint,
            activated_by_identity_id=actor.identity_id,
            activated_at=now,
            expires_at=min(
                approval.expires_at or (now + policy.break_glass.max_duration_seconds),
                now + policy.break_glass.max_duration_seconds,
            ),
        )
        await self.approvals.consume(
            approval.id,
            ApprovalConsumeRequest(
                target=approval.target,
                idempotency_key=f"break-glass:{grant.id}",
                resulting_operation_reference=grant.id,
            ),
            actor=actor,
        )
        self.store.add_break_glass_grant(grant)
        return grant

    def break_glass_grants(
        self,
        *,
        actor: AuthenticationActor,
        project_id: str | None = None,
    ) -> tuple[AutonomyBreakGlassGrant, ...]:
        now = float(self.clock())
        return self.store.active_break_glass_grants(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            project_id=project_id,
            now=now,
        )
