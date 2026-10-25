from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
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
    ActionIntentReconcileRequest,
    ActionIntentStatus,
)
from codex_web.action_providers import ActionRequest
from codex_web.services.action_providers import ActionProviderNotFoundError
from codex_web.services.async_operations import run_settled
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
    WorkItemOwnerDispatchRequest,
)
from codex_web.code_hosts import (
    PULL_REQUEST_QUERY_STATES,
    normalize_pull_request_state,
    CodeHostCapability,
    CodeHostError,
    CodeHostProviderBinding,
    CodeHostTransientError,
)
from codex_web.execution_workers import ExecutionAssignment
from codex_web.execution_workspaces import PublicationRecoveryHandoffRequest
from codex_web.services.publication_recovery import PublicationRecoveryConflict
from codex_web.services.publication_recovery_handoff import PublicationRecoveryHandoffService
from codex_web.identity import AuthenticationActor, TenantScope
from codex_web.models import (
    WorkItemAckCreate,
    WorkItemHandoffCreate,
    WorkItemProgressUpdate,
)
from codex_web.services.authority_roles import AuthorityRoleService
from codex_web.services.action_intents import ActionIntentConflictError, ActionIntentService
from codex_web.services.agent_profiles import AgentProfileError
from codex_web.services.definitions import DefinitionCompatibilityError, DefinitionConflictError
from codex_web.services.code_host_action_contract import (
    CODE_HOST_BRANCH_PUBLISH_ACTION_ID,
    CODE_HOST_ISSUE_COMMENT_ACTION_ID,
    CODE_HOST_ISSUE_CREATE_ACTION_ID,
    CODE_HOST_ISSUE_UPDATE_ACTION_ID,
    CODE_HOST_JOB_RERUN_ACTION_ID,
    CODE_HOST_PULL_REQUEST_MERGE_ACTION_ID,
    CODE_HOST_PULL_REQUEST_READY_ACTION_ID,
    CODE_HOST_PULL_REQUEST_UPSERT_ACTION_ID,
)
from codex_web.services.code_hosts import CodeHostService
from codex_web.services.identity import (
    AuthenticationError,
    AuthorizationError,
    IdentityService,
    TenantIsolationError,
)
from codex_web.services.execution_workspaces import (
    ExecutionWorkspaceBackendError,
    ExecutionWorkspaceConflictError,
    ExecutionWorkspaceNotFoundError,
    ExecutionWorkspaceService,
)
from codex_web.services.work_item_operator import WorkItemOperatorService
from codex_web.services.work_items import WorkItemService
from codex_web.storage.control_plane_broker import ControlPlaneBrokerAuditStore
from codex_web.services.project_coordination import (
    PREFIX as COORDINATION_PREFIX, PROJECT_COORDINATION_OPERATIONS, resolve_operation,
)

from codex_web.services.assignment_reasoning_broker import (
    OPERATIONS as REASONING_OPERATIONS, resolve as resolve_reasoning_operation, ProcedureStart, ProcedureAdvance,
)
from codex_web.executive_roles import ExecutiveActivationCreate

from codex_web.services.home_operator_host import HOST_INSPECTION
from codex_web.services.home_host_commands import HOST_COMMAND, HostCommandAdmission, execute_host_command
from codex_web.services.home_host_output import OUTPUT_READ
from codex_web.services.home_operator_coordination import OPERATIONS as HOME_COORDINATION_OPERATIONS, HomeProjectMessage

from codex_web.services.worker_verifications import (
    OPERATIONS as VERIFICATION_OPERATIONS, resolve as resolve_verification_operation,
    WorkerVerificationAdapter,
)


class ControlPlaneBrokerError(RuntimeError):
    pass


class ControlPlaneBrokerDeniedError(ControlPlaneBrokerError):
    pass


class ControlPlaneBrokerRequestError(ControlPlaneBrokerDeniedError):
    def __init__(
        self, message: str, *, status_code: int = 400,
        code: str = "control_plane_broker_denied",
        history_readiness: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.history_readiness = history_readiness


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


HOST_OUTPUT_READ = ControlPlaneBrokerOperation(id=OUTPUT_READ, method='GET',
    path_template='/api/operator/host/outputs/{artifact_id}', capability=OUTPUT_READ,
    authority_level=AuthorityLevel.READ)

OPERATIONS: tuple[ControlPlaneBrokerOperation, ...] = (
    *REASONING_OPERATIONS,
    HOST_INSPECTION,
    HOST_COMMAND,
    HOST_OUTPUT_READ,
    *HOME_COORDINATION_OPERATIONS,
    *PROJECT_COORDINATION_OPERATIONS,
    *VERIFICATION_OPERATIONS,
    ControlPlaneBrokerOperation(id="upgrade.approval.request", method="POST",
        path_template="/api/agent-upgrade-approvals", capability="upgrade.approval.request",
        authority_level=AuthorityLevel.PREPARE),
    ControlPlaneBrokerOperation(
        id="work_graph.edge.create", method="POST", path_template="/api/work-graph/edges",
        capability="work_graph.manage", authority_level=AuthorityLevel.EXECUTE,
    ),
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
        id="work_item.steer",
        method="POST",
        path_template="/api/work-items/{ref}/steer",
        capability="work_item.steer",
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
        id="work_item.projection_retry",
        method="POST",
        path_template="/api/work-items/{ref}/projection-retry",
        capability="work_item.reconcile",
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
        id="action_intent.reconcile",
        method="POST",
        path_template="/api/action-intents/{intent_id}/reconcile",
        capability="repository.branch.publish",
        authority_level=AuthorityLevel.EXECUTE,
    ),
    ControlPlaneBrokerOperation(
        id="repository.read.delivery-evidence",
        method="GET",
        path_template="/api/action-intents/{intent_id}/delivery-evidence",
        capability="repository.read",
        authority_level=AuthorityLevel.READ,
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
        id="repository.read.pull-requests",
        method="GET",
        path_template="/api/repository-facts/pull-requests?state={state}",
        capability="repository.pull-request.read",
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
        id="repository.read.merge-requirements", method="GET",
        path_template="/api/repository-facts/pull-requests/{external_id}/merge-requirements",
        capability="repository.pull-request.merge-requirements.read", authority_level=AuthorityLevel.READ,
    ),
    ControlPlaneBrokerOperation(
        id="repository.read.checks",
        method="GET",
        path_template="/api/repository-facts/checks/{revision}",
        capability="repository.checks.read",
        authority_level=AuthorityLevel.READ,
    ),
    ControlPlaneBrokerOperation(
        id="repository.read.job-logs",
        method="GET",
        path_template="/api/repository-facts/jobs/{job_id}/logs?max_bytes={max_bytes}",
        capability="repository.checks.read",
        authority_level=AuthorityLevel.READ,
    ),
    ControlPlaneBrokerOperation(
        id="repository.read.artifacts",
        method="GET",
        path_template="/api/repository-facts/runs/{run_id}/artifacts",
        capability="repository.checks.read",
        authority_level=AuthorityLevel.READ,
    ),
    ControlPlaneBrokerOperation(
        id="repository.read.artifact-download",
        method="GET",
        path_template="/api/repository-facts/artifacts/{artifact_id}/download?max_bytes={max_bytes}",
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
        id="repository.pull-request.ready",
        method="POST",
        path_template="/api/repository-actions/pull-request/ready",
        capability="repository.pull-request.ready",
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
        id="repository.check.rerun",
        method="POST",
        path_template="/api/repository-actions/check/rerun",
        capability="repository.checks.rerun",
        authority_level=AuthorityLevel.EXECUTE,
    ),
    ControlPlaneBrokerOperation(
        id="repository.workspace.refresh",
        method="POST",
        path_template="/api/repository-workspace/refresh",
        capability="repository.workspace.refresh",
        authority_level=AuthorityLevel.EXECUTE,
    ),
    ControlPlaneBrokerOperation(
        id="repository.workspace.publication-recovery",
        method="POST",
        path_template="/api/repository-workspace/publication-recovery",
        capability="repository.branch.publish",
        authority_level=AuthorityLevel.EXECUTE,
    ),
    ControlPlaneBrokerOperation(
        id="repository.issue.update",
        method="POST",
        path_template="/api/repository-actions/issue/update",
        capability="repository.issue.update",
        authority_level=AuthorityLevel.EXECUTE,
    ),
    ControlPlaneBrokerOperation(
        id="deployment.local.status",
        method="GET",
        path_template="/api/local-deployment",
        capability="deployment.local.read",
        authority_level=AuthorityLevel.READ,
    ),
    ControlPlaneBrokerOperation(
        id="deployment.local.install",
        method="POST",
        path_template="/api/local-deployment/install",
        capability="deployment.local.install",
        authority_level=AuthorityLevel.EXECUTE,
    ),
)

_OPERATION_BY_ID = {item.id: item for item in OPERATIONS}
_MUTATION_SUFFIXES = {
    "handoff": "work_item.handoff",
    "ack": "work_item.acknowledge",
    "progress": "work_item.progress",
    "steer": "work_item.steer",
    "retry": "work_item.retry",
    "reconcile": "work_item.reconcile",
    "projection-retry": "work_item.projection_retry",
}
_SAFE_TRACE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_REPOSITORY_ACTION_PAYLOAD_FIELDS = ("parameters", "idempotency_key")
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
    "/api/repository-actions/pull-request/ready": (
        "repository.pull-request.ready",
        CODE_HOST_PULL_REQUEST_READY_ACTION_ID,
    ),
    "/api/repository-actions/pull-request/merge": (
        "repository.pull-request.merge",
        CODE_HOST_PULL_REQUEST_MERGE_ACTION_ID,
    ),
    "/api/repository-actions/check/rerun": (
        "repository.check.rerun",
        CODE_HOST_JOB_RERUN_ACTION_ID,
    ),
}
_RECONCILABLE_REPOSITORY_ACTION_IDS = frozenset(
    {
        CODE_HOST_BRANCH_PUBLISH_ACTION_ID,
        CODE_HOST_PULL_REQUEST_MERGE_ACTION_ID,
        CODE_HOST_PULL_REQUEST_READY_ACTION_ID,
    }
)

# Receipt inspection is read-only and also covers owned MR metadata updates.
# Keep mutation reconciliation restricted to its existing action allowlist.
_DELIVERY_EVIDENCE_REPOSITORY_ACTION_IDS = (
    _RECONCILABLE_REPOSITORY_ACTION_IDS | {CODE_HOST_PULL_REQUEST_UPSERT_ACTION_ID}
)

# These profiles execute different workloads but share the same narrow broker
# boundary. The broker never grants generic network or localhost access: every
# request is still operation-allowlisted, tenant/project scoped, authorized,
# fenced, rate limited, and audited below.
CONTROL_PLANE_EXECUTION_PROFILES = frozenset(
    {"orchestration-only", "repository-write", "orchestration-admin", "project-governor"}
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
        execution_workspaces: ExecutionWorkspaceService | None = None,
        upgrades=None,
        local_execution_worker=None,
        service_instance_id: str | None = None,
        execution_profiles=None,
        execution_workers=None,
        work_graph=None,
        operator: WorkItemOperatorService | None = None,
        limits: ControlPlaneBrokerLimits | None = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.identity = identity
        self.authority = authority
        self.work_items = work_items
        self.execution_profiles = execution_profiles
        self.execution_workers = execution_workers
        self.work_graph = work_graph
        self.operator = operator or WorkItemOperatorService(work_items)
        self.audit = audit
        self.action_intents = action_intents
        self.code_hosts = code_hosts
        self.execution_workspaces = execution_workspaces
        self.upgrades = upgrades
        self.local_execution_worker = local_execution_worker
        self.service_instance_id = service_instance_id
        self.limits = limits or ControlPlaneBrokerLimits()
        self._clock = clock
        self._monotonic = monotonic
        self._local_installers = {}
        self.assignment_reasoning = None
        self.procedure_admission = None
        self.project_coordination = None
        self.home_host_outputs = None
        self.home_operator_host = None
        self.home_operator_coordination = None

    @staticmethod
    def operations() -> tuple[ControlPlaneBrokerOperation, ...]:
        return OPERATIONS

    @staticmethod
    def public_operation(item: ControlPlaneBrokerOperation) -> dict[str, Any]:
        result = {
            "id": item.id,
            "method": item.method,
            "path_template": item.path_template,
            "capability": item.capability,
            "authority_level": item.authority_level.value,
        }
        reasoning_model = {"procedure.start": ProcedureStart, "procedure.advance": ProcedureAdvance,
            "executive.activation.create": ExecutiveActivationCreate}.get(item.id)
        if reasoning_model is not None:
            result["request_body"] = reasoning_model.model_json_schema()
        if item.id == "procedure.advance":
            result["current_input_snapshot_id"] = "Optional explicit fresh canonical equal-content witness; preserves original run pin/history and refuses changed facts"
        if item.id.startswith(("procedure.", "executive.")):
            result["scope"] = "Exact current assignment/fence, requester, project and explicit current execution Definition"
            result["query_parameters"] = ({"allowed": True, "names": ["run_id"]}
                if item.id == "procedure.read" else {"allowed": False})
            result["response_loss"] = "Read current procedure checkpoint or assignment-origin Executive activation; never blind replay"
        work_item_model = {
            "work_item.handoff": WorkItemHandoffCreate,
            "work_item.acknowledge": WorkItemAckCreate,
            "work_item.progress": WorkItemProgressUpdate,
        }.get(item.id)
        if work_item_model is not None:
            result["request_body"] = work_item_model.model_json_schema()
        if item.id == 'operator.project.thread.message':
            result['request_body'] = HomeProjectMessage.model_json_schema()
            result['query_parameters'] = {'allowed': False}
            result['effect'] = 'One retained input in an existing explicitly declared project thread; queue acceptance does not verify native start or task completion'
            result['response_loss'] = 'Inspect the original canonical intent and retained input; no replacement key or automatic replay'
        if item.id == 'operator.project.threads.list':
            result['required_query_parameters'] = ['project_id']
            result['optional_query_parameters'] = ['limit', 'cursor']
            result['scope'] = 'One explicitly declared project and pinned invokable Agent Profile revisions; current target authority remains required'
        if item.id == OUTPUT_READ:
            result['query_parameters'] = {'kind': ['source', 'released'],
                'stream': ['stdout', 'stderr'], 'max_bytes': {'minimum': 1, 'maximum': 65536}}
            result['effect'] = 'Read a bounded excerpt only under current explicit output policy or prior human review; no classification override'
        if item.id == HOST_COMMAND.id:
            result['request_body'] = HostCommandAdmission.model_json_schema()
            result['query_parameters'] = {'allowed': False}
            result['effect'] = 'One bounded privileged host process under the configured OS identity; process completion does not verify requested effects'
            result['response_loss'] = 'Inspect original intent and artifact; never replace the key or replay an uncertain command'
        if item.id == HOST_INSPECTION.id:
            result['scope'] = 'Explicitly selected Home operator; current Home-scoped profile declaration and administrator authority required'
            result['request_body'] = {'allowed': False}
            result['query_parameters'] = {'allowed': False}
            result['effect'] = 'Fixed read-only process/platform/filesystem-capacity metadata; no commands, file contents or credentials'
        if item.id.startswith("verification."):
            result["scope"] = "Current assignment Work Item and exact admitted Resources; writable repository remains required"
            result["authorizer"] = "Current assignment requester, exact API capability"
            result["verifier"] = "Actual authenticated registered worker; no identity overrides"
            result["response_loss"] = "Read-only exact execution/method/source lookup; never blind POST retry"
            result["bounds"] = {"content_bytes": 262144, "lookup_records": 32}
            if item.id == "verification.create":
                result["request_body"] = {"schema": "VerificationCreate",
                    "method": "recovery-owner-disposition.v2",
                    "methods": ["recovery-owner-disposition.v2", "recovery-storage-capacity.v1"],
                    "source": "sha256:<review digest>", "original_evidence_count": 1,
                    "max_artifacts": 8, "identity_and_independence_overrides": False}
                from codex_web.services.worker_mr_verifications import METHOD, CONTINUATION_METHOD, MRObservationRequest
                result["request_body"]["methods"].extend((METHOD, CONTINUATION_METHOD))
                result["request_body"]["MR_observation"] = {
                    "method": METHOD, "schema": MRObservationRequest.model_json_schema(),
                    "effect": "Fresh exact-head provider observations with derived Evidence; never a caller-stated result or merge permission"}
                result["request_body"]["MR_continuation_input"] = {
                    "method": CONTINUATION_METHOD, "schema": MRObservationRequest.model_json_schema(),
                    "effect": "Current governed MR observation plus managed procedure_input Artifact; stable business fingerprint excludes delivery/time/proof IDs; no QA or merge consent",
                    "continuation": "Use returned Artifact with a published1.2 required input_protocol. New same-head business facts may start a settled next run; equal-content fresh Artifact can explicitly witness advance. Reconcile original source after response loss; never blind replay."}
            if item.id == "verification.read":
                result["required_query_parameters"] = ["work_item_ref", "source"]
                from codex_web.services.worker_mr_verifications import METHOD, CONTINUATION_METHOD
                result["MR_observation_query"] = {"required": ["method", "source"],
                    "method": METHOD, "methods": [METHOD, CONTINUATION_METHOD], "source": "sha256:<original assignment/request digest>",
                    "effect": "Read the original historical records; expired observations are not refreshed or replayed"}
        if item.id.startswith("repository.read."):
            result["optional_query_parameters"] = ["repository_resource_id"]
        if item.id == "repository.read.pull-requests":
            result["optional_query_parameters"].append("state")
            result["query_parameters"] = {"state": {
                "type": "string", "enum": list(PULL_REQUEST_QUERY_STATES), "default": "open"}}
        if item.id == "repository.read.artifact-download":
            result["optional_query_parameters"].append("member_path")
        if item.id == 'repository.read.merge-requirements':
            result['requires_explicit_declaration'] = True
            result['provider_capability'] = CodeHostCapability.MERGE_REQUIREMENTS_READ.value
            result['unsupported_provider_guidance'] = (
                'An unsupported adapter capability is an evidence gap, not a binding repair. '
                'Do not retry unchanged reads or broaden grants; other reads do not replace missing merge-policy evidence.'
            )
        if item.id == "repository.read.delivery-evidence":
            result["supported_action_ids"] = sorted(_DELIVERY_EVIDENCE_REPOSITORY_ACTION_IDS)
            result["inspection"] = {
                "read_only": True,
                "superseded_execution_receipts": True,
                "requires_matching_requester_and_repository_scope": True,
                "provider_outcome_requires_authoritative_evidence": True,
            }
        if any(item.id == operation_id for operation_id, _ in _REPOSITORY_ACTIONS.values()):
            result["request_body"] = {
                "type": "object",
                "allowed_fields": list(_REPOSITORY_ACTION_PAYLOAD_FIELDS),
                "parameters": {"type": "object"},
                "idempotency_key": {"type": "string"},
                "assignment_bound_parameters": (
                    ["execution_workspace_id"]
                    if item.id == "repository.branch.publish" else []
                ),
            }
            if item.id == "repository.pull-request.ready":
                result["explicit_declaration_required"] = True
                result["request_body"]["parameters"] = {
                    "type": "object", "additionalProperties": False,
                    "required": ["pull_request_number", "expected_head_sha", "expected_base_sha", "expected_base_ref"],
                    "properties": {"pull_request_number": {"type": "integer", "minimum": 1},
                        "expected_head_sha": {"type": "string", "pattern": "^[a-f0-9]{40}$"},
                        "expected_base_sha": {"type": "string", "pattern": "^[a-f0-9]{40}$"},
                        "expected_base_ref": {"type": "string"}}}
            if item.id == "repository.pull-request.merge":
                result["request_body"]["parameters"] = {
                    "type": "object", "additionalProperties": False,
                    "required": ["pull_request_number"],
                    "properties": {
                        "pull_request_number": {"type": "integer", "minimum": 1},
                        "merge_method": {"type": "string", "enum": ["merge", "squash", "rebase"],
                            "default": "squash",
                            "description": "Provider constraints still apply; GitLab rejects rebase-and-merge."},
                        "expected_head_sha": {"type": "string", "pattern": "^[a-fA-F0-9]{40}$",
                            "description": "Optional exact head precondition; obtain from a fresh authorized read."},
                    },
                    "description": "Merge parameters only. expected_base_sha and expected_base_ref belong to readiness, not merge.",
                }
            if item.id == "repository.branch.publish":
                result["request_body"]["parameters"] = {
                    "type": "object",
                    "required": ["branch", "head_revision"],
                    "properties": {
                        "branch": {"type": "string"},
                        "head_revision": {"type": "string"},
                        "expected_remote_revision": {"type": "string"},
                        "expected_remote_absent": {"type": "boolean"},
                        "existing_change_request_number": {"type": "integer"},
                    },
                    "not": {"required": ["expected_remote_revision", "expected_remote_absent"]},
                    "recovery_precondition": (
                        "confirmed absent codex/ ref: expected_remote_absent=true; "
                        "existing ref: expected_remote_revision=observed SHA"
                    ),
                }
        if item.id == "repository.workspace.publication-recovery":
            result["handoff_request_body"] = PublicationRecoveryHandoffRequest.model_json_schema()
            result["request_body"] = {
                "type": "object",
                "required": ["work_item_ref"],
                "allowed_fields": ["work_item_ref"],
                "provenance": (
                    "records bounded issue scope without changing the execution subject"
                ),
            }
        return result

    @staticmethod
    def _public_assignment_context(assignment: ExecutionAssignment) -> dict[str, Any]:
        scope = assignment.repository_scope
        target = assignment.repository_target
        writable = (
            scope.writable_repository_ids if scope is not None else
            ((target.mutable_repository_id,) if target and target.mutable_repository_id else ())
        )
        readonly = (
            scope.read_only_repository_ids if scope is not None else
            (target.read_only_repository_ids if target is not None else ())
        )
        profile = assignment.agent_profile
        return {
            "assignment_id": assignment.id,
            "execution_id": assignment.execution_id,
            "execution_workspace_id": assignment.execution_workspace_id,
            "execution_profile_id": assignment.execution_profile_id,
            "execution_profile_definition": (
                assignment.execution_profile_definition.model_dump(mode="json")
                if assignment.execution_profile_definition is not None else None
            ),
            "agent_profile": (
                {
                    "profile_id": profile.profile_id,
                    "profile_revision": profile.profile_revision,
                    "profile_record_id": profile.profile_record_id,
                    "role_id": profile.role_id,
                } if profile is not None else None
            ),
            "declared_repository_scope": {
                "writable_repository_ids": list(writable),
                "read_only_repository_ids": list(readonly),
            },
            # Declaration/provenance does not attest a live lease or grant
            # effective authority. Every operation still crosses its guard.
            "effective_authority_requires_operation_checks": True,
        }

    def public_assignment_capability(
        self,
        assignment: ExecutionAssignment,
    ) -> dict[str, Any]:
        enabled = assignment.execution_profile_id in CONTROL_PLANE_EXECUTION_PROFILES
        context = self._public_assignment_context(assignment) if enabled else None
        operations = [self.public_operation(item) for item in OPERATIONS] if enabled else []
        for item in operations:
            if item['id'] == 'repository.read.merge-requirements':
                declared = False
                try:
                    profile, ref = self.execution_profiles.resolve(assignment.execution_profile_id,
                        organization_id=assignment.organization_id, workspace_id=assignment.workspace_id,
                        project_id=assignment.project_id)
                    declared = (ref == assignment.execution_profile_definition
                                and item['id'] in profile.control_plane_operations)
                except (AttributeError, ValueError, LookupError):
                    pass
                if context is not None:
                    context['merge_requirements_declaration'] = {'declared': declared,
                        'status': 'declared' if declared else 'unavailable',
                        'effective_authority_requires_operation_checks': True}
        host_declared = False
        if enabled and assignment.project_id == 'home' and self.home_operator_host is not None:
            try:
                self.home_operator_host.authorize(assignment, self._requester_actor(assignment))
                host_declared = True
            except ControlPlaneBrokerDeniedError:
                pass
        operations = [op for op in operations if op['id'] != HOST_INSPECTION.id or host_declared]
        home_coordination_ids = set()
        home_envelope = None
        if host_declared and self.home_operator_coordination is not None:
            try:
                profile, reference = self.execution_profiles.resolve(assignment.execution_profile_id,
                    organization_id=assignment.organization_id, workspace_id=assignment.workspace_id, project_id='home')
                if reference == assignment.execution_profile_definition and getattr(profile, 'home_operator', None):
                    home_coordination_ids = set(profile.control_plane_operations)
                    home_envelope = profile.home_operator
            except (ValueError, LookupError):
                pass
        operations = [op for op in operations if op['id'] not in {HOST_COMMAND.id, OUTPUT_READ} or op['id'] in home_coordination_ids]
        operations = [op for op in operations if not op['id'].startswith('operator.project.')
                      or op['id'] in home_coordination_ids]
        if context is not None and home_envelope is not None:
            context['home_operator_declaration'] = home_envelope.model_dump(mode='json')
            context['home_operator_declaration']['effective_authority_requires_operation_checks'] = True
        if context is not None and assignment.project_id == 'home':
            context['host_inspection_declaration'] = {
                'declared': host_declared,
                'effective_authority_requires_operation_checks': True,
                'scope': 'Read-only fixed host metadata; no shell or file contents',
            }
        # New action recognition must not widen stored/default profile authority.
        try:
            self._ready_declaration(assignment)
        except (ValueError, ControlPlaneBrokerDeniedError):
            operations = [op for op in operations if op["id"] != "repository.pull-request.ready"]
        # Ordinary repository-write declarations and historical catalogs never
        # acquire governor operations simply because the engine supports them.
        coordination = None
        if enabled and self.project_coordination is not None:
            try:
                profile, ref = self.execution_profiles.resolve(assignment.execution_profile_id,
                    organization_id=assignment.organization_id, workspace_id=assignment.workspace_id,
                    project_id=assignment.project_id)
                if profile.project_coordination:
                    coordination = {"declaration": profile.project_coordination.model_dump(mode="json"),
                        "definition": assignment.execution_profile_definition.model_dump(mode="json") if assignment.execution_profile_definition else None,
                        "current_definition": ref.model_dump(mode="json"),
                        "definition_is_current": ref == assignment.execution_profile_definition,
                        "effective_authority_requires_operation_checks": True,
                        "operations": []}
                    actor = self._requester_actor(assignment)
                    for op in PROJECT_COORDINATION_OPERATIONS:
                        observation = {"id": op.id, "capability": op.capability,
                            "declared": op.id in profile.control_plane_operations}
                        try:
                            self.project_coordination.guard(assignment, op)
                            observation.update(status="allowed", reason="Observed live scope and authority; rechecked on use")
                        except Exception as exc:
                            observation.update(status="blocked", reason=str(getattr(exc, "detail", exc)))
                        coordination["operations"].append(observation)
            except Exception:
                # Catalog inspection grants nothing; dispatch retains typed denial.
                coordination = None
        operations = [item for item in operations if not item["id"].startswith("project.")
            or (coordination and any(v["id"] == item["id"] and v["declared"] for v in coordination["operations"]))]
        if context is not None and coordination is not None:
            context["project_coordination"] = coordination
        if assignment.execution_profile_id == "project-governor":
            operations = [item for item in operations if coordination and item["id"] in profile.control_plane_operations]
        if (context is not None and assignment.execution_workspace_id and self.execution_workspaces is not None
                and getattr(self.execution_workspaces, "store", None) is not None):
            workspace = self.execution_workspaces.store.workspace(assignment.execution_workspace_id)
            if (workspace is not None and workspace.organization_id == assignment.organization_id
                    and workspace.workspace_id == assignment.workspace_id and workspace.project_id == assignment.project_id
                    and workspace.execution_id == assignment.execution_id and workspace.subject == assignment.subject):
                context["publication_recovery"] = {
                    "current_scope": workspace.publication_recovery.model_dump(mode="json") if workspace.publication_recovery else None,
                    "retired_scopes": [entry.model_dump(mode="json") for entry in workspace.publication_recovery_history],
                    "workspace_id": workspace.id,
                    "next_action": "Inspect exact current/retired evidence; reconcile unresolved intents before previewing a handoff",
                }

        if enabled and assignment.execution_profile_id == "orchestration-admin":
            envelope = {"repository_ids": [], "operation_ids": [],
                        "definition": (assignment.execution_profile_definition.model_dump(mode="json")
                                       if assignment.execution_profile_definition else None),
                        "authority_role_id": getattr(assignment.agent_profile, "authority_role_id", None),
                        "authority_definition": (
                            assignment.agent_profile.authority_definition_ref.model_dump(mode="json")
                            if assignment.agent_profile and assignment.agent_profile.authority_definition_ref else None),
                        "effective_authority_requires_operation_checks": True}
            try:
                if self.execution_profiles is None:
                    raise ValueError("administrative execution capability is unavailable")
                from codex_web.execution_profiles import ExecutionProfileCatalogDefinition
                from codex_web.definitions import reference_for
                pinned = self.execution_profiles.registry.get_record(assignment.execution_profile_definition.record_id)
                if reference_for(pinned) != assignment.execution_profile_definition:
                    raise ValueError("pinned administrative definition identity is invalid")
                profile = ExecutionProfileCatalogDefinition.model_validate(pinned.payload).profile_map[assignment.execution_profile_id]
                envelope["repository_ids"] = list(profile.administrative_repository_ids)
                envelope["operation_ids"] = list(profile.control_plane_operations)
                _, current = self.execution_profiles.resolve(
                    assignment.execution_profile_id, organization_id=assignment.organization_id,
                    workspace_id=assignment.workspace_id, project_id=assignment.project_id,
                )
                envelope["definition_is_current"] = current == assignment.execution_profile_definition
                envelope["current_definition"] = current.model_dump(mode="json")
                observation = {"status": "unavailable", "reason": "Administrative declaration inspection is unavailable"}
                envelope["configuration_observation"] = observation
                try:
                    requester = self._requester_actor(assignment)
                    envelope["requester_identity_id"] = requester.identity_id
                    profiles = self.execution_profiles.agent_profiles
                    if assignment.agent_profile is not None and profiles is not None:
                        latest = profiles.get(assignment.agent_profile.profile_id, actor=requester)
                        envelope["current_agent_profile"] = {
                            "profile_id": latest.profile_id, "revision": latest.revision,
                            "record_id": latest.record_id, "lifecycle": str(latest.lifecycle),
                            "role_id": latest.role_id, "authority_role_id": latest.authority_role_id,
                        }
                    if profile.administrative_repository_ids and profile.control_plane_operations:
                        # Inspect one explicitly named declaration through the
                        # same guard used by dispatch. This is no operation grant.
                        operation_id = profile.control_plane_operations[0]
                        repository_id = profile.administrative_repository_ids[0]
                        observation.update(operation_id=operation_id, repository_id=repository_id)
                        self.execution_profiles.authorize_administrative_target(
                            assignment, operation_id=operation_id, repository_id=repository_id,
                            actor=requester, authority=self.authority, now=self._clock())
                        observation.update(status="valid", reason="Declaration checks passed; each operation still requires live authorization")
                    else:
                        observation.update(status="blocked", reason="No administrative operation and repository target are declared")
                except (ValueError, LookupError, AgentProfileError, AuthorizationError, AuthenticationError,
                        ControlPlaneBrokerDeniedError, DefinitionCompatibilityError, DefinitionConflictError) as exc:
                    observation.update(status="blocked", reason=str(exc))
                operations = [item for item in operations if item["id"] in profile.control_plane_operations]
                for item in operations:
                    if item["id"].startswith("repository.") or item["id"] == "action_intent.reconcile":
                        item["required_query_parameters"] = ["repository_resource_id"]
            except (ValueError, KeyError, LookupError, AttributeError,
                    DefinitionCompatibilityError, DefinitionConflictError) as exc:
                envelope["inspection_error"] = str(exc)
                operations = []
            context["declared_administrative_scope"] = envelope
        return {
            "enabled": enabled,
            "transport": "assignment-bound-unix-socket" if enabled else None,
            "project_id": assignment.project_id if enabled else None,
            "assignment_context": context,
            "operations": operations,
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
        reasoning = resolve_reasoning_operation(method, path)
        if reasoning is not None:
            operation, target = reasoning
            return _ResolvedOperation(operation=operation, target_ref=target,
                query=parse_qs(parsed.query, keep_blank_values=True))
        verification_operation = resolve_verification_operation(method, path)
        if verification_operation is not None:
            operation, target = verification_operation
            return _ResolvedOperation(operation=operation, target_ref=target,
                query=parse_qs(parsed.query, keep_blank_values=True))
        query = parse_qs(parsed.query, keep_blank_values=False)
        for operation in HOME_COORDINATION_OPERATIONS:
            if path == operation.path_template:
                if method != operation.method:
                    raise ControlPlaneBrokerRequestError('Unsupported Home coordination method')
                return _ResolvedOperation(operation=operation, target_ref=None,
                    query=parse_qs(parsed.query, keep_blank_values=True))
        if path == COORDINATION_PREFIX or path.startswith(COORDINATION_PREFIX + "/"):
            operation, target_ref = resolve_operation(method, path)
            return _ResolvedOperation(operation=operation, target_ref=target_ref,
                query=parse_qs(parsed.query, keep_blank_values=True))
        member_query = parse_qs(parsed.query, keep_blank_values=True)
        for key in ("member_path", "repository_resource_id"):
            if key in member_query:
                query[key] = member_query[key]
        if path.startswith('/api/operator/host/outputs/'):
            artifact_id = path.removeprefix('/api/operator/host/outputs/')
            if method != 'GET' or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', artifact_id):
                raise ControlPlaneBrokerRequestError('Host output requires GET and one exact artifact reference')
            return _ResolvedOperation(operation=HOST_OUTPUT_READ, target_ref=artifact_id,
                query=parse_qs(parsed.query, keep_blank_values=True))
        if path == HOST_COMMAND.path_template:
            if method != HOST_COMMAND.method or parsed.query:
                raise ControlPlaneBrokerRequestError('Host command requires POST without query parameters')
            return _ResolvedOperation(operation=HOST_COMMAND, target_ref=None, query={})
        if path == HOST_INSPECTION.path_template:
            if method != HOST_INSPECTION.method or parsed.query:
                raise ControlPlaneBrokerRequestError('Host inspection accepts only GET without query parameters')
            return _ResolvedOperation(operation=HOST_INSPECTION, target_ref=None, query={})
        if path == "/api/agent-upgrade-approvals":
            operation = _OPERATION_BY_ID["upgrade.approval.request"]
            if method != operation.method or parsed.query:
                raise ControlPlaneBrokerDeniedError("Only POST without query is allowed for agent Upgrade approval")
            return _ResolvedOperation(operation=operation, target_ref=None, query={})
        if path == "/api/work-graph/edges":
            operation = _OPERATION_BY_ID["work_graph.edge.create"]
            if method != operation.method:
                raise ControlPlaneBrokerDeniedError("method is not allowed for dependency creation")
            return _ResolvedOperation(operation=operation, target_ref=None, query=query)
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
        workspace_operation = {
            "/api/repository-workspace/refresh": "repository.workspace.refresh",
            "/api/repository-workspace/publication-recovery": (
                "repository.workspace.publication-recovery"
            ),
        }.get(path)
        if workspace_operation is not None:
            operation = _OPERATION_BY_ID[workspace_operation]
            if method != operation.method:
                raise ControlPlaneBrokerDeniedError(
                    f"method {method} is not allowed for {operation.id}"
                )
            return _ResolvedOperation(operation=operation, target_ref=None, query=query)
        repository_read_exact = {
            "/api/repository-facts": "repository.read.metadata",
            "/api/repository-facts/refs": "repository.read.refs",
            "/api/repository-facts/releases": "repository.read.releases",
            "/api/repository-facts/compare": "repository.read.compare",
            "/api/repository-facts/pull-requests": "repository.read.pull-requests",
        }
        operation_id = repository_read_exact.get(path)
        target_ref = None
        if operation_id is None:
            repository_read_prefixes = (
                (
                    "/api/repository-facts/jobs/",
                    "repository.read.job-logs",
                ),
                (
                    "/api/repository-facts/runs/",
                    "repository.read.artifacts",
                ),
                (
                    "/api/repository-facts/artifacts/",
                    "repository.read.artifact-download",
                ),
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
                required_suffix = {
                    "repository.read.job-logs": "/logs",
                    "repository.read.artifacts": "/artifacts",
                    "repository.read.artifact-download": "/download",
                }.get(candidate)
                if required_suffix is not None:
                    if not encoded_target.endswith(required_suffix):
                        continue
                    encoded_target = encoded_target.removesuffix(required_suffix)
                if candidate == "repository.read.pull-request" and encoded_target.endswith(
                    "/reviews"
                ):
                    candidate = "repository.read.reviews"
                    encoded_target = encoded_target.removesuffix("/reviews")
                target_ref = unquote(encoded_target).strip()
                if candidate == "repository.read.pull-request" and encoded_target.endswith('/merge-requirements'):
                    candidate = 'repository.read.merge-requirements'
                    target_ref = unquote(encoded_target.removesuffix('/merge-requirements')).strip()
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
        action_intent_prefix = "/api/action-intents/"
        action_intent_suffix = (
            "/delivery-evidence" if path.endswith("/delivery-evidence") else "/reconcile"
        )
        if path.startswith(action_intent_prefix) and path.endswith(
            action_intent_suffix
        ):
            intent_id = unquote(
                path[
                    len(action_intent_prefix) : -len(action_intent_suffix)
                ]
            ).strip("/")
            if not re.fullmatch(r"action-intent-[A-Za-z0-9]+", intent_id):
                raise ControlPlaneBrokerDeniedError("invalid action intent id")
            operation = _OPERATION_BY_ID[
                "repository.read.delivery-evidence"
                if action_intent_suffix == "/delivery-evidence" else "action_intent.reconcile"
            ]
            if method != operation.method:
                raise ControlPlaneBrokerDeniedError(
                    f"method is not allowed for {operation.id}"
                )
            return _ResolvedOperation(
                operation=operation,
                target_ref=intent_id,
                query=query,
            )
        local_deployment = {
            "/api/local-deployment": "deployment.local.status",
            "/api/local-deployment/install": "deployment.local.install",
        }.get(path)
        if local_deployment is not None:
            operation = _OPERATION_BY_ID[local_deployment]
            if method != operation.method:
                raise ControlPlaneBrokerDeniedError(
                    f"method {method} is not allowed for {operation.id}"
                )
            return _ResolvedOperation(operation=operation, target_ref=None, query=query)
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

    def _operation_repository_id(self, assignment, operation_id, query, *, actor=None):
        if assignment.execution_profile_id != "orchestration-admin":
            return (self._read_repository_id(assignment, query)
                    if operation_id.startswith("repository.read.")
                    else self._writable_repository_id(assignment))
        values = query.get("repository_resource_id")
        if values is None or len(values) != 1 or not values[0].strip():
            raise ControlPlaneBrokerRequestError("administrative operation requires one explicit repository_resource_id")
        if self.execution_profiles is None:
            raise ControlPlaneBrokerDeniedError("administrative execution capability is unavailable")
        selected = values[0].strip()
        requester = actor or self._requester_actor(assignment)
        try:
            self.execution_profiles.authorize_administrative_target(
                assignment, operation_id=operation_id, repository_id=selected,
                actor=requester, authority=self.authority,
            )
        except ValueError as exc:
            raise ControlPlaneBrokerDeniedError(str(exc)) from exc
        return selected

    @staticmethod
    def _writable_repository_id(assignment: ExecutionAssignment) -> str:
        if assignment.execution_profile_id not in {"repository-write", "project-governor"}:
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

    @classmethod
    def _read_repository_id(
        cls,
        assignment: ExecutionAssignment,
        query: dict[str, list[str]],
    ) -> str:
        primary = cls._writable_repository_id(assignment)
        scope = assignment.repository_scope
        if scope is not None and (
            scope.organization_id != assignment.organization_id
            or scope.workspace_id != assignment.workspace_id
            or scope.project_id != assignment.project_id
        ):
            raise ControlPlaneBrokerDeniedError("repository scope tenant/project mismatch")
        values = query.get("repository_resource_id")
        if values is None:
            return primary
        if len(values) != 1 or not values[0].strip():
            raise ControlPlaneBrokerRequestError(
                "repository_resource_id must occur once and be nonempty"
            )
        selected = values[0].strip()
        allowed = {primary}
        if scope is not None:
            allowed.update(scope.read_only_repository_ids)
        if selected not in allowed or selected not in assignment.resource_ids:
            raise ControlPlaneBrokerDeniedError(
                "repository fact target is outside assignment repository scope"
            )
        return selected

    @staticmethod
    def _repository_action_binding(
        *,
        action_registry,
        actor: AuthenticationActor,
        project_id: str | None,
        repository_id: str,
        action_id: str | None = None,
        code_host_registry=None,
        capability: CodeHostCapability | None = None,
    ):
        """Resolve the unique eligible provider binding for an assignment repository.

        Repository resources carry their provider boundary through the binding;
        the assignment broker must not assume that every repository is hosted
        by GitHub.  Ambiguous bindings remain a hard failure so a worker cannot
        choose a credential/provider implicitly.
        """
        bindings = []
        unsupported_provider = False
        supported_capability_seen = False
        for item in action_registry.list_bindings(actor):
            if (not item.enabled or repository_id not in item.resource_ids
                    or item.project_id not in {None, project_id}):
                continue
            try:
                provider = action_registry.provider(
                    item.provider_type, item.provider_instance, actor=actor,
                )
            except ActionProviderNotFoundError:
                continue
            if action_id is not None:
                if not any(definition.action_id == action_id and definition.capabilities.execute
                           for definition in provider.actions()):
                    continue
            elif capability is not None and code_host_registry is not None:
                try:
                    host = code_host_registry.provider(item.provider_type)
                except CodeHostError:
                    continue
                if capability not in host.capabilities():
                    unsupported_provider = True
                    continue
                supported_capability_seen = True
                if not str(getattr(provider, "api_base", "")).strip():
                    continue
            else:
                raise ControlPlaneBrokerDeniedError("repository provider capability is required")
            bindings.append(item)
        if len(bindings) != 1:
            if not bindings and unsupported_provider and not supported_capability_seen:
                raise ControlPlaneBrokerDeniedError(
                    f"repository provider does not support code-host capability {capability.value}; "
                    "another binding cannot implement an unsupported provider operation"
                )
            raise ControlPlaneBrokerDeniedError(
                "repository operation requires exactly one eligible enabled provider binding"
            )
        return bindings[0]

    def _read_home_output(self, assignment, artifact_id, worker_actor, *, kind, stream='stdout', maximum=4096):
        from codex_web.services.data_governance import DataGovernanceError
        from codex_web.services.crypto_keys import CryptoKeyError
        from codex_web.services.artifact_evidence import ArtifactEvidenceError
        from codex_web.services.definitions import DefinitionError
        from codex_web.services.identity import AuthorizationError
        try:
            if kind == 'released':
                return self.home_host_outputs.read(assignment, artifact_id, worker_actor=worker_actor)
            return self.home_host_outputs.read_source(assignment, artifact_id,
                worker_actor=worker_actor, stream=stream, max_bytes=maximum)
        except (DataGovernanceError, CryptoKeyError, ArtifactEvidenceError, DefinitionError, AuthorizationError) as error:
            raise ControlPlaneBrokerDeniedError('Current Home output is unavailable or remains protected; inspect the original command and output policy') from error

    async def _execute_repository_action(
        self,
        *,
        assignment: ExecutionAssignment,
        worker_actor,
        requester_actor,
        operation: ControlPlaneBrokerOperation,
        payload: dict[str, Any],
        repository_id: str | None = None,
    ) -> dict[str, Any]:
        if self.action_intents is None:
            raise ControlPlaneBrokerDeniedError(
                "governed repository actions are unavailable"
            )
        repository_id = repository_id or self._writable_repository_id(assignment)
        action_id = next(
            action_id
            for operation_id, action_id in _REPOSITORY_ACTIONS.values()
            if operation_id == operation.id
        )
        allowed_keys = set(_REPOSITORY_ACTION_PAYLOAD_FIELDS)
        unexpected = sorted(set(payload) - allowed_keys)
        if unexpected:
            raise ControlPlaneBrokerRequestError(
                "repository action contains unsupported fields: " + ", ".join(unexpected)
                + ". Put action fields inside the parameters JSON object; "
                "only parameters and idempotency_key are allowed at the top level."
            )
        parameters = payload.get("parameters", {})
        if not isinstance(parameters, dict):
            raise ControlPlaneBrokerRequestError("parameters must be a JSON object")
        if action_id == CODE_HOST_BRANCH_PUBLISH_ACTION_ID:
            parameters = {
                **parameters,
                "execution_workspace_id": assignment.execution_workspace_id,
            }
        intent_work_item_ref = assignment.work_item_ref
        if intent_work_item_ref is None and self.execution_workspaces and assignment.execution_workspace_id:
            recovery = await asyncio.to_thread(self.execution_workspaces.publication_recovery,
                assignment.execution_workspace_id, repository_id, actor=requester_actor)
            if recovery is not None:
                if recovery.assignment_id != assignment.id or recovery.assignment_fence != assignment.fence:
                    raise ControlPlaneBrokerDeniedError("publication recovery provenance does not match the live assignment fence")
                intent_work_item_ref = recovery.work_item_ref
        if (
            action_id == CODE_HOST_BRANCH_PUBLISH_ACTION_ID
            and parameters.get("existing_change_request_number") is not None
            and intent_work_item_ref is None
        ):
            if self.execution_workspaces is None or not assignment.execution_workspace_id:
                raise ControlPlaneBrokerDeniedError(
                    "existing change request recovery requires an execution workspace"
                )
            recovery = await asyncio.to_thread(
                self.execution_workspaces.publication_recovery,
                assignment.execution_workspace_id,
                repository_id,
                actor=requester_actor,
            )
            if recovery is None:
                raise ControlPlaneBrokerDeniedError(
                    "existing change request recovery requires active issue-scoped publication recovery"
                )
            if (
                recovery.assignment_id != assignment.id
                or recovery.assignment_fence != assignment.fence
                or recovery.source_execution_id != assignment.execution_id
                or recovery.source_subject != assignment.subject
                or recovery.authorized_by != requester_actor.identity_id
            ):
                raise ControlPlaneBrokerDeniedError(
                    "publication recovery provenance does not match the live assignment fence"
                )
            intent_work_item_ref = recovery.work_item_ref
        action_registry = self.action_intents.execution.registry
        binding = self._repository_action_binding(
            action_registry=action_registry,
            actor=requester_actor,
            project_id=assignment.project_id,
            repository_id=repository_id,
            action_id=action_id,
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
        # Publishing, MR creation, and merging may include provider-side checks. Give
        # those bounded operations enough time without letting the caller extend
        # execution or retry authority. The lease also covers receipt recording.
        extended_deadline = action_id in {
            CODE_HOST_BRANCH_PUBLISH_ACTION_ID,
            CODE_HOST_PULL_REQUEST_MERGE_ACTION_ID,
            CODE_HOST_PULL_REQUEST_UPSERT_ACTION_ID,
            CODE_HOST_PULL_REQUEST_READY_ACTION_ID,
        }
        deadline = {"timeout_seconds": 120.0} if extended_deadline else {}
        try:
            intent = await asyncio.to_thread(
                self.action_intents.create,
                ActionIntentCreate(
                    binding_id=binding.id,
                    request=request,
                    work_item_ref=intent_work_item_ref,
                    execution_id=assignment.execution_id,
                    **deadline,
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
                assignment_id=assignment.id,
            )
        except ActionIntentConflictError as exc:
            # Only CREATE conflicts are admission refusals. A same-key intent
            # may already have executed or be uncertain; do not imply replay
            # permission or translate failures from later claim/execution stages.
            raise ControlPlaneBrokerRequestError(
                "Repository action admission conflicts with origin, scope or idempotency. "
                "Inspect authorized intent and delivery evidence; an existing intent "
                "may have executed or be uncertain. Do not automatically retry or replay.",
                status_code=409,
                code="repository_action_admission_conflict",
            ) from exc
        return await self._claim_and_execute_repository_intent(
            intent.id, assignment=assignment, worker_actor=worker_actor,
            requester_actor=requester_actor, extended_deadline=extended_deadline,
        )

    async def _claim_and_execute_repository_intent(
        self, intent_id: str, *, assignment: ExecutionAssignment,
        worker_actor, requester_actor, extended_deadline: bool = True,
    ) -> dict[str, Any]:
        worker_id = f"control-plane-broker:{assignment.assigned_worker_id or 'worker'}"
        claimed = await asyncio.to_thread(
            self.action_intents.claim,
            ActionIntentClaimRequest(
                worker_id=worker_id,
                **({"lease_seconds": 180} if extended_deadline else {}),
            ),
            actor=worker_actor,
            intent_id=intent_id,
        )
        if claimed is None:
            current = await asyncio.to_thread(
                self.action_intents.get, intent_id, requester_actor
            )
            return {"item": current.model_dump(mode="json")}
        completed = await self.action_intents.execute_claimed(
            intent_id,
            worker_id,
            actor=worker_actor,
        )
        return {"item": completed.model_dump(mode="json")}

    def _code_host_binding(
        self,
        *,
        assignment: ExecutionAssignment,
        actor: AuthenticationActor,
        query: dict[str, list[str]] | None = None,
        operation_id: str = "repository.read.metadata",
    ) -> tuple[str, str]:
        if self.code_hosts is None or self.action_intents is None:
            raise ControlPlaneBrokerDeniedError(
                "governed repository reads are unavailable"
            )
        repository_id = self._operation_repository_id(
            assignment, operation_id, query or {},
        )
        # Protocol operation-to-capability mapping, never a mutable provider catalog.
        capability = {
            "repository.read.metadata": CodeHostCapability.REPOSITORY_READ,
            "repository.read.refs": CodeHostCapability.REFS_READ,
            "repository.read.commit": CodeHostCapability.COMMITS_READ,
            "repository.read.pull-request": CodeHostCapability.PULL_REQUEST_READ,
            "repository.read.pull-requests": CodeHostCapability.PULL_REQUEST_READ,
            "repository.read.reviews": CodeHostCapability.REVIEW_READ,
            "repository.read.merge-requirements": CodeHostCapability.MERGE_REQUIREMENTS_READ,
            "repository.read.checks": CodeHostCapability.CHECKS_READ,
            "repository.read.job-logs": CodeHostCapability.JOB_LOGS_READ,
            "repository.read.artifacts": CodeHostCapability.ARTIFACTS_READ,
            "repository.read.artifact-download": CodeHostCapability.ARTIFACTS_READ,
            "repository.read.releases": CodeHostCapability.RELEASE_READ,
            "repository.read.compare": CodeHostCapability.COMPARE_READ,
        }.get(operation_id)
        if capability is None:
            raise ControlPlaneBrokerDeniedError("unsupported repository fact capability")
        action_registry = self.action_intents.execution.registry
        action_binding = self._repository_action_binding(
            action_registry=action_registry,
            actor=actor,
            project_id=assignment.project_id,
            repository_id=repository_id,
            code_host_registry=self.code_hosts.registry,
            capability=capability,
        )
        action_provider = action_registry.provider(
            action_binding.provider_type,
            action_binding.provider_instance,
            actor=actor,
        )
        api_base = str(getattr(action_provider, "api_base", "")).strip()
        if not api_base:
            raise ControlPlaneBrokerDeniedError(
                "code-host action provider does not expose its configured API base"
            )
        binding_id = f"control-plane-read:{action_binding.id}"
        self.code_hosts.registry.register_binding(
            CodeHostProviderBinding(
                id=binding_id,
                organization_id=assignment.organization_id,
                workspace_id=assignment.workspace_id,
                provider_type=action_binding.provider_type,
                provider_instance=action_binding.provider_instance,
                base_url=api_base,
                credential_ref=action_binding.credential_ref,
                capabilities=tuple(self.code_hosts.registry.provider(action_binding.provider_type).capabilities()),
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
            query=query,
            operation_id=operation.id,
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
        if operation.id == "repository.read.pull-requests":
            try:
                state = normalize_pull_request_state((query.get("state") or ["open"])[0])
            except CodeHostError as exc:
                raise ControlPlaneBrokerRequestError(str(exc), code="invalid_pull_request_state") from None
            items = await self.code_hosts.pull_requests(
                binding_id,
                repository_id,
                actor=actor,
                state=state,
            )
            return {"items": [item.model_dump(mode="json") for item in items]}
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
        if operation.id in {
            "repository.read.job-logs",
            "repository.read.artifacts",
            "repository.read.artifact-download",
        }:
            assert target_ref is not None
            if not target_ref.isdigit() or int(target_ref) < 1:
                raise ControlPlaneBrokerRequestError(
                    "Code-host job, run, and artifact identifiers must be positive integers"
                )
            external_id = int(target_ref)
            max_bytes_raw = (query.get("max_bytes") or [str(128 * 1024)])[0]
            try:
                max_bytes = int(max_bytes_raw)
            except ValueError as exc:
                raise ControlPlaneBrokerRequestError("max_bytes must be an integer") from exc
            if max_bytes < 1 or max_bytes > 320 * 1024:
                raise ControlPlaneBrokerRequestError(
                    "max_bytes must be between 1 and 327680"
                )
            if operation.id == "repository.read.job-logs":
                item = await self.code_hosts.job_logs(
                    binding_id, repository_id, external_id,
                    actor=actor, max_bytes=max_bytes,
                )
                return {"item": item.model_dump(mode="json")}
            if operation.id == "repository.read.artifacts":
                items = await self.code_hosts.artifacts(
                    binding_id, repository_id, external_id, actor=actor
                )
                return {"items": [item.model_dump(mode="json") for item in items]}
            options = {}
            if "member_path" in query:
                if len(query["member_path"]) != 1:
                    raise ControlPlaneBrokerRequestError("member_path must occur once")
                options["member_path"] = query["member_path"][0]
            item = await self.code_hosts.artifact_download(
                binding_id, repository_id, external_id,
                actor=actor, max_bytes=max_bytes, **options,
            )
            return {"item": item.model_dump(mode="json")}
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

    def _ready_declaration(self, assignment):
        if self.execution_profiles is None:
            raise ControlPlaneBrokerDeniedError("Readiness requires an explicit current execution-profile declaration")
        try:
            profile, reference = self.execution_profiles.resolve(assignment.execution_profile_id,
                organization_id=assignment.organization_id, workspace_id=assignment.workspace_id,
                project_id=assignment.project_id)
        except Exception as exc:
            raise ControlPlaneBrokerDeniedError("Readiness execution-profile declaration is unavailable") from exc
        if (reference != assignment.execution_profile_definition
                or "repository.pull-request.ready" not in profile.control_plane_operations):
            raise ControlPlaneBrokerDeniedError("Readiness is not explicitly declared by the current pinned execution profile")

    def _authorize(
        self,
        assignment: ExecutionAssignment,
        operation: ControlPlaneBrokerOperation,
        *,
        actor,
        resource_ids: tuple[str, ...],
    ):
        if operation.id == "repository.pull-request.ready":
            self._ready_declaration(assignment)
        if assignment.execution_profile_id not in CONTROL_PLANE_EXECUTION_PROFILES:
            raise ControlPlaneBrokerDeniedError(
                "assignment execution profile does not permit control-plane broker access"
            )
        if not assignment.project_id:
            raise ControlPlaneBrokerDeniedError(
                "control-plane broker requires an assignment project"
            )
        if operation.id.startswith("project.") or assignment.execution_profile_id == "project-governor":
            if self.project_coordination is None:
                raise ControlPlaneBrokerDeniedError("project coordination adapters unavailable")
            self.project_coordination.authorize(assignment, operation, self._requester_actor(assignment))
        if operation.id.startswith('operator.host.') or operation.id.startswith('operator.project.'):
            if self.home_operator_host is None:
                raise ControlPlaneBrokerDeniedError('Home operator inspection is unavailable')
            self.home_operator_host.authorize(assignment, actor)
        if assignment.execution_profile_id == "orchestration-admin":
            if self.execution_profiles is None:
                raise ControlPlaneBrokerDeniedError("administrative execution capability is unavailable")
            requester = self._requester_actor(assignment)
            try:
                profile, _ = self.execution_profiles.resolve(
                    assignment.execution_profile_id, organization_id=assignment.organization_id,
                    workspace_id=assignment.workspace_id, project_id=assignment.project_id,
                )
                targets = resource_ids or profile.administrative_repository_ids[:1]
                if not targets:
                    raise ValueError("administrative target envelope is empty")
                for target in targets:
                    self.execution_profiles.authorize_administrative_target(
                        assignment, operation_id=operation.id, repository_id=target,
                        actor=requester, authority=self.authority,
                    )
            except ValueError as exc:
                raise ControlPlaneBrokerDeniedError(str(exc)) from exc
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

    def _dispatch_scope(
        self,
        assignment: ExecutionAssignment,
        worker_actor: AuthenticationActor,
        resolved: _ResolvedOperation,
    ) -> tuple[AuthenticationActor, AuthenticationActor, Any, Any]:
        # Preserve identity -> target scope -> authority ordering as one
        # synchronous boundary. to_thread carries the caller's ContextVars.
        operation = resolved.operation
        actor = self._actor(assignment, worker_actor)
        repository_read = operation.id.startswith("repository.read.")
        if ("repository_resource_id" in resolved.query and not repository_read
                and not operation.id.startswith("project.")
                and assignment.execution_profile_id != "orchestration-admin"):
            raise ControlPlaneBrokerRequestError(
                "repository_resource_id is only supported for repository fact reads"
            )
        requester_actor = (
            self._requester_actor(assignment)
            if assignment.execution_profile_id in {"orchestration-admin", "project-governor"}
            or operation.id.startswith("repository.")
            or operation.id.startswith("action_intent.")
            or operation.id.startswith("deployment.local.")
            or operation.id == "control_plane.operations.list"
            or operation.id.startswith("project.")
            or operation.id.startswith("verification.")
            or operation.id.startswith(("procedure.", "executive."))
            or operation.id == "upgrade.approval.request"
            or operation.id.startswith('operator.host.')
            or operation.id.startswith('operator.project.')
            else actor
        )
        authority_actor = actor if repository_read else requester_actor
        state = None
        resource_ids: tuple[str, ...] = ()
        if operation.id.startswith("project.") or operation.id.startswith("verification.") or operation.id == "upgrade.approval.request":
            resource_ids = (self._writable_repository_id(assignment),)
        elif (
            operation.id.startswith("repository.")
            or operation.id.startswith("action_intent.")
            or operation.id.startswith("deployment.local.")
        ):
            resource_ids = (
                self._operation_repository_id(
                    assignment, operation.id, resolved.query, actor=requester_actor
                ),
            )
        elif operation.id.startswith(("procedure.", "executive.")):
            resource_ids = tuple(assignment.resource_ids)
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

        return actor, requester_actor, state, authority_decision

    def _read_delivery_evidence(
        self, assignment: ExecutionAssignment, requester_actor: AuthenticationActor,
        intent_id: str | None, query: dict[str, list[str]],
    ) -> dict[str, Any]:
        if self.action_intents is None or intent_id is None:
            raise ControlPlaneBrokerDeniedError("delivery evidence is unavailable")
        intent = self.action_intents.get(intent_id, requester_actor)
        if intent.action_id == CODE_HOST_PULL_REQUEST_READY_ACTION_ID:
            self._ready_declaration(assignment)
            if (intent.execution_id != assignment.execution_id or intent.readiness_origin is None
                    or intent.readiness_origin.assignment_id != assignment.id
                    or intent.readiness_origin.fence != assignment.fence):
                raise ControlPlaneBrokerDeniedError("Readiness receipts belong to a different originating assignment")
        repository_id = self._operation_repository_id(
            assignment, "repository.read.delivery-evidence", query, actor=requester_actor
        )
        if (
            intent.action_id not in _DELIVERY_EVIDENCE_REPOSITORY_ACTION_IDS
            or intent.organization_id != assignment.organization_id
            or intent.workspace_id != assignment.workspace_id
            or intent.project_id != assignment.project_id
            or tuple(intent.resource_ids) != (repository_id,)
            or intent.requested_by != requester_actor.identity_id
        ):
            raise ControlPlaneBrokerDeniedError("delivery evidence is outside assignment scope")
        history = self.action_intents.history(intent_id, requester_actor)
        original = history["intent"]
        def select(row: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
            return {key: row.get(key) for key in fields}
        # Metadata only: never expose arbitrary request/provider/inbox bodies.
        # Eight recent rows bound inspection; original receipt IDs remain exact.
        result = {
            "intent": select(original, ("id", "action_id", "status", "attempt",
                "organization_id", "workspace_id", "project_id", "resource_ids",
                "work_item_ref", "execution_id", "execution_started_at", "completed_at",
                "last_receipt_id", "last_verification_id")),
            "parameters": select((original.get("request") or {}).get("parameters") or {},
                ("execution_workspace_id", "branch", "head_revision", "expected_remote_revision",
                 "expected_remote_absent", "work_item_issue_number", "existing_change_request_number")),
            "receipts": [select(row, ("id", "intent_id", "attempt", "outcome",
                "provider_external_id", "received_at")) for row in history["receipts"][-8:]],
            "verifications": [select(row, ("id", "intent_id", "verified", "verified_at"))
                for row in history["verifications"][-8:]],
            "omitted_receipts": max(0, len(history["receipts"]) - 8),
            "omitted_verifications": max(0, len(history["verifications"]) - 8),
        }
        if intent.action_id == CODE_HOST_PULL_REQUEST_READY_ACTION_ID:
            result["intent"]["readiness_origin"] = intent.readiness_origin.model_dump(mode="json")
            result["parameters"] = select(intent.request.parameters,
                ("pull_request_number", "expected_head_sha", "expected_base_sha", "expected_base_ref"))
        return result

    async def dispatch(self, *, assignment, worker_actor, method, raw_target, body, assignment_validator=None):
        from codex_web.procedure_reconciliation import read_only_reconciliation, NORMAL_PROCEDURE_HISTORY
        scope = None
        resolved = self._resolve_operation(method, raw_target)
        if (resolved.operation.id == 'action_intent.reconcile' and self.action_intents is not None
                and len(getattr(assignment, 'procedure_discoveries', ())) >= NORMAL_PROCEDURE_HISTORY):
            # Reuse current canonical delivery-scope checks before any reserve
            # is available. Client JSON cannot supply a recovery witness.
            _, requester, _, _ = await asyncio.to_thread(self._dispatch_scope, assignment, worker_actor, resolved)
            try:
                payload = json.loads(body) if body else {}
                request = ActionIntentReconcileRequest.model_validate(payload)
            except (ValueError, TypeError):
                payload = None
            if isinstance(payload, dict) and payload.get('retry_if_idempotent') is False:
                intent = await asyncio.to_thread(self.action_intents.get, resolved.target_ref, requester)
                repository_id = self._operation_repository_id(assignment, resolved.operation.id, resolved.query, actor=requester)
                if (intent.action_id == CODE_HOST_PULL_REQUEST_READY_ACTION_ID
                        and intent.requested_by == requester.identity_id
                        and (intent.organization_id, intent.workspace_id, intent.project_id)
                            == (assignment.organization_id, assignment.workspace_id, assignment.project_id)
                        and tuple(intent.resource_ids) == (repository_id,)):
                    scope = await asyncio.to_thread(self.action_intents.read_only_reconciliation_scope,
                        intent, request, worker_actor, assignment=assignment)
        with read_only_reconciliation(scope):
            return await self._dispatch_current(assignment=assignment, worker_actor=worker_actor,
                method=method, raw_target=raw_target, body=body, assignment_validator=assignment_validator)

    async def _dispatch_current(
        self,
        *,
        assignment: ExecutionAssignment,
        worker_actor: AuthenticationActor,
        method: str,
        raw_target: str,
        body: bytes,
        assignment_validator: Callable[[], ExecutionAssignment] | None = None,
    ) -> tuple[int, dict[str, Any], ControlPlaneBrokerOperation, str | None, Any]:
        resolved = self._resolve_operation(method, raw_target)
        operation = resolved.operation
        repository_read = operation.id.startswith("repository.read.")
        actor, requester_actor, state, authority_decision = (
            await asyncio.to_thread(
                self._dispatch_scope, assignment, worker_actor, resolved
            )
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

        if self.procedure_admission is not None:
            procedure_targets = tuple(getattr(state, "resource_ids", ()) or assignment.resource_ids)
            from codex_web.procedure_reconciliation import current_reconciliation_scope
            recovery = current_reconciliation_scope()
            if recovery is not None:
                procedure_targets = recovery.resource_ids
            if operation.id == "work_graph.edge.create":
                source_state = self._state_for_target(assignment, str(payload.get("source_ref") or ""))
                target_state = self._state_for_target(assignment, str(payload.get("target_ref") or ""))
                procedure_targets = tuple(dict.fromkeys((*source_state.resource_ids, *target_state.resource_ids)))
            await self.procedure_admission(assignment=assignment, actor=requester_actor,
                operation=operation, resource_ids=procedure_targets, payload=payload)

        if assignment.execution_profile_id == "orchestration-admin":
            # Administrative coordination never impersonates an implementer or
            # independent validator. These are agent-role fields, distinct from
            # the authenticated requester/worker identities used above.
            own_role = assignment.agent_profile.role_id
            attributed_field = ("from_agent" if operation.id == "work_item.handoff"
                                else "actor" if operation.id in {
                                    "work_item.acknowledge", "work_item.progress"} else None)
            if attributed_field:
                if payload.get(attributed_field) not in {None, own_role}:
                    raise ControlPlaneBrokerDeniedError("administrative coordination must use its own agent Role attribution")
                payload[attributed_field] = own_role

        if operation.id.startswith(("procedure.", "executive.")):
            if self.assignment_reasoning is None:
                raise ControlPlaneBrokerDeniedError("assignment reasoning adapters unavailable")
            result = await self.assignment_reasoning.dispatch(assignment, worker_actor,
                operation, resolved.target_ref, payload, resolved.query, assignment_validator)
            if isinstance(result.get("item"), dict):
                resolved = _ResolvedOperation(operation, result["item"]["id"], resolved.query)
        elif operation.id == OUTPUT_READ:
            if (body or self.home_host_outputs is None or set(resolved.query) - {'kind', 'stream', 'max_bytes'}
                    or any(len(v) != 1 or not v[0] for v in resolved.query.values())):
                raise ControlPlaneBrokerRequestError('Host output requires a bounded read without a request body')
            kind = resolved.query.get('kind', ['source'])[0]
            if kind == 'released':
                if set(resolved.query) - {'kind'}:
                    raise ControlPlaneBrokerRequestError('Released excerpts accept no range override')
                result = await run_settled(lambda: self._read_home_output(
                    assignment, resolved.target_ref, worker_actor, kind='released'))
            elif kind == 'source':
                stream = resolved.query.get('stream', ['stdout'])[0]
                try:
                    maximum = int(resolved.query.get('max_bytes', ['4096'])[0])
                except ValueError as error:
                    raise ControlPlaneBrokerRequestError('Invalid output byte bound') from error
                if stream not in {'stdout', 'stderr'} or not 1 <= maximum <= 65536:
                    raise ControlPlaneBrokerRequestError('Invalid output stream or byte bound')
                result = await run_settled(lambda: self._read_home_output(
                    assignment, resolved.target_ref, worker_actor, kind='source', stream=stream, maximum=maximum))
            else:
                raise ControlPlaneBrokerRequestError('Unknown output read kind')
        elif operation.id == HOST_COMMAND.id:
            result = await execute_host_command(self, assignment, worker_actor, payload)
        elif operation.id.startswith('operator.project.'):
            adapter = self.home_operator_coordination
            if adapter is None:
                raise ControlPlaneBrokerDeniedError('Home coordination is unavailable')
            if operation.id == 'operator.project.threads.list':
                if body or set(resolved.query) - {'project_id', 'limit', 'cursor'} or any(
                        len(v) != 1 or not v[0] for v in resolved.query.values()) or 'project_id' not in resolved.query:
                    raise ControlPlaneBrokerRequestError('Home inspection requires one explicit target Project and no body')
                result = await run_settled(lambda: adapter.inspect(assignment_id=assignment.id,
                    fence=assignment.fence, project_id=resolved.query['project_id'][0], query=resolved.query))
            else:
                if resolved.query:
                    raise ControlPlaneBrokerRequestError('Home delegation accepts no query parameters')
                result = await adapter.message(assignment_id=assignment.id, fence=assignment.fence,
                    payload=payload, worker_actor=worker_actor)
        elif operation.id == HOST_INSPECTION.id:
            if body:
                raise ControlPlaneBrokerRequestError('Host inspection accepts no request body')
            result = await run_settled(lambda: self.home_operator_host.inspect(
                assignment, worker_actor, resolved, assignment_validator))
        elif operation.id == "upgrade.approval.request":
            from codex_web.services.worker_upgrade_approvals import WorkerUpgradeApprovalAdapter
            result = await WorkerUpgradeApprovalAdapter(self).request(
                assignment, worker_actor, resolved, payload, assignment_validator)
        elif operation.id.startswith("verification."):
            adapter = WorkerVerificationAdapter(self)
            if operation.id == 'verification.create':
                if resolved.query:
                    raise ControlPlaneBrokerRequestError('Verification write does not accept query parameters')
                from codex_web.services.worker_mr_verifications import METHOD, CONTINUATION_METHOD, WorkerMRVerificationAdapter
                if payload.get('method') in (METHOD, CONTINUATION_METHOD):
                    result = await WorkerMRVerificationAdapter(adapter).create(
                        assignment, worker_actor, resolved, payload, assignment_validator)
                else:
                    result = await asyncio.to_thread(adapter.create, assignment, worker_actor,
                        resolved, payload, assignment_validator)
            else:
                if payload:
                    raise ControlPlaneBrokerRequestError('Verification read does not accept a request body')
                result = await asyncio.to_thread(adapter.read, assignment, worker_actor,
                    resolved, assignment_validator)
        elif operation.id.startswith("project."):
            result = await self.project_coordination.dispatch(assignment, operation,
                resolved.target_ref, resolved.query, payload, assignment_validator, actor)
        elif operation.id == "work_graph.edge.create":
            if self.work_graph is None or assignment.execution_profile_id != "orchestration-admin":
                raise ControlPlaneBrokerDeniedError("dependency administration requires an enabled administrative profile")
            from codex_web.work_graph import WorkGraphEdgeCreate
            edge_request = WorkGraphEdgeCreate.model_validate(payload)
            source = self._state_for_target(assignment, edge_request.source_ref)
            target = self._state_for_target(assignment, edge_request.target_ref)
            self._authorize(assignment, operation, actor=requester_actor,
                            resource_ids=tuple(dict.fromkeys((*source.resource_ids, *target.resource_ids))))
            edge = await asyncio.to_thread(
                self.work_graph.add_edge, edge_request, scope=requester_actor.tenant,
                actor_id=requester_actor.identity_id, project_id=assignment.project_id,
            )
            result = {"edge": edge.model_dump(mode="json")}
        elif operation.id == "control_plane.operations.list":
            result = self.public_assignment_capability(assignment)
        elif operation.id == "deployment.local.status":
            result = self._local_deployment_status()
        elif operation.id == "deployment.local.install":
            result = self._schedule_local_deployment(
                payload,
                actor=requester_actor,
            )
        elif operation.id == "repository.read.delivery-evidence":
            if payload:
                raise ControlPlaneBrokerRequestError("delivery evidence read accepts no body")
            result = await asyncio.to_thread(
                self._read_delivery_evidence, assignment, requester_actor,
                resolved.target_ref, resolved.query,
            )
        elif operation.id == 'repository.read.merge-requirements':
            if body:
                raise ControlPlaneBrokerRequestError("Merge-requirement reads accept no body")
            from codex_web.services.broker_merge_requirements import read_merge_requirements
            result = await read_merge_requirements(self, assignment, worker_actor, resolved, assignment_validator)
        elif repository_read:
            try:
                result = await self._read_repository_fact(
                    assignment=assignment,
                    actor=actor,
                    operation=operation,
                    target_ref=resolved.target_ref,
                    query=resolved.query,
                )
            except CodeHostError as exc:
                provider_status = getattr(exc, "provider_http_status", None)
                if type(provider_status) is int and 400 <= provider_status <= 599:
                    message = f"Governed repository read returned provider HTTP {provider_status}."
                    code = "repository_read_provider_error"
                elif getattr(exc, "transport_failure", False) is True:
                    message = "Governed repository read transport failed."
                    code = "repository_read_transport_error"
                else:
                    raise  # Unknown/internal failures retain their original classification.
                # A hidden/inaccessible repository can also return404. This is
                # never an absence witness or permission to publish/retry.
                raise ControlPlaneBrokerRequestError(
                    message + " Remote absence and permission are unproven; no retry is authorized.",
                    status_code=409, code=code,
                ) from None
        elif operation.id == "repository.workspace.refresh":
            if self.execution_workspaces is None or not assignment.execution_workspace_id:
                raise ControlPlaneBrokerDeniedError(
                    "assignment execution workspace refresh is unavailable"
                )
            if set(payload) - {"revision"}:
                raise ControlPlaneBrokerRequestError(
                    "workspace refresh accepts only revision"
                )
            try:
                current_workspace = await run_settled(lambda: self.execution_workspaces.get(
                    assignment.execution_workspace_id, requester_actor
                ))
            except ExecutionWorkspaceNotFoundError as exc:
                raise ControlPlaneBrokerRequestError(
                    str(exc), status_code=404
                ) from exc
            if (
                current_workspace.execution_id != assignment.execution_id
                or current_workspace.project_id != assignment.project_id
                or tuple(current_workspace.writable_repository_ids)
                != (self._writable_repository_id(assignment),)
            ):
                raise ControlPlaneBrokerDeniedError(
                    "execution workspace is outside assignment scope"
                )
            requested_revision = str(payload.get("revision") or "").strip().casefold()
            materialization = None

            async def refresh(materialization=None):
                cancelled = threading.Event()

                def validate_current():
                    if cancelled.is_set():
                        raise ExecutionWorkspaceConflictError("workspace refresh cancelled")
                    if assignment_validator is not None:
                        assignment_validator()

                return await run_settled(lambda: self.execution_workspaces.refresh_revision(
                    assignment.execution_workspace_id, requested_revision,
                    actor=requester_actor, materialization=materialization,
                    validate_current=validate_current,
                ), on_cancel=cancelled.set)

            try:
                refreshed = await refresh()
            except ExecutionWorkspaceNotFoundError as exc:
                if not re.fullmatch(r"[0-9a-f]{40}", requested_revision):
                    raise ControlPlaneBrokerRequestError(
                        str(exc), status_code=404
                    ) from exc
                if self.code_hosts is None:
                    raise ControlPlaneBrokerRequestError(
                        "provider revision materialization is unavailable",
                        status_code=409,
                    ) from exc
                repository_id = self._writable_repository_id(assignment)
                member = next(
                    (
                        item for item in current_workspace.repository_members
                        if item.resource_id == repository_id
                        and item.access_mode.value == "write"
                    ),
                    None,
                )
                if member is None:
                    raise ControlPlaneBrokerDeniedError(
                        "assignment writable repository is not the workspace writable member"
                    ) from exc
                binding_id, bound_repository_id = self._code_host_binding(
                    assignment=assignment,
                    actor=actor,
                )
                if bound_repository_id != repository_id:
                    raise ControlPlaneBrokerDeniedError(
                        "provider repository binding is outside assignment scope"
                    ) from exc
                try:
                    disk_used = getattr(current_workspace, "actual_disk_bytes", None)
                    if disk_used is None:
                        raise CodeHostError(
                            "workspace disk usage unavailable; reconcile accounting before hydration"
                        )
                    materialization = (
                        await self.code_hosts.materialize_default_branch_revision(
                            binding_id,
                            repository_id,
                            requested_revision,
                            checkout=Path(member.workspace_path),
                            actor=actor,
                            disk_budget_bytes=max(0, assignment.limits.disk_bytes
                                - disk_used),
                            disk_quota_bytes=assignment.limits.disk_bytes,
                        )
                    )
                except CodeHostTransientError as materialization_exc:
                    raise ControlPlaneBrokerRequestError(
                        str(materialization_exc), status_code=502,
                        history_readiness={"state": "unavailable", "target_revision": requested_revision,
                            "local_revision": current_workspace.head_revision,
                            "next_action": "reconcile_provider_and_retry_refresh", "deadline_seconds": 120,
                            "maximum_depth": 4096}
                    ) from materialization_exc
                except CodeHostError as materialization_exc:
                    raise ControlPlaneBrokerRequestError(
                        str(materialization_exc), status_code=409,
                        history_readiness={"state": "not_ready", "target_revision": requested_revision,
                            "local_revision": current_workspace.head_revision,
                            "next_action": "inspect_history_bound_or_destination_refusal",
                            "deadline_seconds": 120, "maximum_depth": 4096}
                    ) from materialization_exc
                if assignment_validator is not None:
                    await asyncio.to_thread(assignment_validator)
                if self.procedure_admission is not None:
                    await self.procedure_admission(assignment=assignment, actor=requester_actor,
                        operation=operation, resource_ids=assignment.resource_ids, payload=payload)
                try:
                    refreshed = await refresh(materialization)
                except ExecutionWorkspaceNotFoundError as refresh_exc:
                    raise ControlPlaneBrokerRequestError(
                        "materialized revision is unavailable in the execution workspace",
                        status_code=409,
                    ) from refresh_exc
                except ExecutionWorkspaceConflictError as refresh_exc:
                    raise ControlPlaneBrokerRequestError(
                        str(refresh_exc), status_code=409
                    ) from refresh_exc
                except ExecutionWorkspaceBackendError as refresh_exc:
                    raise ControlPlaneBrokerRequestError(str(refresh_exc)) from refresh_exc
            except ExecutionWorkspaceConflictError as exc:
                raise ControlPlaneBrokerRequestError(
                    str(exc), status_code=409
                ) from exc
            except ExecutionWorkspaceBackendError as exc:
                raise ControlPlaneBrokerRequestError(str(exc)) from exc
            result = {
                "item": refreshed.model_dump(mode="json"),
                "materialization": (
                    {
                        "required": True,
                        "outcome": (
                            "materialized"
                            if materialization.fetched
                            else "already_materialized"
                        ),
                        **materialization.model_dump(mode="json"),
                    }
                    if materialization is not None
                    else {
                        "required": False,
                        "outcome": "already_present",
                        "revision": requested_revision,
                    }
                ),
            }
        elif (operation.id == "repository.workspace.publication-recovery"
              and "expected_prior_scope" in payload):
            handoff_request = PublicationRecoveryHandoffRequest.model_validate(payload)
            if (handoff_request.expected_assignment_id != assignment.id
                    or handoff_request.expected_assignment_fence != assignment.fence
                    or handoff_request.execution_workspace_id != assignment.execution_workspace_id):
                raise ControlPlaneBrokerRequestError("handoff assignment/workspace/fence precondition is stale", status_code=409)
            if assignment_validator is not None:
                await asyncio.to_thread(assignment_validator)
            try:
                result = await PublicationRecoveryHandoffService(self).handoff(handoff_request, actor=requester_actor)
            except PublicationRecoveryConflict as exc:
                raise ControlPlaneBrokerRequestError(str(exc), status_code=409) from exc
        elif operation.id == "repository.workspace.publication-recovery":
            if self.execution_workspaces is None or not assignment.execution_workspace_id:
                raise ControlPlaneBrokerDeniedError(
                    "assignment publication recovery is unavailable"
                )
            if set(payload) != {"work_item_ref"}:
                raise ControlPlaneBrokerRequestError(
                    "publication recovery requires only work_item_ref"
                )
            work_item_ref = str(payload.get("work_item_ref") or "").strip()
            if not work_item_ref or len(work_item_ref) > 500:
                raise ControlPlaneBrokerRequestError(
                    "publication recovery work_item_ref is invalid"
                )
            state = await asyncio.to_thread(
                self._state_for_target, assignment, work_item_ref
            )
            repository_id = self._writable_repository_id(assignment)
            if repository_id not in tuple(state.resource_ids):
                raise ControlPlaneBrokerDeniedError(
                    "publication recovery work item does not own the writable repository"
                )
            profile = assignment.agent_profile
            owner_ids = {
                value
                for value in (
                    getattr(profile, "profile_id", None),
                    getattr(profile, "role_id", None),
                )
                if value
            }
            if not owner_ids or getattr(state, "current_owner", None) not in owner_ids:
                raise ControlPlaneBrokerDeniedError(
                    "publication recovery requires the current Work Item implementation owner"
                )
            if getattr(state, "closed_at", None) is not None or getattr(
                state, "current_stage", None
            ) == "closed":
                raise ControlPlaneBrokerDeniedError(
                    "publication recovery Work Item is closed"
                )
            if assignment.lease is None or assignment.fence < 1:
                raise ControlPlaneBrokerDeniedError(
                    "publication recovery requires a live fenced assignment"
                )
            try:
                recovered = await asyncio.to_thread(
                    self.execution_workspaces.authorize_publication_recovery,
                    assignment.execution_workspace_id,
                    work_item_ref=work_item_ref,
                    repository_resource_id=repository_id,
                    assignment_id=assignment.id,
                    assignment_fence=assignment.fence,
                    source_execution_id=assignment.execution_id,
                    source_subject=assignment.subject,
                    assignment_deadline_at=assignment.deadline_at,
                    actor=requester_actor,
                )
            except (
                ExecutionWorkspaceBackendError,
                ExecutionWorkspaceConflictError,
                ExecutionWorkspaceNotFoundError,
            ) as exc:
                raise ControlPlaneBrokerRequestError(str(exc), status_code=409) from exc
            result = {"item": recovered.model_dump(mode="json")}
        elif operation.id.startswith("repository."):
            result = await self._execute_repository_action(
                assignment=assignment,
                worker_actor=actor,
                requester_actor=requester_actor,
                operation=operation,
                payload=payload,
                repository_id=self._operation_repository_id(
                    assignment, operation.id, resolved.query, actor=requester_actor
                ),
            )
        elif operation.id == "action_intent.reconcile":
            if self.action_intents is None:
                raise ControlPlaneBrokerDeniedError(
                    "action-intent reconciliation is unavailable"
                )
            assert resolved.target_ref is not None
            intent = self.action_intents.get(
                resolved.target_ref,
                requester_actor,
            )
            repository_id = self._operation_repository_id(
                assignment, operation.id, resolved.query, actor=requester_actor
            )
            reconcile_request = ActionIntentReconcileRequest.model_validate(
                payload
            )
            if (
                intent.action_id not in _RECONCILABLE_REPOSITORY_ACTION_IDS
                or intent.organization_id != assignment.organization_id
                or intent.workspace_id != assignment.workspace_id
                or intent.project_id != assignment.project_id
                or tuple(intent.resource_ids) != (repository_id,)
                or intent.requested_by != requester_actor.identity_id
            ):
                raise ControlPlaneBrokerDeniedError(
                    "action intent is outside the assignment delivery scope"
                )
            if intent.action_id == CODE_HOST_PULL_REQUEST_READY_ACTION_ID:
                self._ready_declaration(assignment)
                if (intent.execution_id != assignment.execution_id or intent.readiness_origin is None
                        or intent.readiness_origin.assignment_id != assignment.id
                        or intent.readiness_origin.fence != assignment.fence or reconcile_request.retry_if_idempotent):
                    raise ControlPlaneBrokerDeniedError("Readiness permits only own-assignment read-only reconciliation")
            # A recovered thread receives a fresh execution assignment. It must
            # still be able to verify and settle an uncertain repository action
            # from its superseded assignment, otherwise a service restart
            # permanently strands the governed delivery. Historical intents
            # remain non-retryable here: only the exact originating execution
            # may ask the provider to repeat a mutation.
            if (
                intent.execution_id != assignment.execution_id
                and reconcile_request.retry_if_idempotent
            ):
                raise ControlPlaneBrokerDeniedError(
                    "a recovered assignment may reconcile but not retry a "
                    "historical action intent"
                )
            item = await self.action_intents.reconcile(
                intent.id,
                reconcile_request,
                actor=actor,
            )
            result = {"item": item.model_dump(mode="json")}
            if (reconcile_request.retry_if_idempotent
                    and intent.execution_id == assignment.execution_id
                    and item.status == ActionIntentStatus.PENDING):
                # Requeue alone has no provider effect. An explicitly authorized
                # current-origin retry must cross the normal claim/execute path.
                result = await self._claim_and_execute_repository_intent(
                    intent.id, assignment=assignment, worker_actor=actor,
                    requester_actor=requester_actor,
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
            raw_limit = (resolved.query.get("limit") or [None])[0]
            try:
                limit = int(raw_limit) if raw_limit is not None else None
            except ValueError as exc:
                raise ControlPlaneBrokerRequestError(
                    "work-item list limit must be an integer"
                ) from exc
            result = await self.work_items.list(
                project_id=assignment.project_id,
                owner=(resolved.query.get("owner") or [None])[0],
                stage=(resolved.query.get("stage") or [None])[0],
                q=(resolved.query.get("q") or [None])[0],
                limit=limit,
                cursor=(resolved.query.get("cursor") or [None])[0],
                release_gate=None,
                scope=actor.tenant,
            )
        elif operation.id == "work_item.read":
            assert resolved.target_ref is not None
            assert state is not None
            result = await self.work_items.get(resolved.target_ref)
            result["owner_dispatch"] = await asyncio.to_thread(
                self.operator.owner_dispatch_projection,
                state,
            )
            authoritative_source = await self.operator.source_detail(
                resolved.target_ref
            )
            result["authoritative_source"] = authoritative_source
            snapshot = authoritative_source.get("snapshot") or {}
            next_action = result.get("next_action")
            result["assigned_scope"] = {
                "source": (
                    "canonical_next_action"
                    if next_action
                    else "authoritative_task_source"
                ),
                "next_action": next_action,
                "title": snapshot.get("title"),
                "body_text": (
                    None if next_action else snapshot.get("body_text")
                ),
                "source_identity": snapshot.get("identity"),
            }
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
        elif operation.id == "work_item.steer":
            assert resolved.target_ref is not None
            request = WorkItemOwnerDispatchRequest.model_validate(payload)
            result = await self.operator.steer(
                resolved.target_ref,
                expected_owner=request.expected_owner,
                expected_thread_id=request.expected_thread_id,
                **({'expected_definition': request.expected_definition} if request.expected_definition is not None else {}),
                idempotency_key=request.idempotency_key,
                actor=actor.identity_id,
            )
        elif operation.id == "work_item.retry":
            assert resolved.target_ref is not None
            result = await self.operator.retry(
                resolved.target_ref,
                actor=actor.identity_id,
                reason=(str(payload["reason"]) if payload.get("reason") else None),
            )
        elif operation.id == "work_item.projection_retry":
            from codex_web.api.work_items import WorkItemProjectionRetry
            from codex_web.services.task_source_projection import TaskSourceProjectionError
            request = WorkItemProjectionRetry.model_validate(payload)
            def check_projection_authority():
                current_assignment = assignment_validator() if assignment_validator else assignment
                self._dispatch_scope(current_assignment, worker_actor, resolved)
            check_projection_authority()
            try:
                await self.work_items.task_source_writeback.retry_projection(
                    state, expected_revision=request.expected_revision, actor=actor.identity_id,
                    authorization_guard=check_projection_authority,
                )
            except TaskSourceProjectionError as exc:
                raise ControlPlaneBrokerRequestError(exc.reason, status_code=409) from exc
            result = {"ok": True, **await asyncio.to_thread(self.operator.detail, resolved.target_ref)}
        elif operation.id == "work_item.reconcile":
            assert resolved.target_ref is not None
            result = await self.operator.reconcile(
                resolved.target_ref,
                actor=actor.identity_id,
                reason=(str(payload["reason"]) if payload.get("reason") else None),
            )
        else:  # pragma: no cover - closed operation catalog
            raise ControlPlaneBrokerDeniedError("unsupported control-plane operation")

        encoded = jsonable_encoder(result)
        if not isinstance(encoded, dict):
            encoded = {"result": encoded}
        if operation.id.startswith("work_item."):
            coordination_view = (
                operation.id == "work_item.read"
                and resolved.query.get("view") == ["coordination"]
            )
            # Retained history must not turn an already committed ACK into
            # an apparent failure. Leave room for the HTTP/broker receipt.
            oversized = len(json.dumps(encoded, ensure_ascii=False).encode()) > (
                self.limits.max_response_bytes - 2048
            )
            if coordination_view or oversized:
                encoded = self._coordination_response(encoded)
        return 200, encoded, operation, resolved.target_ref, authority_decision

    @staticmethod
    def _coordination_response(payload: dict[str, Any]) -> dict[str, Any]:
        """Project existing canonical state; never trim live routing facts."""
        result = dict(payload)
        wrapped = isinstance(result.get("item"), dict)
        state = dict(result["item"] if wrapped else result)
        omitted = []
        for field in ("notes", "execution", "handoff_history"):
            if field in state:
                omitted.append(field)
                state.pop(field)
        # Keep the latest actual handoff outcomes, including ACK timestamps,
        # without replaying every prior handoff or checkpoint context.
        history = (payload.get("item") if wrapped else payload).get(
            "handoff_history", []
        )
        if history:
            state["recent_handoffs"] = history[-3:]
        source = state.get("authoritative_source")
        if isinstance(source, dict) and "discussion" in source:
            state["authoritative_source"] = {
                key: value for key, value in source.items() if key != "discussion"
            }
            omitted.append("authoritative_source.discussion")
        state["_projection"] = {
            "view": "coordination",
            "omitted_fields": omitted,
            "history_retained_in_canonical_state": True,
        }
        if wrapped:
            result["item"] = state
            return result
        return state

    @staticmethod
    def _local_deployment_paths() -> tuple[Path, Path, Path]:
        source = Path(
            os.environ.get(
                "CODEX_WEB_LOCAL_DEPLOY_SOURCE",
                "/home/nbingester/worktrees/codex-web-canonical-main",
            )
        )
        live = Path(
            os.environ.get(
                "CODEX_WEB_LOCAL_DEPLOY_LIVE",
                "/home/nbingester/codex-web-live-9990e20",
            )
        )
        helper = Path(__file__).resolve().parents[2] / "scripts" / "local_deploy.py"
        return source, live, helper

    @staticmethod
    def _git_revision(path: Path, revision: str = "HEAD") -> str | None:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--verify", revision],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout.strip() if result.returncode == 0 else None

    def _local_deployment_status(self) -> dict[str, Any]:
        source, live, helper = self._local_deployment_paths()
        return {
            "project_id": "c58dccd0df3a",
            "source_path": str(source),
            "live_path": str(live),
            "source_commit": self._git_revision(source),
            "available_commit": self._git_revision(source, "origin/main"),
            "live_commit": self._git_revision(live),
            "service_instance_id": self.service_instance_id,
            "qualified_install_required": True,
            "helper_available": helper.is_file(),
            "log_path": "/home/nbingester/codex-web-native-runtime/local-deploy.log",
        }

    def local_installation_process(self, plan):
        tracked = self._local_installers.get(plan.id)
        if not tracked:
            return {"status": "unknown", "reason": "No live tracked installer handle"}
        process, instance, source, target = tracked
        if (instance != self.service_instance_id or instance != plan.service_instance_id
                or source != plan.deployment_source_revision or target != plan.deployment_target_revision):
            return {"status": "unknown", "reason": "Tracked installer scope or instance is stale"}
        result = process.poll()
        return {"status": "running" if result is None else "exited",
                "process_id": process.pid, "exit_code": result}

    def _qualify_local_deployment(self, payload: dict[str, Any], *, actor):
        if self.upgrades is None or not self.service_instance_id:
            raise ControlPlaneBrokerDeniedError(
                "canonical local deployment qualification is unavailable"
            )
        required = {
            "candidate_revision",
            "upgrade_plan_id",
            "preflight_evidence_id",
            "source_revision",
            "service_instance_id",
        }
        missing = sorted(required - set(payload))
        if missing:
            raise ControlPlaneBrokerRequestError(
                "local deployment is missing fields: " + ", ".join(missing)
            )
        candidate = str(payload["candidate_revision"]).strip()
        source_revision = str(payload["source_revision"]).strip()
        if not re.fullmatch(r"[0-9a-f]{40}", candidate):
            raise ControlPlaneBrokerRequestError(
                "candidate_revision must be an exact 40-character lowercase Git SHA"
            )
        if not re.fullmatch(r"[0-9a-f]{40}", source_revision):
            raise ControlPlaneBrokerRequestError(
                "source_revision must be an exact 40-character lowercase Git SHA"
            )
        service_instance_id = str(payload["service_instance_id"]).strip()
        if service_instance_id != self.service_instance_id:
            raise ControlPlaneBrokerDeniedError(
                "local deployment service instance is stale"
            )
        return self.upgrades.qualify_local_deployment(
            str(payload["upgrade_plan_id"]),
            actor=actor,
            service_instance_id=service_instance_id,
            source_revision=source_revision,
            target_revision=candidate,
            preflight_evidence_id=str(payload["preflight_evidence_id"]),
        )

    def _serve_local_deployment_guard(
        self,
        channel: socket.socket,
        payload: dict[str, Any],
        *,
        actor,
    ) -> None:
        try:
            channel.settimeout(900)
            request = channel.recv(32)
            if request != b"verify\n":
                raise ControlPlaneBrokerDeniedError(
                    "local deployment helper did not request canonical verification"
                )
            source, live, _helper = self._local_deployment_paths()
            candidate = str(payload["candidate_revision"])
            source_revision = str(payload["source_revision"])
            if self._git_revision(source, "origin/main") != candidate:
                raise ControlPlaneBrokerDeniedError(
                    "candidate changed before local deployment switch"
                )
            if self._git_revision(live) != source_revision:
                raise ControlPlaneBrokerDeniedError(
                    "live source changed before local deployment switch"
                )
            qualification = self._qualify_local_deployment(payload, actor=actor)
            response = {"ok": True, "qualification": qualification}
        except Exception as exc:
            response = {"ok": False, "error": str(exc)[:500]}
        try:
            channel.sendall(json.dumps(response).encode("utf-8") + b"\n")
        finally:
            channel.close()

    def _schedule_local_deployment(
        self,
        payload: dict[str, Any],
        *,
        actor,
    ) -> dict[str, Any]:
        allowed = {
            "candidate_revision",
            "upgrade_plan_id",
            "preflight_evidence_id",
            "source_revision",
            "service_instance_id",
        }
        unexpected = sorted(set(payload) - allowed)
        if unexpected:
            raise ControlPlaneBrokerRequestError(
                "local deployment contains unsupported fields: " + ", ".join(unexpected)
            )
        qualification = self._qualify_local_deployment(payload, actor=actor)
        candidate = str(payload["candidate_revision"])
        source_revision = str(payload["source_revision"])
        source, live, helper = self._local_deployment_paths()
        if not helper.is_file():
            raise ControlPlaneBrokerDeniedError("local deployment helper is unavailable")
        log_path = Path("/home/nbingester/codex-web-native-runtime/local-deploy.log")
        log_handle = log_path.open("a", encoding="utf-8")
        parent_channel, child_channel = socket.socketpair()
        try:
            try:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        str(helper),
                        "--candidate",
                        candidate,
                        "--source",
                        str(source),
                        "--live",
                        str(live),
                        "--expected-live",
                        source_revision,
                        "--guard-fd",
                        str(child_channel.fileno()),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    close_fds=True,
                    pass_fds=(child_channel.fileno(),),
                    env={**os.environ, "CODEX_WEB_UPGRADE_PLAN_ID": str(payload["upgrade_plan_id"])},
                )
            except Exception:
                parent_channel.close()
                raise
        finally:
            child_channel.close()
            log_handle.close()
        threading.Thread(
            target=self._serve_local_deployment_guard,
            args=(parent_channel, dict(payload)),
            kwargs={"actor": actor},
            name=f"local-deployment-guard-{process.pid}",
            daemon=True,
        ).start()
        self._local_installers[str(payload["upgrade_plan_id"])] = (
            process, self.service_instance_id, source_revision, candidate)
        return {
            "accepted": True,
            "candidate_revision": candidate,
            "process_id": process.pid,
            "qualification": qualification,
            "status_href": "/api/local-deployment",
            "log_path": str(log_path),
        }

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
        self._stopping = False
        self._client_writers: set[asyncio.StreamWriter] = set()
        self._handler_tasks: set[asyncio.Task[None]] = set()

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
            self._accept_client,
            path=str(self.socket_path),
        )
        os.chmod(self.socket_path, 0o600)
        return self

    def _accept_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # Register synchronously, before the handler's first scheduled step, so
        # shutdown also owns clients accepted immediately before close().
        if self._stopping:
            writer.transport.abort()
            return
        self._client_writers.add(writer)
        task = asyncio.create_task(self._handle(reader, writer))
        self._handler_tasks.add(task)
        task.add_done_callback(self._handler_tasks.discard)

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
        if getattr(assignment,'resource_context',None)!=getattr(self.snapshot,'resource_context',None):
            raise ControlPlaneBrokerDeniedError('assignment immutable Resource context changed')
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
            504: "Gateway Timeout",
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
        denial_code = "control_plane_broker_denied"
        outcome_unknown = False
        history_readiness = None
        procedure_discovery = None
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
            assignment = await asyncio.to_thread(self._validate_current)
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
            if self._stopping:
                raise ControlPlaneBrokerDeniedError("control-plane broker is stopping")
            resolved = self.service._resolve_operation(method, target)
            operation = resolved.operation
            target_ref = resolved.target_ref
            actor = await asyncio.to_thread(
                self.service._actor, assignment, self.worker_actor
            )
            actor_identity_id = actor.identity_id
            if self._stopping:
                raise ControlPlaneBrokerDeniedError("control-plane broker is stopping")

            status, payload, operation, target_ref, authority_decision = (
                await self.service.dispatch(
                    assignment=assignment,
                    worker_actor=self.worker_actor,
                    method=method,
                    raw_target=target,
                    body=body,
                    assignment_validator=self._validate_current,
                )
            )
            await run_settled(lambda: self.service._audit(
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
            ))
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
            history_readiness = getattr(exc, "history_readiness", None)
            status = exc.status_code
            denial_reason = str(exc)
            denial_code = getattr(exc, "code", denial_code)
            procedure_discovery = getattr(exc, "procedure_discovery", None)
        except ControlPlaneBrokerDeniedError as exc:
            denial_reason = str(exc)
            if status == 500:
                if "rate limit" in denial_reason or "concurrency limit" in denial_reason:
                    status = 429
                elif "method " in denial_reason:
                    status = 405
                else:
                    status = 403
        except asyncio.TimeoutError:
            if operation is None:
                status = 400
                denial_reason = "incomplete or timed-out control-plane request"
            else:
                # An operation is resolved only after the complete body was
                # received. A downstream timeout cannot prove no mutation ran.
                status = 504
                denial_code = "control_plane_operation_timeout"
                denial_reason = "control-plane operation timed out; outcome is unknown; reconcile without retry"
                outcome_unknown = True
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            status = 400
            denial_reason = "incomplete or timed-out control-plane request"
        except Exception as exc:
            status = 500
            denial_reason = f"control-plane broker internal error: {type(exc).__name__}"
        finally:
            if denial_reason is not None:
                with contextlib.suppress(Exception):
                    await run_settled(lambda: self.service._audit(
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
                        denial_reason=(json.dumps({"message": denial_reason[:80],
                            "history_readiness": history_readiness}) if history_readiness else denial_reason),
                    ))
                if not writer.is_closing():
                    with contextlib.suppress(Exception):
                        await self._write(
                            writer,
                            status,
                            {
                                "error": {
                                    "code": denial_code,
                                    **({"outcome": "unknown", "retryable": False} if outcome_unknown else {}),
                                    **({"procedure_discovery": procedure_discovery} if procedure_discovery is not None else {}),
                                    **({"history_readiness": history_readiness} if history_readiness else {}),
                                    "message": denial_reason[:500],
                                    "correlation_id": correlation_id,
                                }
                            },
                            correlation_id=correlation_id,
                        )
            if counted:
                self._active_requests = max(0, self._active_requests - 1)
            try:
                if not writer.is_closing():
                    writer.close()
                # close() can still be flushing buffered output. Keep this
                # transport owned until closure so stop() can abort it too.
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
            except BaseException:
                writer.transport.abort()
                raise
            finally:
                self._client_writers.discard(writer)

    async def stop(self) -> None:
        self._stopping = True
        server = self.server
        self.server = None
        if server is not None:
            server.close()
        # wait_closed() includes accepted connections on current Python. An
        # incomplete request or a peer not reading its response must not keep
        # a fenced worker's teardown and subsequent admission locked forever.
        # Only retire transports: existing ActionIntent outcomes remain owned
        # by their canonical execution/reconciliation paths.
        for writer in tuple(self._client_writers):
            writer.transport.abort()
        if server is not None:
            await server.wait_closed()
        self._client_writers.clear()
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
