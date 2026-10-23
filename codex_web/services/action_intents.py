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
    ActionIntentRollbackCheckpoint,
    ActionIntentStatus,
    ReadinessExecutionOrigin,
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
from codex_web.services.code_host_action_contract import (
    CODE_HOST_CHANGE_REQUEST_UPSERT_ACTION_ID,
    CODE_HOST_JOB_RERUN_ACTION_ID,
    CODE_HOST_PULL_REQUEST_MERGE_ACTION_ID,
    CODE_HOST_PULL_REQUEST_VERIFY_MERGE_ACTION_ID,
    CODE_HOST_PULL_REQUEST_READY_ACTION_ID,
    CODE_HOST_PULL_REQUEST_READY_EVIDENCE,
    CodeHostActionContract,
)
from codex_web.services.code_host_readiness import (
    ReadyExecution, ReadyIdentityObservation, ReadySecretObservation, readiness_execution, readiness_binding_checksum,
)
from codex_web.services.code_host_continuation import (
    ChangeRequestContinuation, continuation_context, request_identity,
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
from codex_web.definitions import DefinitionContext, reference_for
from codex_web.services.publication_recovery import ASSIGNMENTS, WORKERS
from codex_web.services.publication_recovery import WS, capture_scope, execution_assignment, intent_guard, scope_rows


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
        execution_profiles=None,
        maintenance_guard=None,
        maintenance_admission=None,
        status_notifier=None,
        procedure_discovery=None,
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
        self.execution_profiles = execution_profiles
        self.maintenance_guard = maintenance_guard
        self.maintenance_admission = maintenance_admission
        self.status_notifier = status_notifier
        self.procedure_execution = None
        self.procedure_discovery = procedure_discovery
        if procedure_discovery is not None:
            self.execution.procedure_admission = self.procedure_provider_admission

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
        intent = self.store.get(intent_id)
        if intent is None or not self._same_scope(intent, actor):
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
        state = self.store.load(intent_id=intent.id)
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
        execution_id: str | None = None,
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
        approval_roles = self.procedure_execution.authority_approval_roles(execution_id, request, actor) if self.procedure_execution is not None else ()
        decisions = [
            self.authority.evaluate(
                AuthorityEvaluationRequest(
                    capability=capability,
                    level=level,
                    project_id=request.project_id,
                    resource_ids=request.resource_ids,
                    autonomous_risk=self._authority_risk(definition),
                    approval_role_ids=approval_roles,
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

        updated = self.store.update(apply, intent_ids=(intent_id,))
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
        # Each caller starts a new observation. The refresh itself must still
        # compare that observation under the intent writer lock.
        current = self._intent(intent.id, actor)
        refreshed, error = self._refresh_security(current, actor=actor,
            authority_decision=authority_decision)
        return refreshed is not None, error

    def _refresh_security(
        self, intent: ActionIntent, *, actor: AuthenticationActor,
        authority_decision: ActionDecisionSnapshot,
    ) -> tuple[ActionIntent | None, str | None]:
        binding, _, definition, request = self.execution.resolve_contract(
            intent.binding_id,
            intent.request,
            actor=actor,
        )
        if self.security_boundary is None:
            return (
                intent if intent.security_decision.outcome == SecurityDecisionOutcome.ALLOW else None,
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

        refreshed = None
        def apply(state):
            nonlocal refreshed
            refreshed = None
            for index, item in enumerate(state.intents):
                if item.id == intent.id:
                    if (item.security_policy != intent.security_policy
                            or item.security_decision != intent.security_decision):
                        break  # A newer observation wins, including a denial.
                    refreshed = item.model_copy(
                        update={
                            "security_policy": binding.security_policy,
                            "security_decision": decision,
                            "updated_at": time.time(),
                        }
                    )
                    state.intents[index] = refreshed
                    break
            return state

        self.store.update(apply, intent_ids=(intent.id,))
        if refreshed is None:
            return None, "security observation changed during refresh; reconcile read-only"
        if decision.outcome != SecurityDecisionOutcome.ALLOW:
            return None, "; ".join(decision.reasons) or "security trust boundary denied action"
        return refreshed, None

    def _validate_execution_repository_scope(
        self,
        execution_id: str | None,
        request: ActionRequest,
        actor: AuthenticationActor,
        assignment_id: str | None = None,
        *, binding_id: str | None = None,
    ) -> None:
        if request.action_id == CODE_HOST_PULL_REQUEST_READY_ACTION_ID:
            self._ready_assignment(execution_id, request, actor, assignment_id)
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
        read_only_observation = False
        if request.action_id == CODE_HOST_PULL_REQUEST_VERIFY_MERGE_ACTION_ID and binding_id is not None:
            # This is a metadata scope predicate, not credential use by the
            # requester. The ordinary provider boundary independently resolves
            # the current worker's existing credential before either GET.
            binding = self.execution.registry.binding(binding_id,actor)
            provider = self.execution.registry.provider(binding.provider_type,binding.provider_instance,actor=actor)
            definition = self.execution._definition(provider,request.action_id)
            read_only_observation = (binding.enabled and provider.provider_type == "gitlab" and provider.contract_version == "1.2"
                and (binding.project_id is None or binding.project_id == request.project_id)
                and (not binding.resource_ids or set(request.resource_ids).issubset(binding.resource_ids))
                and definition.capabilities.read and definition.required_authority_level == "read"
                and definition.required_authority == ("repository.read.metadata",)
                and definition.filesystem_access == "none" and not definition.process_access)
        if request.action_id == CODE_HOST_PULL_REQUEST_VERIFY_MERGE_ACTION_ID and not read_only_observation:
            raise ActionIntentConflictError("merge observation provider contract is unavailable")
        if self.execution_workers is None:
            raise ActionIntentConflictError(
                "execution-bound repository action requires canonical execution assignment state"
            )

        try:
            assignment = execution_assignment(self.execution_workers, execution_id, actor, assignment_id)
        except (AssignmentNotFoundError, WorkerConflictError) as exc:
            raise ActionIntentConflictError(
                "execution-bound repository action requires a matching canonical execution assignment"
            ) from exc

        if getattr(assignment, "execution_profile_id", None) == "orchestration-admin":
            if self.execution_profiles is None or self.authority is None:
                raise ActionIntentConflictError("administrative execution capability is unavailable")
            try:
                for repository_id in repository_ids:
                    self.execution_profiles.authorize_administrative_target(
                        assignment,
                        operation_id=("repository.read.metadata"
                                      if read_only_observation else
                                      "repository.check.rerun"
                                      if request.action_id == CODE_HOST_JOB_RERUN_ACTION_ID
                                      else request.action_id.replace("code-host.", "repository.", 1)),
                        repository_id=repository_id, actor=actor, authority=self.authority,
                    )
            except ValueError as exc:
                raise ActionIntentConflictError(str(exc)) from exc
            return

        scope = assignment.repository_scope
        if scope is not None:
            writable = set(scope.writable_repository_ids)
            if read_only_observation:
                writable.update(scope.read_only_repository_ids)
        else:
            target = assignment.repository_target
            writable = (
                {target.mutable_repository_id}
                if target is not None and target.mutable_repository_id is not None
                else set()
            )

        outside_scope = sorted(set(repository_ids) - writable)
        if outside_scope:
            scope_label = ("read scope" if read_only_observation
                           else "writable scope")
            raise ActionIntentConflictError(
                f"action request targets repository outside execution {scope_label}: "
                + ", ".join(outside_scope)
            )

    def _change_request_continuation(
        self, request: ActionRequest, *, binding_id: str,
        execution_id: str | None, work_item_ref: str | None,
        actor: AuthenticationActor,
    ) -> ChangeRequestContinuation | None:
        origin_id = request.parameters.get("origin_intent_id")
        if origin_id is None:
            return None
        def denied():
            raise ActionIntentConflictError("change request origin or current execution ownership is not verified")
        if (
            request.action_id != CODE_HOST_CHANGE_REQUEST_UPSERT_ACTION_ID
            or not isinstance(origin_id, str) or not origin_id.startswith("action-intent-")
            or not execution_id or self.execution_workers is None
        ):
            denied()
        origin = self.store.get(origin_id)
        if (
            origin is None or origin.status != ActionIntentStatus.SUCCEEDED
            or origin.organization_id != actor.organization_id
            or origin.workspace_id != actor.workspace_id
            or origin.project_id != request.project_id
            or origin.binding_id != binding_id
            or origin.action_id != request.action_id
            or origin.resource_ids != request.resource_ids
            or origin.work_item_ref != work_item_ref
            or not origin.execution_id
            or origin.request.parameters.get("origin_intent_id") is not None
            or origin.idempotency_key == request.idempotency_key
        ):
            denied()
        new_payload = CodeHostActionContract.change_request(request)
        old_payload = CodeHostActionContract.change_request(origin.request)
        if any(new_payload[key] != old_payload[key] for key in ("head", "base")):
            denied()
        history = self.store.load(intent_id=origin.id)
        receipt = next((x for x in history.receipts if x.id == origin.last_receipt_id), None)
        verification = next((x for x in history.verifications if x.id == origin.last_verification_id), None)
        if (
            receipt is None or receipt.outcome != "completed"
            or receipt.intent_id != origin.id
            or receipt.provider_type != origin.provider_type
            or receipt.provider_instance != origin.provider_instance
            or receipt.action_id != origin.action_id
            or receipt.idempotency_key != origin.idempotency_key
            or receipt.result is None or receipt.result.status != "succeeded"
            or receipt.result.provider_binding_id != binding_id
            or receipt.result.action_id != request.action_id
            or receipt.result.idempotency_key != origin.idempotency_key
            or verification is None or not verification.verified
            or verification.intent_id != origin.id
        ):
            denied()
        output = receipt.result.output
        number = output.get("change_request_number")
        if (
            not isinstance(number, int) or isinstance(number, bool) or number < 1
            or not receipt.result.external_id
            or receipt.provider_external_id != receipt.result.external_id
            or output.get("resource_id") not in request.resource_ids
            or any(output.get(key) != old_payload[key] for key in ("head", "base"))
        ):
            denied()
        try:
            previous = self.execution_workers.assignment_for_execution(origin.execution_id, actor=actor)
            current = self.execution_workers.assignment_for_execution(execution_id, actor=actor)
        except (AssignmentNotFoundError, WorkerConflictError):
            denied()
        now = time.time()
        profile_fields = (
            "profile_id", "profile_revision", "profile_record_id", "role_id",
            "role_definition_ref", "authority_role_id", "authority_definition_ref",
            "execution_profile_id", "sandbox_requirement", "instructions_ref", "skill_refs",
        )
        if (
            previous.agent_profile is None or current.agent_profile is None
            or any(getattr(previous.agent_profile, key) != getattr(current.agent_profile, key)
                   for key in profile_fields)
            or previous.project_id != request.project_id or current.project_id != request.project_id
            or previous.created_by != current.created_by
            or current.status not in {"claimed", "running"}
            or current.lease is None or current.lease.expires_at <= now
            or current.lease.fence != current.fence
            or current.lease.worker_id != current.assigned_worker_id
            or (current.deadline_at is not None and current.deadline_at <= now)
        ):
            denied()
        worker = self.execution_workers.store.worker(current.assigned_worker_id)
        if (
            worker is None or worker.lifecycle not in {"active", "draining"}
            or worker.organization_id != actor.organization_id
            or worker.workspace_id != actor.workspace_id
        ):
            denied()
        # Use the existing execution-scope guard; same requester identity or a
        # historical lease is never authority for this new external write.
        self._validate_execution_repository_scope(execution_id, request, actor)
        return ChangeRequestContinuation(
            origin_intent_id=origin.id, binding_id=binding_id,
            request_identity=request_identity(request),
            marker=CodeHostActionContract.marker_from_key(origin.idempotency_key),
            external_id=receipt.result.external_id, number=number,
        )

    def _ready_assignment(self, execution_id, request, requester, assignment_id=None):
        if not execution_id or self.execution_workers is None or self.execution_profiles is None:
            raise ActionRequirementError("Readiness requires an explicitly declared live execution assignment")
        assignment = execution_assignment(self.execution_workers, execution_id, requester, assignment_id)
        profile, reference = self.execution_profiles.resolve(assignment.execution_profile_id,
            organization_id=assignment.organization_id, workspace_id=assignment.workspace_id,
            project_id=assignment.project_id)
        now = time.time()
        worker = self.execution_workers.store.worker(assignment.assigned_worker_id)
        lease = assignment.lease
        if ("repository.pull-request.ready" not in profile.control_plane_operations
                or reference != assignment.execution_profile_definition
                or assignment.status not in {"claimed", "running"}
                or lease is None or lease.expires_at <= now
                or lease.worker_id != assignment.assigned_worker_id or lease.fence != assignment.fence
                or worker is None or worker.lifecycle not in {"active", "draining"}
                or worker.organization_id != request.organization_id or worker.workspace_id != request.workspace_id
                or assignment.organization_id != request.organization_id or assignment.workspace_id != request.workspace_id
                or assignment.project_id != request.project_id or assignment.created_by != requester.identity_id
                or assignment.deadline_at is not None and assignment.deadline_at <= now):
            raise ActionRequirementError("Readiness declaration, current Definition or worker fence/lease is unavailable")
        return assignment

    async def _ready_execution_context(self, intent, actor):
        requester = await asyncio.to_thread(self._requester_actor, intent)
        origin = intent.readiness_origin
        if origin is None:
            raise ActionRequirementError("Readiness has no canonical originating assignment proof")
        original = await asyncio.to_thread(self._ready_assignment, intent.execution_id, intent.request, requester, origin.assignment_id)
        if (original.fence != origin.fence or original.assigned_worker_id != origin.worker_id
                or original.execution_profile_definition != origin.execution_profile_definition):
            raise ActionRequirementError("Readiness originating assignment proof changed")
        original_binding, original_provider, _, _ = self.execution.resolve_contract(intent.binding_id, intent.request, actor=actor)
        provider_pin = tuple(getattr(original_provider, field, None) for field in
            ("provider_type", "provider_instance", "contract_version", "api_base", "web_base"))
        resource_pins = tuple(self.execution.resources.get(resource, actor) for resource in intent.resource_ids)
        authority_pin = (intent.authority_recheck or intent.authority_decision).definition_refs
        pin = (original.id, original.fence, original.assigned_worker_id, original.execution_profile_definition)
        def validate():
            current = self._intent(intent.id, actor)
            if (current.status != ActionIntentStatus.EXECUTING or current.attempt != intent.attempt
                    or current.lease is None or current.lease.expires_at <= time.time()
                    or intent.lease is None or current.lease.owner != intent.lease.owner):
                raise ActionRequirementError("Readiness ActionIntent claim is no longer current")
            requester = self._requester_actor(intent)
            self.identity.actor_for_identity(actor.identity_id, scope=actor.tenant)
            worker = self.execution_workers.store.worker(original.assigned_worker_id)
            if worker is None or worker.service_identity_id != actor.identity_id:
                raise ActionRequirementError("Readiness executing worker identity does not match the assignment")
            binding, provider, definition, request = self.execution.resolve_contract(intent.binding_id, intent.request, actor=actor)
            if (binding != original_binding or definition != intent.action_definition
                    or readiness_binding_checksum(binding, provider) != origin.binding_checksum):
                raise ActionRequirementError("Readiness provider binding or action Definition changed")
            if (tuple(getattr(provider, field, None) for field in
                    ("provider_type", "provider_instance", "contract_version", "api_base", "web_base")) != provider_pin
                    or tuple(self.execution.resources.get(resource, actor) for resource in intent.resource_ids) != resource_pins):
                raise ActionRequirementError("Readiness provider or canonical repository changed")
            if provider.contract.locator_for_resource(self.execution.resources.get(request.resource_ids[0], actor)) != origin.repository:
                raise ActionRequirementError("Readiness originating repository locator changed")
            if request.credential_ref:
                reference = self.execution.secret_broker.metadata(request.credential_ref, actor=actor, require_use=True)
                if reference.status().value != "active":
                    raise ActionRequirementError("Readiness credential reference is no longer active")
            self._validate_execution_repository_scope(intent.execution_id, request, requester,
                assignment_id=origin.assignment_id)
            assignment = self._ready_assignment(intent.execution_id, request, requester, origin.assignment_id)
            if (assignment.id, assignment.fence, assignment.assigned_worker_id, assignment.execution_profile_definition) != pin:
                raise ActionRequirementError("Readiness originating assignment changed")
            decision = self._canonical_authority_snapshot(definition=definition, request=request,
                actor=requester, execution_id=intent.execution_id)
            if decision.outcome != ActionDecisionOutcome.ALLOW or decision.definition_refs != authority_pin:
                raise ActionRequirementError("Readiness current authority denied or Definition changed")
            allowed, _ = self._recheck_security(intent, authority_decision=decision, actor=requester)
            if not allowed:
                raise ActionRequirementError("Readiness current security policy denied")
            return binding, provider, definition, request
        async def checkpoint():
            binding, provider, definition, request = await asyncio.to_thread(validate)
            if self.execution.procedure_admission is None:
                raise ActionRequirementError("Readiness mandatory procedure admission is unavailable")
            await self.execution.procedure_admission(binding, provider, definition, request, actor, "execute")
            current = await asyncio.to_thread(self._intent, intent.id, actor)
            if (self.procedure_execution is None and current.procedure_discoveries
                    and current.procedure_discoveries[-1].selected):
                raise ActionRequirementError("Readiness selected procedure requires the canonical procedure interpreter")
            await asyncio.to_thread(validate)
        return ReadyExecution(intent.binding_id, request_identity(intent.request), checkpoint)

    async def _execute_provider(self, intent: ActionIntent, *, actor: AuthenticationActor):
        proof = None
        if intent.request.parameters.get("origin_intent_id") is not None:
            try:
                requester = await asyncio.to_thread(self._requester_actor, intent)
                proof = await asyncio.to_thread(
                    self._change_request_continuation, intent.request,
                    binding_id=intent.binding_id, execution_id=intent.execution_id,
                    work_item_ref=intent.work_item_ref, actor=requester,
                )
            except Exception as exc:
                # No provider invocation has begun. A stale/missing origin or
                # unavailable canonical proof is a known precondition failure,
                # never an uncertain external write requiring reconciliation.
                raise ActionRequirementError("change request continuation proof unavailable before provider execution") from exc
        from codex_web.procedure_discovery import procedure_action_intent
        ready_context = None
        if intent.action_id == CODE_HOST_PULL_REQUEST_READY_ACTION_ID:
            ready_context = await self._ready_execution_context(intent, actor)
        with continuation_context(proof), procedure_action_intent(intent.id), readiness_execution(ready_context):
            if intent.action_id == 'operator.host.command':
                from codex_web.services.home_host_action_provider import home_host_executor
                with home_host_executor(self, intent.id, actor):
                    return await self.execution.execute(intent.binding_id, intent.request, actor=actor)
            if intent.action_id in {'recovery.scheduled-firing.reconcile', 'recovery.storage-preservation.capture', 'recovery.storage-preservation.verify'}:
                from codex_web.services.recovery_mutation_admission import recovery_mutation_executor
                with recovery_mutation_executor(self, intent.id, actor):
                    return await self.execution.execute(intent.binding_id, intent.request, actor=actor)
            return await self.execution.execute(intent.binding_id, intent.request, actor=actor)

    def _procedure_context(self, *, actor, request, definition, provider, policy_source, policy_allows, execution_id, assignment_id=None):
        context = self.procedure_discovery.action_context(actor=actor, request=request,
            definition=definition, provider=provider, policy_source=policy_source, policy_allows=policy_allows)
        if execution_id is not None and self.execution_workers is not None:
            try:
                assignment = execution_assignment(self.execution_workers, execution_id, actor, assignment_id)
                context = self.procedure_discovery.assignment_context(context, assignment, actor=actor)
            except Exception:
                from dataclasses import replace
                context = replace(context, resolution_blockers=("procedure_assignment_lookup_failed",))
        return context

    def _record_procedure_discovery(self, intent_id, discovery):
        def apply(state):
            for index, item in enumerate(state.intents):
                if item.id == intent_id:
                    # Readiness revalidates at every network boundary. Retain
                    # changed evidence; identical decisions need only one record.
                    if (item.action_id == CODE_HOST_PULL_REQUEST_READY_ACTION_ID
                            and item.procedure_discoveries
                            and item.procedure_discoveries[-1].model_dump(exclude={"evaluated_at"})
                            == discovery.model_dump(exclude={"evaluated_at"})):
                        break
                    if len(item.procedure_discoveries) >= 64:
                        raise ActionRequirementError("procedure_history_limit: create a new explicitly admitted intent")
                    state.intents[index] = item.model_copy(update={
                        "procedure_discoveries": (*item.procedure_discoveries, discovery),
                        "updated_at": time.time(),
                    })
                    break
            return state
        self.store.update(apply, intent_ids=(intent_id,))

    async def procedure_provider_admission(self, binding, provider, definition, request_or_result, worker, phase):
        """Last common boundary before provider credential use and mutation."""
        from codex_web.procedure_reconciliation import current_reconciliation_scope
        recovery = current_reconciliation_scope()
        if recovery is not None and phase != 'verify':
            raise ActionRequirementError('Read-only reconciliation cannot execute or roll back a provider action')
        from codex_web.procedure_discovery import current_procedure_action_intent
        intent_id = current_procedure_action_intent()
        if recovery is not None and recovery.intent_id != intent_id:
            raise ActionRequirementError('Read-only reconciliation provider intent changed')
        from codex_web.services.procedure_discovery import ProcedureAdmissionError
        if intent_id is None:
            raise ProcedureAdmissionError(code="procedure_unsupported_enforcement", message=
                "procedure_unsupported_enforcement: direct provider mutation requires a canonical ActionIntent")
        intent = await asyncio.to_thread(self._intent, intent_id, worker)
        if binding.id != intent.binding_id or definition.action_id != intent.action_id:
            raise ActionRequirementError("procedure_scope_changed: provider target differs from canonical intent")
        if phase == "execute" and request_or_result != intent.request:
            raise ActionRequirementError("procedure_scope_changed: request differs from canonical intent")
        requester = await asyncio.to_thread(self._requester_actor, intent)
        if intent.action_id == CODE_HOST_PULL_REQUEST_VERIFY_MERGE_ACTION_ID:
            await asyncio.to_thread(self._validate_execution_repository_scope,
                intent.execution_id,intent.request,requester,binding_id=intent.binding_id)
        authority = await asyncio.to_thread(self._canonical_authority_snapshot,
            definition=definition, request=intent.request, actor=requester, execution_id=intent.execution_id)
        if authority.outcome != ActionDecisionOutcome.ALLOW:
            raise ActionRequirementError("procedure_authority_denied: " + (authority.reason or "requester authority denied"))
        context = await asyncio.to_thread(self._procedure_context,
            actor=requester, request=intent.request, definition=definition, provider=provider,
            policy_source=intent.policy_decision.source,
            policy_allows=intent.policy_decision.outcome != ActionDecisionOutcome.DENY, execution_id=intent.execution_id,
            assignment_id=(intent.readiness_origin.assignment_id if intent.readiness_origin else
                    intent.publication_recovery_scope.recovery.assignment_id if intent.publication_recovery_scope else None))
        if phase == "rollback":
            from dataclasses import replace
            context = replace(context, operation=intent.action_id + ".rollback", enforcement="canonical_rollback")
        if self.procedure_execution is not None:
            mediated = await self.procedure_execution.provider_admission(intent, requester, phase)
            if mediated:
                return
        previous = intent.procedure_discoveries[-1] if intent.procedure_discoveries else None
        discovery, _ = await asyncio.to_thread(self.procedure_discovery.discover, context, previous=previous)
        await asyncio.to_thread(self._record_procedure_discovery, intent.id, discovery)
        if intent.execution_id is not None and self.execution_workers is not None:
            try:
                assignment = await asyncio.to_thread(execution_assignment, self.execution_workers,
                    intent.execution_id, requester,
                    intent.readiness_origin.assignment_id if intent.readiness_origin else
                    intent.publication_recovery_scope.recovery.assignment_id if intent.publication_recovery_scope else None)
                await asyncio.to_thread(self.execution_workers.record_procedure_discovery,
                    assignment.id, discovery, expected_fence=assignment.fence, actor=requester)
            except Exception as exc:
                # No provider has been invoked. Lookup/fence failure here must
                # remain a typed precondition failure, never an unknown write.
                if discovery.admitted:
                    discovery = discovery.model_copy(update={"outcome": "failed",
                        "blockers": (*discovery.blockers, "procedure_assignment_evidence_unavailable")})
                    await asyncio.to_thread(self._record_procedure_discovery, intent.id, discovery)
                raise ProcedureAdmissionError(discovery) from exc
        if not discovery.admitted:
            raise ProcedureAdmissionError(discovery)
        if discovery.selected and self.procedure_execution is not None:
            raise ProcedureAdmissionError(code="procedure_interpreter_required", message="Matched operational actions require a durable current procedure step")

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
        assignment_id: str | None = None,
        admission_idempotency_key: str | None = None,
    ) -> ActionIntent:
        # Internal canonical admission dedupe is distinct from a provider's
        # ability to replay an effect. Never advertise provider idempotency for
        # thread creation/input whose native outcome can be uncertain.
        if admission_idempotency_key is not None and (
            not isinstance(admission_idempotency_key, str)
            or not 1 <= len(admission_idempotency_key) <= 500
            or payload.request.idempotency_key is not None
        ):
            raise ActionIntentConflictError("invalid separate admission idempotency key")
        request = payload.request
        if (request.action_id == CODE_HOST_PULL_REQUEST_READY_ACTION_ID
                and payload.verification_required is False):
            raise ActionIntentConflictError("Readiness requires independent provider verification")
        recovery_admission = capture_scope(self.execution_workers, request, payload.execution_id, actor, assignment_id)
        if recovery_admission is not None:
            recovery_scope = recovery_admission[0]
            if payload.work_item_ref not in {None, recovery_scope.recovery.work_item_ref}:
                raise ActionIntentConflictError("ActionIntent Work Item differs from current publication recovery scope")
            payload = payload.model_copy(update={"work_item_ref": recovery_scope.recovery.work_item_ref})
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
            assignment_id,
            binding_id=binding.id,
        )
        self._change_request_continuation(
            request, binding_id=binding.id, execution_id=payload.execution_id,
            work_item_ref=payload.work_item_ref, actor=actor,
        )
        if request.action_id in {"recovery.storage-preservation.capture", "recovery.storage-preservation.verify"} and payload.work_item_success is not None:
            raise ActionIntentConflictError("Storage-only preservation cannot advance Work Item success")
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
        admission_key = admission_idempotency_key or request.idempotency_key
        if admission_key:
            existing = self.store.find_idempotent(
                actor.organization_id, actor.workspace_id, binding.id,
                request.action_id, admission_key,
            )
            if existing is not None:
                same_request = (
                    self._idempotency_request_identity(existing.request)
                    == self._idempotency_request_identity(request)
                )
                same_attribution = (
                    existing.work_item_ref == payload.work_item_ref
                    and existing.execution_id == payload.execution_id
                    and existing.requested_by == actor.identity_id
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
            definition=definition, request=request, actor=actor, execution_id=payload.execution_id,
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
        idempotency_key = admission_key or f"codex-intent:{intent_id}"
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
        discovery = None
        if self.procedure_discovery is not None:
            context = self._procedure_context(actor=actor, request=request,
                definition=definition, provider=provider, policy_source=payload.policy_decision.source,
                policy_allows=payload.policy_decision.outcome != ActionDecisionOutcome.DENY, execution_id=payload.execution_id, assignment_id=assignment_id)
            pinned = self.procedure_execution.creation_discovery(payload, actor) if self.procedure_execution is not None else None
            discovery, _ = (pinned, "") if pinned is not None else self.procedure_discovery.discover(context)
        denied = (
            (discovery is not None and not discovery.admitted)
            or
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
        readiness_origin = None
        if request.action_id == CODE_HOST_PULL_REQUEST_READY_ACTION_ID:
            origin = self._ready_assignment(payload.execution_id, request, actor, assignment_id)
            readiness_origin = ReadinessExecutionOrigin(assignment_id=origin.id, fence=origin.fence,
                worker_id=origin.assigned_worker_id, execution_profile_definition=origin.execution_profile_definition,
                repository=provider.contract.locator_for_resource(self.execution.resources.get(request.resource_ids[0], actor)),
                binding_checksum=readiness_binding_checksum(binding, provider))
        intent = ActionIntent(
            id=intent_id,
            readiness_origin=readiness_origin,
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
            procedure_discoveries=(discovery,) if discovery is not None else (),
            procedure_deviation_reason=payload.procedure_deviation_reason,
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
                "; ".join(discovery.blockers) if discovery is not None and not discovery.admitted and discovery.blockers
                else authority_decision.reason
                if authority_decision.outcome == ActionDecisionOutcome.DENY
                else "; ".join(security_decision.reasons)
                if security_decision.outcome == SecurityDecisionOutcome.DENY
                else "policy denied action"
                if payload.policy_decision.outcome == ActionDecisionOutcome.DENY
                else None
            ),
            failure=denied_failure,
            work_item_success=payload.work_item_success,
            publication_recovery_scope=recovery_admission[0] if recovery_admission else None,
        )

        persisted = self.store.insert(intent, **({
            "guard_rows": recovery_admission[1], "guard": recovery_admission[2],
        } if recovery_admission else {}))
        if persisted.id != intent.id and (
            self._idempotency_request_identity(persisted.request)
            != self._idempotency_request_identity(request)
            or persisted.work_item_ref != payload.work_item_ref
            or persisted.execution_id != payload.execution_id
            or persisted.requested_by != actor.identity_id
        ):
            raise ActionIntentConflictError(
                "idempotency key is already bound to a different action target or execution context"
            )
        return persisted

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

        updated = self.store.update(apply, intent_ids=(intent_id,))
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

        for item in self.store.indexed_intents(
            "lease", organization_id=organization_id, workspace_id=workspace_id, now=current,
        ):
            self.store.update(apply, intent_ids=(item.id,))
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

        candidates = ([self.store.get(intent_id)] if intent_id is not None else
                      self.store.indexed_intents("pending", organization_id=actor.organization_id,
                                                 workspace_id=actor.workspace_id))
        for candidate in candidates:
            if candidate is None:
                continue
            if self.maintenance_admission is not None:
                with self.maintenance_admission(candidate) as admitted:
                    if not admitted:
                        continue
                    self.store.update(apply, intent_ids=(candidate.id,))
            else:
                self.store.update(apply, intent_ids=(candidate.id,))
            if claimed:
                break
        return claimed[0] if claimed else None

    def fail_if_unstarted(
        self, intent_id: str, *, actor: AuthenticationActor, reason: str,
    ) -> ActionIntent:
        """Reject invalid origin authority without inventing an external outcome.

        The comparison happens under the intent record lock, shared with claim,
        execution and receipt updates. Historical uncertain records are untouched.
        """
        self._require_worker(actor)
        self._intent(intent_id, actor)
        changed = False
        now = time.time()

        def apply(state):
            nonlocal changed
            for index, item in enumerate(state.intents):
                if (item.id == intent_id and item.status == ActionIntentStatus.PENDING
                        and item.attempt == 0 and item.lease is None
                        and item.execution_started_at is None
                        and item.last_receipt_id is None
                        and item.last_verification_id is None):
                    state.intents[index] = item.model_copy(update={
                        "status": ActionIntentStatus.FAILED,
                        "last_error": reason, "updated_at": now, "completed_at": now,
                        "failure": self._failure(
                            item, FailureReason.AUTHORITY_DENIED,
                            summary="Deferred origin is invalid; no provider attempt started. A fresh authorized assignment is required.",
                            source_native_code="deferred_origin_invalid",
                            details={"provider_attempts": 0, "external_outcome": "not_started"},
                        ),
                    })
                    changed = True
            return state

        state = self.store.update(apply, intent_ids=(intent_id,))
        result = next(item for item in state.intents if item.id == intent_id)
        if changed and self.status_notifier is not None:
            try:
                self.status_notifier(result)
            except Exception:
                pass
        return result

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
                    if (item.lease is None or item.lease.owner != worker_id
                            or item.lease.expires_at <= time.time()
                            or item.status not in {ActionIntentStatus.CLAIMED, ActionIntentStatus.EXECUTING}):
                        raise ActionIntentLeaseError("action intent lease is not active for worker")
                    state.intents[index] = item.model_copy(
                        update={"lease": renewed, "updated_at": now}
                    )
                    break
            return state

        updated = self.store.update(apply, intent_ids=(intent.id,))
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
                    if (item.lease is None or item.lease.owner != worker_id
                            or item.lease.expires_at <= time.time()
                            or item.status not in {ActionIntentStatus.CLAIMED, ActionIntentStatus.EXECUTING}):
                        raise ActionIntentLeaseError("action intent lease is not active for worker")
                    if item.rollback_checkpoint is not None:
                        raise ActionIntentUnsafeRetryError("an attempted rollback cannot replay the forward mutation")
                    if item.status != ActionIntentStatus.CLAIMED or item.attempt >= item.retry_policy.max_attempts:
                        raise ActionIntentLeaseError("action intent claim is no longer executable")
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

        guarded = self._publication_execution_guard(intent, actor=actor)
        updated = self.store.update(apply, intent_ids=(intent.id,), **guarded)
        return next(item for item in updated.intents if item.id == intent.id)

    def _publication_execution_guard(self, intent, *, actor=None):
        scope = intent.publication_recovery_scope
        guarded = {}
        if scope is not None:
            def guard(documents):
                intent_guard(documents, assignment_id=scope.recovery.assignment_id,
                    workspace_id=scope.execution_workspace_id, actor_id=intent.requested_by,
                    expected_scope=scope, expected_profile=scope.execution_profile_definition,
                    created_at=intent.created_at)
            assignment = self.execution_workers.store.assignment(scope.recovery.assignment_id)
            from codex_web.execution_workspaces import ExecutionWorkspace
            workspace = ExecutionWorkspace.model_validate(self.store.store.record_get(WS, scope.execution_workspace_id))
            guarded = {"guard_rows": scope_rows(assignment, workspace), "guard": guard}
        elif intent.execution_id and intent.action_id.startswith("code-host.") and self.execution_workers:
            assignment = self.execution_workers.assignment_for_execution(intent.execution_id, actor=self._requester_actor(intent))
            if getattr(assignment, "execution_workspace_id", None):
                def guard(documents):
                    raw = documents[WS].get(assignment.execution_workspace_id)
                    if raw and raw.get("publication_recovery_history"):
                        raise ActionIntentConflictError("Legacy ActionIntent scope was retired; inspect receipts instead of replaying")
                from codex_web.execution_workspaces import ExecutionWorkspace
                workspace = ExecutionWorkspace.model_validate(self.store.store.record_get(WS, assignment.execution_workspace_id))
                guarded = {"guard_rows": scope_rows(assignment, workspace), "guard": guard}
        if intent.action_id == CODE_HOST_PULL_REQUEST_READY_ACTION_ID:
            origin = intent.readiness_origin
            if (origin is None or actor is None or self.execution_workers is None
                    or self.execution_workers.store.store is not self.store.store):
                raise ActionRequirementError('Readiness execution requires the canonical live origin transaction')
            previous_guard = guarded.get('guard')
            def ready_guard(documents):
                if previous_guard is not None:
                    previous_guard(documents)
                requester = self._requester_actor(intent)
                assignment = self._ready_assignment(intent.execution_id, intent.request,
                    requester, origin.assignment_id)
                worker = self.execution_workers.store.worker(origin.worker_id)
                if (assignment.fence != origin.fence
                        or assignment.assigned_worker_id != origin.worker_id
                        or assignment.execution_profile_definition != origin.execution_profile_definition
                        or worker is None or worker.revoked_at is not None
                        or worker.service_identity_id != actor.identity_id):
                    raise ActionRequirementError('Readiness originating assignment changed before execution')
            guarded = {'guard_rows': {**guarded.get('guard_rows', {}),
                ASSIGNMENTS: (origin.assignment_id,), WORKERS: (origin.worker_id,)},
                'guard': ready_guard}
        return guarded

    def _begin_recovery_rollback(self, intent):
        # A recovery rollback is also an external execution. Serialize its start
        # with handoff and make the in-flight effect visible to completion gates.
        guarded = self._publication_execution_guard(intent)
        if not guarded:
            return intent
        def apply(state):
            for index, item in enumerate(state.intents):
                if item.id == intent.id:
                    if (item.status not in TERMINAL_ACTION_INTENT_STATUSES or item.status == ActionIntentStatus.ROLLED_BACK
                            or item.status != intent.status or item.last_receipt_id != intent.last_receipt_id):
                        raise ActionIntentConflictError('Recovery rollback state changed or requires reconciliation')
                    state.intents[index] = item.model_copy(update={'status':ActionIntentStatus.EXECUTING,
                        'execution_started_at':time.time(), 'updated_at':time.time(), 'last_error':None})
            return state
        updated = self.store.update(apply, intent_ids=(intent.id,), **guarded)
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

        self.store.update(apply, intent_ids=(intent.id,))
        return receipt

    def _append_verification(
        self,
        intent: ActionIntent,
        *,
        provider_verification: Any | None,
        evidence_evaluation: Any | None,
        verified: bool,
        findings: tuple[str, ...] = (),
        guarded: dict | None = None,
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

        self.store.update(apply, intent_ids=(intent.id,), **(guarded or {}))
        return receipt

    def _set_status(
        self,
        intent_id: str,
        status: ActionIntentStatus,
        *,
        error: str | None = None,
        failure: FailureRecord | None = None,
        clear_lease: bool = True,
        guarded: dict | None = None,
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

        updated = self.store.update(apply, intent_ids=(intent_id,), **(guarded or {}))
        result = next(item for item in updated.intents if item.id == intent_id)
        if self.status_notifier is not None:
            try:
                self.status_notifier(result)
            except Exception:
                # Projection hooks are observational and cannot break
                # canonical ActionIntent state transitions.
                pass
        return result

    @staticmethod
    def _check_ready_intent(current, expected):
        # Only bookkeeping may differ. Security is the exact observation
        # admitted by this invocation, including its decision identity/time.
        refreshed = {"updated_at", "procedure_discoveries"}
        if (current is None or current.status not in {ActionIntentStatus.EXECUTING,
                ActionIntentStatus.UNCERTAIN, ActionIntentStatus.REQUIRES_RECONCILIATION}
                or current.model_dump(exclude=refreshed) != expected.model_dump(exclude=refreshed)
                or current.status == ActionIntentStatus.EXECUTING
                and (current.lease is None or current.lease.expires_at <= time.time())):
            raise ActionRequirementError("Readiness settlement intent changed; reconcile read-only")

    def _ready_settlement_domains(self):
        # These canonical documents own the mutable admission inputs used by
        # verification/procedure checks. Their revisions are captured BEFORE
        # those checks, then fenced with the actual successful write. In
        # particular, another checkpoint outside the transaction cannot fence
        # a binding disable, credential revoke or new mandatory procedure.
        repositories = [self.execution.registry.store, self.execution.resources.store,
                        self.identity.store, self.execution_profiles.registry.store]
        if self.execution.secret_broker is not None:
            repositories.append(self.execution.secret_broker.store)
        if self.execution_profiles.agent_profiles is not None:
            repositories.append(self.execution_profiles.agent_profiles.store)
        if self.procedure_execution is not None:
            repositories.append(self.procedure_execution.approvals.store)
        if self.artifact_evidence is not None:
            repositories.append(self.artifact_evidence.store)
        if self.procedure_discovery is not None:
            repositories.append(self.procedure_discovery.skills.definitions.store)
        if any(repository.store is not self.store.store for repository in repositories):
            raise ActionRequirementError("Readiness admission requires one canonical transaction store")
        domains = {repository.namespace: (None,) for repository in repositories}
        security = self.procedure_discovery.skills.security if self.procedure_discovery else None
        if security is not None:
            store = security.store
            if store.state is not self.store.store:
                raise ActionRequirementError("Readiness procedure security requires the canonical store")
            names = (store.SCANS, store.OVERRIDES, store.BASELINES)
            existing = self.store.store.record_collections_exist(names)
            # Lock absent collections as documents without materializing them.
            domains.update({name: () if existing[name] else (None,) for name in names})
        return domains

    def _ready_admission_snapshot(self):
        identity_namespace = self.identity.store.namespace
        secret_store = getattr(getattr(self.execution, "secret_broker", None), "store", None)
        pins = {}
        for name in self._ready_settlement_domains():
            if name == identity_namespace:
                observed_at = time.time()
                pins[name] = ReadyIdentityObservation(self.identity.store.load(), observed_at)
            elif secret_store is not None and name == secret_store.namespace:
                observed_at = time.time()
                pins[name] = ReadySecretObservation(secret_store.load(), observed_at)
            else:
                pins[name] = self.store.store.namespace_revision(name)
        return pins

    def _ready_admission_unchanged(self, pins, *, documents=None):
        if set(pins) != set(self._ready_settlement_domains()):
            return False
        identity_namespace = self.identity.store.namespace
        secret_store = getattr(getattr(self.execution, "secret_broker", None), "store", None)
        for name, pin in pins.items():
            if name == identity_namespace:
                if not isinstance(pin, ReadyIdentityObservation):
                    return False
                try:
                    # The actual successful writer has this singleton selected
                    # and locked; compare its supplied row, never an unlocked
                    # observation after the checkpoint.
                    current = (self.identity.store._decode(documents[name].get(None))
                               if documents is not None else self.identity.store.load())
                    if not pin.matches(current, now=time.time()):
                        return False
                except (ValueError, TypeError, KeyError) as exc:
                    raise ActionRequirementError("Readiness current identity admission is unavailable") from exc
            elif secret_store is not None and name == secret_store.namespace:
                if not isinstance(pin, ReadySecretObservation):
                    return False
                try:
                    current = (secret_store._decode(documents[name].get(None))
                               if documents is not None else secret_store.load())
                    if not pin.matches(current, now=time.time()):
                        return False
                except (ValueError, TypeError, KeyError) as exc:
                    raise ActionRequirementError("Readiness current credential admission is unavailable") from exc
            elif self.store.store.namespace_revision(name) != pin:
                return False
        return True

    def _ready_settlement_guard(self, intent, actor, admission_pins):
        """Recheck origin and exact Definition pins under the shared writer lock.

        The async admission includes policy/procedure evaluation. This final,
        read-only guard closes the interval after its last await, and serializes
        verification, Work Item and terminal writes with fence/Definition writes.
        """
        origin = intent.readiness_origin
        if origin is None or self.execution_workers is None or self.execution_profiles is None:
            raise ActionRequirementError("Readiness originating admission is unavailable")
        registry = self.execution_profiles.registry
        if (self.execution_workers.store.store is not self.store.store
                or registry.store.store is not self.store.store
                or self.authority is None or self.authority.registry.store.store is not self.store.store):
            raise ActionRequirementError("Readiness settlement requires the shared canonical transaction store")
        domains = self._ready_settlement_domains()
        if not admission_pins or set(admission_pins) != set(domains):
            raise ActionRequirementError("Readiness has no completed admission checkpoint")
        def guard(documents):
            if not self._ready_admission_unchanged(admission_pins, documents=documents):
                raise ActionRequirementError("Readiness admission changed at settlement; reconcile read-only")
            raw = documents[self.store.records_namespace].get("intents:" + intent.id)
            current = ActionIntent.model_validate(raw) if raw else None
            self._check_ready_intent(current, intent)
            requester = self._requester_actor(intent)
            # Expiry can remove admission without changing a namespace revision.
            # Re-enter read-only canonical checks under the same locks as the
            # write, including current secret status and time-bounded authority.
            self.identity.actor_for_identity(actor.identity_id, scope=actor.tenant)
            # Membership refresh is credential-free; retain the authenticated
            # worker's original service scopes for SecretBroker use checks.
            worker_actor = actor
            binding, provider, definition, request = self.execution.resolve_contract(
                intent.binding_id, intent.request, actor=worker_actor)
            if (definition != intent.action_definition
                    or readiness_binding_checksum(binding, provider) != origin.binding_checksum
                    or provider.contract.locator_for_resource(
                        self.execution.resources.get(request.resource_ids[0], worker_actor)) != origin.repository):
                raise ActionRequirementError("Readiness provider target changed at settlement")
            if (current.security_decision.outcome != SecurityDecisionOutcome.ALLOW
                    or current.security_policy != binding.security_policy):
                raise ActionRequirementError("Readiness current security observation denied settlement")
            if request.credential_ref and self.execution.secret_broker.metadata(
                    request.credential_ref, actor=worker_actor, require_use=True).status().value != "active":
                raise ActionRequirementError("Readiness credential expired at settlement")
            authority = self._canonical_authority_snapshot(definition=definition,
                request=request, actor=requester, execution_id=intent.execution_id)
            if (authority.outcome != ActionDecisionOutcome.ALLOW
                    or authority.definition_refs != (intent.authority_recheck or intent.authority_decision).definition_refs):
                raise ActionRequirementError("Readiness authority expired or changed at settlement")
            assignment = self._ready_assignment(intent.execution_id, intent.request, requester, origin.assignment_id)
            worker = self.execution_workers.store.worker(origin.worker_id)
            if (assignment.fence != origin.fence or assignment.assigned_worker_id != origin.worker_id
                    or assignment.execution_profile_definition != origin.execution_profile_definition
                    or worker is None or worker.service_identity_id != actor.identity_id):
                raise ActionRequirementError("Readiness originating assignment changed at settlement")
            context = DefinitionContext(organization_id=intent.organization_id,
                workspace_id=intent.workspace_id, project_id=intent.project_id)
            for pin in (origin.execution_profile_definition,
                        *(intent.authority_recheck or intent.authority_decision).definition_refs,
                        *(current.procedure_discoveries[-1].selected if current.procedure_discoveries else ())):
                record = registry.resolve(definition_id=pin.definition_id, kind=pin.kind,
                    context=context, now=time.time())
                if reference_for(record) != pin:
                    raise ActionRequirementError("Readiness pinned Definition changed at settlement")
            if current.procedure_discoveries:
                for pin in current.procedure_discoveries[-1].selected:
                    self.procedure_discovery.skills.validate_reference(pin, actor=requester, project_id=intent.project_id)
        return {"guard_rows": {**domains, ASSIGNMENTS: (origin.assignment_id,), WORKERS: (origin.worker_id,),
                               registry.store.namespace: (None,),
                               self.store.records_namespace: ("intents:" + intent.id,)}, "guard": guard}

    async def _ready_verification_context(self, intent, result, actor):
        origin = intent.readiness_origin
        parameters = CodeHostActionContract.pull_request_ready(intent.request)
        if (origin is None or result.output.get("repository") != origin.repository
                or result.provider_binding_id != intent.binding_id or result.action_id != intent.action_id
                or result.idempotency_key != intent.idempotency_key
                or any(result.output.get(key) != value for key, value in parameters.items())):
            raise ActionRequirementError("Readiness verification has no exact originating target proof")
        admitted = intent
        def validate():
            nonlocal admitted
            current = self._intent(intent.id, actor)
            self._check_ready_intent(current, admitted)
            if current.security_decision.outcome != SecurityDecisionOutcome.ALLOW:
                raise ActionRequirementError("Readiness current security observation denied")
            requester = self._requester_actor(intent)
            self.identity.actor_for_identity(actor.identity_id, scope=actor.tenant)
            assignment = self._ready_assignment(intent.execution_id, intent.request, requester, origin.assignment_id)
            worker = self.execution_workers.store.worker(origin.worker_id)
            if (assignment.fence != origin.fence or assignment.assigned_worker_id != origin.worker_id
                    or assignment.execution_profile_definition != origin.execution_profile_definition
                    or worker is None or worker.service_identity_id != actor.identity_id):
                raise ActionRequirementError("Readiness verification originating assignment changed")
            binding, provider, definition, request = self.execution.resolve_contract(
                intent.binding_id, intent.request, actor=actor)
            if (definition != intent.action_definition
                    or readiness_binding_checksum(binding, provider) != origin.binding_checksum
                    or provider.contract.locator_for_resource(self.execution.resources.get(request.resource_ids[0], actor)) != origin.repository):
                raise ActionRequirementError("Readiness verification binding or repository changed")
            self._validate_execution_repository_scope(intent.execution_id, request, requester,
                assignment_id=origin.assignment_id)
            decision = self._canonical_authority_snapshot(
                definition=definition, request=request, actor=requester, execution_id=intent.execution_id)
            if (decision.outcome != ActionDecisionOutcome.ALLOW
                    or decision.definition_refs != (intent.authority_recheck or intent.authority_decision).definition_refs):
                raise ActionRequirementError("Readiness verification current authority denied or Definition changed")
            refreshed, error = self._refresh_security(current, authority_decision=decision, actor=requester)
            if refreshed is None:
                raise ActionRequirementError(error or "Readiness verification current security policy denied")
            admitted = refreshed
            if request.credential_ref and self.execution.secret_broker.metadata(
                    request.credential_ref, actor=actor, require_use=True).status().value != "active":
                raise ActionRequirementError("Readiness verification credential is inactive")
            return binding, provider, definition
        admission_pins = {}
        async def checkpoint():
            before = await asyncio.to_thread(self._ready_admission_snapshot)
            binding, provider, definition = await asyncio.to_thread(validate)
            if self.execution.procedure_admission is None:
                raise ActionRequirementError("Readiness verification procedure admission is unavailable")
            await self.execution.procedure_admission(binding, provider, definition, result, actor, "verify")
            current = await asyncio.to_thread(self._intent, intent.id, actor)
            if (self.procedure_execution is None and current.procedure_discoveries
                    and current.procedure_discoveries[-1].selected):
                raise ActionRequirementError("Readiness selected procedure requires the canonical procedure interpreter")
            await asyncio.to_thread(validate)
            if not await asyncio.to_thread(self._ready_admission_unchanged, before):
                raise ActionRequirementError("Readiness admission changed during checkpoint")
            admission_pins.clear()
            admission_pins.update(before)
        def settlement_guard(current):
            # Preserve the caller's receipt/status snapshot while pinning the
            # exact security observation produced by this admission checkpoint.
            expected = current.model_copy(update={"security_policy": admitted.security_policy,
                "security_decision": admitted.security_decision})
            return self._ready_settlement_guard(expected, actor, dict(admission_pins))
        return ReadyExecution(intent.binding_id, request_identity(intent.request), checkpoint,
            settlement_guard, lambda: admitted)

    async def _verify_completion(
        self,
        intent: ActionIntent,
        result: ActionResult,
        *,
        actor: AuthenticationActor,
        settlement_guard: dict | None = None,
    ) -> bool | tuple[ActionIntentVerificationReceipt, ActionIntent]:
        provider_verification = None
        findings: list[str] = []
        provider_ok = True
        # Readiness success is never caller-optional, including legacy or
        # previously persisted records with verification_required=False.
        if intent.verification_required or intent.action_id in {CODE_HOST_PULL_REQUEST_READY_ACTION_ID, "recovery.scheduled-firing.reconcile", "recovery.storage-preservation.capture", "recovery.storage-preservation.verify"}:
            try:
                ready_context = (await self._ready_verification_context(intent, result, actor)
                                 if intent.action_id == CODE_HOST_PULL_REQUEST_READY_ACTION_ID else None)
                from codex_web.procedure_discovery import procedure_action_intent
                with procedure_action_intent(intent.id), readiness_execution(ready_context):
                    provider_verification = await self.execution.verify(intent.binding_id, result, actor=actor)
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
                evidence_evaluation = await asyncio.to_thread(self.artifact_evidence.evaluate,
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
        guarded = settlement_guard
        if verified and intent.action_id == CODE_HOST_PULL_REQUEST_READY_ACTION_ID:
            try:
                # Evidence may await after the last provider observation. Re-enter
                # canonical admission, then guard the actual successful write.
                with procedure_action_intent(intent.id):
                    await ready_context.checkpoint()
                guarded = ready_context.settlement_guard(intent)
            except Exception as exc:
                verified = False
                findings.append(f"readiness settlement denied: {type(exc).__name__}: {exc}")
        try:
            receipt = await asyncio.to_thread(self._append_verification, intent,
                provider_verification=provider_verification, evidence_evaluation=evidence_evaluation,
                verified=verified, findings=tuple(findings), guarded=guarded)
        except Exception as exc:
            if not guarded:
                raise
            verified = False
            findings.append(f"readiness settlement guard denied: {type(exc).__name__}: {exc}")
            await asyncio.to_thread(self._append_verification, intent,
                provider_verification=provider_verification, evidence_evaluation=evidence_evaluation,
                verified=False, findings=tuple(findings))
        return ((receipt, ready_context.admitted_intent())
                if verified and intent.action_id == CODE_HOST_PULL_REQUEST_READY_ACTION_ID else verified)

    async def _finish_ready(self, intent, result, actor, verification):
        verification, admitted = verification
        intent = intent.model_copy(update={"security_policy": admitted.security_policy,
            "security_decision": admitted.security_decision})
        try:
            # A verification/receipt write and every later await can race origin
            # revocation. Retain the real observation; never repeat the POST.
            current = await asyncio.to_thread(self._intent, intent.id, actor)
            # Accept only the exact verification produced by this invocation.
            self._check_ready_intent(current, intent.model_copy(update={
                "last_verification_id": verification.id}))
            context = await self._ready_verification_context(current, result, actor)
            from codex_web.procedure_discovery import procedure_action_intent
            with procedure_action_intent(intent.id):
                await context.checkpoint()
            if current.work_item_success is not None and current.work_item_ref is not None:
                host = self.work_item_host
                if host is None or not hasattr(host, "_advance_verified_action_intent"):
                    raise ActionRequirementError("Guarded canonical Work Item advancement is unavailable")
                await asyncio.to_thread(host._advance_verified_action_intent, current,
                    **context.settlement_guard(current))
            return await asyncio.to_thread(self._set_status, intent.id, ActionIntentStatus.SUCCEEDED,
                guarded=context.settlement_guard(current))
        except Exception as exc:
            return await asyncio.to_thread(self._ready_reconciliation, intent.id,
                f"Readiness settlement denied; reconcile read-only: {type(exc).__name__}: {exc}")

    def _ready_reconciliation(self, intent_id, error):
        # A concurrent terminal transition cannot be replaced by this older
        # verifier. Keep provider observations without inventing a new outcome.
        def apply(state):
            for index, item in enumerate(state.intents):
                if item.id == intent_id and item.status in {ActionIntentStatus.EXECUTING,
                        ActionIntentStatus.UNCERTAIN, ActionIntentStatus.REQUIRES_RECONCILIATION}:
                    state.intents[index] = item.model_copy(update={
                        "status": ActionIntentStatus.REQUIRES_RECONCILIATION,
                        "lease": None, "last_error": error, "updated_at": time.time()})
            return state
        state = self.store.update(apply, intent_ids=(intent_id,))
        result = next(item for item in state.intents if item.id == intent_id)
        if self.status_notifier is not None:
            try:
                self.status_notifier(result)
            except Exception:
                pass  # Same observational projection boundary as _set_status.
        return result

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
        pending = await asyncio.to_thread(self._intent, intent_id, actor)
        try:
            requester = await asyncio.to_thread(self._requester_actor, pending)
            _, _, current_definition, current_request = await asyncio.to_thread(self.execution.resolve_contract,
                pending.binding_id,
                pending.request,
                actor=actor,
            )
            await asyncio.to_thread(
                self._validate_execution_repository_scope,
                pending.execution_id, current_request, requester,
                pending.readiness_origin.assignment_id if pending.readiness_origin else
                pending.publication_recovery_scope.recovery.assignment_id if pending.publication_recovery_scope else None,
                binding_id=pending.binding_id,
            )
            authority_recheck = await asyncio.to_thread(self._canonical_authority_snapshot,
                definition=current_definition,
                request=current_request,
                actor=requester,
                execution_id=pending.execution_id,
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
        pending = await asyncio.to_thread(self._persist_authority_recheck,
            pending.id,
            authority_recheck,
        )
        if authority_recheck.outcome != ActionDecisionOutcome.ALLOW:
            return await asyncio.to_thread(self._set_status,
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
        allowed, reason = await asyncio.to_thread(self._recheck_security,
            pending,
            actor=actor,
            authority_decision=authority_recheck,
        )
        if not allowed:
            return await asyncio.to_thread(self._set_status,
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
                await asyncio.to_thread(self.entitlements.consume,
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
                return await asyncio.to_thread(self._set_status,
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
                capacity_lease = await asyncio.to_thread(self.capacity.acquire,
                    organization_id=pending.organization_id,
                    workspace_id=pending.workspace_id,
                    workload=WorkloadKind.ACTION,
                    priority=self._capacity_priority(pending),
                    owner_ref=pending.id,
                    component_key=component_key,
                    lease_seconds=pending.timeout_seconds + 30.0,
                )
            except CapacityDeferredError as exc:
                return await asyncio.to_thread(self._defer_for_capacity,
                    pending.id,
                    reason=exc.reason,
                    retry_at=exc.retry_at,
                )
        try:
            intent = await asyncio.to_thread(self._mark_executing, intent_id, worker_id, actor)
        except Exception:
            if self.capacity is not None and capacity_lease is not None:
                await asyncio.to_thread(self.capacity.release, capacity_lease.id)
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
                    self._execute_provider(intent, actor=actor),
                    timeout=intent.timeout_seconds,
                )
                if self.procedure_execution is not None:
                    await asyncio.to_thread(self.procedure_execution.result_admission, intent, result)
            except asyncio.TimeoutError:
                if self.capacity is not None:
                    await asyncio.to_thread(self.capacity.record_failure,
                        organization_id=intent.organization_id,
                        workspace_id=intent.workspace_id,
                        component_key=component_key,
                        reason="provider_timeout",
                    )
                    if capacity_lease is not None:
                        await asyncio.to_thread(self.capacity.release, capacity_lease.id)
                        capacity_lease = None
                await asyncio.to_thread(self._append_receipt,
                    intent,
                    result=None,
                    outcome="unknown",
                    details={"reason": "timeout"},
                )
                return await asyncio.to_thread(self._set_status,
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
                    await asyncio.to_thread(self.capacity.release, capacity_lease.id)
                    capacity_lease = None
                raise
            except ActionRequirementError as exc:
                if self.capacity is not None and capacity_lease is not None:
                    await asyncio.to_thread(self.capacity.release, capacity_lease.id)
                    capacity_lease = None
                await asyncio.to_thread(self._append_receipt,
                    intent,
                    result=None,
                    outcome="failed",
                    details={"reason": type(exc).__name__},
                )
                return await asyncio.to_thread(self._set_status,
                    intent.id,
                    ActionIntentStatus.FAILED,
                    error=f"provider precondition failed: {exc}",
                    failure=self._failure(
                        intent,
                        FailureReason.CONFIGURATION_MISSING_OR_INVALID,
                        source_native_code=type(exc).__name__,
                    ),
                )
            except Exception as exc:
                if self.capacity is not None:
                    await asyncio.to_thread(self.capacity.record_failure,
                        organization_id=intent.organization_id,
                        workspace_id=intent.workspace_id,
                        component_key=component_key,
                        reason=type(exc).__name__,
                    )
                    if capacity_lease is not None:
                        await asyncio.to_thread(self.capacity.release, capacity_lease.id)
                        capacity_lease = None
                await asyncio.to_thread(self._append_receipt,
                    intent,
                    result=None,
                    outcome="unknown",
                    details={"reason": type(exc).__name__},
                )
                return await asyncio.to_thread(self._set_status,
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
            await asyncio.to_thread(self.capacity.release, capacity_lease.id)
            capacity_lease = None
        await asyncio.to_thread(self._append_receipt,
            intent,
            result=result,
            outcome="failed" if result.status == "failed" else "completed",
        )
        current = await asyncio.to_thread(self._intent, intent.id, actor)
        if result.status == "failed":
            if self.capacity is not None:
                await asyncio.to_thread(self.capacity.record_failure,
                    organization_id=intent.organization_id,
                    workspace_id=intent.workspace_id,
                    component_key=component_key,
                    reason=result.error_code or "provider_failed",
                )
            native_code = result.error_code or "provider_failed"
            reason_code = action_failure_reason(native_code)
            return await asyncio.to_thread(self._set_status,
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
            await asyncio.to_thread(self.capacity.record_success,
                organization_id=intent.organization_id,
                workspace_id=intent.workspace_id,
                component_key=component_key,
            )
        if result.status == "rolled_back":
            return await asyncio.to_thread(self._set_status, intent.id, ActionIntentStatus.ROLLED_BACK)

        verified = await self._verify_completion(current, result, actor=actor)
        if not verified:
            if intent.action_id == CODE_HOST_PULL_REQUEST_READY_ACTION_ID:
                return await asyncio.to_thread(self._ready_reconciliation, intent.id,
                    "required readiness provider/evidence/current-origin verification is not satisfied; reconcile read-only")
            return await asyncio.to_thread(self._set_status,
                intent.id,
                ActionIntentStatus.REQUIRES_RECONCILIATION,
                error="required provider/evidence verification is not satisfied",
                failure=self._failure(
                    intent,
                    FailureReason.VERIFICATION_FAILED,
                ),
            )
        if intent.action_id == CODE_HOST_PULL_REQUEST_READY_ACTION_ID:
            return await self._finish_ready(current, result, actor, verified)
        current = await asyncio.to_thread(self._intent, intent.id, actor)
        try:
            await asyncio.to_thread(self._advance_work_item, current)
        except Exception as exc:
            return await asyncio.to_thread(self._set_status,
                intent.id,
                ActionIntentStatus.REQUIRES_RECONCILIATION,
                error=f"external action verified but canonical state advance failed: {type(exc).__name__}: {exc}",
            )
        return await asyncio.to_thread(self._set_status, intent.id, ActionIntentStatus.SUCCEEDED)

    def _schedule_retry(
        self,
        intent: ActionIntent,
        payload: ActionIntentRetryRequest,
    ) -> ActionIntent:
        if intent.attempt >= intent.retry_policy.max_attempts:
            raise ActionIntentConflictError("action intent retry limit reached")
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

        updated = self.store.update(apply, intent_ids=(intent.id,))
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
        if intent.action_id in {"recovery.scheduled-firing.reconcile", "recovery.storage-preservation.capture", "recovery.storage-preservation.verify"}:
            raise ActionIntentUnsafeRetryError("Recovery custody action is single-attempt; reconcile read-only")
        if intent.rollback_checkpoint is not None:
            raise ActionIntentUnsafeRetryError("an attempted rollback cannot replay the forward mutation")
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

    def read_only_reconciliation_scope(self, intent, payload, worker, *, assignment=None,
                                       include_normal_budget=False):
        """A recovery reservation is never a provider-write or origin-transfer grant."""
        from codex_web.procedure_reconciliation import ReadOnlyReconciliationScope, current_reconciliation_scope
        self._require_worker(worker)
        if (payload.retry_if_idempotent or intent.action_id != CODE_HOST_PULL_REQUEST_READY_ACTION_ID
                or intent.status not in {ActionIntentStatus.UNCERTAIN, ActionIntentStatus.REQUIRES_RECONCILIATION}
                or intent.attempt < 1 or intent.execution_started_at is None or intent.readiness_origin is None):
            return None
        origin = intent.readiness_origin
        if self.execution_workers is None:
            raise ActionRequirementError('Read-only reconciliation requires canonical execution workers')
        # Preserve ordinary reconciliation semantics below the normal budget;
        # recovery must not turn optional verification into a new early error.
        retained = self.execution_workers.store.assignment(origin.assignment_id)
        previous = current_reconciliation_scope()
        if (retained is None or len(retained.procedure_discoveries) < 64
                and not include_normal_budget and previous is None):
            return None
        requester = self._requester_actor(intent)
        current = self._ready_assignment(intent.execution_id, intent.request, requester, origin.assignment_id)
        current_worker = self.execution_workers.store.worker(current.assigned_worker_id)
        if (current.fence != origin.fence or current.assigned_worker_id != origin.worker_id
                or current.execution_profile_definition != origin.execution_profile_definition
                or current_worker is None or current_worker.service_identity_id != worker.identity_id
                or (assignment is not None and (assignment.id, assignment.execution_id, assignment.fence)
                    != (current.id, current.execution_id, current.fence))):
            raise ActionRequirementError('Read-only reconciliation requires unchanged originating custody')
        if previous is not None:
            if (previous.intent_id, previous.assignment_id, previous.fence, previous.worker_id) != (
                    intent.id, current.id, current.fence, current.assigned_worker_id):
                raise ActionRequirementError('Read-only reconciliation context changed')
            return previous
        return ReadOnlyReconciliationScope(id=uuid.uuid4().hex, intent_id=intent.id,
            assignment_id=current.id, execution_id=current.execution_id,
            worker_id=current.assigned_worker_id, fence=current.fence,
            requester_identity_id=requester.identity_id, organization_id=current.organization_id,
            workspace_id=current.workspace_id, project_id=current.project_id,
            resource_ids=tuple(intent.resource_ids), agent_profile=current.agent_profile,
            execution_profile_definition=current.execution_profile_definition,
            expires_at=min(current.deadline_at or time.time()+300, time.time()+300))

    async def reconcile(self, intent_id, payload, *, actor):
        from codex_web.procedure_reconciliation import read_only_reconciliation
        self._require_worker(actor)
        intent = await asyncio.to_thread(self._intent, intent_id, actor)
        scope = await asyncio.to_thread(self.read_only_reconciliation_scope, intent, payload, actor)
        with read_only_reconciliation(scope):
            return await self._reconcile_current(intent_id, payload, actor=actor)

    async def _reconcile_current(
        self,
        intent_id: str,
        payload: ActionIntentReconcileRequest,
        *,
        actor: AuthenticationActor,
    ) -> ActionIntent:
        self._require_worker(actor)
        intent = await asyncio.to_thread(self._intent, intent_id, actor)
        state = await asyncio.to_thread(self.store.load, intent_id=intent.id)
        results = [
            receipt.result
            for receipt in state.receipts
            if receipt.intent_id == intent.id and receipt.result is not None
        ]
        latest_result = results[-1] if results else None
        if intent.action_id in {"recovery.scheduled-firing.reconcile", "recovery.storage-preservation.capture", "recovery.storage-preservation.verify"}:
            if payload.retry_if_idempotent:
                raise ActionIntentConflictError("Recovery reconciliation is read-only; never retry execute")
            if intent.status in {ActionIntentStatus.UNCERTAIN, ActionIntentStatus.REQUIRES_RECONCILIATION} and intent.attempt > 0:
                from codex_web.services.recovery_intent_reconciliation import reconcile_recovery_intent
                return await reconcile_recovery_intent(self, intent, actor=actor)
            return intent  # no attempted unknown operation: do not invent one
        if intent.rollback_checkpoint is not None:
            rollback_receipt = next((r for r in state.receipts if r.id == intent.rollback_checkpoint.receipt_id), None)
            if rollback_receipt is None:
                rollback_receipt = next((r for r in reversed(state.receipts)
                    if r.details.get("operation") == "rollback"
                    and r.details.get("origin_receipt_id") == intent.rollback_checkpoint.origin_receipt_id
                    and r.result is not None and r.result.status == "rolled_back"), None)
            if rollback_receipt is None or rollback_receipt.result is None or rollback_receipt.result.status != "rolled_back":
                return await asyncio.to_thread(self._set_status, intent.id, ActionIntentStatus.REQUIRES_RECONCILIATION,
                    error="rollback outcome is unresolved; observe the provider and supply a durable rollback receipt before reconciliation")
            checkpoint = intent.rollback_checkpoint.model_copy(update={"receipt_id": rollback_receipt.id, "status": "rolled_back", "error_code": None})
            await asyncio.to_thread(self._rollback_checkpoint, intent.id, checkpoint)
            return await asyncio.to_thread(self._set_status, intent.id, ActionIntentStatus.ROLLED_BACK)

        if latest_result is not None and latest_result.status == "rolled_back":
            return await asyncio.to_thread(self._set_status, intent.id, ActionIntentStatus.ROLLED_BACK)
        if latest_result is not None and latest_result.status in {"succeeded", "dry_run"}:
            verified = await self._verify_completion(
                intent,
                latest_result,
                actor=actor,
            )
            if verified:
                if intent.action_id == CODE_HOST_PULL_REQUEST_READY_ACTION_ID:
                    return await self._finish_ready(intent, latest_result, actor, verified)
                current = await asyncio.to_thread(self._intent, intent.id, actor)
                try:
                    await asyncio.to_thread(self._advance_work_item, current)
                except Exception as exc:
                    return await asyncio.to_thread(self._set_status,
                        intent.id,
                        ActionIntentStatus.REQUIRES_RECONCILIATION,
                        error=f"verification succeeded but canonical state advance failed: {type(exc).__name__}: {exc}",
                    )
                return await asyncio.to_thread(self._set_status, intent.id, ActionIntentStatus.SUCCEEDED)
            if intent.action_id == CODE_HOST_PULL_REQUEST_READY_ACTION_ID:
                return await asyncio.to_thread(self._ready_reconciliation, intent.id,
                    "readiness reconciliation verification is not satisfied")
            return await asyncio.to_thread(self._set_status,
                intent.id,
                ActionIntentStatus.REQUIRES_RECONCILIATION,
                error="reconciliation verification is not satisfied",
            )

        if intent.action_id == CODE_HOST_PULL_REQUEST_READY_ACTION_ID:
            if payload.retry_if_idempotent:
                raise ActionIntentConflictError("Readiness may only reconcile by read-only verification; never replay")
            if latest_result is None and intent.attempt > 0:
                return await self._reconcile_pull_request_ready(intent, actor=actor)

        # Merge actions are deliberately single-attempt mutations.  When a
        # provider response is lost after the possible write, reconstruct only
        # the result shape from the already-authorized canonical request and
        # run the provider's read-only verification path.  Preparing an action
        # is contractually side-effect free; execute is never called here.
        if (
            latest_result is None
            and not payload.retry_if_idempotent
            and intent.action_id == CODE_HOST_PULL_REQUEST_MERGE_ACTION_ID
            and intent.status
            in {
                ActionIntentStatus.UNCERTAIN,
                ActionIntentStatus.REQUIRES_RECONCILIATION,
            }
            and intent.attempt > 0
        ):
            return await self._reconcile_pull_request_merge(intent, actor=actor)

        if payload.retry_if_idempotent and intent.provider_idempotency_supported:
            if intent.attempt >= intent.retry_policy.max_attempts:
                error = (
                    "reconciliation cannot requeue action: retry budget exhausted "
                    f"({intent.attempt}/{intent.retry_policy.max_attempts}); "
                    "confirm the provider outcome and create a newly authorized "
                    "replacement ActionIntent if delivery is still required"
                )
                if latest_result is not None and latest_result.status == "failed":
                    return await asyncio.to_thread(self._set_status,
                        intent.id,
                        ActionIntentStatus.FAILED,
                        error=error,
                    )
                return await asyncio.to_thread(self._set_status,
                    intent.id,
                    ActionIntentStatus.REQUIRES_RECONCILIATION,
                    error=error,
                    failure=self._failure(
                        intent,
                        FailureReason.UNKNOWN_OUTCOME,
                        summary=error,
                        details={
                            "retry_budget_exhausted": True,
                            "attempt": intent.attempt,
                            "max_attempts": intent.retry_policy.max_attempts,
                        },
                    ),
                )
            return await asyncio.to_thread(self._schedule_retry,
                intent,
                ActionIntentRetryRequest(
                    reason="reconciliation retry using provider idempotency"
                ),
            )

        return await asyncio.to_thread(self._set_status,
            intent.id,
            ActionIntentStatus.REQUIRES_RECONCILIATION,
            error=(
                "no durable provider result is available; manual/provider reconciliation required"
            ),
        )

    async def _reconcile_pull_request_ready(self, intent, *, actor):
        # Reconstruct only the authorized target. No execute call and no new mutation.
        parameters = CodeHostActionContract.pull_request_ready(intent.request)
        preparation = await self.execution.prepare(intent.binding_id, intent.request, actor=actor)
        if (preparation.provider_plan.get("operation") != "pull-request-ready" or intent.readiness_origin is None
                or preparation.provider_plan.get("repository") != intent.readiness_origin.repository):
            raise ActionIntentConflictError("Readiness reconciliation provider plan changed")
        now = time.time()
        result = ActionResult(provider_binding_id=intent.binding_id,
            action_id=intent.action_id, status="succeeded", started_at=now, completed_at=now,
            idempotency_key=intent.idempotency_key, external_id=str(parameters["pull_request_number"]),
            output={"resource_id": intent.resource_ids[0], **parameters,
                    "repository": preparation.provider_plan["repository"],
                    "read_only_reconciliation": True})
        verified = await self._verify_completion(intent, result, actor=actor)
        if not verified:
            return await asyncio.to_thread(self._ready_reconciliation, intent.id,
                "Provider/evidence/current origin does not confirm exact PR repository/head/base and non-draft state")
        receipt = await asyncio.to_thread(self._append_receipt, intent, result=result, outcome="completed",
            details={"operation": "readiness-state-reconciliation", "provider_mutation_replayed": False})
        return await self._finish_ready(intent.model_copy(update={"last_receipt_id": receipt.id}),
            result, actor, verified)

    async def _reconcile_pull_request_merge(
        self,
        intent: ActionIntent,
        *,
        actor: AuthenticationActor,
    ) -> ActionIntent:
        try:
            preparation = await self.execution.prepare(
                intent.binding_id,
                intent.request,
                actor=actor,
            )
            plan = preparation.provider_plan
            if plan.get("operation") != "pull-request-merge":
                raise ValueError("provider returned an unexpected merge plan")
            repository = str(plan.get("repository") or "").strip()
            number = int(plan.get("pull_request_number") or 0)
            if not repository or number < 1 or len(intent.resource_ids) != 1:
                raise ValueError("provider merge plan is incomplete")
            expected_head_sha = str(plan.get("expected_head_sha") or "").strip()
            if len(expected_head_sha) != 40 or any(
                character not in "0123456789abcdef"
                for character in expected_head_sha.casefold()
            ):
                raise ValueError(
                    "provider merge plan requires an exact expected head SHA"
                )
            now = time.time()
            result = ActionResult(
                provider_binding_id=intent.binding_id,
                action_id=intent.action_id,
                status="succeeded",
                started_at=intent.execution_started_at or now,
                completed_at=now,
                idempotency_key=intent.idempotency_key,
                external_id=str(number),
                output={
                    "resource_id": intent.resource_ids[0],
                    "repository": repository,
                    "pull_request_number": number,
                    "head_sha": expected_head_sha,
                    "merge_commit_sha": "",
                },
            )
        except Exception as exc:
            return await asyncio.to_thread(self._set_status,
                intent.id,
                ActionIntentStatus.REQUIRES_RECONCILIATION,
                error=(
                    "merge reconciliation preparation failed: "
                    f"{type(exc).__name__}: {exc}"
                ),
            )

        verified = await self._verify_completion(intent, result, actor=actor)
        if not verified:
            return await asyncio.to_thread(self._set_status,
                intent.id,
                ActionIntentStatus.REQUIRES_RECONCILIATION,
                error="provider does not confirm the exact pull request merge",
                failure=self._failure(
                    intent,
                    FailureReason.VERIFICATION_FAILED,
                ),
            )
        await asyncio.to_thread(self._append_receipt,
            intent,
            result=result,
            outcome="completed",
            details={
                "operation": "provider-state-reconciliation",
                "provider_mutation_replayed": False,
            },
        )
        current = await asyncio.to_thread(self._intent, intent.id, actor)
        try:
            await asyncio.to_thread(self._advance_work_item, current)
        except Exception as exc:
            return await asyncio.to_thread(self._set_status,
                intent.id,
                ActionIntentStatus.REQUIRES_RECONCILIATION,
                error=(
                    "merge verification succeeded but canonical state advance failed: "
                    f"{type(exc).__name__}: {exc}"
                ),
            )
        return await asyncio.to_thread(self._set_status, intent.id, ActionIntentStatus.SUCCEEDED)

    def _rollback_checkpoint(self, intent_id, checkpoint, *, reserve=False):
        def apply(state):
            item = state.intents[0]
            if reserve and (item.rollback_checkpoint is not None
                or item.status not in {ActionIntentStatus.SUCCEEDED, ActionIntentStatus.REQUIRES_RECONCILIATION}
                or item.last_receipt_id != checkpoint.origin_receipt_id):
                raise ActionIntentConflictError("rollback origin changed or was already attempted; reconcile instead of replaying it")
            item.rollback_checkpoint = checkpoint
            item.status = ActionIntentStatus.REQUIRES_RECONCILIATION
            item.updated_at = time.time()
            return state
        guarded = self._publication_execution_guard(self.store.get(intent_id)) if reserve else {}
        state = self.store.update(apply, intent_ids=(intent_id,), **guarded)
        return state.intents[0]

    async def rollback(
        self, intent_id: str, payload: ActionIntentRollbackRequest, *, actor: AuthenticationActor,
    ) -> ActionIntent:
        self._require_worker(actor)
        intent = await asyncio.to_thread(self._intent, intent_id, actor)
        if intent.rollback_checkpoint is not None:
            # Durable receipt wins over a lost local response. Never send the
            # provider the originating result a second time after interruption.
            return await self.reconcile(intent.id, ActionIntentReconcileRequest(retry_if_idempotent=False), actor=actor)
        if intent.status not in {ActionIntentStatus.SUCCEEDED, ActionIntentStatus.REQUIRES_RECONCILIATION}:
            raise ActionIntentConflictError("rollback requires a known completed original action")
        if not intent.action_definition.capabilities.rollback or not intent.action_definition.reversible:
            raise ActionIntentConflictError("action intent does not support rollback")
        history = await asyncio.to_thread(self.store.load, intent_id=intent.id)
        receipt = next((r for r in reversed(history.receipts) if r.result is not None
            and r.result.status == "succeeded" and r.outcome == "completed"), None)
        if receipt is None:
            raise ActionIntentConflictError("no completed provider result is available for rollback")
        checkpoint = ActionIntentRollbackCheckpoint(origin_receipt_id=receipt.id, status="running", started_at=time.time())
        intent = await asyncio.to_thread(self._rollback_checkpoint, intent.id, checkpoint, reserve=True)
        try:
            from codex_web.procedure_discovery import procedure_action_intent
            with procedure_action_intent(intent.id):
                rolled_back = await asyncio.wait_for(self.execution.rollback(intent.binding_id, receipt.result, actor=actor, credential_ref=intent.credential_ref),
                    timeout=intent.timeout_seconds)
                if self.procedure_execution is not None:
                    await asyncio.to_thread(self.procedure_execution.result_admission, intent, rolled_back)
        except Exception as exc:
            # Known pre-invocation gate refusal is distinct from uncertain effects.
            from codex_web.services.procedure_discovery import ProcedureAdmissionError
            known = isinstance(exc, (ActionRequirementError, ProcedureAdmissionError)) or getattr(exc, "code", "").startswith("procedure_")
            checkpoint.status = "failed" if known else "unknown"
            checkpoint.error_code = getattr(exc, "code", type(exc).__name__)
            await asyncio.to_thread(self._rollback_checkpoint, intent.id, checkpoint)
            return await asyncio.to_thread(self._set_status, intent.id, ActionIntentStatus.REQUIRES_RECONCILIATION,
                error=f"rollback {'refused' if known else 'outcome unknown'}: {checkpoint.error_code}",
                failure=self._failure(intent, FailureReason.AUTHORITY_DENIED if known else FailureReason.UNKNOWN_OUTCOME,
                    source_native_code=checkpoint.error_code, details={"operation":"rollback", "origin_receipt_id":receipt.id}))
        saved = await asyncio.to_thread(self._append_receipt, intent, result=rolled_back,
            outcome="completed" if rolled_back.status == "rolled_back" else "failed",
            details={"operation": "rollback", "origin_receipt_id": receipt.id, "reason": payload.reason})
        checkpoint.receipt_id = saved.id
        checkpoint.status = "rolled_back" if rolled_back.status == "rolled_back" else "failed"
        checkpoint.error_code = None if rolled_back.status == "rolled_back" else "provider_rollback_failed"
        await asyncio.to_thread(self._rollback_checkpoint, intent.id, checkpoint)
        return await asyncio.to_thread(self._set_status, intent.id,
            ActionIntentStatus.ROLLED_BACK if rolled_back.status == "rolled_back" else ActionIntentStatus.REQUIRES_RECONCILIATION,
            error=checkpoint.error_code,
            failure=self._failure(intent, FailureReason.UNCLASSIFIED, source_native_code=checkpoint.error_code,
                details={"operation":"rollback", "origin_receipt_id":receipt.id}) if checkpoint.error_code else None)
