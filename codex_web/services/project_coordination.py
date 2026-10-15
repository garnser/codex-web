"""Typed, opt-in adapters over canonical project services; no controller or store."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from urllib.parse import unquote

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from codex_web.action_intents import ActionDecisionSnapshot, ActionIntentCreate, ActionIntentRetryPolicy
from codex_web.action_providers import (
    ActionCapability, ActionDefinition, ActionEvidence, ActionRequest, ActionResult,
    ActionVerification, UnsupportedActionCapabilityError,
)
from codex_web.authority import AuthorityEvaluationRequest, AuthorityLevel
from codex_web.control_plane_broker import ControlPlaneBrokerOperation
from codex_web.data_governance import ContextFilterRequest, DataClassification
from codex_web.definitions import DefinitionScope
from codex_web.identity import TenantScope
from codex_web.models import TurnCreate
from codex_web.organizational_memory import (
    KnowledgeCreate, KnowledgeIngestBatch, KnowledgeProvenance,
)


PREFIX = "/api/project-coordination"
# Protocol/engine allowlist, not a mutable operational catalog. Activation is data.
_SPECS = (
    ("threads.list", "GET", "/threads", "thread.read"),
    ("thread.read", "GET", "/threads/{ref}", "thread.read"),
    ("thread.create", "POST", "/threads", "thread.create"),
    ("thread.message", "POST", "/threads/{ref}/messages", "thread.message"),
    ("assignments.list", "GET", "/assignments", "execution.read"),
    ("assignment.read", "GET", "/assignments/{ref}", "execution.read"),
    ("memory.list", "GET", "/memory", "memory.read"),
    ("memory.read", "GET", "/memory/{ref}", "memory.read"),
    ("memory.checkpoint", "POST", "/memory/checkpoint", "memory.write"),
    ("elevation.read", "GET", "/elevation", "project.elevation.read"),
    ("action.read", "GET", "/actions/{ref}", "project.coordinate"),
)
PROJECT_COORDINATION_OPERATIONS = tuple(ControlPlaneBrokerOperation(
    id="project." + name, method=method, path_template=PREFIX + path,
    capability=capability,
    authority_level=AuthorityLevel.READ if method == "GET" else AuthorityLevel.EXECUTE,
) for name, method, path, capability in _SPECS)
OPERATION_MAP = {v.id: v for v in PROJECT_COORDINATION_OPERATIONS}
MUTATIONS = {v.id for v in PROJECT_COORDINATION_OPERATIONS if v.method == "POST"}


class CreateThread(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_id: str = Field(min_length=1, max_length=128)
    profile_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=128)


class MessageThread(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=16000)
    idempotency_key: str = Field(min_length=1, max_length=128)


class MemoryCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(pattern=r"^[A-Za-z0-9._-]{1,100}$")
    summary: str = Field(min_length=1, max_length=4000)
    content: str = Field(min_length=1, max_length=16000)
    idempotency_key: str = Field(min_length=1, max_length=128)


def denied(reason):
    raise HTTPException(status_code=403, detail={"code": "project_coordination_denied", "message": reason})


def resolve_operation(method, path):
    """No generic API forwarding, regex wildcards, caller-selected provider or actor."""
    for op in PROJECT_COORDINATION_OPERATIONS:
        template = op.path_template
        if "{ref}" not in template:
            if path == template and method == op.method:
                return op, None
        else:
            start, end = template.split("{ref}")
            if path.startswith(start) and (not end or path.endswith(end)) and method == op.method:
                ref = unquote(path[len(start):len(path)-len(end) if end else None])
                if ref and len(ref) <= 200 and "/" not in ref and not any(ord(c) < 32 for c in ref):
                    return op, ref
    denied("project coordination path or method is not supported")


class ProjectCoordinationService:
    def __init__(self, broker, *, threads, turns, scope, bindings, workers, memory,
                 approvals, attention):
        self.broker, self.threads, self.turns, self.scope = broker, threads, turns, scope
        self.bindings, self.workers, self.memory = bindings, workers, memory
        self.approvals, self.attention = approvals, attention
        # Serializes local bootstrap admission through the existing canonical
        # quota/profile gates. It is not persisted state or a distributed lease.
        self._creation_lock = asyncio.Lock()

    def authorize(self, assignment, operation, actor):
        now = self.broker._clock()
        lease = assignment.lease
        if (assignment.status not in {"claimed", "running"} or not lease
                or lease.fence != assignment.fence or lease.worker_id != assignment.assigned_worker_id
                or lease.expires_at <= now or assignment.deadline_at is None or assignment.deadline_at <= now
                or actor.identity_id != assignment.created_by
                or (actor.organization_id, actor.workspace_id) != (assignment.organization_id, assignment.workspace_id)):
            denied("matching live assignment, requester, deadline and fence required")
        profiles = self.broker.execution_profiles
        if profiles is None or assignment.execution_profile_definition is None:
            denied("project coordination requires an exact published opt-in Definition")
        try:
            self.scope.project(assignment.project_id, actor)
            profile, ref = profiles.resolve(assignment.execution_profile_id,
                organization_id=assignment.organization_id, workspace_id=assignment.workspace_id,
                project_id=assignment.project_id)
        except (ValueError, LookupError, HTTPException):
            denied("current Project or coordination Definition unavailable")
        record = profiles.registry.get_record(ref.record_id)
        envelope = profile.project_coordination
        if (not envelope or ref != assignment.execution_profile_definition
                or record.scope_type != DefinitionScope.PROJECT or record.scope_id != assignment.project_id
                or record.definition_schema_version != "1.2"
                or operation.id not in profile.control_plane_operations):
            denied("operation is not declared in the current exact Project coordination Definition")
        repository = self.broker._writable_repository_id(assignment)
        if envelope.repository_resource_id != repository or set(assignment.resource_ids) != {repository}:
            denied("coordination requires its single explicitly declared repository Resource")
        self.scope.project(assignment.project_id, actor)
        if assignment.agent_profile is not None:
            if profiles.agent_profiles is None:
                denied("current Agent Profile validation unavailable")
            pinned = assignment.agent_profile
            current, _ = profiles.agent_profiles.resolve_for_execution(pinned.profile_id,
                revision=pinned.profile_revision, actor=actor, project_id=assignment.project_id)
            if current.record_id != pinned.profile_record_id:
                denied("Agent Profile binding changed")
        decision = self.broker.authority.evaluate(AuthorityEvaluationRequest(
            capability="project.coordinate", level=operation.authority_level,
            project_id=assignment.project_id, resource_ids=(repository,)), actor=actor)
        if decision.outcome != "allow":
            denied("project.coordinate authority denied: " + "; ".join(decision.reasons))
        return envelope

    def guard(self, snapshot, operation):
        """Re-resolve every boundary from canonical state, never DTO projectId."""
        current = self.workers.store.assignment(snapshot.id)
        if current is None or any(getattr(current, key) != getattr(snapshot, key) for key in (
            "execution_id", "created_by", "organization_id", "workspace_id", "project_id",
            "assigned_worker_id", "fence", "execution_workspace_id", "execution_profile_definition",
            "repository_scope", "resource_ids", "agent_profile")):
            denied("assignment scope, requester, Definition or fence changed")
        worker = self.workers.store.worker(current.assigned_worker_id)
        if (worker is None or worker.lifecycle not in {"active", "draining"}
                or (worker.organization_id, worker.workspace_id) != (current.organization_id, current.workspace_id)):
            denied("assignment worker is unavailable or quarantined")
        if operation.id in MUTATIONS and worker.lifecycle != "active":
            denied("draining workers cannot admit new coordination mutations")
        actor = self.broker._requester_actor(current)
        # Validates the worker identity still exists in the canonical tenant.
        self.broker.identity.actor_for_identity(worker.service_identity_id,
            scope=TenantScope(organization_id=current.organization_id, workspace_id=current.workspace_id))
        envelope = self.authorize(current, operation, actor)
        self.broker._authorize(current, operation, actor=actor, resource_ids=(envelope.repository_resource_id,))
        workspace = self.broker.execution_workspaces.store.workspace(current.execution_workspace_id)
        lease = self.broker.execution_workspaces.store.lease(workspace.lease_id) if workspace else None
        if (not workspace or workspace.status != "active" or workspace.execution_id != current.execution_id
                or workspace.subject != current.subject or workspace.project_id != current.project_id
                or (workspace.organization_id, workspace.workspace_id) != (current.organization_id, current.workspace_id)
                or workspace.owner_identity_id != current.created_by
                or set(workspace.writable_repository_ids) != {envelope.repository_resource_id}
                or not lease or lease.execution_workspace_id != workspace.id
                or (lease.organization_id, lease.workspace_id, lease.execution_id, lease.subject, lease.owner_identity_id) !=
                   (current.organization_id, current.workspace_id, current.execution_id, current.subject, current.created_by)
                or set(lease.resource_ids) != {envelope.repository_resource_id}
                or lease.released_at is not None or lease.expires_at <= self.broker._clock()):
            denied("execution workspace scope or lease is stale")
        return current, actor, envelope

    def target(self, thread_id, assignment, actor, envelope):
        self.scope.for_thread(thread_id, actor, assignment.project_id)
        binding = self.bindings.get_by_thread(thread_id, actor)
        target = self.workers.store.assignment(binding.assignment_id)
        if (target is None or target.project_id != assignment.project_id
                or (target.organization_id, target.workspace_id) != (assignment.organization_id, assignment.workspace_id)
                or set(target.resource_ids) != {envelope.repository_resource_id}
                or target.execution_id != binding.execution_id
                or target.execution_workspace_id != binding.execution_workspace_id
                or target.subject != binding.subject):
            denied("thread canonical binding is outside assignment project/resource scope")
        return binding, target

    @staticmethod
    def page(rows, query, identity):
        """Filter first; cursors pin the visible snapshot and complete caller scope."""
        if len(rows) > 5000:
            denied("coordination inventory exceeds bounded inspection capacity")
        try:
            limit = int(query.get("limit", [20])[0])
        except (ValueError, TypeError):
            denied("coordination limit must be an integer")
        if not 1 <= limit <= 50:
            denied("coordination limit must be between 1 and 50")
        def stable(value):
            if isinstance(value, dict):
                return {k: stable(v) for k, v in value.items() if k != "observedAt"}
            return [stable(v) for v in value] if isinstance(value, list) else value
        revision = hashlib.sha256(json.dumps([identity, stable(rows)], sort_keys=True, default=str).encode()).hexdigest()
        cursor = query.get("cursor", [None])[0]
        offset = 0
        if cursor:
            parts = cursor.split(":")
            if len(parts) != 2 or parts[0] != revision or not parts[1].isdigit():
                raise HTTPException(status_code=409, detail={"code": "coordination_cursor_stale"})
            offset = int(parts[1])
        return {"items": rows[offset:offset+limit], "count": len(rows),
            "next_cursor": f"{revision}:{offset+limit}" if offset+limit < len(rows) else None}

    def thread_rows(self, assignment, actor, envelope):
        bindings = self.bindings.list(actor)
        if len(bindings) > 5000:
            denied("canonical thread inventory exceeds bounded inspection capacity")
        rows = []
        for binding in bindings:
            target = self.workers.store.assignment(binding.assignment_id)
            if (target is None or (target.organization_id, target.workspace_id, target.project_id) !=
                    (assignment.organization_id, assignment.workspace_id, assignment.project_id)
                    or set(target.resource_ids) != {envelope.repository_resource_id}):
                continue
            self.target(binding.thread_id, assignment, actor, envelope)
            rows.append(self.thread_row(binding, target))
        return sorted(rows, key=lambda v: v["thread_id"])

    def assignments(self, assignment, envelope):
        """Bounded canonical pages; foreign rows never enter counts or cursors."""
        rows, cursor, scanned = [], None, 0
        while True:
            page, cursor = self.workers.store.assignment_page(after=cursor, limit=100)
            scanned += len(page)
            if scanned > 5000:
                denied("assignment inventory exceeds bounded inspection capacity")
            rows.extend(v for v in page if
                (v.organization_id, v.workspace_id, v.project_id) ==
                (assignment.organization_id, assignment.workspace_id, assignment.project_id)
                and set(v.resource_ids) == {envelope.repository_resource_id})
            if cursor is None:
                return sorted(rows, key=lambda v: v.id)

    @staticmethod
    def assignment_view(value):
        return {"id": value.id, "status": str(value.status), "project_id": value.project_id,
            "resource_ids": list(value.resource_ids), "requester": value.created_by,
            "execution_id": value.execution_id, "execution_workspace_id": value.execution_workspace_id,
            "worker_id": value.assigned_worker_id, "fence": value.fence,
            "lease_expires_at": value.lease.expires_at if value.lease else None,
            "deadline_at": value.deadline_at, "blocker": value.failure_code,
            "definition": value.execution_profile_definition.model_dump(mode="json") if value.execution_profile_definition else None}

    def thread_row(self, binding, target):
        # Existing runner projection separates live custody from active work.
        runner = self.threads._runner_projection(binding.thread_id)
        active = self.threads.active_turn_getter(binding.thread_id) if self.threads.active_turn_getter else None
        depth = self.turns.queue_policy.depth(binding.thread_id)
        return {"thread_id": binding.thread_id, "assignment_id": target.id,
            "project_id": target.project_id, "resource_ids": list(target.resource_ids),
            "assignment_status": str(target.status), "fence": target.fence,
            "execution_id": target.execution_id, "execution_workspace_id": target.execution_workspace_id,
            "definition": target.execution_profile_definition.model_dump(mode="json") if target.execution_profile_definition else None,
            "agent_profile": {"profile_id": target.agent_profile.profile_id, "revision": target.agent_profile.profile_revision} if target.agent_profile else None,
            "runner": runner, "active_execution_id": getattr(active, "execution_id", None),
            "queue_depth": depth, "work_item_ref": target.work_item_ref,
            "blocker": target.failure_code,
            "activity": "active" if active else "queued" if depth else "idle" if runner and runner.get("live") else "unknown"}

    def memory_view(self, item, actor, project):
        if item.project_id != project:
            denied("Memory is outside current Project")
        allowed = self.memory.governance.filter_context(ContextFilterRequest(
            record_ids=(item.governance_record_id,), max_classification=DataClassification.INTERNAL), actor=actor)
        if item.governance_record_id not in allowed.allowed_record_ids:
            denied("Memory data governance denies model context")
        if len(item.content or "") > 16000:
            denied("Memory content exceeds bounded coordination context")
        return {"id": item.id, "project_id": item.project_id, "logical_key": item.logical_key,
            "version": item.version, "content_digest": item.content_sha256, "summary": item.summary,
            "content": item.content, "freshness": str(self.memory.freshness(item)),
            "governance_record_id": item.governance_record_id, "provenance": item.provenance.model_dump(mode="json")}

    async def dispatch(self, assignment, operation, target_ref, query, payload, validator, worker_actor):
        if set(query) - {"project_id", "repository_resource_id", "limit", "cursor"} or any(len(v) != 1 for v in query.values()):
            denied("unsupported or repeated coordination query fields")
        current, actor, envelope = self.guard(assignment, operation)
        if (query.get("project_id") != [current.project_id]
                or query.get("repository_resource_id") != [envelope.repository_resource_id]):
            denied("explicit own Project and repository Resource query scope required")
        identity = [current.id, current.fence, actor.identity_id, current.project_id, envelope.repository_resource_id, operation.id]
        if operation.method == "GET" and payload:
            denied("coordination reads accept no body")
        if operation.id in MUTATIONS:
            model = {"project.thread.create": CreateThread, "project.thread.message": MessageThread,
                     "project.memory.checkpoint": MemoryCheckpoint}[operation.id]
            parsed = model.model_validate(payload)
            result = await self.mutate(current, actor, envelope, operation, target_ref, parsed, worker_actor)
        elif operation.id == "project.threads.list":
            result = self.page(self.thread_rows(current, actor, envelope), query, identity)
        elif operation.id == "project.thread.read":
            binding, target = self.target(target_ref, current, actor, envelope)
            result = {"item": self.thread_row(binding, target)}
        elif operation.id == "project.assignments.list":
            result = self.page([self.assignment_view(v) for v in self.assignments(current, envelope)], query, identity)
        elif operation.id == "project.assignment.read":
            target = self.workers.store.assignment(target_ref)
            if (target is None or (target.organization_id, target.workspace_id, target.project_id, set(target.resource_ids)) !=
                    (current.organization_id, current.workspace_id, current.project_id, {envelope.repository_resource_id})):
                denied("assignment is outside own Project and repository Resource")
            result = {"item": self.assignment_view(target)}
        elif operation.id == "project.memory.list":
            rows = self.memory.list(actor=actor, project_id=current.project_id)
            visible = []
            for item in rows:
                try:
                    value = self.memory_view(item, actor, current.project_id)
                except HTTPException:
                    continue
                visible.append({k: v for k, v in value.items() if k != "content"})
            result = self.page(sorted(visible, key=lambda v: v["id"]), query, identity)
        elif operation.id == "project.memory.read":
            item = self.memory.get(target_ref, actor=actor)
            result = {"item": self.memory_view(item, actor, current.project_id)}
        elif operation.id == "project.action.read":
            result = self.action_view(target_ref, current, actor)
        else:
            result = self.elevations(current, actor, envelope, query, identity)
        # Reads and receipts cannot disclose after scope/authority changes during awaits.
        if validator:
            validator()
        self.guard(current, operation)
        return {**result, "coordination_scope": {"project_id": current.project_id,
            "repository_resource_id": envelope.repository_resource_id, "assignment_id": current.id,
            "requester": actor.identity_id, "definition": current.execution_profile_definition.model_dump(mode="json"),
            "authority_checked": True, "declaration_grants_authority": False}}

    def action_view(self, intent_id, assignment, actor):
        item = self.broker.action_intents.get(intent_id, actor)
        if (item.execution_id != assignment.execution_id or item.requested_by != actor.identity_id
                or (item.organization_id, item.workspace_id, item.project_id, item.resource_ids) !=
                   (assignment.organization_id, assignment.workspace_id, assignment.project_id, assignment.resource_ids)
                or item.action_id not in MUTATIONS):
            denied("coordination receipt is outside assignment/requester scope")
        history = self.broker.action_intents.history(intent_id, actor)
        return {"intent": {k: getattr(item, k) for k in ("id", "status", "last_receipt_id", "last_verification_id", "last_error")},
            "receipts": [{k: row.get(k) for k in ("id", "outcome", "result")} for row in history["receipts"][-1:]],
            "verifications": [{k: row.get(k) for k in ("id", "verified")} for row in history["verifications"][-1:]]}

    async def mutate(self, assignment, actor, envelope, op, target, payload, worker_actor):
        intents = self.broker.action_intents
        if intents is None:
            denied("canonical ActionIntent service is unavailable")
        registry = intents.execution.registry
        bindings = [v for v in registry.store.load().bindings if v.enabled
            and v.provider_type == "project-coordination" and v.provider_instance == "canonical"
            and (v.organization_id, v.workspace_id, v.project_id) == (assignment.organization_id, assignment.workspace_id, assignment.project_id)
            and v.resource_ids == (envelope.repository_resource_id,) and v.credential_ref is None]
        if len(bindings) != 1:
            denied("one explicitly enabled own-Project coordination ActionProvider binding required")
        if op.id == "project.thread.message":
            self.target(target, assignment, actor, envelope)
        request = ActionRequest(action_id=op.id, organization_id=assignment.organization_id,
            workspace_id=assignment.workspace_id, project_id=assignment.project_id,
            resource_ids=(envelope.repository_resource_id,), requested_by=actor.identity_id,
            parameters={"assignment_id": assignment.id, "fence": assignment.fence,
                "worker_id": assignment.assigned_worker_id, "execution_id": assignment.execution_id,
                "execution_workspace_id": assignment.execution_workspace_id,
                "definition_record_id": assignment.execution_profile_definition.record_id,
                "target_ref": target, "input": payload.model_dump(exclude={"idempotency_key"})})
        self.guard(assignment, op)
        from codex_web.services.action_intents import ActionIntentConflictError
        try:
            intent = intents.create(ActionIntentCreate(binding_id=bindings[0].id, request=request,
            execution_id=assignment.execution_id, timeout_seconds=120,
            retry_policy=ActionIntentRetryPolicy(max_attempts=1),
            policy_decision=ActionDecisionSnapshot(decision_id=f"coordination-policy:{assignment.id}:{assignment.fence}",
                outcome="allow", source="policy:assignment-control-plane",
                reason="Exact current opted-in Project Definition and live assignment scope checked",
                capabilities=(op.capability,))), actor=actor, assignment_id=assignment.id,
                admission_idempotency_key=f"coordination:{assignment.id}:{op.id}:{payload.idempotency_key}")
        except ActionIntentConflictError as exc:
            raise HTTPException(status_code=409, detail={"code": "coordination_intent_conflict",
                "message": "Inspect the original receipt; do not replay accepted or uncertain input"}) from exc
        await self.broker._claim_and_execute_repository_intent(intent.id, assignment=assignment,
            worker_actor=worker_actor, requester_actor=actor)
        return self.action_view(intent.id, assignment, actor)

    def elevations(self, assignment, actor, envelope, query, identity):
        """Only existing scoped evidence. No approval decisions or grant mutations."""
        rows = []
        for item in self.approvals.list(actor):
            if item.project_id == assignment.project_id and set(item.target.resource_ids) == {envelope.repository_resource_id}:
                rows.append({"kind": "approval", "id": item.id, "status": str(item.status),
                    "actor": item.requested_by, "scope": assignment.project_id,
                    "target": item.target.model_dump(mode="json"),
                    "next_action": "Inspect the exact canonical ApprovalRequest; only an authorized approver may decide"})
        for item in self.attention.list(actor):
            if item.project_id == assignment.project_id and item.source.object_type == "execution_assignment":
                target = self.workers.store.assignment(item.source.object_id)
                if target and target.project_id == assignment.project_id and set(target.resource_ids) == {envelope.repository_resource_id}:
                    rows.append({"kind": "attention", "id": item.id, "status": str(item.status),
                        "assignment_id": target.id, "reason": item.reason,
                        "evidence_ids": list(item.evidence_ids), "next_action": "Inspect existing scoped Attention and evidence; this request grants no authority"})
        for event in self.broker.audit.load().events:
            if (event.organization_id, event.workspace_id, event.project_id) != (assignment.organization_id, assignment.workspace_id, assignment.project_id):
                continue
            target = self.workers.store.assignment(event.assignment_id)
            if target and set(target.resource_ids) == {envelope.repository_resource_id} and event.decision == "deny":
                rows.append({"kind": "broker_denial", "id": event.id, "actor": event.actor_identity_id,
                    "assignment_id": event.assignment_id, "operation": event.operation_id,
                    "correlation_id": event.correlation_id, "reason": event.denial_reason,
                    "authority_definition": event.authority_definition.model_dump(mode="json") if event.authority_definition else None,
                    "next_action": "Request scoped operator review of this denial; do not retry unchanged or self-grant"})
        return self.page(sorted(rows, key=lambda v: (v["kind"], v["id"])), query, identity)


class ProjectCoordinationActionProvider:
    """Canonical local actions: persistent intent precedes any possibly uncertain effect."""
    contract_version = "1.2"
    provider_type, provider_instance = "project-coordination", "canonical"

    def __init__(self, service):
        self.service = service

    def actions(self):
        return tuple(ActionDefinition(action_id=op.id, title=op.id,
            capabilities=ActionCapability(read=False, idempotency=False, verification=True, evidence=True),
            required_authority=("project.coordinate", op.capability), required_resource_types=("repository",),
            timeout_seconds=120, retry_max_attempts=1) for op in PROJECT_COORDINATION_OPERATIONS if op.id in MUTATIONS)

    def context(self, request, binding):
        params = request.parameters
        if set(params) != {"assignment_id", "fence", "worker_id", "execution_id", "execution_workspace_id", "definition_record_id", "target_ref", "input"}:
            denied("invalid canonical coordination action parameters")
        snapshot = self.service.workers.store.assignment(params["assignment_id"])
        if (snapshot is None or snapshot.fence != params["fence"]
                or snapshot.assigned_worker_id != params["worker_id"]
                or snapshot.execution_id != params["execution_id"]
                or snapshot.execution_workspace_id != params["execution_workspace_id"]
                or snapshot.execution_profile_definition is None
                or snapshot.execution_profile_definition.record_id != params["definition_record_id"]
                or snapshot.created_by != request.requested_by
                or (snapshot.organization_id, snapshot.workspace_id, snapshot.project_id) !=
                   (request.organization_id, request.workspace_id, request.project_id)
                or request.resource_ids != (self.service.broker._writable_repository_id(snapshot),)
                or (binding.organization_id, binding.workspace_id, binding.project_id, binding.resource_ids) !=
                   (request.organization_id, request.workspace_id, request.project_id, request.resource_ids)
                or binding.credential_ref is not None):
            denied("coordination action origin or scope changed")
        op = OPERATION_MAP[request.action_id]
        current, actor, envelope = self.service.guard(snapshot, op)
        return current, actor, envelope, op

    async def prepare(self, request, *, binding):
        self.context(request, binding)
        return {"ready": True, "automatic_retry": False}

    async def execute(self, request, *, binding, credential=None):
        current, actor, envelope, op = self.context(request, binding)
        started = time.time()
        guard = lambda: self.service.guard(current, op)
        data = request.parameters["input"]
        if op.id == "project.thread.create":
            async with self.service._creation_lock:
                guard()
                allowed = {(v.profile_id, v.revision) for v in envelope.child_profiles}
                if set(data) != {"profile_id", "profile_revision"} or (data.get("profile_id"), data.get("profile_revision")) not in allowed:
                    denied("child Agent Profile is outside the explicit revision envelope")
                profiles = self.service.broker.execution_profiles
                if profiles.agent_profiles is None:
                    denied("child Agent Profile resolution unavailable")
                child, _ = profiles.agent_profiles.resolve_for_execution(data["profile_id"],
                    revision=data["profile_revision"], actor=actor, project_id=current.project_id)
                child_execution, _ = profiles.resolve(child.execution_profile_id,
                    organization_id=current.organization_id, workspace_id=current.workspace_id, project_id=current.project_id)
                if child_execution.project_coordination or child_execution.administrative_repository_ids:
                    denied("children cannot inherit governor or administrative control authority")
                rows = self.service.assignments(current, envelope)
                active = [v for v in rows if v.id != current.id and v.status in {"pending", "claimed", "running"}]
                if len(active) >= envelope.max_delivery_threads:
                    denied("delivery-thread bound reached; inspect existing work, do not create more")
                if sum(v.agent_profile is not None and v.agent_profile.profile_id == child.profile_id for v in active) >= child.budgets.max_concurrency:
                    denied("child Agent Profile concurrency budget reached")
                result = await self.service.threads.create(project_id=current.project_id, actor=actor,
                    repository_resource_id=envelope.repository_resource_id,
                    writable_repository_resource_ids=(envelope.repository_resource_id,),
                    execution_profile_id=child.execution_profile_id,
                    agent_profile_id=child.profile_id, agent_profile_revision=child.revision,
                    admission_guard=guard)
                thread_id = result.get("thread", result).get("id")
                guard()
                self.service.target(thread_id, current, actor, envelope)
                output = {"thread_id": thread_id, "status": "created", "input_delivered": False}
        elif op.id == "project.thread.message":
            if set(data) != {"message"}:
                denied("invalid message input")
            thread_id = request.parameters["target_ref"]
            _, target = self.service.target(thread_id, current, actor, envelope)
            if target.agent_profile is None or (target.agent_profile.profile_id, target.agent_profile.profile_revision) not in {(v.profile_id, v.revision) for v in envelope.child_profiles}:
                denied("message target is outside child profile envelope")
            profile = target.agent_profile
            result = await self.service.turns.start(thread_id, TurnCreate(message=data["message"],
                project_id=current.project_id, repository_resource_id=envelope.repository_resource_id,
                writable_repository_resource_ids=(envelope.repository_resource_id,),
                execution_profile_id=target.execution_profile_id, agent_profile_id=profile.profile_id,
                agent_profile_revision=profile.profile_revision, defer_start=True),
                actor=actor, admission_guard=guard,
                execution_id=f"coordination-input:{request.correlation_id}")
            guard()
            output = {"thread_id": thread_id, "queued_id": result.get("queuedId"),
                "status": "queued", "native_start_verified": False}
            if not output["queued_id"]:
                raise RuntimeError("message admission outcome is unknown")
        else:
            parsed = MemoryCheckpoint.model_validate({**data, "idempotency_key": "canonical-intent"})
            guard()
            batch = self.service.memory.ingest(KnowledgeIngestBatch(items=(KnowledgeCreate(
                logical_key=f"governor:{current.project_id}:{envelope.repository_resource_id}:{parsed.key}",
                object_type="project", project_id=current.project_id, title=f"Governor checkpoint: {parsed.key}",
                summary=parsed.summary, content=parsed.content,
                provenance=KnowledgeProvenance(source_kind="other", source_ref=f"assignment:{current.id}",
                    source_revision=current.execution_profile_definition.record_id, authored_by=actor.identity_id,
                    origin="ai_inference")),)), actor=actor)
            guard()
            output = {"records": [{"outcome": outcome, "id": item.id, "version": item.version,
                "content_digest": item.content_sha256, "governance_record_id": item.governance_record_id}
                for outcome, item in batch]}
        output["origin"] = {"assignment_id": current.id, "fence": current.fence,
            "definition_record_id": current.execution_profile_definition.record_id,
            "requester": actor.identity_id, "project_id": current.project_id,
            "repository_resource_id": envelope.repository_resource_id}
        return ActionResult(provider_binding_id=binding.id, action_id=op.id, status="succeeded",
            started_at=started, completed_at=time.time(), idempotency_key=request.idempotency_key,
            output=output, evidence=(ActionEvidence(evidence_type="canonical-coordination-receipt",
                reference=f"assignment:{current.id}", summary="Canonical service result; queued is not native acceptance"),))

    async def verify(self, result, *, binding, credential=None):
        origin = result.output.get("origin", {})
        current = self.service.workers.store.assignment(origin.get("assignment_id", ""))
        if (current is None or current.fence != origin.get("fence")
                or current.created_by != origin.get("requester")
                or current.project_id != origin.get("project_id")
                or current.execution_profile_definition.record_id != origin.get("definition_record_id")
                or binding.project_id != current.project_id
                or binding.resource_ids != (origin.get("repository_resource_id"),)):
            return ActionVerification(verified=False, findings=("Coordination verification origin changed",))
        _, actor, envelope = self.service.guard(current, OPERATION_MAP[result.action_id])
        if result.action_id == "project.thread.create":
            self.service.target(result.output["thread_id"], current, actor, envelope)
            verified = True
        elif result.action_id == "project.thread.message":
            thread = result.output["thread_id"]
            self.service.target(thread, current, actor, envelope)
            queued = self.service.turns.queue_policy.queue(thread)
            verified = any(v.id == result.output["queued_id"] for v in queued)
            if not verified and self.service.turns.preflight is not None:
                verified = any((v.queued_turn_snapshot or {}).get("id") == result.output["queued_id"]
                    and v.project_id == current.project_id
                    for v in self.service.turns.preflight.store.for_thread(thread, limit=50))
        else:
            verified = all((item := self.service.memory.get(v["id"], actor=actor)).project_id == current.project_id
                and item.version == v["version"] and item.content_sha256 == v["content_digest"]
                for v in result.output["records"])
        # Queue verification proves durable admission, never native acceptance
        # or downstream business completion. Unknown evidence cannot pass.
        return ActionVerification(verified=verified, evidence=result.evidence,
            findings=() if verified else ("Canonical coordination receipt no longer discoverable",))

    async def rollback(self, result, *, binding, credential=None):
        raise UnsupportedActionCapabilityError("rollback")
