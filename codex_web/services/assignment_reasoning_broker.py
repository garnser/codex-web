"""Narrow assignment adapters over the canonical procedure and Executive owners.

No new controller, grants, definition catalog or idempotency store. An Executive
origin is deliberately limited to one activation per assignment/fence.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
from urllib.parse import unquote
from types import SimpleNamespace

from fastapi import HTTPException

from codex_web.authority import AuthorityLevel
from codex_web.control_plane_broker import ControlPlaneBrokerOperation
from codex_web.procedure_execution import ProcedureStart, ProcedureAdvance
from codex_web.executive_roles import ExecutiveActivationCreate
from codex_web.identity import PrincipalKind
from codex_web.services.async_operations import run_settled


_SPECS = (
    ("procedure.read", "GET", "/api/execution-workers/assignments/{ref}/procedure", "execution.read"),
    ("procedure.start", "POST", "/api/execution-workers/assignments/{ref}/procedure/start", "execution.execute"),
    ("procedure.advance", "POST", "/api/execution-workers/assignments/{ref}/procedure/advance", "execution.execute"),
    ("executive.activation.read", "GET", "/api/executive/activations", "executive.consult"),
    ("executive.activation.create", "POST", "/api/executive/activations", "executive.consult"),
    ("executive.activation.consult", "POST", "/api/executive/activations/{ref}/consult", "executive.consult"),
)
OPERATIONS = tuple(ControlPlaneBrokerOperation(id=name, method=method,
    path_template=path, capability=capability,
    authority_level=AuthorityLevel.READ if method == "GET" else AuthorityLevel.EXECUTE)
    for name, method, path, capability in _SPECS)


def deny(message):
    raise HTTPException(403, detail={"code": "assignment_reasoning_denied", "message": message})


def resolve(method, path):
    for operation in OPERATIONS:
        pattern = re.escape(operation.path_template).replace(re.escape("{ref}"), "([^/]+)")
        match = re.fullmatch(pattern, path)
        if match and operation.method == method:
            target = unquote(match.group(1)) if match.groups() else None
            if target is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", target):
                deny("invalid exact reference")
            return operation, target
    if path.startswith(("/api/executive/", "/api/execution-workers/assignments/")):
        deny("unsupported assignment reasoning operation")
    return None


class AssignmentReasoningBroker:
    def __init__(self, broker, *, procedures, executives):
        self.broker, self.procedures, self.executives = broker, procedures, executives

    @staticmethod
    def origin(assignment):
        return f"assignment:{assignment.id}:fence:{assignment.fence}"

    def current(self, assignment, worker_actor, operation, target, validator=None):
        if validator is not None:
            validator()
        workers = self.broker.execution_workers
        current = workers.store.assignment(assignment.id) if workers else None
        if (current is None or current.fence != assignment.fence
                or current.execution_id != assignment.execution_id
                or current.assigned_worker_id != assignment.assigned_worker_id
                or current.project_id != assignment.project_id
                or current.resource_context != assignment.resource_context
                or current.resource_ids != assignment.resource_ids
                or current.execution_profile_definition != assignment.execution_profile_definition
                or current.agent_profile != assignment.agent_profile
                or current.created_by != assignment.created_by
                or (current.organization_id, current.workspace_id) != (assignment.organization_id, assignment.workspace_id)):
            deny("current assignment changed")
        self.procedures._live(current)
        self.broker._actor(current, worker_actor)
        actor = self.broker._requester_actor(current)
        profiles = self.broker.execution_profiles
        if profiles is None:
            deny("current execution Definition is unavailable")
        profile, reference = profiles.resolve(current.execution_profile_id,
            organization_id=current.organization_id, workspace_id=current.workspace_id,
            project_id=current.project_id)
        if (reference != current.execution_profile_definition
                or operation.id not in profile.control_plane_operations):
            deny("operation is not explicitly declared in the current pinned Definition")
        if operation.id.startswith("procedure.") and target != current.id:
            deny("procedure target must be this exact assignment")
        if actor.principal_kind == PrincipalKind.SERVICE and operation.id.startswith("executive."):
            if not set(actor.service_scopes) & {"executive:consult", "executive:admin"}:
                deny("Executive service scope is required")
        self.broker._authorize(current, operation, actor=actor, resource_ids=tuple(current.resource_ids))
        return current, actor

    def scoped(self, item, assignment, actor, operation):
        if ((item.organization_id, item.workspace_id) != (assignment.organization_id, assignment.workspace_id)
                or getattr(item, "project_id", None) != assignment.project_id):
            deny("canonical context exceeds assignment project or resource scope")
        resources = tuple(getattr(item, "resource_ids", ()))
        if assignment.execution_profile_id == "orchestration-admin":
            # An empty scratch assignment is not universal access. Reuse the
            # explicitly published administrative target owner for EACH resource.
            for resource in resources:
                self.broker.execution_profiles.authorize_administrative_target(
                    assignment, operation_id=operation.id, repository_id=resource,
                    actor=actor, authority=self.broker.authority)
                self.broker._authorize(assignment, operation, actor=actor, resource_ids=(resource,))
        elif set(resources) - set(assignment.resource_ids):
            deny("canonical context exceeds assigned resources")

    def context(self, payload, assignment, actor, operation):
        if payload.project_id != assignment.project_id or not assignment.project_id:
            deny("Executive context requires this exact project")
        if payload.business_entity_ids or payload.business_kpi_ids or payload.business_domains:
            deny("business context is not supported by this assignment adapter")
        for ref in payload.work_item_refs:
            item = self.broker._state_for_target(assignment, ref)
            self.scoped(item, assignment, actor, operation)
        for ref in payload.evidence_ids:
            self.scoped(self.executives.artifact_evidence.get_evidence(ref, actor=actor), assignment, actor, operation)
        for ref in payload.decision_ids:
            self.scoped(self.executives.decisions.get(ref, actor=actor), assignment, actor, operation)
        for ref in payload.goal_ids:
            goal = self.executives.goals.get(ref, scope=actor.tenant, project_id=assignment.project_id)
            bindings = goal.work_graph_bindings
            if not bindings or any(binding.project_id != assignment.project_id for binding in bindings):
                deny("Goal has no exclusive assignment-project binding")
            for binding in bindings:
                for work_ref in binding.root_work_item_refs:
                    self.scoped(self.broker._state_for_target(assignment, work_ref), assignment, actor, operation)

    def prepared_context(self, item, assignment, actor, operation):
        # These canonical projections lack a verifiable Resource envelope. Do
        # not silently broaden an assignment into company-wide Executive context.
        context = item.context
        if context.business_entities or context.business_facts or context.business_kpis:
            deny("automatic Executive context has no assignment resource proof")
        memory = self.executives.organizational_memory
        for row in context.memory:
            if memory is None or not callable(getattr(memory, "validate_assignment_snapshot", None)):
                deny("automatic Executive Memory has no canonical Resource proof")
            try:
                record = memory.validate_assignment_snapshot(row, actor=actor)
            except (KeyError, ValueError, RuntimeError):
                deny("automatic Executive Memory Resource proof is unavailable or changed")
            self.scoped(record, assignment, actor, operation)
        for field, requested, id_key in (
                ("work_items", item.work_item_refs, "ref"),
                ("decisions", item.decision_ids, "id"),
                ("evidence", item.evidence_ids, "id")):
            for row in getattr(context, field):
                if row.get(id_key) not in requested:
                    deny("prepared context contains an unrequested canonical object")
                self.scoped(SimpleNamespace(**row), assignment, actor, operation)
        for snapshot in context.goals:
            goal = snapshot.get("goal", {})
            if (goal.get("id") not in item.goal_ids
                    or (goal.get("organization_id"),goal.get("workspace_id")) != (assignment.organization_id,assignment.workspace_id)):
                deny("prepared Goal is not an exact requested tenant object")
            bindings = goal.get("work_graph_bindings", ())
            if not bindings or any(binding.get("project_id") != assignment.project_id for binding in bindings):
                deny("prepared Goal binding exceeds assignment project")
            for binding in bindings:
                for ref in binding.get("root_work_item_refs", ()):
                    self.scoped(self.broker._state_for_target(assignment, ref), assignment, actor, operation)
        for graph in context.work_graphs:
            if graph.get("project_id") != assignment.project_id:
                deny("WorkGraph is outside assignment project")
            for edge in graph.get("edges", ()):
                for ref in (edge["source_ref"],edge["target_ref"]):
                    self.scoped(self.broker._state_for_target(assignment, ref), assignment, actor, operation)
            for node in graph.get("nodes", ()):
                self.scoped(self.broker._state_for_target(assignment, node["ref"]), assignment, actor, operation)

    def memory_resources(self, assignment, actor, operation):
        """Current canonical assignment envelope, never Memory/request labels."""
        resources = tuple(assignment.resource_ids)
        if assignment.execution_profile_id == "orchestration-admin":
            profile, reference = self.broker.execution_profiles.resolve(assignment.execution_profile_id,
                organization_id=assignment.organization_id, workspace_id=assignment.workspace_id,
                project_id=assignment.project_id)
            if reference != assignment.execution_profile_definition:
                deny("current execution Definition changed")
            resources = tuple(profile.administrative_repository_ids)
            for resource in resources:
                self.broker.execution_profiles.authorize_administrative_target(assignment,
                    operation_id=operation.id, repository_id=resource, actor=actor,
                    authority=self.broker.authority)
                self.broker._authorize(assignment, operation, actor=actor, resource_ids=(resource,))
        return resources

    def existing(self, assignment, actor):
        return tuple(item for item in self.executives.list(actor=actor)
            if item.trigger_ref == self.origin(assignment)
            and item.project_id == assignment.project_id
            and item.initiated_by == actor.identity_id)

    def activation(self, target, assignment, actor, operation):
        item = self.executives.get(target, actor=actor)
        self.scoped(item, assignment, actor, operation)
        if item.trigger_ref != self.origin(assignment) or item.initiated_by != actor.identity_id:
            deny("activation origin does not match this assignment requester/fence")
        self.context(self.request_for(item), assignment, actor, operation)
        self.prepared_context(item, assignment, actor, operation)
        return item

    @staticmethod
    def request_for(item):
        return ExecutiveActivationCreate.model_validate({
            **{key: getattr(item, key) for key in ExecutiveActivationCreate.model_fields
                if hasattr(item, key)},
            "requested_role_ids": tuple(selection.role_id for selection in item.selections),
            "max_roles": len(item.selections),
        })

    async def dispatch(self, assignment, worker_actor, operation, target, payload, query, validator=None):
        if query and (operation.id != "procedure.read" or set(query) != {"run_id"}
                or not isinstance(query["run_id"], list) or len(query["run_id"]) != 1
                or not isinstance(query["run_id"][0], str) or not 0 < len(query["run_id"][0]) <= 100):
            deny("only procedure.read accepts an exact run_id query")
        if operation.id not in {"procedure.start", "procedure.advance", "executive.activation.create"} and payload:
            deny("this operation accepts no body")
        current, actor = await run_settled(lambda: self.current(assignment, worker_actor, operation, target, validator))
        if operation.id.startswith("procedure."):
            if operation.id == "procedure.start":
                request = ProcedureStart.model_validate(payload)
                item = await run_settled(lambda: self.procedures.start(current.id,
                    definition=request.definition, input_snapshot_id=request.input_snapshot_id, actor=actor))
            elif operation.id == "procedure.advance":
                request = ProcedureAdvance.model_validate(payload or {})
                item = await self.procedures.advance(current.id, actor=actor, run_id=request.run_id)
            else:
                return await run_settled(lambda: self.procedures.inspect(current.id, actor,
                    run_id=query["run_id"][0] if query else None))
            return self.procedures.view(item)
        if operation.id == "executive.activation.read":
            items = await run_settled(lambda: self.existing(current, actor))
            for item in items:
                await run_settled(lambda: self.activation(item.id, current, actor, operation))
            return {"items": [item.model_dump(mode="json") for item in items], "count": len(items),
                "origin": self.origin(current)}
        if operation.id == "executive.activation.create":
            request = ExecutiveActivationCreate.model_validate(payload)
            if not request.requested_role_ids or request.max_roles != len(request.requested_role_ids):
                deny("explicit role selection and matching max_roles are required")
            if request.trigger_ref != self.origin(current):
                deny("trigger_ref must be the advertised exact assignment origin")
            def prepare():
                self.context(request, current, actor, operation)
                matches = self.existing(current, actor)
                if matches:
                    if len(matches) != 1 or self.request_for(matches[0]) != request:
                        deny("one activation per assignment; origin already has different context")
                    self.prepared_context(matches[0], current, actor, operation)
                    return matches[0], True
                prepared = self.executives.prepare(request, actor=actor,
                    memory_resource_ids=self.memory_resources(current, actor, operation))
                # Reuse canonical ID uniqueness for concurrent/lost-response
                # create protection; no second idempotency record or store.
                origin = f"{current.organization_id}/{current.workspace_id}/{self.origin(current)}"
                prepared.id = "executive-activation-" + hashlib.sha256(origin.encode()).hexdigest()[:32]
                self.context(self.request_for(prepared), current, actor, operation)
                self.prepared_context(prepared, current, actor, operation)
                return prepared, False
            prepared, reused = await asyncio.wait_for(asyncio.to_thread(prepare), timeout=5.0)
            current, actor = await run_settled(lambda: self.current(assignment, worker_actor, operation, target, validator))
            if not reused:
                prepared = await run_settled(lambda: self.executives.persist_prepared(prepared, actor=actor))
            return {"item": prepared.model_dump(mode="json"), "reused": reused}
        item = await run_settled(lambda: self.activation(target, current, actor, operation))
        current, actor = await run_settled(lambda: self.current(assignment, worker_actor, operation, target, validator))
        native = {}
        if getattr(self.executives, 'native_invocation_authorizer', None) is not None:
            def current_origin():
                latest, requester = self.current(assignment, worker_actor, operation, target, validator)
                # The Executive owner separately point-checks the exact
                # CONSULTING snapshot; do not reload its entire history here.
                self.scoped(item, latest, requester, operation)
                if item.trigger_ref != self.origin(latest) or item.initiated_by != requester.identity_id:
                    deny('native consultation origin changed')
                self.context(self.request_for(item), latest, requester, operation)
                self.prepared_context(item, latest, requester, operation)
            native = {'native_origin_guard': current_origin}
        result = await self.executives.consult(item.id, actor=actor, **native)
        return {"item": result.model_dump(mode="json")}
