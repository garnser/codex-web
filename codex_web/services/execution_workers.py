from __future__ import annotations

import secrets
import time

from codex_web.execution_subjects import normalize_execution_subject
from codex_web.execution_workers import (
    AssignmentClaimRequest,
    AssignmentCompleteRequest,
    AssignmentLease,
    AssignmentRenewRequest,
    AssignmentStartRequest,
    AssignmentStatus,
    ExecutionAssignment,
    ExecutionAssignmentCreate,
    ExecutionWorker,
    ExecutionWorkerRegister,
    ExecutionWorkerState,
    WorkerEvent,
    WorkerExecutionReadiness,
    WorkerHeartbeatRequest,
    WorkerLifecycle,
)
from codex_web.failures import (
    FailureReason,
    create_failure,
    worker_failure_reason,
)
from codex_web.identity import AuthenticationActor, MembershipRole, PrincipalKind
from codex_web.services.identity import AuthorizationError, IdentityService
from codex_web.services.execution_workspaces import ExecutionWorkspaceService
from codex_web.storage.execution_workers import ExecutionWorkerStore


class ExecutionWorkerError(RuntimeError):
    pass


class WorkerNotFoundError(ExecutionWorkerError):
    pass


class AssignmentNotFoundError(ExecutionWorkerError):
    pass


class WorkerConflictError(ExecutionWorkerError):
    pass


class WorkerLeaseError(ExecutionWorkerError):
    reason_code = FailureReason.WORKER_LEASE_LOST


class WorkerCapabilityError(ExecutionWorkerError):
    reason_code = FailureReason.CAPABILITY_UNAVAILABLE


class ExecutionWorkerService:
    def __init__(
        self,
        store: ExecutionWorkerStore,
        *,
        identity: IdentityService | None = None,
        workspaces: ExecutionWorkspaceService | None = None,
        maintenance_guard=None,
        assignment_notifier=None,
    ) -> None:
        self.store = store
        self.identity = identity
        self.workspaces = workspaces
        self.maintenance_guard = maintenance_guard
        self.assignment_notifier = assignment_notifier

    def _notify_assignment(
        self,
        assignment: ExecutionAssignment,
        event_type: str,
    ) -> None:
        if self.assignment_notifier is None:
            return
        try:
            self.assignment_notifier(assignment, event_type)
        except Exception:
            # Browser notification is observational; it must never break
            # canonical worker state transitions.
            return

    @staticmethod
    def _admin(actor: AuthenticationActor) -> bool:
        if actor.principal_kind == PrincipalKind.SERVICE:
            return "execution-worker:admin" in actor.service_scopes
        return actor.has_role(MembershipRole.OWNER, MembershipRole.ADMIN)

    @classmethod
    def _require_admin(cls, actor: AuthenticationActor) -> None:
        if not cls._admin(actor):
            raise AuthorizationError("execution worker administrator required")

    @staticmethod
    def _same_scope(item, actor: AuthenticationActor) -> bool:
        return (
            item.organization_id == actor.organization_id
            and item.workspace_id == actor.workspace_id
        )

    @staticmethod
    def _event(
        state: ExecutionWorkerState,
        *,
        actor: AuthenticationActor | None,
        event_type: str,
        worker_id: str | None = None,
        assignment_id: str | None = None,
        details: dict[str, str | int | float | bool | None] | None = None,
    ) -> None:
        state.events.append(
            WorkerEvent(
                organization_id=(actor.organization_id if actor else "system"),
                workspace_id=(actor.workspace_id if actor else "system"),
                event_type=event_type,
                worker_id=worker_id,
                assignment_id=assignment_id,
                actor_id=actor.identity_id if actor else None,
                details=details or {},
            )
        )
        state.events = state.events[-10000:]

    def _worker(
        self,
        state: ExecutionWorkerState,
        worker_id: str,
        actor: AuthenticationActor,
    ) -> ExecutionWorker:
        item = next(
            (
                value
                for value in state.workers
                if value.id == worker_id and self._same_scope(value, actor)
            ),
            None,
        )
        if item is None:
            raise WorkerNotFoundError("execution worker not found")
        return item

    def _assignment(
        self,
        state: ExecutionWorkerState,
        assignment_id: str,
        actor: AuthenticationActor,
    ) -> ExecutionAssignment:
        item = next(
            (
                value
                for value in state.assignments
                if value.id == assignment_id and self._same_scope(value, actor)
            ),
            None,
        )
        if item is None:
            raise AssignmentNotFoundError("execution assignment not found")
        return item

    @staticmethod
    def _require_worker_actor(
        worker: ExecutionWorker,
        actor: AuthenticationActor,
    ) -> None:
        if actor.principal_kind != PrincipalKind.SERVICE:
            raise AuthorizationError("execution worker operation requires service identity")
        if actor.identity_id != worker.service_identity_id:
            raise AuthorizationError("worker service identity does not match registered worker")
        if not {
            "execution-worker:run",
            "execution-worker:admin",
        }.intersection(actor.service_scopes):
            raise AuthorizationError("execution-worker:run scope required")

    @staticmethod
    def _active_count(
        state: ExecutionWorkerState,
        worker_id: str,
        now: float,
    ) -> int:
        return sum(
            1
            for item in state.assignments
            if item.assigned_worker_id == worker_id
            and item.status in {AssignmentStatus.CLAIMED, AssignmentStatus.RUNNING}
            and item.lease is not None
            and item.lease.expires_at > now
        )

    @staticmethod
    def _eligible(
        worker: ExecutionWorker,
        assignment: ExecutionAssignment,
        state: ExecutionWorkerState,
        now: float,
    ) -> tuple[bool, str | None]:
        if worker.lifecycle != WorkerLifecycle.ACTIVE:
            return False, f"worker_{worker.lifecycle.value}"
        if assignment.deadline_at is not None and assignment.deadline_at <= now:
            return False, "assignment_deadline_expired"
        missing = set(assignment.required_capabilities) - set(worker.capabilities)
        if missing:
            return False, "capability_mismatch"
        if (
            assignment.execution_contract_version
            not in worker.supported_execution_contract_versions
        ):
            return False, "execution_contract_version_mismatch"
        if ExecutionWorkerService._active_count(state, worker.id, now) >= worker.max_concurrency:
            return False, "worker_concurrency_exhausted"
        return True, None

    def execution_readiness(
        self,
        *,
        required_capabilities: tuple,
        execution_contract_version: str,
        actor: AuthenticationActor,
    ) -> WorkerExecutionReadiness:
        self._require_admin(actor)
        required = tuple(
            sorted(
                set(required_capabilities),
                key=lambda value: value.value,
            )
        )
        scoped = [
            worker
            for worker in self.store.load().workers
            if self._same_scope(worker, actor)
        ]
        active = [
            worker
            for worker in scoped
            if worker.lifecycle == WorkerLifecycle.ACTIVE
        ]
        available = tuple(
            sorted(
                {
                    capability
                    for worker in active
                    for capability in worker.capabilities
                },
                key=lambda value: value.value,
            )
        )
        eligible = [
            worker
            for worker in active
            if set(required).issubset(set(worker.capabilities))
            and execution_contract_version
            in worker.supported_execution_contract_versions
        ]
        if eligible:
            return WorkerExecutionReadiness(
                ready=True,
                code="ready",
                required_capabilities=required,
                available_capabilities=available,
                eligible_worker_ids=tuple(worker.id for worker in eligible),
                active_worker_ids=tuple(worker.id for worker in active),
                execution_contract_version=execution_contract_version,
                reason="eligible execution worker is available",
            )

        missing = tuple(
            capability
            for capability in required
            if capability not in available
        )
        if not active:
            code = "worker_unavailable"
            reason = "no active execution worker is available"
            remediation = (
                "Start or reactivate a qualified execution worker and verify "
                "its isolation probe."
            )
        elif missing:
            code = "worker_capability_missing"
            reason = (
                "active execution workers are missing required capabilities: "
                + ", ".join(capability.value for capability in missing)
            )
            remediation = (
                "Fix the local isolation/runtime configuration, rerun the "
                "worker probe, and verify the required capabilities are "
                "advertised."
            )
        else:
            code = "execution_contract_version_unsupported"
            reason = (
                "no active execution worker supports execution contract "
                f"{execution_contract_version}"
            )
            remediation = (
                "Upgrade/reconcile the execution worker so it advertises the "
                "required execution contract version."
            )
        return WorkerExecutionReadiness(
            ready=False,
            code=code,
            required_capabilities=required,
            available_capabilities=available,
            eligible_worker_ids=(),
            active_worker_ids=tuple(worker.id for worker in active),
            execution_contract_version=execution_contract_version,
            reason=reason,
            remediation=remediation,
        )

    def list_workers(self, actor: AuthenticationActor) -> list[ExecutionWorker]:
        self._require_admin(actor)
        return sorted(
            [item for item in self.store.load().workers if self._same_scope(item, actor)],
            key=lambda item: (item.registered_at, item.id),
            reverse=True,
        )

    def register(
        self,
        payload: ExecutionWorkerRegister,
        *,
        actor: AuthenticationActor,
    ) -> ExecutionWorker:
        self._require_admin(actor)
        if self.identity is not None:
            identity_state = self.identity.state()
            service = next(
                (
                    item
                    for item in identity_state.services
                    if item.id == payload.service_identity_id
                    and item.disabled_at is None
                ),
                None,
            )
            memberships = self.identity._active_memberships(
                identity_state,
                payload.service_identity_id,
                actor.tenant,
                principal_kind=PrincipalKind.SERVICE,
            )
            if service is None or not memberships:
                raise WorkerConflictError(
                    "worker service identity must exist and belong to the tenant/workspace"
                )
        created: list[ExecutionWorker] = []

        def apply(state: ExecutionWorkerState) -> ExecutionWorkerState:
            existing = next(
                (
                    item
                    for item in state.workers
                    if self._same_scope(item, actor)
                    and item.service_identity_id == payload.service_identity_id
                    and item.pool == payload.pool
                    and item.lifecycle != WorkerLifecycle.REVOKED
                ),
                None,
            )
            if existing is not None:
                raise WorkerConflictError("active worker already registered for service identity and pool")
            worker = ExecutionWorker(
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                service_identity_id=payload.service_identity_id,
                pool=payload.pool,
                version=payload.version,
                capabilities=payload.capabilities,
                supported_execution_contract_versions=(
                    payload.supported_execution_contract_versions
                ),
                max_concurrency=payload.max_concurrency,
                registered_by=actor.identity_id,
            )
            state.workers.append(worker)
            self._event(
                state,
                actor=actor,
                event_type="worker_registered",
                worker_id=worker.id,
                details={"pool": worker.pool, "version": worker.version},
            )
            created.append(worker)
            return state

        self.store.update(apply)
        return created[0]

    def ensure_local_worker(
        self,
        *,
        service_identity_id: str,
        version: str,
        capabilities: tuple,
        actor: AuthenticationActor,
        supported_execution_contract_versions: tuple[str, ...] = ("1.0",),
    ) -> ExecutionWorker:
        self._require_admin(actor)
        existing = next(
            (
                item
                for item in self.store.load().workers
                if self._same_scope(item, actor)
                and item.service_identity_id == service_identity_id
                and item.pool == "local"
                and item.lifecycle != WorkerLifecycle.REVOKED
            ),
            None,
        )
        if existing is not None:
            normalized_capabilities = tuple(
                sorted(set(capabilities), key=lambda value: value.value)
            )
            updated: list[ExecutionWorker] = []

            def apply(state: ExecutionWorkerState) -> ExecutionWorkerState:
                current = next(item for item in state.workers if item.id == existing.id)
                lifecycle = (
                    WorkerLifecycle.ACTIVE
                    if current.lifecycle == WorkerLifecycle.OFFLINE
                    else current.lifecycle
                )
                replacement = current.model_copy(
                    update={
                        "version": version,
                        "capabilities": normalized_capabilities,
                        "last_heartbeat_at": time.time(),
                        "lifecycle": lifecycle,
                        "supported_execution_contract_versions": (
                            supported_execution_contract_versions
                        ),
                    }
                )
                state.workers = [
                    replacement if item.id == current.id else item
                    for item in state.workers
                ]
                changed = (
                    current.version != version
                    or current.capabilities != normalized_capabilities
                    or current.lifecycle != lifecycle
                    or current.supported_execution_contract_versions
                    != supported_execution_contract_versions
                )
                if changed:
                    self._event(
                        state,
                        actor=actor,
                        event_type="worker_capabilities_reconciled",
                        worker_id=current.id,
                        details={
                            "version": version,
                            "capability_count": len(normalized_capabilities),
                        },
                    )
                updated.append(replacement)
                return state

            self.store.update(apply)
            return updated[0]
        return self.register(
            ExecutionWorkerRegister(
                service_identity_id=service_identity_id,
                pool="local",
                version=version,
                capabilities=capabilities,
                supported_execution_contract_versions=(
                    supported_execution_contract_versions
                ),
                max_concurrency=1,
            ),
            actor=actor,
        )

    def heartbeat(
        self,
        worker_id: str,
        payload: WorkerHeartbeatRequest,
        *,
        actor: AuthenticationActor,
    ) -> ExecutionWorker:
        updated: list[ExecutionWorker] = []

        def apply(state: ExecutionWorkerState) -> ExecutionWorkerState:
            worker = self._worker(state, worker_id, actor)
            self._require_worker_actor(worker, actor)
            if worker.lifecycle == WorkerLifecycle.REVOKED:
                raise WorkerConflictError("revoked worker cannot heartbeat")
            replacement = worker.model_copy(
                update={
                    "last_heartbeat_at": time.time(),
                    "version": payload.version or worker.version,
                    "lifecycle": (
                        WorkerLifecycle.ACTIVE
                        if worker.lifecycle == WorkerLifecycle.OFFLINE
                        else worker.lifecycle
                    ),
                }
            )
            state.workers = [
                replacement if item.id == worker.id else item for item in state.workers
            ]
            updated.append(replacement)
            return state

        self.store.update(apply)
        return updated[0]

    def set_lifecycle(
        self,
        worker_id: str,
        lifecycle: WorkerLifecycle,
        *,
        actor: AuthenticationActor,
        reason: str | None = None,
    ) -> ExecutionWorker:
        self._require_admin(actor)
        if lifecycle == WorkerLifecycle.ACTIVE:
            raise WorkerConflictError("use heartbeat/reactivation flow for active lifecycle")
        updated: list[ExecutionWorker] = []

        def apply(state: ExecutionWorkerState) -> ExecutionWorkerState:
            worker = self._worker(state, worker_id, actor)
            now = time.time()
            replacement = worker.model_copy(
                update={
                    "lifecycle": lifecycle,
                    "quarantine_reason": (
                        reason if lifecycle == WorkerLifecycle.QUARANTINED else None
                    ),
                    "revoked_at": (
                        now if lifecycle == WorkerLifecycle.REVOKED else worker.revoked_at
                    ),
                }
            )
            state.workers = [
                replacement if item.id == worker.id else item for item in state.workers
            ]
            self._event(
                state,
                actor=actor,
                event_type=f"worker_{lifecycle.value}",
                worker_id=worker.id,
                details={"reason": reason},
            )
            updated.append(replacement)
            return state

        self.store.update(apply)
        return updated[0]

    def activate(
        self,
        worker_id: str,
        *,
        actor: AuthenticationActor,
    ) -> ExecutionWorker:
        self._require_admin(actor)
        updated: list[ExecutionWorker] = []

        def apply(state: ExecutionWorkerState) -> ExecutionWorkerState:
            worker = self._worker(state, worker_id, actor)
            if worker.lifecycle == WorkerLifecycle.REVOKED:
                raise WorkerConflictError("revoked worker cannot be reactivated")
            replacement = worker.model_copy(
                update={
                    "lifecycle": WorkerLifecycle.ACTIVE,
                    "quarantine_reason": None,
                    "last_heartbeat_at": time.time(),
                }
            )
            state.workers = [
                replacement if item.id == worker.id else item for item in state.workers
            ]
            self._event(
                state,
                actor=actor,
                event_type="worker_activated",
                worker_id=worker.id,
            )
            updated.append(replacement)
            return state

        self.store.update(apply)
        return updated[0]

    def create_assignment(
        self,
        payload: ExecutionAssignmentCreate,
        *,
        actor: AuthenticationActor,
    ) -> ExecutionAssignment:
        self._require_admin(actor)
        if (
            self.maintenance_guard is not None
            and not self.maintenance_guard(
                actor.organization_id,
                actor.workspace_id,
            )
        ):
            raise WorkerConflictError(
                "execution assignments are drained during upgrade maintenance"
            )
        if payload.repository_target is not None:
            target = payload.repository_target
            if (
                target.organization_id != actor.organization_id
                or target.workspace_id != actor.workspace_id
            ):
                raise WorkerConflictError(
                    "repository target tenant does not match assignment actor"
                )
            if payload.project_id is not None and target.project_id != payload.project_id:
                raise WorkerConflictError(
                    "repository target project does not match assignment"
                )
        if payload.repository_scope is not None:
            scope = payload.repository_scope
            if (
                scope.organization_id != actor.organization_id
                or scope.workspace_id != actor.workspace_id
            ):
                raise WorkerConflictError(
                    "repository scope tenant does not match assignment actor"
                )
            if payload.project_id is not None and scope.project_id != payload.project_id:
                raise WorkerConflictError(
                    "repository scope project does not match assignment"
                )
        if payload.execution_workspace_id is not None:
            if self.workspaces is None:
                raise WorkerConflictError(
                    "execution workspace validation service is unavailable"
                )
            workspace = self.workspaces.get(payload.execution_workspace_id, actor)
            if workspace.execution_id != payload.execution_id:
                raise WorkerConflictError(
                    "assignment execution does not match execution workspace"
                )
            workspace_subject, _ = normalize_execution_subject(
                getattr(workspace, "subject", None),
                getattr(workspace, "work_item_ref", None),
            )
            if workspace_subject != payload.subject:
                raise WorkerConflictError(
                    "assignment execution subject does not match execution workspace"
                )
            if payload.project_id is not None and workspace.project_id != payload.project_id:
                raise WorkerConflictError(
                    "assignment project does not match execution workspace"
                )
            if set(payload.resource_ids) - set(workspace.resource_ids):
                raise WorkerConflictError(
                    "assignment resources exceed execution workspace lease"
                )
            if (
                payload.base_revision is not None
                and workspace.base_revision is not None
                and payload.base_revision != workspace.base_revision
            ):
                raise WorkerConflictError(
                    "assignment base revision does not match execution workspace"
                )
        created: list[ExecutionAssignment] = []

        def apply(state: ExecutionWorkerState) -> ExecutionWorkerState:
            duplicate = next(
                (
                    item
                    for item in state.assignments
                    if self._same_scope(item, actor)
                    and item.execution_id == payload.execution_id
                    and item.status
                    not in {
                        AssignmentStatus.CANCELLED,
                        AssignmentStatus.FAILED,
                        AssignmentStatus.LOST,
                    }
                ),
                None,
            )
            if duplicate is not None:
                raise WorkerConflictError("active assignment already exists for execution")
            assignment = ExecutionAssignment(
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                created_by=actor.identity_id,
                **payload.model_dump(),
            )
            state.assignments.append(assignment)
            self._event(
                state,
                actor=actor,
                event_type="assignment_created",
                assignment_id=assignment.id,
                details={
                    "execution_id": assignment.execution_id,
                    "subject_kind": assignment.subject.kind.value,
                    "subject_ref": assignment.subject.ref,
                    "work_item_ref": assignment.work_item_ref,
                    "repository_target_source": (
                        assignment.repository_target.source.value
                        if assignment.repository_target is not None
                        else None
                    ),
                    "mutable_repository_id": (
                        assignment.repository_target.mutable_repository_id
                        if assignment.repository_target is not None
                        else None
                    ),
                    "repository_write_mode": (
                        assignment.repository_scope.write_mode.value
                        if assignment.repository_scope is not None
                        else "single"
                    ),
                    "writable_repository_ids": (
                        list(assignment.repository_scope.writable_repository_ids)
                        if assignment.repository_scope is not None
                        else (
                            [assignment.repository_target.mutable_repository_id]
                            if (
                                assignment.repository_target is not None
                                and assignment.repository_target.mutable_repository_id is not None
                            )
                            else []
                        )
                    ),
                    "execution_profile_id": assignment.execution_profile_id,
                    "execution_profile_revision": (
                        assignment.execution_profile_definition.revision
                        if assignment.execution_profile_definition is not None
                        else None
                    ),
                },
            )
            created.append(assignment)
            return state

        self.store.update(apply)
        result = created[0]
        self._notify_assignment(result, "assignment_created")
        return result

    def list_assignments(
        self,
        actor: AuthenticationActor,
        *,
        worker_id: str | None = None,
    ) -> list[ExecutionAssignment]:
        self._require_admin(actor)
        items = [
            item for item in self.store.load().assignments if self._same_scope(item, actor)
        ]
        if worker_id is not None:
            items = [item for item in items if item.assigned_worker_id == worker_id]
        return sorted(items, key=lambda item: (item.created_at, item.id), reverse=True)

    def claim(
        self,
        worker_id: str,
        payload: AssignmentClaimRequest,
        *,
        actor: AuthenticationActor,
        assignment_id: str | None = None,
    ) -> ExecutionAssignment | None:
        if (
            self.maintenance_guard is not None
            and not self.maintenance_guard(
                actor.organization_id,
                actor.workspace_id,
            )
        ):
            return None
        claimed: list[ExecutionAssignment] = []
        now = time.time()

        def apply(state: ExecutionWorkerState) -> ExecutionWorkerState:
            worker = self._worker(state, worker_id, actor)
            self._require_worker_actor(worker, actor)
            candidates = [
                item
                for item in state.assignments
                if self._same_scope(item, actor)
                and item.status == AssignmentStatus.PENDING
                and (assignment_id is None or item.id == assignment_id)
            ]
            candidates.sort(key=lambda item: (item.created_at, item.id))
            target = None
            for candidate in candidates:
                eligible, _ = self._eligible(worker, candidate, state, now)
                if eligible:
                    target = candidate
                    break
            if target is None:
                return state
            fence = target.fence + 1
            lease = AssignmentLease(
                worker_id=worker.id,
                fence=fence,
                lease_token=secrets.token_urlsafe(32),
                acquired_at=now,
                expires_at=now + payload.lease_seconds,
            )
            replacement = target.model_copy(
                update={
                    "status": AssignmentStatus.CLAIMED,
                    "fence": fence,
                    "lease": lease,
                    "assigned_worker_id": worker.id,
                    "agent_profile": (
                        target.agent_profile.model_copy(
                            update={
                                "selected_worker_id": worker.id,
                            }
                        )
                        if target.agent_profile is not None
                        else None
                    ),
                    "updated_at": now,
                    "failure_code": None,
                    "failure_message": None,
                }
            )
            state.assignments = [
                replacement if item.id == target.id else item for item in state.assignments
            ]
            self._event(
                state,
                actor=actor,
                event_type="assignment_claimed",
                worker_id=worker.id,
                assignment_id=target.id,
                details={"fence": fence, "expires_at": lease.expires_at},
            )
            claimed.append(replacement)
            return state

        self.store.update(apply)
        result = claimed[0] if claimed else None
        if result is not None:
            self._notify_assignment(result, "assignment_claimed")
        return result

    def _validate_lease(
        self,
        assignment: ExecutionAssignment,
        worker: ExecutionWorker,
        *,
        actor: AuthenticationActor,
        fence: int,
        token: str,
        now: float,
    ) -> AssignmentLease:
        self._require_worker_actor(worker, actor)
        lease = assignment.lease
        if (
            assignment.assigned_worker_id != worker.id
            or lease is None
            or lease.worker_id != worker.id
            or lease.fence != fence
            or assignment.fence != fence
            or not secrets.compare_digest(lease.lease_token, token)
            or lease.expires_at <= now
        ):
            raise WorkerLeaseError("assignment lease is stale or invalid")
        if worker.lifecycle not in {
            WorkerLifecycle.ACTIVE,
            WorkerLifecycle.DRAINING,
        }:
            raise WorkerLeaseError("worker is not trusted to continue assignment")
        if assignment.deadline_at is not None and assignment.deadline_at <= now:
            raise WorkerLeaseError("assignment deadline has expired")
        return lease

    def renew(
        self,
        worker_id: str,
        assignment_id: str,
        payload: AssignmentRenewRequest,
        *,
        actor: AuthenticationActor,
    ) -> ExecutionAssignment:
        updated: list[ExecutionAssignment] = []
        now = time.time()

        def apply(state: ExecutionWorkerState) -> ExecutionWorkerState:
            worker = self._worker(state, worker_id, actor)
            assignment = self._assignment(state, assignment_id, actor)
            lease = self._validate_lease(
                assignment,
                worker,
                actor=actor,
                fence=payload.fence,
                token=payload.lease_token,
                now=now,
            )
            if assignment.status not in {AssignmentStatus.CLAIMED, AssignmentStatus.RUNNING}:
                raise WorkerLeaseError("assignment is not renewable")
            replacement = assignment.model_copy(
                update={
                    "lease": lease.model_copy(
                        update={
                            "expires_at": now + payload.lease_seconds,
                            "renewed_at": now,
                        }
                    ),
                    "updated_at": now,
                }
            )
            state.assignments = [
                replacement if item.id == assignment.id else item
                for item in state.assignments
            ]
            updated.append(replacement)
            return state

        self.store.update(apply)
        result = updated[0]
        self._notify_assignment(result, "assignment_renewed")
        return result

    def start(
        self,
        worker_id: str,
        assignment_id: str,
        payload: AssignmentStartRequest,
        *,
        actor: AuthenticationActor,
    ) -> ExecutionAssignment:
        updated: list[ExecutionAssignment] = []
        now = time.time()

        def apply(state: ExecutionWorkerState) -> ExecutionWorkerState:
            worker = self._worker(state, worker_id, actor)
            assignment = self._assignment(state, assignment_id, actor)
            self._validate_lease(
                assignment,
                worker,
                actor=actor,
                fence=payload.fence,
                token=payload.lease_token,
                now=now,
            )
            if assignment.status != AssignmentStatus.CLAIMED:
                raise WorkerConflictError("assignment must be claimed before start")
            replacement = assignment.model_copy(
                update={
                    "status": AssignmentStatus.RUNNING,
                    "started_at": now,
                    "updated_at": now,
                }
            )
            state.assignments = [
                replacement if item.id == assignment.id else item
                for item in state.assignments
            ]
            self._event(
                state,
                actor=actor,
                event_type="assignment_started",
                worker_id=worker.id,
                assignment_id=assignment.id,
                details={"fence": payload.fence},
            )
            updated.append(replacement)
            return state

        self.store.update(apply)
        result = updated[0]
        self._notify_assignment(result, "assignment_started")
        return result

    def complete(
        self,
        worker_id: str,
        assignment_id: str,
        payload: AssignmentCompleteRequest,
        *,
        actor: AuthenticationActor,
    ) -> ExecutionAssignment:
        updated: list[ExecutionAssignment] = []
        now = time.time()

        def apply(state: ExecutionWorkerState) -> ExecutionWorkerState:
            worker = self._worker(state, worker_id, actor)
            assignment = self._assignment(state, assignment_id, actor)
            self._validate_lease(
                assignment,
                worker,
                actor=actor,
                fence=payload.fence,
                token=payload.lease_token,
                now=now,
            )
            if assignment.status != AssignmentStatus.RUNNING:
                raise WorkerConflictError("assignment must be running before completion")
            status = AssignmentStatus.SUCCEEDED if payload.succeeded else AssignmentStatus.FAILED
            failure = None
            if not payload.succeeded:
                reason = worker_failure_reason(payload.failure_code)
                failure = create_failure(
                    reason,
                    source_subsystem="execution_worker",
                    summary=None,
                    worker_id=worker.id,
                    assignment_id=assignment.id,
                    execution_id=assignment.execution_id,
                    source_native_code=payload.failure_code,
                    evidence_ids=tuple(payload.evidence_ids),
                    artifact_ids=tuple(payload.artifact_ids),
                    details={
                        "project_id": assignment.project_id,
                        "execution_contract_version": (
                            assignment.execution_contract_version
                        ),
                    },
                )
            replacement = assignment.model_copy(
                update={
                    "status": status,
                    "lease": None,
                    "completed_at": now,
                    "updated_at": now,
                    "failure_code": payload.failure_code,
                    "failure_message": payload.failure_message,
                    "failure": failure,
                    "artifact_ids": tuple(dict.fromkeys(payload.artifact_ids)),
                    "evidence_ids": tuple(dict.fromkeys(payload.evidence_ids)),
                }
            )
            state.assignments = [
                replacement if item.id == assignment.id else item
                for item in state.assignments
            ]
            self._event(
                state,
                actor=actor,
                event_type="assignment_completed",
                worker_id=worker.id,
                assignment_id=assignment.id,
                details={
                    "fence": payload.fence,
                    "status": status.value,
                    "artifact_count": len(replacement.artifact_ids),
                    "evidence_count": len(replacement.evidence_ids),
                },
            )
            updated.append(replacement)
            return state

        self.store.update(apply)
        result = updated[0]
        self._notify_assignment(result, "assignment_completed")
        return result

    def mark_stale_workers_offline(
        self,
        *,
        actor: AuthenticationActor,
        stale_after_seconds: int = 120,
        now: float | None = None,
    ) -> list[str]:
        self._require_admin(actor)
        current = time.time() if now is None else now
        cutoff = current - max(10, stale_after_seconds)
        changed: list[str] = []

        def apply(state: ExecutionWorkerState) -> ExecutionWorkerState:
            for index, worker in enumerate(state.workers):
                if (
                    not self._same_scope(worker, actor)
                    or worker.lifecycle != WorkerLifecycle.ACTIVE
                    or worker.last_heartbeat_at > cutoff
                ):
                    continue
                state.workers[index] = worker.model_copy(
                    update={"lifecycle": WorkerLifecycle.OFFLINE}
                )
                changed.append(worker.id)
                self._event(
                    state,
                    actor=actor,
                    event_type="worker_offline",
                    worker_id=worker.id,
                    details={"last_heartbeat_at": worker.last_heartbeat_at},
                )
            return state

        self.store.update(apply)
        return changed

    def recover_expired(
        self,
        *,
        actor: AuthenticationActor,
        now: float | None = None,
    ) -> list[str]:
        self._require_admin(actor)
        current = time.time() if now is None else now
        lost: list[str] = []

        def apply(state: ExecutionWorkerState) -> ExecutionWorkerState:
            for index, assignment in enumerate(state.assignments):
                if not self._same_scope(assignment, actor):
                    continue
                lease = assignment.lease
                if (
                    lease is None
                    or lease.expires_at > current
                    or assignment.status
                    not in {AssignmentStatus.CLAIMED, AssignmentStatus.RUNNING}
                ):
                    continue
                failure = create_failure(
                    FailureReason.WORKER_LEASE_LOST,
                    source_subsystem="execution_worker",
                    worker_id=assignment.assigned_worker_id,
                    assignment_id=assignment.id,
                    execution_id=assignment.execution_id,
                    source_native_code="worker_lease_expired",
                    details={"fence": assignment.fence},
                    occurred_at=current,
                )
                state.assignments[index] = assignment.model_copy(
                    update={
                        "status": AssignmentStatus.LOST,
                        "lease": None,
                        "updated_at": current,
                        "failure_code": "worker_lease_expired",
                        "failure_message": "worker lease expired before trusted completion",
                        "failure": failure,
                    }
                )
                lost.append(assignment.id)
                self._event(
                    state,
                    actor=actor,
                    event_type="assignment_lost",
                    worker_id=assignment.assigned_worker_id,
                    assignment_id=assignment.id,
                    details={"fence": assignment.fence},
                )
            return state

        self.store.update(apply)
        if lost:
            current = self.store.load()
            by_id = {item.id: item for item in current.assignments}
            for assignment_id in lost:
                item = by_id.get(assignment_id)
                if item is not None:
                    self._notify_assignment(item, "assignment_lost")
        return lost

    def retry_lost(
        self,
        assignment_id: str,
        *,
        actor: AuthenticationActor,
    ) -> ExecutionAssignment:
        self._require_admin(actor)
        updated: list[ExecutionAssignment] = []

        def apply(state: ExecutionWorkerState) -> ExecutionWorkerState:
            assignment = self._assignment(state, assignment_id, actor)
            if assignment.status not in {AssignmentStatus.LOST, AssignmentStatus.FAILED}:
                raise WorkerConflictError("only lost or failed assignment can be retried")
            failure = assignment.failure
            if failure is None:
                legacy_reason = worker_failure_reason(
                    assignment.failure_code
                    or (
                        "worker_lease_expired"
                        if assignment.status == AssignmentStatus.LOST
                        else None
                    )
                )
                failure = create_failure(
                    legacy_reason,
                    source_subsystem="execution_worker",
                    worker_id=assignment.assigned_worker_id,
                    assignment_id=assignment.id,
                    execution_id=assignment.execution_id,
                    source_native_code=assignment.failure_code,
                )
            if not failure.automatic_retry_allowed:
                raise WorkerConflictError(
                    "canonical failure classification does not allow automatic retry"
                )
            replacement = assignment.model_copy(
                update={
                    "status": AssignmentStatus.PENDING,
                    "lease": None,
                    "assigned_worker_id": None,
                    "updated_at": time.time(),
                    "failure_code": None,
                    "failure_message": None,
                    "failure": None,
                }
            )
            state.assignments = [
                replacement if item.id == assignment.id else item
                for item in state.assignments
            ]
            self._event(
                state,
                actor=actor,
                event_type="assignment_retried",
                worker_id=assignment.assigned_worker_id,
                assignment_id=assignment.id,
                details={"fence": assignment.fence},
            )
            updated.append(replacement)
            return state

        self.store.update(apply)
        result = updated[0]
        self._notify_assignment(result, "assignment_retried")
        return result

    def events(self, actor: AuthenticationActor) -> list[WorkerEvent]:
        self._require_admin(actor)
        return sorted(
            [
                item
                for item in self.store.load().events
                if item.organization_id == actor.organization_id
                and item.workspace_id == actor.workspace_id
            ],
            key=lambda item: (item.occurred_at, item.id),
            reverse=True,
        )
