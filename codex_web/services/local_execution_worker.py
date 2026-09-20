from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence, TypeVar

from codex_web.artifact_evidence import EvidenceCreate, EvidenceResult, EvidenceType
from codex_web.execution_workers import (
    AssignmentClaimRequest,
    AssignmentCompleteRequest,
    AssignmentRenewRequest,
    AssignmentStartRequest,
    AssignmentStatus,
    ExecutionAssignment,
    ExecutionWorker,
    WorkerHeartbeatRequest,
)
from codex_web.execution_workspaces import ExecutionWorkspaceStatus
from codex_web.identity import AuthenticationActor
from codex_web.local_execution_backend import (
    BubblewrapExecutionBackend,
    LocalExecutionBackendError,
    LocalExecutionResult,
)
from codex_web.services.artifact_evidence import ArtifactEvidenceService
from codex_web.services.codex_auth_delegation import (
    CodexAuthDelegationService,
    CodexDelegatedLaunch,
)
from codex_web.services.execution_workers import (
    ExecutionWorkerService,
    WorkerConflictError,
)
from codex_web.services.execution_workspaces import ExecutionWorkspaceService


class LocalExecutionWorkerRuntimeError(RuntimeError):
    pass


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class LocalExecutionCompletion:
    assignment: ExecutionAssignment
    result: LocalExecutionResult
    evidence_id: str | None = None


class LocalExecutionWorkerRuntime:
    """Execute a canonical worker assignment through the local sandbox backend."""

    def __init__(
        self,
        worker_service: ExecutionWorkerService,
        workspace_service: ExecutionWorkspaceService,
        backend: BubblewrapExecutionBackend,
        *,
        worker: ExecutionWorker,
        worker_actor: AuthenticationActor,
        control_actor: AuthenticationActor,
        artifact_evidence: ArtifactEvidenceService | None = None,
        codex_auth_delegation: CodexAuthDelegationService | None = None,
        heartbeat_interval_seconds: float = 30.0,
        renew_margin_seconds: float = 45.0,
    ) -> None:
        self.worker_service = worker_service
        self.workspace_service = workspace_service
        self.backend = backend
        self.worker = worker
        self.worker_actor = worker_actor
        self.control_actor = control_actor
        self.artifact_evidence = artifact_evidence
        self.codex_auth_delegation = codex_auth_delegation
        self.heartbeat_interval_seconds = max(5.0, heartbeat_interval_seconds)
        self.renew_margin_seconds = max(10.0, renew_margin_seconds)

    def _pending_assignment(self, assignment_id: str) -> ExecutionAssignment:
        item = next(
            (
                assignment
                for assignment in self.worker_service.store.load().assignments
                if assignment.id == assignment_id
                and assignment.organization_id == self.control_actor.organization_id
                and assignment.workspace_id == self.control_actor.workspace_id
            ),
            None,
        )
        if item is None:
            raise LocalExecutionWorkerRuntimeError("execution assignment not found")
        return item

    def codex_auth_status(
        self,
        assignment_id: str,
    ) -> dict[str, str | int | float | bool | None]:
        assignment = self._pending_assignment(assignment_id)
        if self.codex_auth_delegation is None:
            return {
                "ready": False,
                "assignment_id": assignment.id,
                "worker_id": self.worker.id,
                "fence": assignment.fence,
                "reason": "Codex auth delegation is not configured",
                "credential_store": "ephemeral",
                "worker_home": "/tmp/codex-worker-home",
                "child_environment_filtered": True,
            }
        return self.codex_auth_delegation.status(
            assignment,
            worker_id=self.worker.id,
            fence=assignment.fence,
            actor=self.worker_actor,
        )

    def use_codex_auth(
        self,
        assignment_id: str,
        consumer: Callable[[CodexDelegatedLaunch], T],
        *,
        subcommand: tuple[str, ...] = ("app-server",),
    ) -> T:
        if self.codex_auth_delegation is None:
            raise LocalExecutionWorkerRuntimeError(
                "Codex auth delegation is not configured"
            )
        assignment = self._pending_assignment(assignment_id)
        if assignment.lease is None:
            raise LocalExecutionWorkerRuntimeError(
                "Codex auth delegation requires an active worker lease"
            )
        return self.codex_auth_delegation.use(
            assignment,
            worker_id=self.worker.id,
            fence=assignment.fence,
            actor=self.worker_actor,
            consumer=consumer,
            subcommand=subcommand,
        )

    def _workspace(self, assignment: ExecutionAssignment):
        if not assignment.execution_workspace_id:
            raise LocalExecutionWorkerRuntimeError(
                "local command execution requires an isolated execution workspace"
            )
        workspace = self.workspace_service.get(
            assignment.execution_workspace_id,
            self.control_actor,
        )
        if workspace.status != ExecutionWorkspaceStatus.ACTIVE:
            raise LocalExecutionWorkerRuntimeError(
                "execution workspace is not active"
            )
        if workspace.execution_id != assignment.execution_id:
            raise LocalExecutionWorkerRuntimeError(
                "assignment execution no longer matches its execution workspace"
            )
        if set(assignment.resource_ids) - set(workspace.resource_ids):
            raise LocalExecutionWorkerRuntimeError(
                "assignment resources exceed execution workspace lease"
            )
        actual_disk_bytes = getattr(workspace, "actual_disk_bytes", None)
        if (
            actual_disk_bytes is not None
            and actual_disk_bytes > assignment.limits.disk_bytes
        ):
            raise LocalExecutionWorkerRuntimeError(
                "execution workspace exceeds assignment disk limit"
            )
        return workspace

    def _workspace_path(self, assignment: ExecutionAssignment) -> Path:
        workspace = self._workspace(assignment)
        if not workspace.path:
            raise LocalExecutionWorkerRuntimeError(
                "local command execution requires a filesystem execution workspace"
            )
        return Path(workspace.path)

    def readonly_mounts(
        self,
        assignment: ExecutionAssignment,
    ) -> tuple[tuple[Path, Path], ...]:
        workspace = self._workspace(assignment)
        target = assignment.repository_target
        authorized = (
            set(target.read_only_repository_ids)
            if target is not None
            else set()
        )
        mounts: list[tuple[Path, Path]] = []
        for member in getattr(workspace, "repository_members", ()):
            if member.resource_id == workspace.repository_resource_id:
                continue
            if member.resource_id not in authorized:
                raise LocalExecutionWorkerRuntimeError(
                    "workspace contains read-only repository outside assignment target"
                )
            if member.access_mode.value != "read":
                raise LocalExecutionWorkerRuntimeError(
                    "non-primary repository member must remain read-only"
                )
            source = Path(member.workspace_path).resolve(strict=True)
            destination = Path(member.sandbox_path)
            if not destination.is_absolute():
                raise LocalExecutionWorkerRuntimeError(
                    "read-only repository sandbox path must be absolute"
                )
            mounts.append((source, destination))
        return tuple(mounts)

    def readonly_disk_bytes(self, assignment: ExecutionAssignment) -> int:
        workspace = self._workspace(assignment)
        return sum(
            int(getattr(member, "disk_bytes", 0) or 0)
            for member in getattr(workspace, "repository_members", ())
            if member.resource_id != workspace.repository_resource_id
        )

    def _claim_or_resume(self, assignment: ExecutionAssignment) -> ExecutionAssignment:
        if assignment.status == AssignmentStatus.PENDING:
            claimed = self.worker_service.claim(
                self.worker.id,
                AssignmentClaimRequest(lease_seconds=120),
                actor=self.worker_actor,
                assignment_id=assignment.id,
            )
            if claimed is None:
                raise LocalExecutionWorkerRuntimeError(
                    "local worker is not eligible to claim assignment"
                )
            return claimed
        if (
            assignment.status in {AssignmentStatus.CLAIMED, AssignmentStatus.RUNNING}
            and assignment.assigned_worker_id == self.worker.id
            and assignment.lease is not None
        ):
            return assignment
        raise LocalExecutionWorkerRuntimeError(
            f"assignment is not runnable by local worker while {assignment.status.value}"
        )

    def _limit_evidence(
        self,
        assignment: ExecutionAssignment,
        result: LocalExecutionResult,
    ) -> str | None:
        if result.limit_breach is None or self.artifact_evidence is None:
            return None
        evidence = self.artifact_evidence.create_evidence(
            EvidenceCreate(
                project_id=assignment.project_id,
                work_item_ref=assignment.work_item_ref,
                execution_id=assignment.execution_id,
                execution_workspace_id=assignment.execution_workspace_id,
                evidence_type=EvidenceType.POLICY_EVALUATION,
                provider="local-execution-worker",
                source=f"execution-worker:{self.worker.id}",
                result=EvidenceResult.FAIL,
                summary=f"isolated local execution exceeded {result.limit_breach}",
                metadata={
                    "assignment_id": assignment.id,
                    "worker_id": self.worker.id,
                    "limit_breach": result.limit_breach,
                    "executable": result.executable,
                    "command_digest": result.command_digest,
                    "exit_code": result.exit_code,
                    "duration_seconds": round(result.duration_seconds, 6),
                    "disk_bytes": result.disk_bytes,
                },
            ),
            actor=self.worker_actor,
        )
        return evidence.id

    def execute(
        self,
        assignment_id: str,
        argv: Sequence[str],
    ) -> LocalExecutionCompletion:
        assignment = self._pending_assignment(assignment_id)
        workspace_path = self._workspace_path(assignment)
        readonly_mounts = self.readonly_mounts(assignment)
        readonly_disk_bytes = self.readonly_disk_bytes(assignment)
        self.backend.validate_assignment(assignment)

        self.worker_service.heartbeat(
            self.worker.id,
            WorkerHeartbeatRequest(version=self.worker.version),
            actor=self.worker_actor,
        )
        assignment = self._claim_or_resume(assignment)
        lease = assignment.lease
        if lease is None:
            raise LocalExecutionWorkerRuntimeError("claimed assignment has no worker lease")

        if assignment.status == AssignmentStatus.CLAIMED:
            assignment = self.worker_service.start(
                self.worker.id,
                assignment.id,
                AssignmentStartRequest(
                    lease_token=lease.lease_token,
                    fence=lease.fence,
                ),
                actor=self.worker_actor,
            )
            lease = assignment.lease
            if lease is None:
                raise LocalExecutionWorkerRuntimeError(
                    "running assignment lost its worker lease"
                )

        current = {"assignment": assignment}
        last_heartbeat = {"at": time.monotonic()}

        def poll() -> None:
            now_monotonic = time.monotonic()
            if now_monotonic - last_heartbeat["at"] >= self.heartbeat_interval_seconds:
                self.worker_service.heartbeat(
                    self.worker.id,
                    WorkerHeartbeatRequest(version=self.worker.version),
                    actor=self.worker_actor,
                )
                last_heartbeat["at"] = now_monotonic

            active = current["assignment"]
            active_lease = active.lease
            if active_lease is None:
                raise LocalExecutionWorkerRuntimeError(
                    "worker lease disappeared during local execution"
                )
            if active_lease.expires_at - time.time() <= self.renew_margin_seconds:
                renewed = self.worker_service.renew(
                    self.worker.id,
                    active.id,
                    AssignmentRenewRequest(
                        lease_token=active_lease.lease_token,
                        fence=active_lease.fence,
                        lease_seconds=120,
                    ),
                    actor=self.worker_actor,
                )
                current["assignment"] = renewed

        try:
            run_kwargs = {
                "argv": argv,
                "workspace_path": workspace_path,
                "poll_hook": poll,
            }
            if readonly_mounts:
                run_kwargs["trusted_readonly_mounts"] = readonly_mounts
            if readonly_disk_bytes:
                run_kwargs["additional_disk_bytes"] = readonly_disk_bytes
            result = self.backend.run(
                current["assignment"],
                **run_kwargs,
            )
        except LocalExecutionBackendError as exc:
            active = current["assignment"]
            active_lease = active.lease
            if active_lease is not None and active.status == AssignmentStatus.RUNNING:
                try:
                    self.worker_service.complete(
                        self.worker.id,
                        active.id,
                        AssignmentCompleteRequest(
                            lease_token=active_lease.lease_token,
                            fence=active_lease.fence,
                            succeeded=False,
                            failure_code="local_execution_backend_error",
                            failure_message=str(exc)[:500],
                        ),
                        actor=self.worker_actor,
                    )
                except Exception:
                    pass
            raise LocalExecutionWorkerRuntimeError(str(exc)) from exc

        active = current["assignment"]
        active_lease = active.lease
        if active_lease is None:
            raise LocalExecutionWorkerRuntimeError(
                "worker lease disappeared before completion"
            )
        evidence_id = self._limit_evidence(active, result)
        evidence_ids = (evidence_id,) if evidence_id else ()
        failure_code = None
        failure_message = None
        if not result.succeeded:
            if result.limit_breach:
                failure_code = f"worker_limit_{result.limit_breach}"
                failure_message = (
                    f"isolated local execution exceeded {result.limit_breach}"
                )
            else:
                failure_code = "worker_command_failed"
                failure_message = (
                    f"isolated command exited with status {result.exit_code}"
                )

        try:
            completed = self.worker_service.complete(
                self.worker.id,
                active.id,
                AssignmentCompleteRequest(
                    lease_token=active_lease.lease_token,
                    fence=active_lease.fence,
                    succeeded=result.succeeded,
                    failure_code=failure_code,
                    failure_message=failure_message,
                    evidence_ids=evidence_ids,
                ),
                actor=self.worker_actor,
            )
        except Exception:
            if evidence_id and self.artifact_evidence is not None:
                try:
                    self.artifact_evidence.invalidate_evidence(
                        evidence_id,
                        "worker completion fence changed before commit",
                        actor=self.worker_actor,
                    )
                except Exception:
                    pass
            raise

        return LocalExecutionCompletion(
            assignment=completed,
            result=result,
            evidence_id=evidence_id,
        )
