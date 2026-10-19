"""Transactional pre-resume custody validation and guarded post-resume projection.

This is a control-plane lifecycle hook, never a worker/API rebind operation.
Historical assignments and RuntimeUsage are not mutated here.
"""
from __future__ import annotations

import hashlib
import time

from codex_web.agent_runtime import AgentSession, AgentSessionStatus
from codex_web.execution_workers import ExecutionAssignment, ExecutionWorker, WorkerEvent
from codex_web.execution_workspaces import ExecutionWorkspace, ExecutionWorkspaceLease
from codex_web.identity import TenantScope
from codex_web.services.agent_runtime import AgentRuntimeError
from codex_web.thread_bootstrap import ThreadBootstrapBindingState


class AgentSessionCustodyPrestartRefusal(AgentRuntimeError):
    """Controller-owned refusal, raised before invoking the provider adapter."""

    def __init__(self, session, previous, assignment):
        super().__init__("Native session repository custody differs from the requested scope")
        self.detail = {
            "code": "native_session_scope_incompatible", "message": str(self),
            "phase": "before_provider_resume", "agent_session_id": session.id,
            "previous_assignment_id": previous.id, "assignment_id": assignment.id,
            "assignment_fence": assignment.fence, "execution_id": assignment.execution_id,
            "execution_workspace_id": assignment.execution_workspace_id,
            "original_resource_ids": list(previous.resource_ids),
            "requested_resource_ids": list(assignment.resource_ids),
            "original_repository_scope": previous.repository_scope.model_dump(mode="json") if previous.repository_scope else None,
            "requested_repository_scope": assignment.repository_scope.model_dump(mode="json") if assignment.repository_scope else None,
            "remediation": "Inspect fresh bootstrap recovery for this exact retained original.",
        }


class AgentSessionCustodyService:
    def __init__(self, sessions, workers, bindings, workspaces, profiles, *, clock=time.time):
        self.sessions, self.workers, self.bindings = sessions, workers, bindings
        self.workspaces, self.profiles, self.clock = workspaces, profiles, clock
        self.store = sessions.store.store
        if any(v.store is not self.store for v in (
            workers.store, bindings.store, workspaces.store,
        )):
            raise ValueError("session custody requires one canonical transactional store")

    @staticmethod
    def require(condition, reason):
        if not condition:
            raise AgentRuntimeError("agent session custody: " + reason)

    def capture(self, thread_id, adapter, worker_session, request, fence):
        """Capture the CAS input before awaiting the actual provider resume."""
        assignment = worker_session.validate_current()
        session = self.sessions.store.find_unique_by_native_id(
            thread_id, provider_id=adapter.provider_id, runtime_id=adapter.runtime_id,
        )
        self.require(session is not None, "missing or ambiguous canonical native session")
        self.require(
            (session.organization_id, session.workspace_id, session.project_id) ==
            (assignment.organization_id, assignment.workspace_id, request.project_id),
            "native session scope differs from admitted execution",
        )
        self.require(assignment.id == request.assignment_id and assignment.fence == fence,
                     "admitted assignment changed before resume")
        self._transition((session, assignment), thread_id, adapter, worker_session,
            request, fence, None, prepare_only=True)
        return session, assignment

    def complete(self, expected, thread_id, adapter, worker_session, request, fence, result):
        """Called only after resume succeeds, before any turn input is sent.

        Cross-domain row/namespace locks are the existing StateStore primitive.
        Never update the aggregate compatibility mirror or historical usage.
        """
        return self._transition(expected, thread_id, adapter, worker_session, request, fence, result)

    def _transition(self, expected, thread_id, adapter, worker_session, request, fence, result, *, prepare_only=False):
        expected, admitted = expected
        if not prepare_only:
            self.require(result.provider_native_session_id in {None, thread_id},
                         "resume returned another native session identity")
        snapshot = worker_session.validate_current()
        self.require(snapshot.id == request.assignment_id, "worker owns another assignment")
        actor = self.workers.identity.actor_for_identity(snapshot.created_by, scope=TenantScope(
            organization_id=snapshot.organization_id, workspace_id=snapshot.workspace_id,
        ))
        workspace = self.workspaces.store.workspace(request.execution_workspace_id)
        self.require(workspace is not None, "execution workspace unavailable")
        ss, ws, bs, xs = self.sessions.store, self.workers.store, self.bindings.store, self.workspaces.store
        # Initialize canonical record collections before entering their writer transaction.
        ss._ensure_records()
        ws._ensure_records()
        xs._ensure_records()
        session_key = ss._session_key(expected.id)
        event_id = "workerevent-session-custody-" + hashlib.sha256(
            f"{expected.id}:{snapshot.id}:{fence}".encode()
        ).hexdigest()
        selected = {
            ss.records_namespace: (session_key,),
            ws.assignment_namespace: tuple(dict.fromkeys(
                v for v in (snapshot.id, expected.assignment_id) if v)),
            ws.worker_namespace: (request.worker_id,),
            ws.event_namespace: (event_id,),
            bs.namespace: (None,),
            xs.workspace_namespace: (workspace.id,),
            xs.lease_namespace: (workspace.lease_id,),
            # These existing document namespaces are held against concurrent
            # identity/profile/Definition changes while their services recheck.
            self.workers.identity.store.namespace: (None,),
            self.profiles.registry.store.namespace: (None,),
        }
        if self.profiles.agent_profiles is not None:
            selected[self.profiles.agent_profiles.store.namespace] = (None,)
        updated = []

        def apply(rows):
            current = AgentSession.model_validate(rows[ss.records_namespace][session_key])
            self.require(current == expected, "canonical session changed during resume")
            self.require(self.sessions.store.find_unique_by_native_id(
                thread_id, provider_id=adapter.provider_id, runtime_id=adapter.runtime_id,
            ) == current, "native session mapping became ambiguous")
            assignment = ExecutionAssignment.model_validate(rows[ws.assignment_namespace][snapshot.id])
            self.require(all(getattr(assignment, key) == getattr(admitted, key) for key in (
                "id", "organization_id", "workspace_id", "project_id", "created_by",
                "execution_id", "execution_workspace_id", "assigned_worker_id", "fence",
                "subject", "resource_ids", "repository_scope", "repository_target",
                "runtime_binding", "agent_profile", "execution_profile_id", "execution_profile_definition",
            )), "admitted execution provenance changed during resume")
            worker = ExecutionWorker.model_validate(rows[ws.worker_namespace][request.worker_id])
            now = self.clock()
            tenant = (current.organization_id, current.workspace_id)
            live_actor = self.workers.identity.actor_for_identity(assignment.created_by,
                scope=TenantScope(organization_id=tenant[0], workspace_id=tenant[1]))
            self.require(
                (assignment.organization_id, assignment.workspace_id) == tenant ==
                (worker.organization_id, worker.workspace_id) ==
                (actor.organization_id, actor.workspace_id), "foreign tenant custody",
            )
            self.require(
                assignment.status == "running" and worker.lifecycle == "active"
                and assignment.id == request.assignment_id
                and assignment.execution_id == request.execution_id
                and assignment.execution_workspace_id == request.execution_workspace_id
                and assignment.project_id == current.project_id == request.project_id
                and assignment.assigned_worker_id == worker.id == request.worker_id
                and assignment.fence == fence
                and assignment.lease is not None
                and assignment.lease.fence == fence and assignment.lease.worker_id == worker.id
                and assignment.lease.expires_at > now
                and assignment.deadline_at is not None and assignment.deadline_at > now,
                "assignment, worker, fence or lease is stale",
            )
            runtime = assignment.runtime_binding
            identity = (runtime.provider_id, runtime.runtime_id) if runtime else ("openai", "codex")
            self.require(identity == (current.provider_id, current.runtime_id) ==
                         (adapter.provider_id, adapter.runtime_id), "runtime identity changed")
            self.require(current.runtime_type == adapter.runtime_type, "runtime type changed")
            binding_state = ThreadBootstrapBindingState.model_validate(rows[bs.namespace][None])
            matches = [v for v in binding_state.bindings if v.thread_id == thread_id
                       and (v.organization_id, v.workspace_id) == tenant]
            self.require(len(matches) == 1, "native execution binding unavailable or ambiguous")
            binding = matches[0]
            self.require((binding.assignment_id, binding.execution_id, binding.execution_workspace_id) ==
                         (assignment.id, assignment.execution_id, assignment.execution_workspace_id),
                         "native binding changed during resume")
            self.require(assignment.subject is not None and (
                (assignment.subject.kind == "thread_bootstrap" and assignment.subject.ref == binding.bootstrap_id)
                or (assignment.subject.kind == "thread" and assignment.subject.ref == thread_id)),
                "assignment subject does not own this native thread")
            active_workspace = ExecutionWorkspace.model_validate(rows[xs.workspace_namespace][workspace.id])
            lease = ExecutionWorkspaceLease.model_validate(rows[xs.lease_namespace][workspace.lease_id])
            self.require(
                active_workspace.status == "active" and active_workspace.lease_id == lease.id
                and (active_workspace.organization_id, active_workspace.workspace_id) == tenant
                and (lease.organization_id, lease.workspace_id) == tenant
                and active_workspace.execution_id == lease.execution_id == assignment.execution_id
                and active_workspace.subject == lease.subject == assignment.subject
                and active_workspace.project_id == assignment.project_id
                and active_workspace.owner_identity_id == lease.owner_identity_id == assignment.created_by
                and lease.execution_workspace_id == active_workspace.id
                and lease.released_at is None and lease.expires_at > now
                and set(lease.resource_ids) == set(assignment.resource_ids)
                and set(request.resource_ids) == set(assignment.resource_ids)
                and str(worker_session.workspace_path) == active_workspace.path == request.workspace_cwd,
                "workspace scope or lease changed",
            )
            if expected.assignment_id and expected.assignment_id != assignment.id:
                prior = rows[ws.assignment_namespace].get(expected.assignment_id)
                self.require(prior is not None and prior["status"] in {
                    "succeeded", "failed", "cancelled", "lost",
                }, "prior assignment custody is still live or unavailable")
                previous = ExecutionAssignment.model_validate(prior)
                self.require(
                    (previous.organization_id, previous.workspace_id, previous.project_id, previous.created_by) ==
                    (assignment.organization_id, assignment.workspace_id, assignment.project_id, assignment.created_by),
                    "rollover changed prior owner or tenant",
                )
                compatible = (set(previous.resource_ids) == set(assignment.resource_ids)
                    and previous.repository_scope == assignment.repository_scope)
                if prepare_only and not compatible:
                    raise AgentSessionCustodyPrestartRefusal(current, previous, assignment)
                self.require(compatible, "rollover changed prior repository scope")
            self.require(assignment.execution_profile_definition is not None,
                         "exact execution Definition missing")
            profile, definition = self.profiles.resolve(assignment.execution_profile_id,
                organization_id=tenant[0], workspace_id=tenant[1], project_id=assignment.project_id)
            self.require(definition == assignment.execution_profile_definition,
                         "execution Definition changed")
            if assignment.agent_profile is not None:
                pin = assignment.agent_profile
                actual, _ = self.profiles.agent_profiles.resolve_for_execution(
                    pin.profile_id, revision=pin.profile_revision, actor=live_actor, project_id=assignment.project_id)
                self.require(actual.record_id == pin.profile_record_id, "Agent Profile changed")
            # Recheck trusted worker/credential authority under the same custody
            # locks. This validates the worker's real lease, never caller input.
            validated = worker_session.validate_current()
            self.require(validated.id == assignment.id and validated.fence == fence,
                         "worker validation changed custody")
            if prepare_only:
                # Validate under the same canonical locks without reassociation,
                # status recovery, or an event claiming provider success.
                updated.append(current)
                return {}
            replacement = current.model_copy(update={
                "execution_id": assignment.execution_id, "assignment_id": assignment.id,
                "execution_workspace_id": assignment.execution_workspace_id,
                "worker_id": worker.id, "resource_ids": tuple(assignment.resource_ids),
                "status": AgentSessionStatus.READY, "updated_at": now,
                "failure": None, "failure_reason": None,
            })
            same_custody = all(getattr(current, k) == getattr(replacement, k) for k in (
                "execution_id", "assignment_id", "execution_workspace_id", "worker_id", "resource_ids",
            ))
            if same_custody and current.status == AgentSessionStatus.READY and current.failure is None and current.failure_reason is None:
                updated.append(current)
                return {}
            updated.append(replacement)
            changes = {ss.records_namespace: {session_key: replacement.model_dump(mode="json")}}
            if same_custody:
                # Status recovery is not a new custody transition. Preserve the
                # previous immutable event and its original predecessor IDs.
                return changes
            self.require(rows[ws.event_namespace][event_id] is None,
                         "reassociation event already exists; refusing historical overwrite")
            event = WorkerEvent(id=event_id, organization_id=tenant[0], workspace_id=tenant[1],
                event_type="agent_session_execution_reassociated", worker_id=worker.id,
                assignment_id=assignment.id, actor_id=actor.identity_id, occurred_at=now,
                details={"agent_session_id": current.id, "thread_id": thread_id,
                    "previous_assignment_id": current.assignment_id,
                    "previous_execution_id": current.execution_id,
                    "previous_execution_workspace_id": current.execution_workspace_id,
                    "previous_worker_id": current.worker_id, "fence": fence,
                    "definition_record_id": definition.record_id})
            changes[ws.event_namespace] = {event.id: event.model_dump(mode="json")}
            return changes

        self.store.record_mutate_many(selected, apply)
        return updated[0]
