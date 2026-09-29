from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import tempfile
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlsplit

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from pydantic import ValidationError

from codex_web.action_intents import (
    ActionDecisionOutcome,
    ActionDecisionSnapshot,
    ActionIntentClaimRequest,
    ActionIntentCreate,
)
from codex_web.action_providers import ActionRequest
from codex_web.authority import (
    AuthorityDecisionOutcome,
    AuthorityEvaluationRequest,
    AuthorityLevel,
)
from codex_web.control_plane_broker import (
    ControlPlaneBrokerAuditEvent,
    ControlPlaneBrokerDecision,
    ControlPlaneBrokerLimits,
    ControlPlaneBrokerOperation,
)
from codex_web.code_hosts import CodeHostCapability, CodeHostProviderBinding
from codex_web.execution_workers import ExecutionAssignment
from codex_web.identity import AuthenticationActor, TenantScope
from codex_web.models import (
    WorkItemAckCreate,
    WorkItemHandoffCreate,
    WorkItemProgressUpdate,
)
from codex_web.services.authority_roles import AuthorityRoleService
from codex_web.services.action_intents import ActionIntentService
from codex_web.services.code_host_action_contract import (
    CODE_HOST_BRANCH_PUBLISH_ACTION_ID,
    CODE_HOST_ISSUE_COMMENT_ACTION_ID,
    CODE_HOST_ISSUE_CREATE_ACTION_ID,
    CODE_HOST_ISSUE_UPDATE_ACTION_ID,
    CODE_HOST_PULL_REQUEST_MERGE_ACTION_ID,
    CODE_HOST_PULL_REQUEST_UPSERT_ACTION_ID,
)
from codex_web.services.code_hosts import CodeHostService
from codex_web.services.identity import (
    AuthenticationError,
    AuthorizationError,
    IdentityService,
    TenantIsolationError,
)
from codex_web.services.work_item_operator import WorkItemOperatorService
from codex_web.services.work_items import WorkItemService
from codex_web.storage.control_plane_broker import ControlPlaneBrokerAuditStore


class ControlPlaneBrokerError(RuntimeError):
    pass


class ControlPlaneBrokerDeniedError(ControlPlaneBrokerError):
    pass


class ControlPlaneBrokerRequestError(ControlPlaneBrokerDeniedError):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


class ControlPlaneBrokerAuthorityDeniedError(ControlPlaneBrokerDeniedError):
    def __init__(self, decision) -> None:
        super().__init__(
            "; ".join(decision.reasons)
            or "canonical authority denied operation"
        )
        self.decision = decision


@dataclass(frozen=True, slots=True)
class _ResolvedOperation:
    operation: ControlPlaneBrokerOperation
    target_ref: str | None
    query: dict[str, list[str]]


OPERATIONS: tuple[ControlPlaneBrokerOperation, ...] = (
    ControlPlaneBrokerOperation(
        id="control_plane.operations.list",
        method="GET",
        path_template="/api/control-plane-broker/operations",
        capability="work_item.read",
        authority_level=AuthorityLevel.READ,
    ),
    ControlPlaneBrokerOperation(
        id="work_item.list",
        method="GET",
        path_template="/api/work-items",
        capability="work_item.read",
        authority_level=AuthorityLevel.READ,
    ),
    ControlPlaneBrokerOperation(
        id="work_item.read",
        method="GET",
        path_template="/api/work-items/{ref}",
        capability="work_item.read",
        authority_level=AuthorityLevel.READ,
    ),
    ControlPlaneBrokerOperation(
        id="work_item.handoff",
        method="POST",
        path_template="/api/work-items/{ref}/handoff",
        capability="work_item.handoff",
        authority_level=AuthorityLevel.EXECUTE,
    ),
    ControlPlaneBrokerOperation(
        id="work_item.acknowledge",
        method="POST",
        path_template="/api/work-items/{ref}/ack",
        capability="work_item.acknowledge",
        authority_level=AuthorityLevel.EXECUTE,
    ),
    ControlPlaneBrokerOperation(
        id="work_item.progress",
        method="POST",
        path_template="/api/work-items/{ref}/progress",
        capability="work_item.progress",
        authority_level=AuthorityLevel.EXECUTE,
    ),
    ControlPlaneBrokerOperation(
        id="work_item.retry",
        method="POST",
        path_template="/api/work-items/{ref}/retry",
        capability="work_item.retry",
        authority_level=AuthorityLevel.EXECUTE,
    ),
    ControlPlaneBrokerOperation(
        id="work_item.reconcile",
        method="POST",
        path_template="/api/work-items/{ref}/reconcile",
        capability="work_item.reconcile",
        authority_level=AuthorityLevel.EXECUTE,
    ),
    ControlPlaneBrokerOperation(
        id="repository.read.metadata",
        method="GET",
        path_template="/api/repository-facts",
        capability="repository.read",
        authority_level=AuthorityLevel.READ,
    ),
    ControlPlaneBrokerOperation(
        id="repository.read.refs",
        method="GET",
        path_template="/api/repository-facts/refs",
        capability="repository.refs.read",
        authority_level=AuthorityLevel.READ,
    ),
    ControlPlaneBrokerOperation(
        id="repository.read.commit",
        method="GET",
        path_template="/api/repository-facts/commits/{revision}",
        capability="repository.commit.read",
        authority_level=AuthorityLevel.READ,
    ),
    ControlPlaneBrokerOperation(
        id="repository.read.pull-request",
        method="GET",
        path_template="/api/repository-facts/pull-requests/{external_id}",
        capability="repository.pull-request.read",
        authority_level=AuthorityLevel.READ,
    ),
    ControlPlaneBrokerOperation(
        id="repository.read.reviews",
        method="GET",
        path_template=(
            "/api/repository-facts/pull-requests/{external_id}/reviews"
        ),
        capability="repository.review.read",
        authority_level=AuthorityLevel.READ,
    ),
    ControlPlaneBrokerOperation(
        id="repository.read.checks",
        method="GET",
        path_template="/api/repository-facts/checks/{revision}",
        capability="repository.checks.read",
        authority_level=AuthorityLevel.READ,
    ),
    ControlPlaneBrokerOperation(
        id="repository.read.releases",
        method="GET",
        path_template="/api/repository-facts/releases",
        capability="repository.releases.read",
        authority_level=AuthorityLevel.READ,
    ),
    ControlPlaneBrokerOperation(
        id="repository.read.compare",
        method="GET",
        path_template="/api/repository-facts/compare?base={base}&head={head}",
        capability="repository.compare.read",
        authority_level=AuthorityLevel.READ,
    ),
    ControlPlaneBrokerOperation(
        id="repository.branch.publish",
        method="POST",
        path_template="/api/repository-actions/branch/publish",
        capability="repository.branch.publish",
        authority_level=AuthorityLevel.EXECUTE,
    ),
    ControlPlaneBrokerOperation(
        id="repository.pull-request.upsert",
        method="POST",
        path_template="/api/repository-actions/pull-request/upsert",
        capability="repository.pull-request.create",
        authority_level=AuthorityLevel.EXECUTE,
    ),
    ControlPlaneBrokerOperation(
        id="repository.issue.create",
        method="POST",
        path_template="/api/repository-actions/issue/create",
        capability="repository.issue.create",
        authority_level=AuthorityLevel.EXECUTE,
    ),
    ControlPlaneBrokerOperation(
        id="repository.issue.comment",
        method="POST",
        path_template="/api/repository-actions/issue/comment",
        capability="repository.issue.comment",
        authority_level=AuthorityLevel.EXECUTE,
    ),
    ControlPlaneBrokerOperation(
        id="repository.pull-request.merge",
        method="POST",
        path_template="/api/repository-actions/pull-request/merge",
        capability="repository.pull-request.merge",
        authority_level=AuthorityLevel.EXECUTE,
    ),
    ControlPlaneBrokerOperation(
        id="repository.issue.update",
        method="POST",
        path_template="/api/repository-actions/issue/update",
        capability="repository.issue.update",
        authority_level=AuthorityLevel.EXECUTE,
    ),
)

_OPERATION_BY_ID = {item.id: item for item in OPERATIONS}
_MUTATION_SUFFIXES = {
    "handoff": "work_item.handoff",
    "ack": "work_item.acknowledge",
    "progress": "work_item.progress",
    "retry": "work_item.retry",
    "reconcile": "work_item.reconcile",
}
_SAFE_TRACE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_REPOSITORY_ACTIONS = {
    "/api/repository-actions/issue/create": (
        "repository.issue.create",
        CODE_HOST_ISSUE_CREATE_ACTION_ID,
    ),
    "/api/repository-actions/branch/publish": (
        "repository.branch.publish",
        CODE_HOST_BRANCH_PUBLISH_ACTION_ID,
    ),
    "/api/repository-actions/pull-request/upsert": (
        "repository.pull-request.upsert",
        CODE_HOST_PULL_REQUEST_UPSERT_ACTION_ID,
    ),
    "/api/repository-actions/issue/comment": (
        "repository.issue.comment",
        CODE_HOST_ISSUE_COMMENT_ACTION_ID,
    ),
    "/api/repository-actions/issue/update": (
        "repository.issue.update",
        CODE_HOST_ISSUE_UPDATE_ACTION_ID,
    ),
    "/api/repository-actions/pull-request/merge": (
        "repository.pull-request.merge",
        CODE_HOST_PULL_REQUEST_MERGE_ACTION_ID,
    ),
}

# These profiles execute different workloads but share the same narrow broker
# boundary. The broker never grants generic network or localhost access: every
# request is still operation-allowlisted, tenant/project scoped, authorized,
# fenced, rate limited, and audited below.
CONTROL_PLANE_EXECUTION_PROFILES = frozenset(
    {"orchestration-only", "repository-write"}
)


class ControlPlaneBrokerService:
    """Canonical authorization/dispatch service behind assignment-bound brokers."""

    def __init__(
        self,
        *,
        identity: IdentityService,
        authority: AuthorityRoleService,
        work_items: WorkItemService,
        audit: ControlPlaneBrokerAuditStore,
        action_intents: ActionIntentService | None = None,
        code_hosts: CodeHostService | None = None,
        operator: WorkItemOperatorService | None = None,
        limits: ControlPlaneBrokerLimits | None = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.identity = identity
        self.authority = authority
        self.work_items = work_items
        self.operator = operator or WorkItemOperatorService(work_items)
        self.audit = audit
        self.action_intents = action_intents
        self.code_hosts = code_hosts
        self.limits = limits or ControlPlaneBrokerLimits()
        self._clock = clock
        self._monotonic = monotonic

    @staticmethod
    def operations() -> tuple[ControlPlaneBrokerOperation, ...]:
        return OPERATIONS

    def public_assignment_capability(
        self,
        assignment: ExecutionAssignment,
    ) -> dict[str, Any]:
        enabled = assignment.execution_profile_id in CONTROL_PLANE_EXECUTION_PROFILES
        return {
            "enabled": enabled,
            "transport": "assignment-bound-unix-socket" if enabled else None,
            "project_id": assignment.project_id if enabled else None,
            "operations": (
                [
                    {
                        "id": item.id,
                        "method": item.method,
                        "path_template": item.path_template,
                        "capability": item.capability,
                        "authority_level": item.authority_level.value,
                    }
                    for item in OPERATIONS
                ]
                if enabled
                else []
            ),
            "audit_href": (
                f"/api/control-plane-broker/audit?assignment_id={assignment.id}"
                if enabled
                else None
            ),
            "credential_exposed": False,
        }

    @staticmethod
    def _trace_id(value: str | None) -> str | None:
        normalized = str(value or "").strip()
        return normalized if _SAFE_TRACE_ID.fullmatch(normalized) else None

    def _audit(
        self,
        *,
        snapshot: ExecutionAssignment,
        worker_id: str,
        fence: int,
        method: str,
        path: str,
        correlation_id: str,
        causation_id: str | None,
        decision: ControlPlaneBrokerDecision,
        operation: ControlPlaneBrokerOperation | None = None,
        target_ref: str | None = None,
        actor_identity_id: str | None = None,
        authority_decision=None,
        response_status: int | None = None,
        denial_reason: str | None = None,
    ) -> None:
        self.audit.append(
            ControlPlaneBrokerAuditEvent(
                organization_id=snapshot.organization_id,
                workspace_id=snapshot.workspace_id,
                project_id=snapshot.project_id,
                execution_id=snapshot.execution_id,
                assignment_id=snapshot.id,
                worker_id=worker_id,
                fence=fence,
                actor_identity_id=actor_identity_id,
                execution_profile_id=snapshot.execution_profile_id,
                operation_id=operation.id if operation else None,
                capability=operation.capability if operation else None,
                method=method[:16] or "UNKNOWN",
                path=path[:1000] or "/",
                target_ref=target_ref[:500] if target_ref else None,
                decision=decision,
                authority_decision_id=(
                    authority_decision.id if authority_decision is not None else None
                ),
                authority_definition=(
                    authority_decision.definition_ref
                    if authority_decision is not None
                    else None
                ),
                response_status=response_status,
                denial_reason=(denial_reason[:500] if denial_reason else None),
                correlation_id=correlation_id,
                causation_id=causation_id,
            )
        )

    @staticmethod
    def _resolve_operation(method: str, raw_target: str) -> _ResolvedOperation:
        parsed = urlsplit(raw_target)
        if parsed.scheme or parsed.netloc:
            raise ControlPlaneBrokerDeniedError(
                "absolute-form/control-plane proxy targets are not allowed"
            )
        path = parsed.path
        query = parse_qs(parsed.query, keep_blank_values=False)
        repository_action = _REPOSITORY_ACTIONS.get(path)
        if repository_action is not None:
            operation = _OPERATION_BY_ID[repository_action[0]]
            if method != operation.method:
                raise ControlPlaneBrokerDeniedError(
                    f"method {method} is not allowed for {operation.id}"
                )
            return _ResolvedOperation(
                operation=operation,
                target_ref=None,
                query=query,
            )
        repository_read_exact = {
            "/api/repository-facts": "repository.read.metadata",
            "/api/repository-facts/refs": "repository.read.refs",
            "/api/repository-facts/releases": "repository.read.releases",
            "/api/repository-facts/compare": "repository.read.compare",
        }
        operation_id = repository_read_exact.get(path)
        target_ref = None
        if operation_id is None:
            repository_read_prefixes = (
                (
                    "/api/repository-facts/pull-requests/",
                    "repository.read.pull-request",
                ),
                (
                    "/api/repository-facts/commits/",
                    "repository.read.commit",
                ),
                (
                    "/api/repository-facts/checks/",
                    "repository.read.checks",
                ),
            )
            for prefix, candidate in repository_read_prefixes:
                if not path.startswith(prefix):
                    continue
                encoded_target = path[len(prefix):]
                if candidate == "repository.read.pull-request" and encoded_target.endswith(
                    "/reviews"
                ):
                    candidate = "repository.read.reviews"
                    encoded_target = encoded_target.removesuffix("/reviews")
                target_ref = unquote(encoded_target).strip()
                if not target_ref or len(target_ref) > 500:
                    raise ControlPlaneBrokerDeniedError(
                        "invalid repository fact target"
                    )
                operation_id = candidate
                break
        if operation_id is not None:
            operation = _OPERATION_BY_ID[operation_id]
            if method != operation.method:
                raise ControlPlaneBrokerDeniedError(
                    f"method {method} is not allowed for {operation.id}"
                )
            return _ResolvedOperation(
                operation=operation,
                target_ref=target_ref,
                query=query,
            )
        if path == "/api/control-plane-broker/operations":
            operation = _OPERATION_BY_ID["control_plane.operations.list"]
            if method != operation.method:
                raise ControlPlaneBrokerDeniedError(
                    "method is not allowed for control-plane operation catalog"
                )
            return _ResolvedOperation(
                operation=operation,
                target_ref=None,
                query=query,
            )
        if path == "/api/work-items":
            operation = _OPERATION_BY_ID["work_item.list"]
            if method != operation.method:
                raise ControlPlaneBrokerDeniedError("method is not allowed for work-item list")
            return _ResolvedOperation(operation=operation, target_ref=None, query=query)
        prefix = "/api/work-items/"
        if not path.startswith(prefix):
            raise ControlPlaneBrokerDeniedError("control-plane path is not allowlisted")
        remainder = path[len(prefix):]
        if not remainder:
            raise ControlPlaneBrokerDeniedError("work-item reference is required")

        operation_id = "work_item.read"
        encoded_ref = remainder
        if "/" in remainder:
            maybe_ref, suffix = remainder.rsplit("/", 1)
            if suffix in _MUTATION_SUFFIXES:
                operation_id = _MUTATION_SUFFIXES[suffix]
                encoded_ref = maybe_ref
        operation = _OPERATION_BY_ID[operation_id]
        if method != operation.method:
            raise ControlPlaneBrokerDeniedError(
                f"method {method} is not allowed for {operation.id}"
            )
        target_ref = unquote(encoded_ref).strip()
        if not target_ref or len(target_ref) > 500:
            raise ControlPlaneBrokerDeniedError("invalid work-item reference")
        return _ResolvedOperation(
            operation=operation,
            target_ref=target_ref,
            query=query,
        )

    def _state_for_target(
        self,
        assignment: ExecutionAssignment,
        target_ref: str,
    ):
        try:
            state = self.work_items.state_machine._work_item_state(target_ref)
        except Exception as exc:
            raise ControlPlaneBrokerDeniedError("work item is unavailable") from exc
        if (
            state.organization_id != assignment.organization_id
            or state.workspace_id != assignment.workspace_id
        ):
            raise ControlPlaneBrokerDeniedError("work item is outside broker tenant scope")
        if not assignment.project_id or state.project_id != assignment.project_id:
            raise ControlPlaneBrokerDeniedError("work item is outside assignment project scope")
        return state

    def _actor(
        self,
        assignment: ExecutionAssignment,
        worker_actor: AuthenticationActor,
    ) -> AuthenticationActor:
        try:
            canonical = self.identity.actor_for_identity(
                worker_actor.identity_id,
                scope=TenantScope(
                    organization_id=assignment.organization_id,
                    workspace_id=assignment.workspace_id,
                ),
            )
        except (AuthenticationError, AuthorizationError, TenantIsolationError) as exc:
            raise ControlPlaneBrokerDeniedError(
                "worker service identity is unavailable or unauthorized"
            ) from exc
        if (
            canonical.principal_kind != worker_actor.principal_kind
            or canonical.tenant != worker_actor.tenant
        ):
            raise ControlPlaneBrokerDeniedError(
                "worker service identity binding changed"
            )
        return canonical.model_copy(
            update={
                "assurance": worker_actor.assurance,
                "service_token_id": worker_actor.service_token_id,
                "service_scopes": worker_actor.service_scopes,
                "authenticated_at": worker_actor.authenticated_at,
            }
        )

    def _requester_actor(self, assignment: ExecutionAssignment):
        try:
            return self.identity.actor_for_identity(
                assignment.created_by,
                scope=TenantScope(
                    organization_id=assignment.organization_id,
                    workspace_id=assignment.workspace_id,
                ),
            )
        except (AuthenticationError, AuthorizationError, TenantIsolationError) as exc:
            raise ControlPlaneBrokerDeniedError(
                "assignment requester identity is unavailable or unauthorized"
            ) from exc

    @staticmethod
    def _writable_repository_id(assignment: ExecutionAssignment) -> str:
        if assignment.execution_profile_id != "repository-write":
            raise ControlPlaneBrokerDeniedError(
                "repository actions require the repository-write execution profile"
            )
        scope = assignment.repository_scope
        writable = tuple(scope.writable_repository_ids) if scope is not None else ()
        if not writable and assignment.repository_target is not None:
            repository_id = assignment.repository_target.mutable_repository_id
            writable = (repository_id,) if repository_id else ()
        if len(writable) != 1:
            raise ControlPlaneBrokerDeniedError(
                "repository actions require exactly one writable repository"
            )
        return writable[0]

    async def _execute_repository_action(
        self,
        *,
        assignment: ExecutionAssignment,
        worker_actor,
        requester_actor,
        operation: ControlPlaneBrokerOperation,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if self.action_intents is None:
            raise ControlPlaneBrokerDeniedError(
                "governed repository actions are unavailable"
            )
        repository_id = self._writable_repository_id(assignment)
        action_id = next(
            action_id
            for operation_id, action_id in _REPOSITORY_ACTIONS.values()
            if operation_id == operation.id
        )
        allowed_keys = {"parameters", "idempotency_key"}
        unexpected = sorted(set(payload) - allowed_keys)
        if unexpected:
            raise ControlPlaneBrokerRequestError(
                "repository action contains unsupported fields: " + ", ".join(unexpected)
            )
        parameters = payload.get("parameters", {})
        if not isinstance(parameters, dict):
            raise ControlPlaneBrokerRequestError("parameters must be a JSON object")
        if action_id == CODE_HOST_BRANCH_PUBLISH_ACTION_ID:
            parameters = {
                **parameters,
                "execution_workspace_id": assignment.execution_workspace_id,
            }
        bindings = [
            item
            for item in self.action_intents.execution.registry.list_bindings(requester_actor)
            if item.enabled
            and item.provider_type == "github"
            and repository_id in item.resource_ids
            and item.project_id in {None, assignment.project_id}
        ]
        if len(bindings) != 1:
            raise ControlPlaneBrokerDeniedError(
                "repository action requires exactly one enabled GitHub binding"
            )
        request = ActionRequest(
            action_id=action_id,
            organization_id=assignment.organization_id,
            workspace_id=assignment.workspace_id,
            project_id=assignment.project_id,
            resource_ids=(repository_id,),
            parameters=parameters,
            idempotency_key=(
                str(payload["idempotency_key"]).strip()
                if payload.get("idempotency_key")
                else None
            ),
            requested_by=requester_actor.identity_id,
        )
        intent = self.action_intents.create(
            ActionIntentCreate(
                binding_id=bindings[0].id,
                request=request,
                work_item_ref=assignment.work_item_ref,
                execution_id=assignment.execution_id,
                policy_decision=ActionDecisionSnapshot(
                    decision_id=f"assignment-policy-{uuid.uuid4().hex}",
                    outcome=ActionDecisionOutcome.ALLOW,
                    source="policy:assignment-control-plane",
                    reason=(
                        "canonical assignment execution profile and repository "
                        "scope permit this allowlisted broker operation"
                    ),
                    capabilities=(operation.capability,),
                    reasons=(
                        f"execution profile {assignment.execution_profile_id} "
                        f"permits {operation.id}",
                        f"repository {repository_id} is the singular writable target",
                    ),
                    evaluated_at=time.time(),
                ),
            ),
            actor=requester_actor,
        )
        worker_id = f"control-plane-broker:{assignment.assigned_worker_id or 'worker'}"
        claimed = self.action_intents.claim(
            ActionIntentClaimRequest(worker_id=worker_id),
            actor=worker_actor,
            intent_id=intent.id,
        )
        if claimed is None:
            current = self.action_intents.get(intent.id, requester_actor)
            return {"item": current.model_dump(mode="json")}
        completed = await self.action_intents.execute_claimed(
            intent.id,
            worker_id,
            actor=worker_actor,
        )
        return {"item": completed.model_dump(mode="json")}

    def _code_host_binding(
        self,
        *,
        assignment: ExecutionAssignment,
        actor: AuthenticationActor,
    ) -> tuple[str, str]:
        if self.code_hosts is None or self.action_intents is None:
            raise ControlPlaneBrokerDeniedError(
                "governed repository reads are unavailable"
            )
        repository_id = self._writable_repository_id(assignment)
        action_registry = self.action_intents.execution.registry
        bindings = [
            item
            for item in action_registry.list_bindings(actor)
            if item.enabled
            and item.provider_type == "github"
            and repository_id in item.resource_ids
            and item.project_id in {None, assignment.project_id}
        ]
        if len(bindings) != 1:
            raise ControlPlaneBrokerDeniedError(
                "repository read requires exactly one enabled GitHub binding"
            )
        action_binding = bindings[0]
        action_provider = action_registry.provider(
            action_binding.provider_type,
            action_binding.provider_instance,
            actor=actor,
        )
        api_base = str(getattr(action_provider, "api_base", "")).strip()
        if not api_base:
            raise ControlPlaneBrokerDeniedError(
                "GitHub provider does not expose its configured API base"
            )
        binding_id = f"control-plane-read:{action_binding.id}"
        self.code_hosts.registry.register_binding(
            CodeHostProviderBinding(
                id=binding_id,
                organization_id=assignment.organization_id,
                workspace_id=assignment.workspace_id,
                provider_type="github",
                provider_instance=action_binding.provider_instance,
                base_url=api_base,
                credential_ref=action_binding.credential_ref,
                capabilities=tuple(CodeHostCapability),
            )
        )
        return binding_id, repository_id

    async def _read_repository_fact(
        self,
        *,
        assignment: ExecutionAssignment,
        actor: AuthenticationActor,
        operation: ControlPlaneBrokerOperation,
        target_ref: str | None,
        query: dict[str, list[str]],
    ) -> dict[str, Any]:
        assert self.code_hosts is not None
        binding_id, repository_id = self._code_host_binding(
            assignment=assignment,
            actor=actor,
        )
        if operation.id == "repository.read.metadata":
            item = await self.code_hosts.repository(
                binding_id, repository_id, actor=actor
            )
            return {"item": item.model_dump(mode="json")}
        if operation.id == "repository.read.refs":
            items = await self.code_hosts.refs(binding_id, repository_id, actor=actor)
            return {"items": [item.model_dump(mode="json") for item in items]}
        if operation.id == "repository.read.commit":
            assert target_ref is not None
            item = await self.code_hosts.commit(
                binding_id, repository_id, target_ref, actor=actor
            )
            return {"item": item.model_dump(mode="json")}
        if operation.id == "repository.read.pull-request":
            assert target_ref is not None
            item = await self.code_hosts.pull_request(
                binding_id, repository_id, target_ref, actor=actor
            )
            return {"item": item.model_dump(mode="json")}
        if operation.id == "repository.read.reviews":
            assert target_ref is not None
            items = await self.code_hosts.reviews(
                binding_id, repository_id, target_ref, actor=actor
            )
            return {"items": [item.model_dump(mode="json") for item in items]}
        if operation.id == "repository.read.checks":
            assert target_ref is not None
            items = await self.code_hosts.checks(
                binding_id, repository_id, target_ref, actor=actor
            )
            return {"items": [item.model_dump(mode="json") for item in items]}
        if operation.id == "repository.read.releases":
            items = await self.code_hosts.releases(
                binding_id, repository_id, actor=actor
            )
            return {"items": [item.model_dump(mode="json") for item in items]}
        if operation.id == "repository.read.compare":
            base = (query.get("base") or [""])[0].strip()
            head = (query.get("head") or [""])[0].strip()
            if not base or not head or len(base) > 500 or len(head) > 500:
                raise ControlPlaneBrokerRequestError(
                    "repository compare requires bounded base and head query values"
                )
            item = await self.code_hosts.compare(
                binding_id,
                repository_id,
                base,
                head,
                actor=actor,
            )
            return {"item": item.model_dump(mode="json")}
        raise ControlPlaneBrokerDeniedError("unsupported repository read operation")

    def _authorize(
        self,
        assignment: ExecutionAssignment,
        operation: ControlPlaneBrokerOperation,
        *,
        actor,
        resource_ids: tuple[str, ...],
    ):
        if assignment.execution_profile_id not in CONTROL_PLANE_EXECUTION_PROFILES:
            raise ControlPlaneBrokerDeniedError(
                "assignment execution profile does not permit control-plane broker access"
            )
        if not assignment.project_id:
            raise ControlPlaneBrokerDeniedError(
                "control-plane broker requires an assignment project"
            )
        decision = self.authority.evaluate(
            AuthorityEvaluationRequest(
                capability=operation.capability,
                level=operation.authority_level,
                project_id=assignment.project_id,
                resource_ids=resource_ids,
            ),
            actor=actor,
        )
        if decision.outcome != AuthorityDecisionOutcome.ALLOW:
            raise ControlPlaneBrokerAuthorityDeniedError(decision)
        return decision

    async def dispatch(
        self,
        *,
        assignment: ExecutionAssignment,
        worker_actor: AuthenticationActor,
        method: str,
        raw_target: str,
        body: bytes,
    ) -> tuple[int, dict[str, Any], ControlPlaneBrokerOperation, str | None, Any]:
        resolved = self._resolve_operation(method, raw_target)
        operation = resolved.operation
        actor = self._actor(assignment, worker_actor)
        repository_read = operation.id.startswith("repository.read.")
        requester_actor = (
            self._requester_actor(assignment)
            if operation.id.startswith("repository.")
            or operation.id == "control_plane.operations.list"
            else actor
        )
        authority_actor = actor if repository_read else requester_actor
        state = None
        resource_ids: tuple[str, ...] = ()
        if operation.id.startswith("repository."):
            resource_ids = (self._writable_repository_id(assignment),)
        elif resolved.target_ref is not None:
            state = self._state_for_target(assignment, resolved.target_ref)
            resource_ids = tuple(state.resource_ids)
        elif assignment.project_id is None:
            raise ControlPlaneBrokerDeniedError(
                "work-item list requires assignment project scope"
            )

        authority_decision = self._authorize(
            assignment,
            operation,
            actor=authority_actor,
            resource_ids=resource_ids,
        )

        payload: dict[str, Any] = {}
        if body:
            try:
                decoded = json.loads(body)
            except json.JSONDecodeError as exc:
                raise ControlPlaneBrokerRequestError(
                    "control-plane request body is invalid JSON",
                    status_code=400,
                ) from exc
            if not isinstance(decoded, dict):
                raise ControlPlaneBrokerDeniedError(
                    "control-plane request body must be a JSON object"
                )
            payload = decoded

        if operation.id == "control_plane.operations.list":
            result = self.public_assignment_capability(assignment)
        elif repository_read:
            result = await self._read_repository_fact(
                assignment=assignment,
                actor=actor,
                operation=operation,
                target_ref=resolved.target_ref,
                query=resolved.query,
            )
        elif operation.id.startswith("repository."):
            result = await self._execute_repository_action(
                assignment=assignment,
                worker_actor=actor,
                requester_actor=requester_actor,
                operation=operation,
                payload=payload,
            )
        elif operation.id == "work_item.list":
            requested_project = (
                resolved.query.get("project_id", [assignment.project_id])[0]
                if resolved.query
                else assignment.project_id
            )
            if requested_project != assignment.project_id:
                raise ControlPlaneBrokerDeniedError(
                    "work-item list project differs from assignment scope"
                )
            result = await self.work_items.list(
                project_id=assignment.project_id,
                owner=(resolved.query.get("owner") or [None])[0],
                stage=(resolved.query.get("stage") or [None])[0],
                release_gate=None,
                scope=actor.tenant,
            )
        elif operation.id == "work_item.read":
            assert resolved.target_ref is not None
            result = await self.work_items.get(resolved.target_ref)
        elif operation.id == "work_item.handoff":
            assert resolved.target_ref is not None
            result = await self.work_items.handoff(
                resolved.target_ref,
                WorkItemHandoffCreate.model_validate(payload),
            )
        elif operation.id == "work_item.acknowledge":
            assert resolved.target_ref is not None
            result = await self.work_items.acknowledge(
                resolved.target_ref,
                WorkItemAckCreate.model_validate(payload),
            )
        elif operation.id == "work_item.progress":
            assert resolved.target_ref is not None
            result = await self.work_items.progress(
                resolved.target_ref,
                WorkItemProgressUpdate.model_validate(payload),
            )
        elif operation.id == "work_item.retry":
            assert resolved.target_ref is not None
            result = await self.operator.retry(
                resolved.target_ref,
                actor=str(payload.get("actor") or actor.identity_id),
                reason=(str(payload["reason"]) if payload.get("reason") else None),
            )
        elif operation.id == "work_item.reconcile":
            assert resolved.target_ref is not None
            result = await self.operator.reconcile(
                resolved.target_ref,
                actor=str(payload.get("actor") or actor.identity_id),
                reason=(str(payload["reason"]) if payload.get("reason") else None),
            )
        else:  # pragma: no cover - closed operation catalog
            raise ControlPlaneBrokerDeniedError("unsupported control-plane operation")

        encoded = jsonable_encoder(result)
        if not isinstance(encoded, dict):
            encoded = {"result": encoded}
        return 200, encoded, operation, resolved.target_ref, authority_decision

    def audit_events(
        self,
        *,
        actor,
        assignment_id: str | None = None,
        limit: int = 200,
    ) -> list[ControlPlaneBrokerAuditEvent]:
        events = [
            item
            for item in self.audit.load().events
            if item.organization_id == actor.organization_id
            and item.workspace_id == actor.workspace_id
            and (assignment_id is None or item.assignment_id == assignment_id)
        ]
        events.sort(key=lambda item: (item.occurred_at, item.id), reverse=True)
        return events[: max(1, min(limit, 1000))]


class AssignmentBoundControlPlaneBroker:
    """Fixed-operation Unix-socket broker for one canonical assignment."""

    def __init__(
        self,
        service: ControlPlaneBrokerService,
        *,
        assignment: ExecutionAssignment,
        worker_id: str,
        worker_actor: AuthenticationActor,
        fence: int,
        validator: Callable[[], ExecutionAssignment],
        worker_service_identity_validator: Callable[[], str] | None = None,
    ) -> None:
        self.service = service
        self.snapshot = assignment.model_copy(deep=True)
        self.worker_id = worker_id
        self.worker_actor = worker_actor
        self.service_identity_id = worker_actor.identity_id
        self.fence = fence
        self.validator = validator
        self.worker_service_identity_validator = worker_service_identity_validator
        self.limits = service.limits
        self._root = Path(tempfile.mkdtemp(prefix="control-plane-broker-"))
        os.chmod(self._root, 0o700)
        self.socket_path = self._root / "broker.sock"
        self.server: asyncio.AbstractServer | None = None
        self._request_times: deque[float] = deque()
        self._active_requests = 0

    @property
    def mount_source(self) -> Path:
        return self._root

    @property
    def mount_destination(self) -> Path:
        return Path("/run/codex-control-plane")

    @property
    def sandbox_socket_path(self) -> Path:
        return self.mount_destination / self.socket_path.name

    @property
    def sandbox_url(self) -> str:
        return "http://127.0.0.1:8788"

    async def start(self) -> "AssignmentBoundControlPlaneBroker":
        if self.server is not None:
            return self
        self.server = await asyncio.start_unix_server(
            self._handle,
            path=str(self.socket_path),
        )
        os.chmod(self.socket_path, 0o600)
        return self

    def _validate_current(self) -> ExecutionAssignment:
        try:
            assignment = self.validator()
        except Exception as exc:
            raise ControlPlaneBrokerDeniedError(
                "assignment or worker authority is stale"
            ) from exc
        if assignment.id != self.snapshot.id:
            raise ControlPlaneBrokerDeniedError("assignment identity changed")
        if assignment.execution_id != self.snapshot.execution_id:
            raise ControlPlaneBrokerDeniedError("assignment execution changed")
        if assignment.assigned_worker_id != self.worker_id:
            raise ControlPlaneBrokerDeniedError("assignment worker changed")
        if assignment.fence != self.fence:
            raise ControlPlaneBrokerDeniedError("assignment fence changed")
        if self.worker_service_identity_validator is not None:
            current_identity = self.worker_service_identity_validator()
            if current_identity != self.service_identity_id:
                raise ControlPlaneBrokerDeniedError(
                    "worker service identity changed"
                )
        lease = assignment.lease
        if (
            lease is None
            or lease.worker_id != self.worker_id
            or lease.fence != self.fence
            or lease.expires_at <= self.service._clock()
        ):
            raise ControlPlaneBrokerDeniedError("assignment lease is stale")
        return assignment

    def _rate_limit(self) -> None:
        now = self.service._monotonic()
        cutoff = now - 60.0
        while self._request_times and self._request_times[0] <= cutoff:
            self._request_times.popleft()
        if len(self._request_times) >= self.limits.max_requests_per_minute:
            raise ControlPlaneBrokerDeniedError("control-plane broker rate limit exceeded")
        self._request_times.append(now)

    @staticmethod
    def _response(
        status: int,
        payload: dict[str, Any],
        *,
        correlation_id: str | None = None,
    ) -> bytes:
        body = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        reason = {
            200: "OK",
            400: "Bad Request",
            403: "Forbidden",
            404: "Not Found",
            405: "Method Not Allowed",
            409: "Conflict",
            413: "Payload Too Large",
            422: "Unprocessable Entity",
            429: "Too Many Requests",
            500: "Internal Server Error",
            502: "Bad Gateway",
        }.get(status, "Error")
        correlation = (
            f"X-Correlation-ID: {correlation_id}\r\n"
            if correlation_id
            else ""
        )
        return (
            f"HTTP/1.1 {status} {reason}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"{correlation}"
            "Connection: close\r\n\r\n"
        ).encode("ascii") + body

    async def _write(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        payload: dict[str, Any],
        *,
        correlation_id: str | None = None,
    ) -> None:
        raw = self._response(
            status,
            payload,
            correlation_id=correlation_id,
        )
        if len(raw) > self.limits.max_response_bytes:
            raw = self._response(
                502,
                {
                    "error": {
                        "code": "broker_response_too_large",
                        "message": "control-plane response exceeds broker limit",
                    }
                },
                correlation_id=correlation_id,
            )
        writer.write(raw)
        with contextlib.suppress(Exception):
            await writer.drain()

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        method = "UNKNOWN"
        target = "/"
        operation = None
        target_ref = None
        actor_identity_id = None
        authority_decision = None
        status = 500
        denial_reason = None
        correlation_id = uuid.uuid4().hex
        causation_id = None
        counted = False
        try:
            raw_headers = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"),
                timeout=10,
            )
            if len(raw_headers) > self.limits.max_header_bytes:
                status = 413
                raise ControlPlaneBrokerDeniedError(
                    "control-plane request headers are too large"
                )
            header_lines = raw_headers.decode("iso-8859-1").split("\r\n")
            try:
                method, target, version = header_lines[0].split(" ", 2)
            except ValueError as exc:
                status = 400
                raise ControlPlaneBrokerDeniedError("invalid HTTP request line") from exc
            method = method.upper()
            if version not in {"HTTP/1.0", "HTTP/1.1"}:
                status = 400
                raise ControlPlaneBrokerDeniedError("unsupported HTTP version")
            headers: dict[str, str] = {}
            for line in header_lines[1:]:
                if not line:
                    continue
                if ":" not in line:
                    status = 400
                    raise ControlPlaneBrokerDeniedError("invalid HTTP header")
                key, value = line.split(":", 1)
                headers[key.strip().casefold()] = value.strip()
            correlation_id = (
                self.service._trace_id(headers.get("x-correlation-id"))
                or correlation_id
            )
            causation_id = self.service._trace_id(headers.get("x-causation-id"))
            if headers.get("transfer-encoding"):
                status = 400
                raise ControlPlaneBrokerDeniedError(
                    "chunked/transfer-encoded requests are not supported"
                )
            raw_length = headers.get("content-length", "0")
            if not raw_length.isdigit():
                status = 400
                raise ControlPlaneBrokerDeniedError("invalid content-length")
            content_length = int(raw_length)
            if self._active_requests >= self.limits.max_concurrent_requests:
                raise ControlPlaneBrokerDeniedError(
                    "control-plane broker concurrency limit exceeded"
                )
            self._active_requests += 1
            counted = True
            self._rate_limit()
            assignment = self._validate_current()
            if content_length > self.limits.max_request_bytes:
                status = 413
                raise ControlPlaneBrokerDeniedError(
                    "control-plane request body exceeds broker limit"
                )
            body = (
                await asyncio.wait_for(
                    reader.readexactly(content_length),
                    timeout=10,
                )
                if content_length
                else b""
            )
            resolved = self.service._resolve_operation(method, target)
            operation = resolved.operation
            target_ref = resolved.target_ref
            actor = self.service._actor(assignment, self.worker_actor)
            actor_identity_id = actor.identity_id

            status, payload, operation, target_ref, authority_decision = (
                await self.service.dispatch(
                    assignment=assignment,
                    worker_actor=self.worker_actor,
                    method=method,
                    raw_target=target,
                    body=body,
                )
            )
            self.service._audit(
                snapshot=self.snapshot,
                worker_id=self.worker_id,
                fence=self.fence,
                method=method,
                path=urlsplit(target).path,
                correlation_id=correlation_id,
                causation_id=causation_id,
                decision=ControlPlaneBrokerDecision.ALLOW,
                operation=operation,
                target_ref=target_ref,
                actor_identity_id=actor_identity_id,
                authority_decision=authority_decision,
                response_status=status,
            )
            await self._write(
                writer,
                status,
                {
                    **payload,
                    "_broker": {
                        "correlation_id": correlation_id,
                        "operation": operation.id,
                    },
                },
                correlation_id=correlation_id,
            )
            return
        except HTTPException as exc:
            status = int(exc.status_code)
            denial_reason = str(exc.detail)
        except ValidationError as exc:
            status = 422
            denial_reason = str(exc)
        except ControlPlaneBrokerAuthorityDeniedError as exc:
            status = 403
            authority_decision = exc.decision
            denial_reason = str(exc)
        except ControlPlaneBrokerRequestError as exc:
            status = exc.status_code
            denial_reason = str(exc)
        except ControlPlaneBrokerDeniedError as exc:
            denial_reason = str(exc)
            if status == 500:
                if "rate limit" in denial_reason or "concurrency limit" in denial_reason:
                    status = 429
                elif "method " in denial_reason:
                    status = 405
                else:
                    status = 403
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError):
            status = 400
            denial_reason = "incomplete or timed-out control-plane request"
        except Exception as exc:
            status = 500
            denial_reason = f"control-plane broker internal error: {type(exc).__name__}"
        finally:
            if denial_reason is not None:
                with contextlib.suppress(Exception):
                    self.service._audit(
                        snapshot=self.snapshot,
                        worker_id=self.worker_id,
                        fence=self.fence,
                        method=method,
                        path=urlsplit(target).path if target else "/",
                        correlation_id=correlation_id,
                        causation_id=causation_id,
                        decision=(
                            ControlPlaneBrokerDecision.DENY
                            if status < 500
                            else ControlPlaneBrokerDecision.ERROR
                        ),
                        operation=operation,
                        target_ref=target_ref,
                        actor_identity_id=actor_identity_id,
                        authority_decision=authority_decision,
                        response_status=status,
                        denial_reason=denial_reason,
                    )
                if not writer.is_closing():
                    with contextlib.suppress(Exception):
                        await self._write(
                            writer,
                            status,
                            {
                                "error": {
                                    "code": "control_plane_broker_denied",
                                    "message": denial_reason[:500],
                                    "correlation_id": correlation_id,
                                }
                            },
                            correlation_id=correlation_id,
                        )
            if counted:
                self._active_requests = max(0, self._active_requests - 1)
            if not writer.is_closing():
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()

    async def stop(self) -> None:
        server = self.server
        self.server = None
        if server is not None:
            server.close()
            await server.wait_closed()
        with contextlib.suppress(FileNotFoundError):
            self.socket_path.unlink()
        shutil.rmtree(self._root, ignore_errors=True)


class DeferredControlPlaneBrokerFactory:
    """Late-bound composition seam because work-item services compose after workers."""

    def __init__(self) -> None:
        self.service: ControlPlaneBrokerService | None = None

    def configure(self, service: ControlPlaneBrokerService) -> None:
        self.service = service

    async def start(
        self,
        *,
        assignment: ExecutionAssignment,
        worker_id: str,
        worker_actor: AuthenticationActor,
        fence: int,
        validator: Callable[[], ExecutionAssignment],
        worker_service_identity_validator: Callable[[], str] | None = None,
    ) -> AssignmentBoundControlPlaneBroker | None:
        service = self.service
        if (
            service is None
            or assignment.execution_profile_id not in CONTROL_PLANE_EXECUTION_PROFILES
        ):
            return None
        broker = AssignmentBoundControlPlaneBroker(
            service,
            assignment=assignment,
            worker_id=worker_id,
            worker_actor=worker_actor,
            fence=fence,
            validator=validator,
            worker_service_identity_validator=worker_service_identity_validator,
        )
        await broker.start()
        return broker


CONTROL_PLANE_RELAY_SCRIPT = r"""
import select
import socket
import subprocess
import sys
import threading

socket_path = sys.argv[1]
port = int(sys.argv[2])
command = sys.argv[3:]


def bridge(client):
    upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        upstream.connect(socket_path)
        peers = (client, upstream)
        while True:
            readable, _, _ = select.select(peers, [], [])
            for source in readable:
                data = source.recv(65536)
                if not data:
                    return
                (upstream if source is client else client).sendall(data)
    finally:
        try:
            client.close()
        except OSError:
            pass
        try:
            upstream.close()
        except OSError:
            pass


def serve():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", port))
    listener.listen(16)
    while True:
        client, _ = listener.accept()
        threading.Thread(target=bridge, args=(client,), daemon=True).start()


threading.Thread(target=serve, daemon=True).start()
raise SystemExit(subprocess.call(command))
"""
