"""Hard fencing for recovery scopes, using existing canonical records only."""
from __future__ import annotations

import time
from codex_web.execution_workspaces import (
    ExecutionWorkspace, ExecutionWorkspaceLease, PublicationRecoveryIntentScope,
)
from codex_web.execution_workers import ExecutionAssignment, ExecutionWorker
from codex_web.storage.execution_workspaces import ExecutionWorkspaceStateStore
from codex_web.storage.execution_workers import ExecutionWorkerStore


class PublicationRecoveryConflict(ValueError):
    """A stale or incomplete scope needs observation, never replay."""


WS = ExecutionWorkspaceStateStore.workspace_namespace
LEASES = ExecutionWorkspaceStateStore.lease_namespace
ASSIGNMENTS = ExecutionWorkerStore.assignment_namespace
WORKERS = ExecutionWorkerStore.worker_namespace


def scope_defaults():
    return {WS: {}, LEASES: {}, ASSIGNMENTS: {}, WORKERS: {}}


def scope_rows(assignment, workspace):
    if assignment is None or workspace is None:
        raise PublicationRecoveryConflict("Canonical scope rows are missing")
    return {WS: (workspace.id,), LEASES: (workspace.lease_id,),
            ASSIGNMENTS: (assignment.id,), WORKERS: (assignment.assigned_worker_id,)}


def live_scope(documents, assignment_id, workspace_id, actor_id, *, now=None):
    now = time.time() if now is None else now
    raw_assignment = documents[ASSIGNMENTS].get(assignment_id)
    raw_workspace = documents[WS].get(workspace_id)
    if raw_assignment is None or raw_workspace is None:
        raise PublicationRecoveryConflict("Recovery assignment or workspace is missing; inspect canonical ownership")
    assignment = ExecutionAssignment.model_validate(raw_assignment)
    workspace = ExecutionWorkspace.model_validate(raw_workspace)
    raw_worker = documents[WORKERS].get(assignment.assigned_worker_id)
    worker = ExecutionWorker.model_validate(raw_worker) if raw_worker else None
    if (worker is None or worker.lifecycle not in {"active", "draining"}
            or worker.organization_id != assignment.organization_id or worker.workspace_id != assignment.workspace_id
            or not set(assignment.required_capabilities).issubset(worker.capabilities)
            or assignment.execution_contract_version not in worker.supported_execution_contract_versions
            or assignment.sandbox not in worker.supported_sandbox_profiles):
        raise PublicationRecoveryConflict("Recovery worker is missing, quarantined or incompatible; inspect canonical worker state")
    raw_lease = documents[LEASES].get(workspace.lease_id)
    lease = ExecutionWorkspaceLease.model_validate(raw_lease) if raw_lease else None
    if (assignment.status not in {"running", "claimed"} or assignment.lease is None
            or assignment.lease.expires_at <= now or assignment.lease.fence != assignment.fence
            or assignment.lease.worker_id != assignment.assigned_worker_id
            or (assignment.deadline_at is not None and assignment.deadline_at <= now)):
        raise PublicationRecoveryConflict("Recovery assignment lease/fence is stale; inspect the live assignment")
    if (workspace.status != "active" or lease is None or not lease.active
            or lease.execution_workspace_id != workspace.id
            or lease.execution_id != workspace.execution_id or lease.subject != workspace.subject
            or lease.owner_identity_id != actor_id or workspace.owner_identity_id != actor_id
            or assignment.created_by != actor_id
            or assignment.execution_workspace_id != workspace.id
            or assignment.execution_id != workspace.execution_id or assignment.subject != workspace.subject
            or assignment.organization_id != workspace.organization_id
            or assignment.workspace_id != workspace.workspace_id or assignment.project_id != workspace.project_id
            or lease.organization_id != workspace.organization_id or lease.workspace_id != workspace.workspace_id
            or assignment.execution_profile_id != "repository-write"
            or assignment.execution_profile_definition is None or assignment.agent_profile is None):
        raise PublicationRecoveryConflict("Recovery owner, tenant, Project, execution or profile is incompatible; inspect canonical scope")
    writable = (assignment.repository_scope.writable_repository_ids if assignment.repository_scope else
                ((assignment.repository_target.mutable_repository_id,) if assignment.repository_target else ()))
    if tuple(writable) != tuple(workspace.writable_repository_ids) or len(writable) != 1:
        raise PublicationRecoveryConflict("Recovery requires the same singular writable repository")
    repository_id = writable[0]
    if lease.resource_modes.get(repository_id) != "write":
        raise PublicationRecoveryConflict("Recovery repository has no live write lease")
    recovery = workspace.publication_recovery
    if recovery is not None and (recovery.expires_at <= now
            or recovery.assignment_id != assignment.id or recovery.assignment_fence != assignment.fence
            or recovery.repository_resource_id != repository_id):
        raise PublicationRecoveryConflict("Recovery scope is expired or fenced; inspect current scope")
    return assignment, workspace, recovery


def intent_guard(documents, *, assignment_id, workspace_id, actor_id, expected_scope,
                 expected_profile, created_at=None):
    assignment, workspace, current = live_scope(documents, assignment_id, workspace_id, actor_id)
    if assignment.execution_profile_definition != expected_profile:
        raise PublicationRecoveryConflict("Recovery execution profile pin changed")
    expected = expected_scope.recovery if expected_scope else None
    if current != expected:
        raise PublicationRecoveryConflict("ActionIntent recovery scope is retired or changed; do not replay; inspect its receipts")
    # Legacy unscoped records never gain the authority of a later recovery scope.
    if created_at is not None and workspace.publication_recovery_history and expected_scope is None:
        raise PublicationRecoveryConflict("Legacy ActionIntent predates the current recovery scope; inspect historical receipts")
    return workspace


def execution_assignment(workers, execution_id, actor, assignment_id=None):
    if assignment_id is None:
        return workers.assignment_for_execution(execution_id, actor=actor)
    assignment = workers.store.assignment(assignment_id)
    if (assignment is None or assignment.execution_id != execution_id
            or assignment.organization_id != actor.organization_id or assignment.workspace_id != actor.workspace_id):
        raise PublicationRecoveryConflict("Exact originating assignment is unavailable or outside actor scope")
    return assignment


def capture_scope(workers, request, execution_id, actor, assignment_id=None):
    """Snapshot an existing recovery scope and fence its concurrent retirement."""
    if workers is None or not execution_id or not request.action_id.startswith("code-host."):
        return None
    assignment = execution_assignment(workers, execution_id, actor, assignment_id)
    if getattr(assignment, "execution_profile_id", None) != "repository-write" or assignment.work_item_ref is not None:
        return None
    workspace_id = assignment.execution_workspace_id
    if not workspace_id:
        return None
    store = workers.store.store
    # Point inspection is followed by a transaction that rechecks these exact rows.
    workspace_raw = store.record_get(WS, workspace_id)
    if not workspace_raw or not workspace_raw.get("publication_recovery"):
        return None
    workspace = ExecutionWorkspace.model_validate(workspace_raw)
    defaults = scope_rows(assignment, workspace)
    documents = {namespace: store.record_get_many(namespace, keys) for namespace, keys in defaults.items()}
    assignment, workspace, recovery = live_scope(documents, assignment.id, workspace_id, actor.identity_id)
    if (request.organization_id != workspace.organization_id or request.workspace_id != workspace.workspace_id
            or request.project_id != workspace.project_id
            or tuple(request.resource_ids) != (recovery.repository_resource_id,)):
        raise PublicationRecoveryConflict("ActionIntent is outside publication recovery tenant/Project/repository scope")
    scope = PublicationRecoveryIntentScope(execution_workspace_id=workspace_id, recovery=recovery,
                                         execution_profile_definition=assignment.execution_profile_definition)
    def guard(current):
        intent_guard(current, assignment_id=assignment.id, workspace_id=workspace_id,
                     actor_id=actor.identity_id, expected_scope=scope,
                     expected_profile=assignment.execution_profile_definition)
    return scope, defaults, guard
