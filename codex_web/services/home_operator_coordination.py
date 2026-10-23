"""Home adapters over canonical project conversations, actions and input custody.

The Home declaration selects targets. It does not create project membership,
invoke rights, a new identity, a work queue or a continuation controller.
"""
from __future__ import annotations

import hashlib
import time

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from codex_web.action_intents import ActionDecisionSnapshot, ActionIntentCreate, ActionIntentRetryPolicy
from codex_web.action_providers import (
    ACTION_PROVIDER_CONTRACT, ActionCapability, ActionDefinition, ActionEvidence,
    ActionRequest, ActionResult, ActionRiskClass, UnsupportedActionCapabilityError,
)
from codex_web.authority import AuthorityEvaluationRequest, AuthorityLevel
from codex_web.control_plane_broker import ControlPlaneBrokerOperation
from codex_web.models import HomeOperatorCoordinationDispatch, TurnCreate
from codex_web.services.project_coordination import ProjectCoordinationService


SOURCE = "home-operator-coordination"
MESSAGE_ACTION = "operator.project.thread.message"
PROVIDER = "home-operator-coordination"
OPERATIONS = (
    ControlPlaneBrokerOperation(id="operator.project.threads.list", method="GET",
        path_template="/api/operator/projects/threads", capability="operator.project.threads.list",
        authority_level=AuthorityLevel.READ),
    ControlPlaneBrokerOperation(id=MESSAGE_ACTION, method="POST",
        path_template="/api/operator/projects/thread/messages", capability=MESSAGE_ACTION,
        authority_level=AuthorityLevel.EXECUTE),
)


class HomeProjectMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    project_id: str = Field(min_length=1, max_length=128)
    thread_id: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=16000)
    idempotency_key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")


class HomeProjectEffect(BaseModel):
    # Internal admission identity is not provider effect-idempotency support.
    admission_key: str = Field(min_length=1, max_length=500)
    model_config = ConfigDict(extra="forbid", frozen=True)
    assignment_id: str = Field(min_length=1, max_length=200)
    fence: int = Field(ge=1)
    home_revision: int = Field(ge=1)
    definition_record_id: str = Field(min_length=1, max_length=200)
    target_assignment_id: str = Field(min_length=1, max_length=200)
    target_fence: int = Field(ge=1)
    project_id: str = Field(min_length=1, max_length=128)
    thread_id: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=16000)


def denied(message):
    raise HTTPException(403, {"code": "home_coordination_denied", "message": message})


class HomeOperatorCoordinationService:
    def __init__(self, *, broker, origin_guard, thread_scope, bootstrap_bindings,
                 project_coordination, turns=None):
        self.broker = broker
        # Shared Home execution/identity/profile/worker/workspace guard. Caller
        # must return current assignment, requester, Home binding and envelope.
        self.origin_guard = origin_guard
        self.scope = thread_scope
        self.bindings = bootstrap_bindings
        self.coordination = project_coordination
        self.turns = turns

    def origin(self, assignment_id, fence, operation):
        snapshot = self.broker.execution_workers.store.assignment(assignment_id)
        if snapshot is None or snapshot.fence != fence:
            denied("Home operator custody changed")
        current, actor, binding, envelope = self.origin_guard(snapshot, operation)
        if current.id != assignment_id or current.fence != fence:
            denied("Home operator guard returned a successor assignment")
        return current, actor, binding, envelope

    def target(self, *, project_id, thread_id, actor, envelope, require_live=False):
        declaration = next((p for p in envelope.project_targets if p.project_id == project_id), None)
        if project_id == "home" or declaration is None:
            denied("Target Project is outside the explicit Home declaration")
        self.scope.for_thread(thread_id, actor, project_id)
        binding = self.bindings.get_by_thread(thread_id, actor)
        target = self.broker.execution_workers.store.assignment(binding.assignment_id)
        if (target is None or target.created_by != actor.identity_id
                or (target.organization_id, target.workspace_id, target.project_id) !=
                   (actor.organization_id, actor.workspace_id, project_id)
                or (target.execution_id, target.execution_workspace_id, target.subject) !=
                   (binding.execution_id, binding.execution_workspace_id, binding.subject)
                or target.agent_profile is None):
            denied("Target conversation does not match current canonical custody")
        pin = target.agent_profile
        if (pin.profile_id, pin.profile_revision) not in {
                (p.profile_id, p.revision) for p in declaration.child_profiles}:
            denied("Target Agent Profile is outside the explicit revision envelope")
        profiles = self.broker.execution_profiles.agent_profiles
        profile, _ = profiles.resolve_for_execution(pin.profile_id,
            revision=pin.profile_revision, actor=actor, project_id=project_id)
        if (profile.profile_id != pin.profile_id or profile.revision != pin.profile_revision
                or profile.record_id != pin.profile_record_id
                or profile.execution_profile_id != target.execution_profile_id):
            denied("Target Agent Profile changed")
        decision = self.broker.authority.evaluate(AuthorityEvaluationRequest(
            capability="operator.project.coordinate",
            level=AuthorityLevel.EXECUTE if require_live else AuthorityLevel.READ,
            project_id=project_id, resource_ids=target.resource_ids), actor=actor)
        if decision.outcome != "allow":
            denied("Current target Project authority denied")
        if require_live:
            now = self.broker._clock()
            lease = target.lease
            worker = self.broker.execution_workers.store.worker(target.assigned_worker_id)
            if (target.status not in {"claimed", "running"} or lease is None
                    or lease.fence != target.fence or lease.worker_id != target.assigned_worker_id
                    or lease.expires_at <= now or target.deadline_at is not None and target.deadline_at <= now
                    or worker is None or worker.lifecycle != "active"
                    or (worker.organization_id, worker.workspace_id) !=
                       (actor.organization_id, actor.workspace_id)):
                denied("Target custody requires canonical recovery before delegation")
        return binding, target, profile

    def target_repository_fields(self, target, actor):
        """Normalize input scope only after resolving the exact current contract."""
        contract, reference = self.broker.execution_profiles.resolve(
            target.execution_profile_id, organization_id=actor.organization_id,
            workspace_id=actor.workspace_id, project_id=target.project_id)
        if reference != target.execution_profile_definition:
            denied("Target execution contract changed")
        scope = target.repository_scope
        if contract.workspace_mode == "scratch" and contract.repository_access == "none":
            if scope is not None and (scope.writable_repository_ids or scope.read_only_repository_ids):
                denied("Scratch target carries unexpected repository custody")
            return None, (), ()
        if (contract.workspace_mode != "repository" or scope is None
                or not scope.writable_repository_ids):
            denied("Canonical target repository scope is unavailable")
        return scope.writable_repository_ids[0], scope.writable_repository_ids, scope.read_only_repository_ids

    def inspect(self, *, assignment_id, fence, project_id, query):
        current, actor, home, envelope = self.origin(assignment_id, fence, "operator.project.threads.list")
        if project_id not in {p.project_id for p in envelope.project_targets}:
            denied("Target Project is outside the explicit Home declaration")
        self.scope.project(project_id, actor)
        bindings = self.bindings.list(actor)
        if len(bindings) > 5000:
            denied("Canonical conversation inventory exceeds the bounded inspection capacity")
        rows = []
        for binding in bindings:
            candidate = self.broker.execution_workers.store.assignment(binding.assignment_id)
            if candidate is None or candidate.project_id != project_id or candidate.agent_profile is None:
                continue
            declaration = next(p for p in envelope.project_targets if p.project_id == project_id)
            if (candidate.agent_profile.profile_id, candidate.agent_profile.profile_revision) not in {
                    (p.profile_id, p.revision) for p in declaration.child_profiles}:
                continue
            matched, target, _ = self.target(project_id=project_id, thread_id=binding.thread_id,
                actor=actor, envelope=envelope)
            rows.append(self.coordination.thread_row(matched, target))
        result = ProjectCoordinationService.page(sorted(rows, key=lambda r: r["thread_id"]), query,
            [current.id, current.fence, home.revision, actor.identity_id, project_id,
             current.execution_profile_definition.model_dump(mode="json")])
        _, _, after, after_envelope = self.origin(assignment_id, fence, "operator.project.threads.list")
        if home != after or envelope != after_envelope:
            denied("Home selection or declaration changed during inspection")
        return {**result, "home_revision": home.revision, "project_id": project_id,
                "assignment_id": current.id, "observed_at": self.broker._clock()}

    def validate_queued_delegation(self, queued):
        proof = queued.project_coordination_dispatch
        if proof is None or proof.schema_version != "1.2" or queued.source != SOURCE:
            denied("Retained Home delegation proof required")
        current, actor, home, envelope = self.origin(proof.assignment_id, proof.fence, MESSAGE_ACTION)
        if home.revision != proof.home_revision:
            denied("Selected Home conversation changed; retain the original input")
        intent = self.broker.action_intents.get(proof.intent_id, actor)
        provider_binding = self.broker.action_intents.execution.registry.binding(intent.binding_id, actor)
        if (not provider_binding.enabled or provider_binding.credential_ref is not None
                or (provider_binding.provider_type, provider_binding.provider_instance) != (PROVIDER, "canonical")
                or (provider_binding.organization_id, provider_binding.workspace_id,
                    provider_binding.project_id, provider_binding.resource_ids) !=
                   (actor.organization_id, actor.workspace_id, "home", (envelope.host_resource_id,))):
            denied("The original Home coordination provider binding was disabled or changed")
        params = intent.request.parameters
        # UNCERTAIN never unlocks a possibly accepted queued input. Reconcile the
        # same intent first; neither a new key nor another native input is made.
        if (intent.idempotency_key != params.get("admission_key")
                or intent.action_id != MESSAGE_ACTION or intent.status not in {"executing", "succeeded"}
                or (intent.provider_type, intent.provider_instance) != (PROVIDER, "canonical")
                or intent.resource_ids != (envelope.host_resource_id,)
                or intent.requested_by != actor.identity_id or intent.execution_id != current.execution_id
                or (intent.organization_id, intent.workspace_id, intent.project_id) !=
                   (actor.organization_id, actor.workspace_id, "home")
                or params.get("assignment_id") != current.id or params.get("fence") != current.fence
                or params.get("home_revision") != home.revision
                or params.get("definition_record_id") != current.execution_profile_definition.record_id
                or params.get("target_assignment_id") != proof.target_assignment_id
                or params.get("target_fence") != proof.target_fence
                or params.get("project_id") != queued.project_id
                or params.get("thread_id") != queued.thread_id
                or params.get("message") != queued.message
                or queued.execution_id != f"home-coordinate-input:{intent.id}"
                or hashlib.sha256(queued.message.encode()).hexdigest() != proof.message_sha256):
            denied("Retained input does not match its original canonical Home action")
        _, target, profile = self.target(project_id=queued.project_id, thread_id=queued.thread_id,
            actor=actor, envelope=envelope, require_live=True)
        repository_id, writable, read_only = self.target_repository_fields(target, actor)
        if (target.id != proof.target_assignment_id or target.fence != proof.target_fence
                or queued.agent_profile_actor_id != actor.identity_id
                or queued.agent_profile_id != profile.profile_id or queued.agent_profile_revision != profile.revision
                or queued.execution_profile_id != target.execution_profile_id
                or queued.repository_resource_id != repository_id
                or queued.writable_repository_resource_ids != writable
                or queued.read_only_repository_resource_ids != read_only):
            denied("Retained input target identity, profile or repository custody changed")

    async def message(self, *, assignment_id, fence, payload, worker_actor):
        data = HomeProjectMessage.model_validate(payload)
        current, actor, home, envelope = self.origin(assignment_id, fence, MESSAGE_ACTION)
        _, target, _ = self.target(project_id=data.project_id, thread_id=data.thread_id,
            actor=actor, envelope=envelope, require_live=True)
        intents = self.broker.action_intents
        if intents is None:
            denied("Canonical action custody is unavailable")
        candidates = [b for b in intents.execution.registry.store.load().bindings
            if b.enabled and (b.provider_type, b.provider_instance) == (PROVIDER, "canonical")
            and (b.organization_id, b.workspace_id, b.project_id, b.resource_ids) ==
                (actor.organization_id, actor.workspace_id, "home", (envelope.host_resource_id,))
            and b.credential_ref is None]
        if len(candidates) != 1:
            denied("Exactly one explicit Home coordination provider binding is required")
        key = f"home-coordinate:{current.id}:{current.fence}:{data.idempotency_key}"
        effect = HomeProjectEffect(admission_key=key, assignment_id=current.id, fence=current.fence,
            home_revision=home.revision, definition_record_id=current.execution_profile_definition.record_id,
            target_assignment_id=target.id, target_fence=target.fence,
            project_id=data.project_id, thread_id=data.thread_id, message=data.message)
        request = ActionRequest(action_id=MESSAGE_ACTION, organization_id=actor.organization_id,
            workspace_id=actor.workspace_id, project_id="home", resource_ids=(envelope.host_resource_id,),
            requested_by=actor.identity_id, parameters=effect.model_dump(mode="json"))
        from codex_web.services.action_intents import ActionIntentConflictError
        try:
            intent = intents.create(ActionIntentCreate(binding_id=candidates[0].id, request=request,
                execution_id=current.execution_id, timeout_seconds=120,
                retry_policy=ActionIntentRetryPolicy(max_attempts=1),
                policy_decision=ActionDecisionSnapshot(outcome="allow", source="policy:assignment-control-plane",
                    reason="Exact current Home selection, project/profile envelope and target authority checked",
                    capabilities=(MESSAGE_ACTION,))), actor=actor, assignment_id=current.id,
                    admission_idempotency_key=key)
        except ActionIntentConflictError as exc:
            raise HTTPException(409, {"code": "home_coordination_conflict",
                "message": "Inspect the original action and retained input; do not replace the key or replay"}) from exc
        return await self.broker._claim_and_execute_repository_intent(intent.id,
            assignment=current, worker_actor=worker_actor, requester_actor=actor)


class HomeOperatorCoordinationActionProvider:
    """One canonical outbox effect admits one retained input, not an agent loop."""
    contract_version = ACTION_PROVIDER_CONTRACT.current
    provider_type, provider_instance = PROVIDER, "canonical"

    def __init__(self, service):
        self.service = service

    def actions(self):
        return (ActionDefinition(action_id=MESSAGE_ACTION, title="Delegate to an explicitly selected project conversation",
            risk_class=ActionRiskClass.HIGH,
            capabilities=ActionCapability(read=False, idempotency=False, verification=False, evidence=True),
            required_authority=(MESSAGE_ACTION,), timeout_seconds=120, retry_max_attempts=1,
            expected_evidence=("canonical-home-delegation-admission",)),)

    def context(self, request, binding):
        data = HomeProjectEffect.model_validate(request.parameters)
        current, actor, home, envelope = self.service.origin(data.assignment_id, data.fence, MESSAGE_ACTION)
        latest = self.service.broker.action_intents.execution.registry.binding(binding.id, actor)
        if latest != binding:
            denied("Home coordination provider binding changed during the action")
        if (request.action_id != MESSAGE_ACTION or request.requested_by != actor.identity_id
                or (request.organization_id, request.workspace_id, request.project_id, request.resource_ids) !=
                   (actor.organization_id, actor.workspace_id, "home", (envelope.host_resource_id,))
                or (binding.provider_type, binding.provider_instance) != (PROVIDER, "canonical")
                or not binding.enabled or binding.credential_ref is not None
                or (binding.organization_id, binding.workspace_id, binding.project_id, binding.resource_ids) !=
                   (actor.organization_id, actor.workspace_id, "home", (envelope.host_resource_id,))
                or data.home_revision != home.revision
                or data.definition_record_id != current.execution_profile_definition.record_id):
            denied("Home action origin, selection, provider or declaration changed")
        _, target, profile = self.service.target(project_id=data.project_id, thread_id=data.thread_id,
            actor=actor, envelope=envelope, require_live=True)
        if target.id != data.target_assignment_id or target.fence != data.target_fence:
            denied("Delegation target custody changed; do not transfer the original input")
        return data, current, actor, home, envelope, target, profile

    async def prepare(self, request, *, binding):
        self.context(request, binding)
        return {"ready": True, "effect": "one retained input in an existing conversation",
                "native_start_verified": False, "automatic_retry": False}

    async def execute(self, request, *, binding, credential=None):
        started = time.time()
        data, current, actor, home, envelope, target, profile = self.context(request, binding)
        intent = self.service.broker.action_intents.store.find_idempotent(
            actor.organization_id, actor.workspace_id, binding.id, MESSAGE_ACTION, data.admission_key)
        if (intent is None or intent.status != "executing" or intent.request != request
                or intent.execution_id != current.execution_id or intent.attempt != 1):
            denied("Exactly one executing canonical Home action is required before input admission")
        proof = HomeOperatorCoordinationDispatch(intent_id=intent.id,
            assignment_id=current.id, fence=current.fence, home_revision=home.revision,
            target_assignment_id=target.id, target_fence=target.fence,
            message_sha256=hashlib.sha256(data.message.encode()).hexdigest())
        repository_id, writable, read_only = self.service.target_repository_fields(target, actor)
        if self.service.turns is None:
            denied("Canonical target repository scope or input service is unavailable")
        def guard():
            context = self.context(request, binding)
            if self.service.target_repository_fields(context[5], actor) != (repository_id, writable, read_only):
                denied("Target repository custody changed during input admission")
            original = self.service.broker.action_intents.get(intent.id, actor)
            if (original.status != "executing" or original.request != request or original.attempt != 1
                    or original.execution_id != current.execution_id or original.lease is None
                    or original.lease.owner != f"control-plane-broker:{current.assigned_worker_id}"
                    or original.lease.expires_at <= self.service.broker._clock()
                    or original.execution_started_at is None
                    or original.execution_started_at + original.timeout_seconds <= self.service.broker._clock()):
                denied("Original Home action custody changed before input admission")
            return context
        guard()
        result = await self.service.turns.start(data.thread_id, TurnCreate(message=data.message,
            project_id=data.project_id, repository_resource_id=repository_id,
            writable_repository_resource_ids=writable,
            read_only_repository_resource_ids=read_only,
            execution_profile_id=target.execution_profile_id, agent_profile_id=profile.profile_id,
            agent_profile_revision=profile.revision, defer_start=True), actor=actor,
            admission_guard=guard, source=SOURCE, project_coordination_dispatch=proof,
            execution_id=f"home-coordinate-input:{intent.id}", allow_assignment_renewal=False)
        guard()
        queued_id = result.get("queuedId")
        if not queued_id:
            raise RuntimeError("Home delegation admission outcome is unknown; retain the same action")
        return ActionResult(provider_binding_id=binding.id, action_id=MESSAGE_ACTION, status="succeeded",
            started_at=started, completed_at=time.time(), idempotency_key=request.idempotency_key,
            output={"intent_id": intent.id, "thread_id": data.thread_id, "project_id": data.project_id,
                    "queued_id": queued_id, "native_start_verified": False},
            evidence=(ActionEvidence(evidence_type="canonical-home-delegation-admission",
                reference=f"queued:{queued_id}", summary="One retained input admitted; native execution is separate"),))

    async def verify(self, result, *, binding, credential=None):
        raise UnsupportedActionCapabilityError("verification")

    async def rollback(self, result, *, binding, credential=None):
        raise UnsupportedActionCapabilityError("rollback")
