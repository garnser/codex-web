from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence, TypeVar

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
from codex_web.execution_workspaces import (
    LeaseMode,
    RepositoryCheckpoint,
    RepositoryCheckpointPushStatus,
)
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
)
from codex_web.services.execution_workspaces import ExecutionWorkspaceService


class LocalExecutionWorkerRuntimeError(RuntimeError):
    pass


class RepositoryCheckpointBlockedError(LocalExecutionWorkerRuntimeError):
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
        trusted_local_codex_delegation: Any | None = None,
        python_tool_root: Path | None = None,
        playwright_tool_root: Path | None = None,
        playwright_browser_root: Path | None = None,
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
        self.python_tool_root = python_tool_root
        self.playwright_tool_root = playwright_tool_root
        self.playwright_browser_root = playwright_browser_root
        self.heartbeat_interval_seconds = max(5.0, heartbeat_interval_seconds)
        self.renew_margin_seconds = max(10.0, renew_margin_seconds)

    def runtime_tool_mounts(self) -> tuple[tuple[Path, Path], ...]:
        """Return optional, read-only validation tools exposed to workers."""
        mounts: list[tuple[Path, Path]] = []
        if self.python_tool_root is not None:
            source = self.python_tool_root.resolve(strict=True)
            mounts.append((source, Path("/opt/codex-python")))
        if self.playwright_tool_root is not None:
            source = self.playwright_tool_root.resolve(strict=True)
            mounts.append((source, Path("/opt/codex-playwright")))
        if self.playwright_browser_root is not None:
            source = self.playwright_browser_root.resolve(strict=True)
            mounts.append((source, Path("/opt/codex-playwright-browsers")))
        return tuple(mounts)

    def _pending_assignment(self, assignment_id: str) -> ExecutionAssignment:
        item = self.worker_service.store.assignment(assignment_id)
        if item is not None and (
            item.organization_id != self.control_actor.organization_id
            or item.workspace_id != self.control_actor.workspace_id
        ):
            item = None
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

    @staticmethod
    def _checkpoint_paths(status: str) -> tuple[str, ...]:
        parts = status.split("\0")
        paths: list[str] = []
        index = 0
        while index < len(parts):
            entry = parts[index]
            index += 1
            if not entry:
                continue
            if len(entry) < 4:
                raise RepositoryCheckpointBlockedError(
                    "execution workspace returned malformed Git status"
                )
            code = entry[:2]
            paths.append(entry[3:])
            if "R" in code or "C" in code:
                if index >= len(parts) or not parts[index]:
                    raise RepositoryCheckpointBlockedError(
                        "execution workspace returned malformed rename status"
                    )
                paths.append(parts[index])
                index += 1
        return tuple(dict.fromkeys(paths))

    @staticmethod
    def _checkpoint_excluded_path(path: str) -> bool:
        normalized = path.replace("\\", "/").casefold().strip("/")
        parts = normalized.split("/")
        name = parts[-1] if parts else normalized
        if name == ".env" or (name.startswith(".env.") and not name.endswith((".example", ".sample", ".template"))):
            return True
        if name in {".npmrc", ".pypirc", ".netrc", "id_rsa", "id_ed25519"}:
            return True
        if name.endswith((".pem", ".p12", ".pfx", ".jks", ".keystore")):
            return True
        return any(part in {".secrets", "private-keys"} for part in parts)

    def _checkpoint_command(
        self,
        assignment: ExecutionAssignment,
        argv: Sequence[str],
    ) -> LocalExecutionResult:
        workspace_path = self._workspace_path(assignment)
        readonly_mounts, writable_mounts = self.repository_mounts(assignment)
        result = self.backend.run(
            assignment,
            argv=argv,
            workspace_path=workspace_path,
            git_metadata_path=self.backend.discover_git_metadata(workspace_path),
            trusted_readonly_mounts=readonly_mounts,
            trusted_writable_mounts=writable_mounts,
            additional_disk_bytes=self.readonly_disk_bytes(assignment),
        )
        if not result.succeeded:
            detail = (result.stderr or result.stdout or "Git checkpoint command failed").strip()
            raise RepositoryCheckpointBlockedError(detail[:500])
        return result

    def checkpoint_assignment(
        self,
        assignment_id: str,
    ) -> tuple[RepositoryCheckpoint, ...]:
        """Create and persist a local Git safety commit before terminal state."""

        assignment = self._pending_assignment(assignment_id)
        if assignment.status != AssignmentStatus.RUNNING:
            raise RepositoryCheckpointBlockedError(
                "repository checkpoint requires a running assignment"
            )
        workspace = self._workspace(assignment)
        writable_repository_ids = tuple(
            getattr(workspace, "writable_repository_ids", ()) or ()
        )
        if not writable_repository_ids:
            return ()
        members = {
            member.resource_id: member
            for member in workspace.repository_members
            if member.access_mode == LeaseMode.WRITE
        }
        checkpoints: list[RepositoryCheckpoint] = []
        blockers: list[str] = []
        for resource_id in writable_repository_ids:
            member = members.get(resource_id)
            if member is None or not member.branch_name:
                blockers.append(f"{resource_id}: canonical writable branch is missing")
                continue
            git_path = (
                member.workspace_path
                if resource_id == workspace.repository_resource_id
                else member.sandbox_path
            )
            status = self._checkpoint_command(
                assignment,
                ("git", "-C", git_path, "status", "--porcelain=v1", "-z", "--untracked-files=all"),
            ).stdout
            paths = self._checkpoint_paths(status)
            head = self._checkpoint_command(
                assignment,
                ("git", "-C", git_path, "rev-parse", "HEAD"),
            ).stdout.strip()
            committed_paths = tuple(
                path
                for path in self._checkpoint_command(
                    assignment,
                    (
                        "git", "-C", git_path, "diff", "--name-only", "-z",
                        f"{member.head_revision}..HEAD",
                    ),
                ).stdout.split("\0")
                if path
            )
            branch = self._checkpoint_command(
                assignment,
                ("git", "-C", git_path, "symbolic-ref", "--short", "HEAD"),
            ).stdout.strip()
            blocker_code = None
            blocker_message = None
            committed = False
            if branch != member.branch_name:
                blocker_code = "checkpoint_branch_mismatch"
                blocker_message = "workspace branch does not match canonical branch"
            excluded = tuple(path for path in paths if self._checkpoint_excluded_path(path))
            if blocker_code is None and excluded:
                blocker_code = "checkpoint_excluded_paths"
                blocker_message = (
                    f"checkpoint refused {len(excluded)} sensitive or policy-excluded path(s)"
                )
            if blocker_code is None and paths:
                try:
                    self._checkpoint_command(
                        assignment,
                        ("git", "-C", git_path, "add", "--all", "--", "."),
                    )
                    self._checkpoint_command(
                        assignment,
                        (
                            "git", "-C", git_path,
                            "-c", "user.name=Codex Checkpoint",
                            "-c", "user.email=checkpoint@codex.invalid",
                            "-c", "core.hooksPath=/dev/null",
                            "commit", "-m", f"chore: checkpoint assignment {assignment.id}",
                        ),
                    )
                    head = self._checkpoint_command(
                        assignment,
                        ("git", "-C", git_path, "rev-parse", "HEAD"),
                    ).stdout.strip()
                    remaining = self._checkpoint_command(
                        assignment,
                        ("git", "-C", git_path, "status", "--porcelain=v1", "-z", "--untracked-files=all"),
                    ).stdout
                    if remaining:
                        blocker_code = "checkpoint_workspace_still_dirty"
                        blocker_message = "workspace remained dirty after safety commit"
                    else:
                        committed = True
                        committed_paths = tuple(
                            path
                            for path in self._checkpoint_command(
                                assignment,
                                (
                                    "git", "-C", git_path, "diff", "--name-only", "-z",
                                    f"{member.head_revision}..HEAD",
                                ),
                            ).stdout.split("\0")
                            if path
                        )
                except RepositoryCheckpointBlockedError as exc:
                    blocker_code = "checkpoint_commit_failed"
                    blocker_message = str(exc)[:500]

            push_status = RepositoryCheckpointPushStatus.NOT_REQUESTED
            remote_branch = None
            remote_revision = None
            change_request_url = None
            if blocker_code is None:
                refs = self._checkpoint_command(
                    assignment,
                    (
                        "git", "-C", git_path, "for-each-ref",
                        "--format=%(refname:short)%00%(objectname)%00", "refs/remotes",
                    ),
                ).stdout.split("\0")
                for ref, revision in zip(refs[0::2], refs[1::2]):
                    if revision.strip() == head and ref and not ref.endswith("/HEAD"):
                        push_status = RepositoryCheckpointPushStatus.VERIFIED
                        remote_branch = ref.strip()
                        remote_revision = head
                        break
                if committed_paths and push_status != RepositoryCheckpointPushStatus.VERIFIED:
                    push_status = RepositoryCheckpointPushStatus.BLOCKED
                    blocker_code = "checkpoint_push_unverified"
                    blocker_message = (
                        "local checkpoint is durable, but no matching remote branch was observed"
                    )
            changed_paths = tuple(dict.fromkeys((*committed_paths, *paths)))
            previous = dict(
                getattr(workspace, "repository_checkpoints", {}) or {}
            ).get(resource_id)
            if (
                not changed_paths
                and previous is not None
                and previous.head_revision == head
            ):
                push_status = previous.push_status
                remote_branch = previous.remote_branch
                remote_revision = previous.remote_revision
                change_request_url = previous.change_request_url
                blocker_code = previous.blocker_code
                blocker_message = previous.blocker_message
            checkpoint = RepositoryCheckpoint(
                resource_id=resource_id,
                branch_name=member.branch_name,
                head_revision=head,
                dirty_file_count=len(paths),
                changed_file_count=len(changed_paths),
                local_commit_created=committed,
                push_status=push_status,
                remote_branch=remote_branch,
                remote_revision=remote_revision,
                change_request_url=change_request_url,
                blocker_code=blocker_code,
                blocker_message=blocker_message,
            )
            self.workspace_service.record_repository_checkpoint(
                workspace.id,
                checkpoint,
                actor=self.control_actor,
            )
            checkpoints.append(checkpoint)
            if blocker_code and blocker_code != "checkpoint_push_unverified":
                blockers.append(f"{resource_id}: {blocker_message}")
        if blockers:
            raise RepositoryCheckpointBlockedError("; ".join(blockers))
        return tuple(checkpoints)

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
