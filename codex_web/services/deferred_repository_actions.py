from __future__ import annotations

import asyncio
import itertools
import time

from codex_web.action_intents import ActionIntentClaimRequest, ActionIntentStatus
from codex_web.execution_workers import AssignmentStatus, WorkerLifecycle
from codex_web.services.control_plane_broker import _REPOSITORY_ACTIONS, ControlPlaneBrokerDeniedError
from codex_web.services.execution_workers import AssignmentNotFoundError, WorkerConflictError
from codex_web.services.identity import AuthenticationError, AuthorizationError
from codex_web.services.execution_workspaces import ExecutionWorkspaceNotFoundError


class DeferredRepositoryActionRecovery:
    """Consume never-started broker actions using their exact live origin only."""

    def __init__(self, *, intents, workers, broker, actor, worker_id):
        self.intents = intents
        self.workers = workers
        self.broker = broker
        self.actor = actor
        self.worker_id = worker_id

    def _origin(self, intent):
        assignment = self.workers.assignment_for_execution(
            intent.execution_id, actor=self.actor,
        )
        now = time.time()
        lease = assignment.lease
        if (assignment.status not in {AssignmentStatus.CLAIMED, AssignmentStatus.RUNNING}
                or assignment.assigned_worker_id != self.worker_id
                or lease is None or lease.worker_id != self.worker_id
                or lease.fence != assignment.fence or lease.expires_at <= now
                or (assignment.deadline_at is not None and assignment.deadline_at <= now)):
            raise ValueError("originating assignment is no longer live")
        if (intent.organization_id != assignment.organization_id
                or intent.workspace_id != assignment.workspace_id
                or intent.project_id != assignment.project_id
                or intent.requested_by != assignment.created_by
                or tuple(intent.resource_ids) != (self.broker._writable_repository_id(assignment),)):
            raise ValueError("originating assignment delivery scope changed")
        path = next(path for path, (_, action) in _REPOSITORY_ACTIONS.items()
                    if action == intent.action_id)
        resolved = self.broker._resolve_operation("POST", path)
        _, requester, _, _ = self.broker._dispatch_scope(assignment, self.actor, resolved)
        if intent.action_id == "code-host.branch.publish":
            workspace_id = intent.request.parameters.get("execution_workspace_id")
            if not workspace_id or workspace_id != assignment.execution_workspace_id:
                raise ValueError("publication workspace differs from live origin")
            workspace = self.broker.execution_workspaces.get(workspace_id, requester)
            if (workspace.status != "active" or workspace.execution_id != intent.execution_id
                    or workspace.owner_identity_id != assignment.created_by
                    or workspace.project_id != assignment.project_id
                    or tuple(workspace.writable_repository_ids) != tuple(intent.resource_ids)):
                raise ValueError("publication workspace is no longer active in origin scope")
        return assignment

    async def run_once(self):
        # The normal claim/execute boundary still enforces maintenance, policy,
        # approvals, resource leases, credentials, CAS and provider verification.
        state = await asyncio.to_thread(self.workers.store.load)
        worker = next((item for item in state.workers if item.id == self.worker_id), None)
        if worker is None or worker.lifecycle != WorkerLifecycle.ACTIVE:
            return
        pending = await asyncio.to_thread(lambda: list(itertools.islice(
            self.intents.store.indexed_intents(
                "pending", organization_id=self.actor.organization_id,
                workspace_id=self.actor.workspace_id,
            ), 256)))
        handled = 0
        actions = {action for _, action in _REPOSITORY_ACTIONS.values()}
        for intent in pending:
            if (intent.status != ActionIntentStatus.PENDING or intent.attempt != 0
                    or intent.lease is not None or intent.action_id not in actions
                    or intent.policy_decision.source != "policy:assignment-control-plane"
                    or not intent.execution_id
                    or (intent.not_before is not None and intent.not_before > time.time())):
                continue
            if handled >= 8:
                break
            handled += 1
            try:
                await asyncio.to_thread(self._origin, intent)
            except (ValueError, KeyError, AssignmentNotFoundError, WorkerConflictError,
                    ControlPlaneBrokerDeniedError, AuthenticationError, AuthorizationError,
                    ExecutionWorkspaceNotFoundError) as exc:
                await asyncio.to_thread(
                    self.intents.require_recovery_if_unstarted, intent.id,
                    actor=self.actor,
                    reason=("Deferred delivery requires a newly authorized live assignment; "
                            "preserve the local commit and reconcile this request before "
                            "publishing a replacement. " + type(exc).__name__),
                )
                continue
            claimed = await asyncio.to_thread(
                self.intents.claim,
                ActionIntentClaimRequest(worker_id=f"control-plane-broker:{self.worker_id}",
                                         lease_seconds=180),
                actor=self.actor, intent_id=intent.id,
            )
            if claimed is not None:
                await self.intents.execute_claimed(
                    intent.id, f"control-plane-broker:{self.worker_id}", actor=self.actor,
                )
