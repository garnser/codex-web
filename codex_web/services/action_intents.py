from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from codex_web.action_intents import (
    ActionDecisionOutcome,
    ActionDecisionSnapshot,
    ActionInboxCreate,
    ActionInboxMessage,
    ActionIntent,
    ActionIntentClaimRequest,
    ActionIntentCreate,
    ActionIntentLease,
    ActionIntentReceipt,
    ActionIntentReconcileRequest,
    ActionIntentRetryRequest,
    ActionIntentRollbackRequest,
    ActionIntentStatus,
    ActionIntentVerificationReceipt,
    TERMINAL_ACTION_INTENT_STATUSES,
)
from codex_web.action_providers import ActionRequest, ActionResult
from codex_web.capacity import WorkloadKind, WorkloadPriority
from codex_web.authority import (
    AuthorityAutonomyRisk,
    AuthorityEvaluationRequest,
    AuthorityLevel,
)
from codex_web.entitlements import (
    CAPABILITY_EXTERNAL_ACTIONS,
    METRIC_EXTERNAL_ACTION_ATTEMPTS,
    UsageEventCreate,
)
from codex_web.failures import (
    FailureReason,
    FailureRecord,
    action_failure_reason,
    create_failure,
    failure_from_exception,
)
from codex_web.identity import (
    AuthenticationActor,
    MembershipRole,
    PrincipalKind,
    TenantScope,
)
from codex_web.observability import correlated, current_correlation, new_correlation_id
from codex_web.resources import ResourceType
from codex_web.security import (
    SecurityDecisionOutcome,
    SecurityTrustDecision,
)
from codex_web.services.action_providers import (
    ActionExecutionService,
    ActionProviderError,
    ActionRequirementError,
)
from codex_web.services.artifact_evidence import ArtifactEvidenceService
from codex_web.services.capacity import CapacityDeferredError, CapacityService
from codex_web.services.authority_roles import AuthorityRoleService
from codex_web.services.entitlements import EntitlementDeniedError, EntitlementService
from codex_web.services.identity import (
    AuthenticationError,
    AuthorizationError,
    IdentityService,
    TenantIsolationError,
)
from codex_web.services.execution_workers import (
    AssignmentNotFoundError,
    WorkerConflictError,
)
from codex_web.services.security_boundary import SecurityBoundaryService
from codex_web.storage.action_intents import ActionIntentStore


class ActionIntentError(RuntimeError):
    pass


class ActionIntentNotFoundError(ActionIntentError):
    pass


class ActionIntentConflictError(ActionIntentError):
    pass


class ActionIntentLeaseError(ActionIntentError):
    pass


class ActionIntentUnsafeRetryError(ActionIntentError):
    pass


class ActionIntentService:
    """Durable outbox/inbox and reconciliation boundary for external side effects."""

    def __init__(
        self,
        store: ActionIntentStore,
        execution: ActionExecutionService,
        *,
        artifact_evidence: ArtifactEvidenceService | None = None,
        work_item_host: Any | None = None,
        security_boundary: SecurityBoundaryService | None = None,
        entitlements: EntitlementService | None = None,
        authority: AuthorityRoleService | None = None,
        identity: IdentityService | None = None,
        capacity: CapacityService | None = None,
        execution_workers: Any | None = None,
        maintenance_guard=None,
        status_notifier=None,
    ) -> None:
        self.store = store
        self.execution = execution
        self.artifact_evidence = artifact_evidence
        self.work_item_host = work_item_host
        self.security_boundary = security_boundary
        self.entitlements = entitlements
        self.authority = authority
        self.identity = identity
        self.capacity = capacity
        self.execution_workers = execution_workers
        self.maintenance_guard = maintenance_guard
        self.status_notifier = status_notifier

    @staticmethod
    def _admin(actor: AuthenticationActor) -> bool:
        if actor.principal_kind == PrincipalKind.SERVICE:
            return "action-intent:admin" in actor.service_scopes
        return actor.has_role(MembershipRole.OWNER, MembershipRole.ADMIN)

    @staticmethod
    def _require_worker(actor: AuthenticationActor) -> None:
        if (
            actor.principal_kind == PrincipalKind.SERVICE
            and "action-intent:worker" in actor.service_scopes
        ):
            return
        raise AuthorizationError(
            "action intent execution requires service principal with action-intent:worker scope"
        )

    @staticmethod
    def _require_callback_actor(actor: AuthenticationActor) -> None:
        if (
            actor.principal_kind == PrincipalKind.SERVICE
            and "action-intent:callback" in actor.service_scopes
        ):
            return
        raise AuthorizationError(
            "provider callback ingestion requires service principal with action-intent:callback scope"
        )

    @classmethod
    def _require_intent_control(
        cls,
        intent: ActionIntent,
        actor: AuthenticationActor,
    ) -> None:
        if intent.requested_by == actor.identity_id or cls._admin(actor):
            return
        if (
            actor.principal_kind == PrincipalKind.SERVICE
            and {"action-intent:worker", "action-intent:admin"}.intersection(actor.service_scopes)
        ):
            return
        raise AuthorizationError("action intent requester, worker, or administrator required")

    @staticmethod
    def _same_scope(intent: ActionIntent, actor: AuthenticationActor) -> bool:
        return (
            intent.organization_id == actor.organization_id
            and intent.workspace_id == actor.workspace_id
        )

    def _intent(self, intent_id: str, actor: AuthenticationActor) -> ActionIntent:
        intent = next(
            (
                item
                for item in self.store.load().intents
                if item.id == intent_id and self._same_scope(item, actor)
            ),
            None,
        )
        if intent is None:
            raise ActionIntentNotFoundError("action intent not found")
        return intent

    def list(
        self,
        actor: AuthenticationActor,
        *,
        work_item_ref: str | None = None,
        status: ActionIntentStatus | None = None,
    ) -> list[ActionIntent]:
        items = [
            item
            for item in self.store.load().intents
            if self._same_scope(item, actor)
        ]
        if work_item_ref is not None:
            items = [item for item in items if item.work_item_ref == work_item_ref]
        if status is not None:
            items = [item for item in items if item.status == status]
        return sorted(items, key=lambda item: (item.created_at, item.id), reverse=True)

    def get(self, intent_id: str, actor: AuthenticationActor) -> ActionIntent:
        return self._intent(intent_id, actor)

    def history(self, intent_id: str, actor: AuthenticationActor) -> dict[str, Any]:
        intent = self._intent(intent_id, actor)
        state = self.store.load()
        return {
            "intent": intent.model_dump(mode="json"),
            "receipts": [
                item.model_dump(mode="json")
                for item in state.receipts
                if item.intent_id == intent.id
            ],
            "verifications": [
                item.model_dump(mode="json")
                for item in state.verifications
                if item.intent_id == intent.id
            ],
            "inbox": [
                item.model_dump(mode="json")
                for item in state.inbox
                if item.intent_id == intent.id
            ],
        }

    @staticmethod
    def _idempotency_request_identity(request: ActionRequest) -> dict[str, Any]:
        identity = request.model_dump(
            mode="json",
            exclude={"correlation_id", "requested_by"},
        )
        identity["resource_ids"] = sorted(request.resource_ids)
        return identity

    def _validate_work_item_attribution(
        self,
        work_item_ref: str | None,
        project_id: str | None,
        actor: AuthenticationActor,
    ) -> None:
        if work_item_ref is None:
            return
        if self.work_item_host is None:
            raise ActionIntentConflictError(
                "work item attribution requires canonical Work Item state"
            )
        states = self.work_item_host._load_work_item_states()
        state = states.get(work_item_ref)
        if state is None or (
            state.organization_id != actor.organization_id
            or state.workspace_id != actor.workspace_id
        ):
            raise ActionIntentNotFoundError("work item not found")
        if (
            state.project_id is not None
            and project_id is not None
            and state.project_id != project_id
        ):
            raise ActionIntentConflictError(
                "action intent project does not match attributed Work Item"
            )

    @staticmethod
    def _authority_risk(definition) -> AuthorityAutonomyRisk:
        return AuthorityAutonomyRisk(definition.risk_class.value)

    def _canonical_authority_snapshot(
        self,
        *,
        definition,
        request: ActionRequest,
        actor: AuthenticationActor,
    ) -> ActionDecisionSnapshot:
        capabilities = tuple(
            sorted(
                dict.fromkeys(
                    str(item or "").strip()
                    for item in definition.required_authority
                    if str(item or "").strip()
                )
            )
        )
        evaluated_at = time.time()
        if not capabilities:
            return ActionDecisionSnapshot(
                decision_id=f"authority-bundle-{uuid.uuid4().hex}",
                outcome=ActionDecisionOutcome.DENY,
                source="canonical:role-authority",
                reason="external action declares no required operational authority",
                capabilities=(),
                reasons=("ActionDefinition.required_authority is empty",),
                evaluated_at=evaluated_at,
            )
        if self.authority is None:
            return ActionDecisionSnapshot(
                decision_id=f"authority-bundle-{uuid.uuid4().hex}",
                outcome=ActionDecisionOutcome.DENY,
                source="canonical:role-authority",
                reason="canonical Role authority service is unavailable",
                capabilities=capabilities,
                reasons=("canonical Role authority service is unavailable",),
                evaluated_at=evaluated_at,
            )

        level = AuthorityLevel(definition.required_authority_level)
        decisions = [
            self.authority.evaluate(
                AuthorityEvaluationRequest(
                    capability=capability,
                    level=level,
                    project_id=request.project_id,
                    resource_ids=request.resource_ids,
                    autonomous_risk=self._authority_risk(definition),
                ),
                actor=actor,
            )
            for capability in capabilities
        ]
        denied = [
            (capability, decision)
            for capability, decision in zip(capabilities, decisions)
            if decision.outcome.value != "allow"
        ]
        refs = {}
        role_ids: set[str] = set()
        grant_ids: set[str] = set()
        delegation_ids: set[str] = set()
        expiries: list[float] = []
        reasons: list[str] = []
        for capability, decision in zip(capabilities, decisions):
            if decision.definition_ref is not None:
                refs[decision.definition_ref.record_id] = decision.definition_ref
            role_ids.update(decision.matched_role_ids)
            grant_ids.update(decision.matched_grant_ids)
            delegation_ids.update(decision.delegation_ids)
            if decision.expires_at is not None:
                expiries.append(decision.expires_at)
            reasons.extend(
                f"{capability}: {reason}"
                for reason in decision.reasons
            )

        outcome = (
            ActionDecisionOutcome.DENY
            if denied
            else ActionDecisionOutcome.ALLOW
        )
        return ActionDecisionSnapshot(
            decision_id=f"authority-bundle-{uuid.uuid4().hex}",
            outcome=outcome,
            source="canonical:role-authority",
            reason=(
                "; ".join(reasons)
                if reasons
                else (
                    "canonical operational Role authority allowed all capabilities"
                    if outcome == ActionDecisionOutcome.ALLOW
                    else "canonical operational Role authority denied action"
                )
            ),
            capabilities=capabilities,
            definition_refs=tuple(
                refs[key]
                for key in sorted(refs)
            ),
            role_ids=tuple(sorted(role_ids)),
            grant_ids=tuple(sorted(grant_ids)),
            delegation_ids=tuple(sorted(delegation_ids)),
            expires_at=min(expiries) if expiries else None,
            reasons=tuple(reasons),
            evaluated_at=max(
                (item.evaluated_at for item in decisions),
                default=evaluated_at,
            ),
        )

    def _requester_actor(self, intent: ActionIntent) -> AuthenticationActor:
        if self.identity is None:
            raise AuthenticationError(
                "canonical identity service is unavailable for authority recheck"
            )
        return self.identity.actor_for_identity(
            intent.requested_by,
            scope=TenantScope(
                organization_id=intent.organization_id,
                workspace_id=intent.workspace_id,
            ),
        )

    def _persist_authority_recheck(
        self,
        intent_id: str,
        decision: ActionDecisionSnapshot,
    ) -> ActionIntent:
        def apply(state):
            for index, item in enumerate(state.intents):
                if item.id == intent_id:
                    state.intents[index] = item.model_copy(
                        update={
                            "authority_recheck": decision,
                            "updated_at": time.time(),
                        }
                    )
                    break
            return state

        updated = self.store.update(apply)
        return next(item for item in updated.intents if item.id == intent_id)

    def _security_decision(
        self,
        *,
        binding,
        definition,
        request,
        payload: ActionIntentCreate,
        authority_decision: ActionDecisionSnapshot,
        actor: AuthenticationActor,
        intent_id: str | None,
    ) -> SecurityTrustDecision:
        if self.security_boundary is not None:
            return self.security_boundary.evaluate_action(
                binding=binding,
                definition=definition,
                request=request,
                authority_outcome=authority_decision.outcome.value,
                authority_source=authority_decision.source,
                policy_outcome=payload.policy_decision.outcome.value,
                policy_source=payload.policy_decision.source,
                actor=actor,
                action_intent_id=intent_id,
                work_item_ref=payload.work_item_ref,
                execution_id=payload.execution_id,
            )
        privileged = definition.risk_class.value in {"high", "critical"}
        trusted = (
            authority_decision.outcome == ActionDecisionOutcome.ALLOW
            and payload.policy_decision.outcome == ActionDecisionOutcome.ALLOW
            and SecurityBoundaryService.trusted_decision_source(
                authority_decision.source
            )
            and SecurityBoundaryService.trusted_decision_source(
                payload.policy_decision.source
            )
        )
        reasons = (
            ("security boundary service unavailable for privileged action",)
            if privileged and not trusted
            else ()
        )
        return SecurityTrustDecision(
            outcome=(
                SecurityDecisionOutcome.DENY
                if reasons
                else SecurityDecisionOutcome.ALLOW
            ),
            source="security:fallback",
            risk_class=definition.risk_class.value,
            resource_ids=request.resource_ids,
            sandbox=binding.security_policy.sandbox,
            network_enabled=binding.security_policy.network.enabled,
            authority_source=authority_decision.source,
            policy_source=payload.policy_decision.source,
            reasons=reasons,
        )

    def _recheck_security(
        self,
        intent: ActionIntent,
        *,
        actor: AuthenticationActor,
        authority_decision: ActionDecisionSnapshot,
    ) -> tuple[bool, str | None]:
        binding, _, definition, request = self.execution.resolve_contract(
            intent.binding_id,
            intent.request,
            actor=actor,
        )
        if self.security_boundary is None:
            return (
                intent.security_decision.outcome == SecurityDecisionOutcome.ALLOW,
                "persisted security trust decision denied action"
                if intent.security_decision.outcome != SecurityDecisionOutcome.ALLOW
                else None,
            )
        decision = self.security_boundary.evaluate_action(
            binding=binding,
            definition=definition,
            request=request,
            authority_outcome=authority_decision.outcome.value,
            authority_source=authority_decision.source,
            policy_outcome=intent.policy_decision.outcome.value,
            policy_source=intent.policy_decision.source,
            actor=actor,
            action_intent_id=intent.id,
            work_item_ref=intent.work_item_ref,
            execution_id=intent.execution_id,
        )

        def apply(state):
            for index, item in enumerate(state.intents):
                if item.id == intent.id:
                    state.intents[index] = item.model_copy(
                        update={
                            "security_policy": binding.security_policy,
                            "security_decision": decision,
                            "updated_at": time.time(),
                        }
                    )
                    break
            return state

        self.store.update(apply)
        if decision.outcome != SecurityDecisionOutcome.ALLOW:
            return False, "; ".join(decision.reasons) or "security trust boundary denied action"
        return True, None

    def _validate_execution_repository_scope(
        self,
        execution_id: str | None,
        request: ActionRequest,
        actor: AuthenticationActor,
    ) -> None:
        if execution_id is None:
            return

        repository_ids = tuple(
            resource_id
            for resource_id in request.resource_ids
            if self.execution.resources.get(resource_id, actor).resource_type
            == ResourceType.REPOSITORY
        )
        if not repository_ids:
            return
        if self.execution_workers is None:
            raise ActionIntentConflictError(
                "execution-bound repository action requires canonical execution assignment state"
            )

        try:
            assignment = self.execution_workers.assignment_for_execution(
                execution_id,
                actor=actor,
            )
        except (AssignmentNotFoundError, WorkerConflictError) as exc:
            raise ActionIntentConflictError(
                "execution-bound repository action requires a matching canonical execution assignment"
            ) from exc

        scope = assignment.repository_scope
        if scope is not None:
            writable = set(scope.writable_repository_ids)
        else:
            target = assignment.repository_target
            writable = (
                {target.mutable_repository_id}
                if target is not None and target.mutable_repository_id is not None
                else set()
            )

        outside_scope = sorted(set(repository_ids) - writable)
        if outside_scope:
            raise ActionIntentConflictError(
                "action request targets repository outside execution writable scope: "
                + ", ".join(outside_scope)
            )

    def _work_item_requirements(
        self,
        work_item_ref: str | None,
        actor: AuthenticationActor,
    ):
        if not work_item_ref or self.artifact_evidence is None:
            return ()
        return self.artifact_evidence.work_item_requirements(
            work_item_ref,
            actor=actor,
        )

    def create(
        self,
        payload: ActionIntentCreate,
        *,
        actor: AuthenticationActor,
    ) -> ActionIntent:
        request = payload.request
        if (
            request.organization_id != actor.organization_id
            or request.workspace_id != actor.workspace_id
        ):
            raise TenantIsolationError("cross-tenant action intent denied")

        binding, provider, definition, request = self.execution.resolve_contract(
            payload.binding_id,
            request,
            actor=actor,
        )
        self._validate_work_item_attribution(
            payload.work_item_ref,
            request.project_id,
            actor,
        )
        self._validate_execution_repository_scope(
            payload.execution_id,
            request,
            actor,
        )
        if payload.work_item_success is not None and payload.work_item_ref is None:
            raise ActionIntentConflictError(
                "work_item_success requires work_item_ref"
            )
        if payload.rollback_required and (
            not definition.capabilities.rollback or not definition.reversible
        ):
            raise ActionRequirementError(
                "rollback-required intent needs a reversible rollback-capable action"
            )

        # Suppress duplicate creation only when the stable provider
        # idempotency key is bound to the same canonical request and execution
        # attribution. Reusing a key for another repository/resource must never
        # return an intent for the wrong target.
        if request.idempotency_key:
            existing = next(
                (
                    item
                    for item in self.store.load().intents
                    if item.organization_id == actor.organization_id
                    and item.workspace_id == actor.workspace_id
                    and item.binding_id == binding.id
                    and item.action_id == request.action_id
                    and item.idempotency_key == request.idempotency_key
                    and item.status != ActionIntentStatus.CANCELLED
                ),
                None,
            )
            if existing is not None:
                same_request = (
                    self._idempotency_request_identity(existing.request)
                    == self._idempotency_request_identity(request)
                )
                same_attribution = (
                    existing.work_item_ref == payload.work_item_ref
                    and existing.execution_id == payload.execution_id
                )
                if not same_request or not same_attribution:
                    raise ActionIntentConflictError(
                        "idempotency key is already bound to a different "
                        "action target or execution context"
                    )
                return existing

        if self.entitlements is not None:
            self.entitlements.require_capability(
                CAPABILITY_EXTERNAL_ACTIONS,
                actor=actor,
            )

        now = time.time()
        context = current_correlation()
        correlation_id = (
            request.correlation_id
            or (context.correlation_id if context else None)
            or new_correlation_id()
        )
        causation_id = context.causation_id if context else None
        intent_id = f"action-intent-{uuid.uuid4().hex}"
        authority_decision = self._canonical_authority_snapshot(
            definition=definition,
            request=request,
            actor=actor,
        )
        security_decision = self._security_decision(
            binding=binding,
            definition=definition,
            request=request,
            payload=payload,
            authority_decision=authority_decision,
            actor=actor,
            intent_id=intent_id,
        )
        provider_idempotency = bool(definition.capabilities.idempotency)
        idempotency_key = request.idempotency_key or f"codex-intent:{intent_id}"
        provider_request = request
        if provider_idempotency and not request.idempotency_key:
            provider_request = request.model_copy(
                update={
                    "idempotency_key": idempotency_key,
                    "correlation_id": correlation_id,
                    "requested_by": request.requested_by or actor.identity_id,
                }
            )
        else:
            provider_request = request.model_copy(
                update={
                    "correlation_id": correlation_id,
                    "requested_by": request.requested_by or actor.identity_id,
                }
            )

        expected_evidence = (
            payload.expected_evidence
            or self._work_item_requirements(payload.work_item_ref, actor)
        )
        verification_required = (
            bool(definition.capabilities.verification)
            if payload.verification_required is None
            else payload.verification_required
        )
        if verification_required and not definition.capabilities.verification:
            raise ActionRequirementError(
                "verification-required intent needs a verification-capable action"
            )
        timeout_seconds = payload.timeout_seconds or definition.timeout_seconds
        retry_policy = payload.retry_policy.model_copy(
            update={
                "max_attempts": min(
                    payload.retry_policy.max_attempts,
                    definition.retry_max_attempts,
                )
            }
        )
        denied = (
            authority_decision.outcome == ActionDecisionOutcome.DENY
            or payload.policy_decision.outcome == ActionDecisionOutcome.DENY
            or security_decision.outcome == SecurityDecisionOutcome.DENY
        )
        denied_failure = (
            create_failure(
                FailureReason.AUTHORITY_DENIED,
                source_subsystem="action_intent",
                correlation_id=correlation_id,
                causation_id=causation_id,
                provider_id=(
                    f"{provider.provider_type}:{provider.provider_instance}"
                ),
                execution_id=payload.execution_id,
                action_intent_id=intent_id,
                details={"action_id": request.action_id},
            )
            if denied
            else None
        )
        intent = ActionIntent(
            id=intent_id,
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            project_id=request.project_id,
            work_item_ref=payload.work_item_ref,
            goal_id=payload.goal_id,
            decision_id=payload.decision_id,
            execution_id=payload.execution_id,
            binding_id=binding.id,
            provider_type=provider.provider_type,
            provider_instance=provider.provider_instance,
            action_id=request.action_id,
            action_definition=definition,
            request=provider_request,
            authority_decision=authority_decision,
            policy_decision=payload.policy_decision,
            security_policy=binding.security_policy,
            security_decision=security_decision,
            credential_ref=provider_request.credential_ref,
            resource_ids=provider_request.resource_ids,
            idempotency_key=idempotency_key,
            provider_idempotency_supported=provider_idempotency,
            expected_evidence=tuple(expected_evidence),
            verification_required=verification_required,
            rollback_required=payload.rollback_required,
            timeout_seconds=timeout_seconds,
            retry_policy=retry_policy,
            status=ActionIntentStatus.CANCELLED if denied else ActionIntentStatus.PENDING,
            correlation_id=correlation_id,
            causation_id=causation_id,
            requested_by=actor.identity_id,
            created_at=now,
            updated_at=now,
            completed_at=now if denied else None,
            last_error=(
                authority_decision.reason
                if authority_decision.outcome == ActionDecisionOutcome.DENY
                else "; ".join(security_decision.reasons)
                if security_decision.outcome == SecurityDecisionOutcome.DENY
                else "policy denied action"
                if payload.policy_decision.outcome == ActionDecisionOutcome.DENY
                else None
            ),
            failure=denied_failure,
            work_item_success=payload.work_item_success,
        )

        def apply(state):
            state.intents.append(intent)
            return state

        self.store.update(apply)
        return intent

    @staticmethod
    def _failure(
        intent: ActionIntent,
        reason: FailureReason,
        *,
        summary: str | None = None,
        source_native_code: str | None = None,
        source_native_status: str | int | None = None,
        details: dict[str, Any] | None = None,
    ) -> FailureRecord:
        return create_failure(
            reason,
            source_subsystem="action_intent",
            summary=summary,
            correlation_id=intent.correlation_id,
            causation_id=intent.causation_id,
            provider_id=(
                f"{intent.provider_type}:{intent.provider_instance}"
            ),
            execution_id=intent.execution_id,
            action_intent_id=intent.id,
            source_native_code=source_native_code,
            source_native_status=source_native_status,
            attempt=max(1, intent.attempt or 1),
            details={
                "action_id": intent.action_id,
                **(details or {}),
            },
        )

    @staticmethod
    def _capacity_priority(intent: ActionIntent) -> WorkloadPriority:
        parameters = intent.request.parameters
        if (
            parameters.get("incident_id")
            or parameters.get("recovery") is True
            or "rollback" in intent.action_id.casefold()
            or "reconcile" in intent.action_id.casefold()
        ):
            return WorkloadPriority.CRITICAL
        risk = getattr(intent.action_definition.risk_class, "value", "")
        if risk in {"high", "critical"}:
            return WorkloadPriority.HIGH
        return WorkloadPriority.NORMAL

    @staticmethod
    def _capacity_component(intent: ActionIntent) -> str:
        return f"action:{intent.provider_type}:{intent.provider_instance}"

    def _defer_for_capacity(
        self,
        intent_id: str,
        *,
        reason: str,
        retry_at: float | None,
    ) -> ActionIntent:
        now = time.time()
        not_before = max(
            now + 0.25,
            retry_at if retry_at is not None else now + 1.0,
        )

        def apply(state):
            for index, item in enumerate(state.intents):
                if item.id != intent_id:
                    continue
                if item.status not in {
                    ActionIntentStatus.CLAIMED,
                    ActionIntentStatus.PENDING,
                }:
                    raise ActionIntentConflictError(
                        "capacity deferral requires pending/claimed action intent"
                    )
                state.intents[index] = item.model_copy(
                    update={
                        "status": ActionIntentStatus.PENDING,
                        "lease": None,
                        "not_before": not_before,
                        "updated_at": now,
                        "last_error": f"capacity deferred: {reason}",
                    }
                )
                return state
            raise ActionIntentNotFoundError("action intent not found")

        updated = self.store.update(apply)
        return next(item for item in updated.intents if item.id == intent_id)

    def recover_stale_claims(
        self,
        *,
        now: float | None = None,
        organization_id: str | None = None,
        workspace_id: str | None = None,
    ) -> list[str]:
        current = time.time() if now is None else now
        recovered: list[str] = []

        def apply(state):
            for index, intent in enumerate(state.intents):
                lease = intent.lease
                if (
                    (organization_id is not None and intent.organization_id != organization_id)
                    or (workspace_id is not None and intent.workspace_id != workspace_id)
                    or lease is None
                    or lease.expires_at > current
                    or intent.status not in {
                        ActionIntentStatus.CLAIMED,
                        ActionIntentStatus.EXECUTING,
                    }
                ):
                    continue
                if intent.status == ActionIntentStatus.CLAIMED:
                    status = ActionIntentStatus.PENDING
                    error = "worker claim expired before provider execution"
                    failure = self._failure(
                        intent,
                        FailureReason.WORKER_LEASE_LOST,
                        summary=error,
                    )
                else:
                    status = ActionIntentStatus.UNCERTAIN
                    error = (
                        "worker lease expired after provider execution started; "
                        "outcome unknown"
                    )
                    failure = self._failure(
                        intent,
                        FailureReason.UNKNOWN_OUTCOME,
                        summary=error,
                    )
                state.intents[index] = intent.model_copy(
                    update={
                        "status": status,
                        "lease": None,
                        "updated_at": current,
                        "last_error": error,
                        "failure": failure,
                    }
                )
                recovered.append(intent.id)
            return state

        self.store.update(apply)
        return recovered

    def claim(
        self,
        payload: ActionIntentClaimRequest,
        *,
        actor: AuthenticationActor,
        intent_id: str | None = None,
    ) -> ActionIntent | None:
        self._require_worker(actor)
        self.recover_stale_claims(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
        )
        now = time.time()
        claimed: list[ActionIntent] = []

        def apply(state):
            candidates = [
                item
                for item in state.intents
                if item.organization_id == actor.organization_id
                and item.workspace_id == actor.workspace_id
                and item.status == ActionIntentStatus.PENDING
                and (item.not_before is None or item.not_before <= now)
                and item.attempt < item.retry_policy.max_attempts
                and (intent_id is None or item.id == intent_id)
                and (
                    self.maintenance_guard is None
                    or self.maintenance_guard(item)
                )
            ]
            candidates.sort(key=lambda item: (item.created_at, item.id))
            if not candidates:
                return state
            target = candidates[0]
            lease = ActionIntentLease(
                owner=payload.worker_id,
                acquired_at=now,
                expires_at=now + payload.lease_seconds,
            )
            updated = target.model_copy(
                update={
                    "status": ActionIntentStatus.CLAIMED,
                    "lease": lease,
                    "updated_at": now,
                }
            )
            for index, item in enumerate(state.intents):
                if item.id == target.id:
                    state.intents[index] = updated
                    claimed.append(updated)
                    break
            return state

        self.store.update(apply)
        return claimed[0] if claimed else None

    def renew_claim(
        self,
        intent_id: str,
        worker_id: str,
        lease_seconds: int,
        *,
        actor: AuthenticationActor,
    ) -> ActionIntent:
        self._require_worker(actor)
        intent = self._intent(intent_id, actor)
        now = time.time()
        if (
            intent.lease is None
            or intent.lease.owner != worker_id
            or intent.lease.expires_at <= now
            or intent.status not in {
                ActionIntentStatus.CLAIMED,
                ActionIntentStatus.EXECUTING,
            }
        ):
            raise ActionIntentLeaseError("action intent lease is not active for worker")
        renewed = intent.lease.model_copy(
            update={
                "expires_at": now + lease_seconds,
                "renewed_at": now,
            }
        )

        def apply(state):
            for index, item in enumerate(state.intents):
                if item.id == intent.id:
                    state.intents[index] = item.model_copy(
                        update={"lease": renewed, "updated_at": now}
                    )
                    break
            return state

        updated = self.store.update(apply)
        return next(item for item in updated.intents if item.id == intent.id)

    def _mark_executing(
        self,
        intent_id: str,
        worker_id: str,
        actor: AuthenticationActor,
    ) -> ActionIntent:
        intent = self._intent(intent_id, actor)
        now = time.time()
        if (
            intent.status != ActionIntentStatus.CLAIMED
            or intent.lease is None
            or intent.lease.owner != worker_id
            or intent.lease.expires_at <= now
        ):
            raise ActionIntentLeaseError("action intent must hold an active claim before execution")
        if intent.attempt >= intent.retry_policy.max_attempts:
            raise ActionIntentConflictError("action intent retry limit reached")

        def apply(state):
            for index, item in enumerate(state.intents):
                if item.id == intent.id:
                    state.intents[index] = item.model_copy(
                        update={
                            "status": ActionIntentStatus.EXECUTING,
                            "attempt": item.attempt + 1,
                            "execution_started_at": now,
                            "updated_at": now,
                            "last_error": None,
                        }
                    )
                    break
            return state

        updated = self.store.update(apply)
        return next(item for item in updated.intents if item.id == intent.id)

    def _append_receipt(
        self,
        intent: ActionIntent,
        *,
        result: ActionResult | None,
        outcome: str,
        details: dict[str, str | int | float | bool | None] | None = None,
    ) -> ActionIntentReceipt:
        receipt = ActionIntentReceipt(
            intent_id=intent.id,
            attempt=intent.attempt,
            provider_type=intent.provider_type,
            provider_instance=intent.provider_instance,
            action_id=intent.action_id,
            idempotency_key=intent.idempotency_key,
            correlation_id=intent.correlation_id,
            provider_external_id=result.external_id if result else None,
            result=result,
            outcome=outcome,
            details=details or {},
        )

        def apply(state):
            state.receipts.append(receipt)
            for index, item in enumerate(state.intents):
                if item.id == intent.id:
                    state.intents[index] = item.model_copy(
                        update={
                            "last_receipt_id": receipt.id,
                            "updated_at": time.time(),
                        }
                    )
                    break
            return state

        self.store.update(apply)
        return receipt

    def _append_verification(
        self,
        intent: ActionIntent,
        *,
        provider_verification: Any | None,
        evidence_evaluation: Any | None,
        verified: bool,
        findings: tuple[str, ...] = (),
    ) -> ActionIntentVerificationReceipt:
        receipt = ActionIntentVerificationReceipt(
            intent_id=intent.id,
            provider_verification=provider_verification,
            evidence_satisfied=(
                evidence_evaluation.satisfied if evidence_evaluation is not None else None
            ),
            evidence_evaluation=(
                evidence_evaluation.model_dump(mode="json")
                if evidence_evaluation is not None
                else None
            ),
            verified=verified,
            findings=findings,
        )

        def apply(state):
            state.verifications.append(receipt)
            for index, item in enumerate(state.intents):
                if item.id == intent.id:
                    state.intents[index] = item.model_copy(
                        update={
                            "last_verification_id": receipt.id,
                            "updated_at": time.time(),
                        }
                    )
                    break
            return state

        self.store.update(apply)
        return receipt

    def _set_status(
        self,
        intent_id: str,
        status: ActionIntentStatus,
        *,
        error: str | None = None,
        failure: FailureRecord | None = None,
        clear_lease: bool = True,
    ) -> ActionIntent:
        now = time.time()
        completed = status in TERMINAL_ACTION_INTENT_STATUSES

        def apply(state):
            for index, item in enumerate(state.intents):
                if item.id == intent_id:
                    state.intents[index] = item.model_copy(
                        update={
                            "status": status,
                            "lease": None if clear_lease else item.lease,
                            "updated_at": now,
                            "completed_at": now if completed else item.completed_at,
                            "last_error": error,
                            "failure": (
                                None
                                if status == ActionIntentStatus.SUCCEEDED
                                else failure
                                if failure is not None
                                else item.failure
                            ),
                        }
                    )
                    break
            return state

        updated = self.store.update(apply)
        result = next(item for item in updated.intents if item.id == intent_id)
        if self.status_notifier is not None:
            try:
                self.status_notifier(result)
            except Exception:
                # Projection hooks are observational and cannot break
                # canonical ActionIntent state transitions.
                pass
        return result

    async def _verify_completion(
        self,
        intent: ActionIntent,
        result: ActionResult,
        *,
        actor: AuthenticationActor,
    ) -> bool:
        provider_verification = None
        findings: list[str] = []
        provider_ok = True
        if intent.verification_required:
            try:
                provider_verification = await self.execution.verify(
                    intent.binding_id,
                    result,
                    actor=actor,
                )
                provider_ok = bool(provider_verification.verified)
                findings.extend(provider_verification.findings)
            except Exception as exc:
                provider_ok = False
                findings.append(f"provider verification failed: {type(exc).__name__}: {exc}")

        evidence_evaluation = None
        evidence_ok = True
        if intent.expected_evidence:
            if self.artifact_evidence is None or not intent.work_item_ref:
                evidence_ok = False
                findings.append("required evidence cannot be evaluated")
            else:
                evidence_evaluation = self.artifact_evidence.evaluate(
                    intent.work_item_ref,
                    intent.expected_evidence,
                    actor=actor,
                )
                evidence_ok = evidence_evaluation.satisfied
                if not evidence_ok:
                    findings.extend(
                        outcome.reason or outcome.requirement_id
                        for outcome in evidence_evaluation.outcomes
                        if not outcome.satisfied
                    )

        verified = provider_ok and evidence_ok
        self._append_verification(
            intent,
            provider_verification=provider_verification,
            evidence_evaluation=evidence_evaluation,
            verified=verified,
            findings=tuple(findings),
        )
        return verified

    def _advance_work_item(self, intent: ActionIntent) -> None:
        if (
            intent.work_item_success is None
            or intent.work_item_ref is None
            or self.work_item_host is None
        ):
            return
        host = self.work_item_host
        state = host._work_item_state(intent.work_item_ref)
        update = intent.work_item_success
        state = host._touch_work_item_progress(
            state,
            actor=intent.requested_by,
            current_stage=update.current_stage,
            next_action=update.next_action,
            next_owner=update.next_owner,
            next_owner_present=update.next_owner is not None,
            event_type="action_intent_verified",
            note=update.note,
        )
        host._save_work_item_state(state)

    async def execute_claimed(
        self,
        intent_id: str,
        worker_id: str,
        *,
        actor: AuthenticationActor,
    ) -> ActionIntent:
        self._require_worker(actor)
        pending = self._intent(intent_id, actor)
        try:
            requester = self._requester_actor(pending)
            _, _, current_definition, current_request = self.execution.resolve_contract(
                pending.binding_id,
                pending.request,
                actor=actor,
            )
            authority_recheck = self._canonical_authority_snapshot(
                definition=current_definition,
                request=current_request,
                actor=requester,
            )
        except Exception as exc:
            authority_recheck = ActionDecisionSnapshot(
                decision_id=f"authority-recheck-{uuid.uuid4().hex}",
                outcome=ActionDecisionOutcome.DENY,
                source="canonical:role-authority",
                reason=(
                    "authority recheck unavailable: "
                    f"{type(exc).__name__}: {exc}"
                ),
                capabilities=pending.authority_decision.capabilities,
                reasons=(
                    "canonical requester/authority recheck failed",
                ),
                evaluated_at=time.time(),
            )
        pending = self._persist_authority_recheck(
            pending.id,
            authority_recheck,
        )
        if authority_recheck.outcome != ActionDecisionOutcome.ALLOW:
            return self._set_status(
                intent_id,
                ActionIntentStatus.CANCELLED,
                error=(
                    authority_recheck.reason
                    or "canonical Role authority denied action at execution"
                ),
                failure=self._failure(
                    pending,
                    FailureReason.AUTHORITY_DENIED,
                ),
            )
        allowed, reason = self._recheck_security(
            pending,
            actor=actor,
            authority_decision=authority_recheck,
        )
        if not allowed:
            return self._set_status(
                intent_id,
                ActionIntentStatus.CANCELLED,
                error=reason or "security trust boundary denied action",
                failure=self._failure(
                    pending,
                    FailureReason.AUTHORITY_DENIED,
                ),
            )
        if self.entitlements is not None:
            try:
                self.entitlements.consume(
                    CAPABILITY_EXTERNAL_ACTIONS,
                    UsageEventCreate(
                        idempotency_key=(
                            f"action-intent:{pending.id}:attempt:{pending.attempt + 1}"
                        ),
                        metric=METRIC_EXTERNAL_ACTION_ATTEMPTS,
                        amount=1.0,
                        source="action-intent",
                        project_id=pending.project_id,
                        work_item_ref=pending.work_item_ref,
                        action_intent_id=pending.id,
                    ),
                    actor=actor,
                )
            except EntitlementDeniedError as exc:
                return self._set_status(
                    intent_id,
                    ActionIntentStatus.FAILED,
                    error=f"entitlement/quota denied before provider execution: {exc}",
                    failure=self._failure(
                        pending,
                        FailureReason.BUDGET_EXHAUSTED,
                        source_native_code=type(exc).__name__,
                    ),
                )
        capacity_lease = None
        component_key = self._capacity_component(pending)
        if self.capacity is not None:
            try:
                capacity_lease = self.capacity.acquire(
                    organization_id=pending.organization_id,
                    workspace_id=pending.workspace_id,
                    workload=WorkloadKind.ACTION,
                    priority=self._capacity_priority(pending),
                    owner_ref=pending.id,
                    component_key=component_key,
                    lease_seconds=pending.timeout_seconds + 30.0,
                )
            except CapacityDeferredError as exc:
                return self._defer_for_capacity(
                    pending.id,
                    reason=exc.reason,
                    retry_at=exc.retry_at,
                )
        try:
            intent = self._mark_executing(intent_id, worker_id, actor)
        except Exception:
            if self.capacity is not None and capacity_lease is not None:
                self.capacity.release(capacity_lease.id)
            raise
        with correlated(
            correlation_id=intent.correlation_id,
            causation_id=intent.causation_id,
            tenant_id=intent.organization_id,
            workspace_id=intent.workspace_id,
            work_item_ref=intent.work_item_ref,
            execution_id=intent.execution_id,
            action_intent_id=intent.id,
        ):
            try:
                result = await asyncio.wait_for(
                    self.execution.execute(
                        intent.binding_id,
                        intent.request,
                        actor=actor,
                    ),
                    timeout=intent.timeout_seconds,
                )
            except asyncio.TimeoutError:
                if self.capacity is not None:
                    self.capacity.record_failure(
                        organization_id=intent.organization_id,
                        workspace_id=intent.workspace_id,
                        component_key=component_key,
                        reason="provider_timeout",
                    )
                    if capacity_lease is not None:
                        self.capacity.release(capacity_lease.id)
                        capacity_lease = None
                self._append_receipt(
                    intent,
                    result=None,
                    outcome="unknown",
                    details={"reason": "timeout"},
                )
                return self._set_status(
                    intent.id,
                    ActionIntentStatus.UNCERTAIN,
                    error="provider execution timed out; external outcome unknown",
                    failure=self._failure(
                        intent,
                        FailureReason.ACTION_TIMEOUT_UNKNOWN_OUTCOME,
                        source_native_code="TimeoutError",
                    ),
                )
            except asyncio.CancelledError:
                if self.capacity is not None and capacity_lease is not None:
                    self.capacity.release(capacity_lease.id)
                    capacity_lease = None
                raise
            except Exception as exc:
                if self.capacity is not None:
                    self.capacity.record_failure(
                        organization_id=intent.organization_id,
                        workspace_id=intent.workspace_id,
                        component_key=component_key,
                        reason=type(exc).__name__,
                    )
                    if capacity_lease is not None:
                        self.capacity.release(capacity_lease.id)
                        capacity_lease = None
                self._append_receipt(
                    intent,
                    result=None,
                    outcome="unknown",
                    details={"reason": type(exc).__name__},
                )
                return self._set_status(
                    intent.id,
                    ActionIntentStatus.UNCERTAIN,
                    error=(
                        f"provider execution raised {type(exc).__name__}; "
                        "external outcome unknown"
                    ),
                    failure=failure_from_exception(
                        exc,
                        source_subsystem="action_intent",
                        default_reason=FailureReason.UNKNOWN_OUTCOME,
                        correlation_id=intent.correlation_id,
                        causation_id=intent.causation_id,
                        provider_id=(
                            f"{intent.provider_type}:"
                            f"{intent.provider_instance}"
                        ),
                        execution_id=intent.execution_id,
                        action_intent_id=intent.id,
                        attempt=max(1, intent.attempt or 1),
                        details={"action_id": intent.action_id},
                    ),
                )

        if self.capacity is not None and capacity_lease is not None:
            self.capacity.release(capacity_lease.id)
            capacity_lease = None
        self._append_receipt(
            intent,
            result=result,
            outcome="failed" if result.status == "failed" else "completed",
        )
        current = self._intent(intent.id, actor)
        if result.status == "failed":
            if self.capacity is not None:
                self.capacity.record_failure(
                    organization_id=intent.organization_id,
                    workspace_id=intent.workspace_id,
                    component_key=component_key,
                    reason=result.error_code or "provider_failed",
                )
            native_code = result.error_code or "provider_failed"
            reason_code = action_failure_reason(native_code)
            return self._set_status(
                intent.id,
                ActionIntentStatus.FAILED,
                error=result.error_message or native_code,
                failure=self._failure(
                    intent,
                    reason_code,
                    source_native_code=native_code,
                    summary=None,
                ),
            )
        if self.capacity is not None:
            self.capacity.record_success(
                organization_id=intent.organization_id,
                workspace_id=intent.workspace_id,
                component_key=component_key,
            )
        if result.status == "rolled_back":
            return self._set_status(intent.id, ActionIntentStatus.ROLLED_BACK)

        verified = await self._verify_completion(current, result, actor=actor)
        if not verified:
            return self._set_status(
                intent.id,
                ActionIntentStatus.REQUIRES_RECONCILIATION,
                error="required provider/evidence verification is not satisfied",
                failure=self._failure(
                    intent,
                    FailureReason.VERIFICATION_FAILED,
                ),
            )
        current = self._intent(intent.id, actor)
        try:
            self._advance_work_item(current)
        except Exception as exc:
            return self._set_status(
                intent.id,
                ActionIntentStatus.REQUIRES_RECONCILIATION,
                error=f"external action verified but canonical state advance failed: {type(exc).__name__}: {exc}",
            )
        return self._set_status(intent.id, ActionIntentStatus.SUCCEEDED)

    def _schedule_retry(
        self,
        intent: ActionIntent,
        payload: ActionIntentRetryRequest,
    ) -> ActionIntent:
        not_before = time.time() + intent.retry_policy.backoff_seconds

        def apply(state):
            for index, item in enumerate(state.intents):
                if item.id == intent.id:
                    state.intents[index] = item.model_copy(
                        update={
                            "status": ActionIntentStatus.PENDING,
                            "lease": None,
                            "not_before": not_before,
                            "updated_at": time.time(),
                            "last_error": payload.reason,
                            "failure": None,
                        }
                    )
                    break
            return state

        updated = self.store.update(apply)
        return next(item for item in updated.intents if item.id == intent.id)

    def retry(
        self,
        intent_id: str,
        payload: ActionIntentRetryRequest,
        *,
        actor: AuthenticationActor,
    ) -> ActionIntent:
        intent = self._intent(intent_id, actor)
        self._require_intent_control(intent, actor)
        if intent.attempt >= intent.retry_policy.max_attempts:
            raise ActionIntentConflictError("action intent retry limit reached")
        if intent.status in {
            ActionIntentStatus.UNCERTAIN,
            ActionIntentStatus.REQUIRES_RECONCILIATION,
        } or (
            intent.failure is not None
            and intent.failure.requires_reconciliation
        ):
            raise ActionIntentUnsafeRetryError(
                "unknown or unreconciled external outcome must be reconciled before retry"
            )
        if intent.attempt > 0 and not intent.provider_idempotency_supported:
            raise ActionIntentUnsafeRetryError(
                "replaying an executed non-idempotent action is unsafe; reconcile instead"
            )
        if intent.status != ActionIntentStatus.FAILED:
            raise ActionIntentConflictError(
                "action intent is not retryable from current status"
            )
        if intent.failure is None:
            raise ActionIntentUnsafeRetryError(
                "failed action lacks canonical transient failure classification"
            )
        if not intent.failure.automatic_retry_allowed:
            raise ActionIntentUnsafeRetryError(
                "canonical failure classification does not allow automatic retry"
            )
        return self._schedule_retry(intent, payload)

    def cancel(
        self,
        intent_id: str,
        reason: str | None,
        *,
        actor: AuthenticationActor,
    ) -> ActionIntent:
        intent = self._intent(intent_id, actor)
        if intent.status in TERMINAL_ACTION_INTENT_STATUSES:
            return intent
        if intent.status == ActionIntentStatus.EXECUTING:
            raise ActionIntentConflictError(
                "executing action cannot be declared cancelled while external outcome is unknown"
            )
        if intent.requested_by != actor.identity_id and not self._admin(actor):
            raise AuthorizationError("action intent requester or administrator required")
        return self._set_status(
            intent.id,
            ActionIntentStatus.CANCELLED,
            error=reason or "cancelled",
        )

    def ingest_callback(
        self,
        payload: ActionInboxCreate,
        *,
        actor: AuthenticationActor,
    ) -> ActionInboxMessage:
        self._require_callback_actor(actor)
        state = self.store.load()
        existing = next(
            (
                item
                for item in state.inbox
                if item.organization_id == actor.organization_id
                and item.workspace_id == actor.workspace_id
                and item.provider_type == payload.provider_type
                and item.provider_instance == payload.provider_instance
                and item.delivery_id == payload.delivery_id
            ),
            None,
        )
        if existing is not None:
            return existing.model_copy(update={"duplicate": True})

        intent = None
        if payload.intent_id:
            intent = next(
                (
                    item
                    for item in state.intents
                    if item.id == payload.intent_id
                    and item.organization_id == actor.organization_id
                    and item.workspace_id == actor.workspace_id
                ),
                None,
            )
        if intent is not None and (
            intent.provider_type != payload.provider_type
            or intent.provider_instance != payload.provider_instance
        ):
            raise ActionIntentConflictError(
                "callback provider does not match action intent provider"
            )
        if intent is None and payload.idempotency_key:
            matches = [
                item
                for item in state.intents
                if item.organization_id == actor.organization_id
                and item.workspace_id == actor.workspace_id
                and item.provider_type == payload.provider_type
                and item.provider_instance == payload.provider_instance
                and item.idempotency_key == payload.idempotency_key
            ]
            if len(matches) == 1:
                intent = matches[0]

        message_payload = payload.model_dump()
        message_payload["intent_id"] = intent.id if intent else payload.intent_id
        message = ActionInboxMessage(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            **message_payload,
            processed_at=time.time(),
        )

        def apply(current):
            current.inbox.append(message)
            if intent is not None:
                receipt = ActionIntentReceipt(
                    intent_id=intent.id,
                    attempt=intent.attempt,
                    provider_type=intent.provider_type,
                    provider_instance=intent.provider_instance,
                    action_id=intent.action_id,
                    idempotency_key=intent.idempotency_key,
                    correlation_id=intent.correlation_id,
                    provider_external_id=payload.provider_external_id,
                    outcome="callback",
                    details={
                        "delivery_id": payload.delivery_id,
                        "event_type": payload.event_type,
                    },
                )
                current.receipts.append(receipt)
                for index, item in enumerate(current.intents):
                    if item.id != intent.id:
                        continue
                    # A callback is durable acknowledgement, not sufficient proof
                    # of success when verification/evidence is required.
                    next_status = item.status
                    if payload.outcome in {
                        ActionIntentStatus.SUCCEEDED,
                        ActionIntentStatus.ROLLED_BACK,
                    }:
                        next_status = ActionIntentStatus.REQUIRES_RECONCILIATION
                    elif payload.outcome == ActionIntentStatus.FAILED:
                        next_status = ActionIntentStatus.FAILED
                    elif item.status in {
                        ActionIntentStatus.UNCERTAIN,
                        ActionIntentStatus.EXECUTING,
                    }:
                        next_status = ActionIntentStatus.REQUIRES_RECONCILIATION
                    updated_at = time.time()
                    failure = item.failure
                    if payload.outcome == ActionIntentStatus.FAILED:
                        native_code = (
                            payload.payload.get("error_code")
                            or payload.payload.get("code")
                            or payload.event_type
                        )
                        failure = self._failure(
                            item,
                            action_failure_reason(native_code),
                            source_native_code=str(native_code),
                            details={
                                "callback_event_type": payload.event_type,
                            },
                        )
                    current.intents[index] = item.model_copy(
                        update={
                            "status": next_status,
                            "last_receipt_id": receipt.id,
                            "lease": None,
                            "updated_at": updated_at,
                            "failure": failure,
                            "completed_at": (
                                updated_at
                                if next_status in TERMINAL_ACTION_INTENT_STATUSES
                                else item.completed_at
                            ),
                        }
                    )
                    break
            return current

        self.store.update(apply)
        return message

    async def reconcile(
        self,
        intent_id: str,
        payload: ActionIntentReconcileRequest,
        *,
        actor: AuthenticationActor,
    ) -> ActionIntent:
        self._require_worker(actor)
        intent = self._intent(intent_id, actor)
        state = self.store.load()
        results = [
            receipt.result
            for receipt in state.receipts
            if receipt.intent_id == intent.id and receipt.result is not None
        ]
        latest_result = results[-1] if results else None

        if latest_result is not None and latest_result.status == "rolled_back":
            return self._set_status(intent.id, ActionIntentStatus.ROLLED_BACK)
        if latest_result is not None and latest_result.status in {"succeeded", "dry_run"}:
            verified = await self._verify_completion(
                intent,
                latest_result,
                actor=actor,
            )
            if verified:
                current = self._intent(intent.id, actor)
                try:
                    self._advance_work_item(current)
                except Exception as exc:
                    return self._set_status(
                        intent.id,
                        ActionIntentStatus.REQUIRES_RECONCILIATION,
                        error=f"verification succeeded but canonical state advance failed: {type(exc).__name__}: {exc}",
                    )
                return self._set_status(intent.id, ActionIntentStatus.SUCCEEDED)
            return self._set_status(
                intent.id,
                ActionIntentStatus.REQUIRES_RECONCILIATION,
                error="reconciliation verification is not satisfied",
            )

        if payload.retry_if_idempotent and intent.provider_idempotency_supported:
            return self._schedule_retry(
                intent,
                ActionIntentRetryRequest(
                    reason="reconciliation retry using provider idempotency"
                ),
            )

        return self._set_status(
            intent.id,
            ActionIntentStatus.REQUIRES_RECONCILIATION,
            error=(
                "no durable provider result is available; manual/provider reconciliation required"
            ),
        )

    async def rollback(
        self,
        intent_id: str,
        payload: ActionIntentRollbackRequest,
        *,
        actor: AuthenticationActor,
    ) -> ActionIntent:
        self._require_worker(actor)
        intent = self._intent(intent_id, actor)
        if not intent.rollback_required and not intent.action_definition.capabilities.rollback:
            raise ActionIntentConflictError("action intent does not support rollback")
        state = self.store.load()
        results = [
            receipt.result
            for receipt in state.receipts
            if receipt.intent_id == intent.id
            and receipt.result is not None
            and receipt.result.status in {"succeeded", "dry_run"}
        ]
        if not results:
            raise ActionIntentConflictError("no completed provider result is available for rollback")
        result = results[-1]
        try:
            rolled_back = await self.execution.rollback(
                intent.binding_id,
                result,
                actor=actor,
            )
        except Exception as exc:
            return self._set_status(
                intent.id,
                ActionIntentStatus.REQUIRES_RECONCILIATION,
                error=f"rollback failed: {type(exc).__name__}",
            )
        self._append_receipt(
            intent,
            result=rolled_back,
            outcome="completed",
            details={"operation": "rollback", "reason": payload.reason},
        )
        if rolled_back.status != "rolled_back":
            return self._set_status(
                intent.id,
                ActionIntentStatus.REQUIRES_RECONCILIATION,
                error="provider rollback did not return rolled_back outcome",
            )
        return self._set_status(intent.id, ActionIntentStatus.ROLLED_BACK)
