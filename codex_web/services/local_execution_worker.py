from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, TypeVar

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


@dataclass(frozen=True, slots=True)
class ExecutionPythonEnvironment:
    environment: Mapping[str, str]
    readonly_mounts: tuple[tuple[Path, Path], ...] = ()


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
        trusted_local_codex_delegation: Any | None = None,
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
        self.trusted_local_codex_delegation = trusted_local_codex_delegation
        self.heartbeat_interval_seconds = max(5.0, heartbeat_interval_seconds)
        self.renew_margin_seconds = max(10.0, renew_margin_seconds)
        self._python_environment_lock = threading.Lock()

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

    @staticmethod
    def _site_packages(venv_path: Path) -> Path:
        candidates = sorted((venv_path / "lib").glob("python*/site-packages"))
        if len(candidates) != 1 or not candidates[0].is_dir():
            raise LocalExecutionWorkerRuntimeError(
                f"virtual environment has no unambiguous site-packages directory: {venv_path}"
            )
        return candidates[0]

    @staticmethod
    def _project_venv(workspace) -> Path | None:
        primary = getattr(workspace, "repository_resource_id", None)
        for member in getattr(workspace, "repository_members", ()):
            if member.resource_id != primary:
                continue
            source_path = getattr(member, "source_path", None)
            if not source_path:
                continue
            candidate = Path(source_path) / ".venv"
            if candidate.is_dir():
                return candidate.resolve(strict=True)
        return None

    def python_environment(
        self,
        assignment: ExecutionAssignment,
    ) -> ExecutionPythonEnvironment:
        """Provide an execution-local venv with an optional project baseline.

        The writable venv always lives inside the isolated execution workspace.
        A project-maintained venv is exposed read-only and added as a baseline
        package layer, so parallel executions cannot mutate shared dependencies.
        """

        workspace = self._workspace(assignment)
        if not workspace.path:
            raise LocalExecutionWorkerRuntimeError(
                "Python execution requires a filesystem execution workspace"
            )
        workspace_path = Path(workspace.path).resolve(strict=True)
        execution_venv = workspace_path / ".venv"
        marker = execution_venv / ".codex-web-execution-venv"
        with self._python_environment_lock:
            if execution_venv.exists() and not marker.is_file():
                raise LocalExecutionWorkerRuntimeError(
                    "execution workspace .venv exists but is not managed by codex-web"
                )
            if not execution_venv.exists():
                temporary = workspace_path / f".venv.provisioning-{uuid.uuid4().hex}"
                try:
                    completed = subprocess.run(
                        [
                            "/usr/bin/python3",
                            "-m",
                            "venv",
                            "--system-site-packages",
                            str(temporary),
                        ],
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=120,
                        env={
                            "PATH": "/usr/local/bin:/usr/bin:/bin",
                            "PYTHONNOUSERSITE": "1",
                        },
                    )
                    if completed.returncode != 0:
                        detail = (completed.stderr or completed.stdout).strip()
                        raise LocalExecutionWorkerRuntimeError(
                            "failed to provision execution Python environment"
                            + (f": {detail[:500]}" if detail else "")
                        )
                    (temporary / ".codex-web-execution-venv").write_text(
                        "managed by codex-web; safe to discard with this execution workspace\n",
                        encoding="utf-8",
                    )
                    os.replace(temporary, execution_venv)
                finally:
                    if temporary.exists():
                        shutil.rmtree(temporary, ignore_errors=True)

        path_entries = [str(execution_venv / "bin")]
        readonly_mounts: tuple[tuple[Path, Path], ...] = ()
        project_venv = self._project_venv(workspace)
        if project_venv is not None and project_venv != execution_venv:
            project_site_packages = self._site_packages(project_venv)
            execution_site_packages = self._site_packages(execution_venv)
            baseline_file = execution_site_packages / "codex_web_project_baseline.pth"
            expected = f"{project_site_packages}\n"
            if not baseline_file.exists() or baseline_file.read_text(
                encoding="utf-8"
            ) != expected:
                baseline_file.write_text(expected, encoding="utf-8")
            readonly_mounts = ((project_venv, project_venv),)
            path_entries.append(str(project_venv / "bin"))

        path_entries.extend(("/usr/local/bin", "/usr/bin", "/bin"))
        return ExecutionPythonEnvironment(
            environment={
                "PATH": ":".join(path_entries),
                "VIRTUAL_ENV": str(execution_venv),
                "PYTHONNOUSERSITE": "1",
                "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                "PIP_REQUIRE_VIRTUALENV": "1",
            },
            readonly_mounts=readonly_mounts,
        )

    def repository_mounts(
        self,
        assignment: ExecutionAssignment,
    ) -> tuple[
        tuple[tuple[Path, Path], ...],
        tuple[tuple[Path, Path], ...],
    ]:
        workspace = self._workspace(assignment)
        scope = assignment.repository_scope
        target = assignment.repository_target
        if scope is not None:
            writable = set(scope.writable_repository_ids)
            read_only = set(scope.read_only_repository_ids)
        elif target is not None:
            writable = (
                {target.mutable_repository_id}
                if target.mutable_repository_id is not None
                else set()
            )
            read_only = set(target.read_only_repository_ids)
        else:
            writable = set()
            read_only = set()

        primary = getattr(workspace, "repository_resource_id", None)
        if scope is None and target is None and primary is not None:
            # Legacy assignments predate explicit repository authority on the
            # worker contract. The execution workspace lease remains canonical
            # for their singular primary repository.
            writable = {primary}
        if primary is not None and primary not in writable:
            raise LocalExecutionWorkerRuntimeError(
                "primary repository is outside assignment repository authority"
            )

        readonly_mounts: list[tuple[Path, Path]] = []
        writable_mounts: list[tuple[Path, Path]] = []
        for member in getattr(workspace, "repository_members", ()):
            if member.resource_id == primary:
                continue
            source = Path(member.workspace_path).resolve(strict=True)
            destination = Path(member.sandbox_path)
            if not destination.is_absolute():
                raise LocalExecutionWorkerRuntimeError(
                    "repository sandbox path must be absolute"
                )
            if member.resource_id in writable:
                if member.access_mode.value != "write":
                    raise LocalExecutionWorkerRuntimeError(
                        "writable repository member does not hold a write lease"
                    )
                writable_mounts.append((source, destination))
                continue
            if member.resource_id in read_only:
                if member.access_mode.value != "read":
                    raise LocalExecutionWorkerRuntimeError(
                        "read-only repository member must remain read-only"
                    )
                readonly_mounts.append((source, destination))
                continue
            raise LocalExecutionWorkerRuntimeError(
                "workspace contains repository outside assignment authority"
            )
        return tuple(readonly_mounts), tuple(writable_mounts)

    def readonly_mounts(
        self,
        assignment: ExecutionAssignment,
    ) -> tuple[tuple[Path, Path], ...]:
        readonly, _writable = self.repository_mounts(assignment)
        return readonly

    def writable_mounts(
        self,
        assignment: ExecutionAssignment,
    ) -> tuple[tuple[Path, Path], ...]:
        _readonly, writable = self.repository_mounts(assignment)
        return writable

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
        readonly_mounts, writable_mounts = self.repository_mounts(assignment)
        python_environment = self.python_environment(assignment)
        readonly_mounts = (*readonly_mounts, *python_environment.readonly_mounts)
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
                "environment": python_environment.environment,
            }
            if readonly_mounts:
                run_kwargs["trusted_readonly_mounts"] = readonly_mounts
            if writable_mounts:
                run_kwargs["trusted_writable_mounts"] = writable_mounts
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
