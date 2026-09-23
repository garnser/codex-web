from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path
from types import SimpleNamespace

from codex_web.execution_workers import (
    AssignmentCompleteRequest,
    AssignmentRenewRequest,
    AssignmentStartRequest,
    AssignmentStatus,
    ExecutionAssignment,
    ExecutionRuntimeBinding,
    WorkerHeartbeatRequest,
    WorkerLifecycle,
)
from codex_web.services.agent_worker_session import AssignmentBoundAgentSessionStatus
from codex_web.services.local_execution_worker import LocalExecutionWorkerRuntime


class AssignmentBoundCliSessionError(RuntimeError):
    pass


class AssignmentBoundCliSessionStaleError(AssignmentBoundCliSessionError):
    pass


class AssignmentBoundCliSession:
    """Canonical assignment/workspace lease for a host-owned CLI runtime.

    Unlike app-server sessions this object launches no provider process. It
    claims and fences the canonical execution assignment, keeps its lease alive,
    and exposes the isolated repository worktree as the only CLI cwd. The
    provider CLI process itself is owned by the AgentRuntime adapter.
    """

    def __init__(
        self,
        local_worker: LocalExecutionWorkerRuntime,
        assignment_id: str,
        *,
        runtime_binding: ExecutionRuntimeBinding,
        watchdog_interval_seconds: float = 1.0,
        clock=time.time,
        monotonic=time.monotonic,
        sleep=asyncio.sleep,
    ) -> None:
        self.local_worker = local_worker
        self.assignment_id = assignment_id
        self.runtime_binding = runtime_binding
        self.watchdog_interval_seconds = max(
            0.05,
            float(watchdog_interval_seconds),
        )
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep
        self.fence: int | None = None
        self.workspace_path: Path | None = None
        self.started_monotonic: float | None = None
        self.last_heartbeat_monotonic: float | None = None
        self.last_error: str | None = None
        self.watchdog_task: asyncio.Task[None] | None = None
        self.runtime = SimpleNamespace(native_session_id=None)
        self._start_lock = asyncio.Lock()
        self._stopping = False

    @property
    def worker_id(self) -> str:
        return self.local_worker.worker.id

    def remember_native_session_id(self, session_id: str | None) -> None:
        normalized = str(session_id or "").strip()
        if normalized:
            self.runtime.native_session_id = normalized

    def _current_worker(self):
        worker = next(
            (
                item
                for item in self.local_worker.worker_service.store.load().workers
                if item.id == self.worker_id
                and item.organization_id
                == self.local_worker.worker_actor.organization_id
                and item.workspace_id
                == self.local_worker.worker_actor.workspace_id
            ),
            None,
        )
        if worker is None:
            raise AssignmentBoundCliSessionStaleError(
                "CLI execution worker no longer exists"
            )
        if worker.lifecycle not in {
            WorkerLifecycle.ACTIVE,
            WorkerLifecycle.DRAINING,
        }:
            raise AssignmentBoundCliSessionStaleError(
                f"CLI execution worker is {worker.lifecycle.value}"
            )
        return worker

    def _current_assignment(self) -> ExecutionAssignment:
        assignment = self.local_worker._pending_assignment(self.assignment_id)
        if assignment.runtime_binding != self.runtime_binding:
            raise AssignmentBoundCliSessionStaleError(
                "CLI assignment runtime binding changed"
            )
        if self.fence is not None:
            lease = assignment.lease
            if (
                assignment.assigned_worker_id != self.worker_id
                or assignment.fence != self.fence
                or lease is None
                or lease.worker_id != self.worker_id
                or lease.fence != self.fence
            ):
                raise AssignmentBoundCliSessionStaleError(
                    "CLI assignment lease/fence changed"
                )
        return assignment

    @staticmethod
    def _require_supported_repository_layout(
        assignment: ExecutionAssignment,
    ) -> None:
        scope = assignment.repository_scope
        target = assignment.repository_target
        if scope is not None:
            if (
                len(scope.writable_repository_ids) > 1
                or scope.read_only_repository_ids
            ):
                raise AssignmentBoundCliSessionError(
                    "CLI runtime currently requires one writable repository "
                    "without secondary repository mounts"
                )
            return
        if target is not None and target.read_only_repository_ids:
            raise AssignmentBoundCliSessionError(
                "CLI runtime currently does not support secondary "
                "read-only repository mounts"
            )

    def _prepare_assignment(self) -> tuple[ExecutionAssignment, Path]:
        assignment = self.local_worker._pending_assignment(self.assignment_id)
        if assignment.runtime_binding != self.runtime_binding:
            raise AssignmentBoundCliSessionStaleError(
                "CLI assignment runtime binding is incompatible"
            )
        self._require_supported_repository_layout(assignment)
        workspace_path = self.local_worker._workspace_path(assignment)

        worker = self._current_worker()
        if (
            assignment.status == AssignmentStatus.PENDING
            and worker.lifecycle != WorkerLifecycle.ACTIVE
        ):
            raise AssignmentBoundCliSessionStaleError(
                "draining worker cannot claim a new CLI assignment"
            )

        self.local_worker.worker_service.heartbeat(
            self.worker_id,
            WorkerHeartbeatRequest(version=worker.version),
            actor=self.local_worker.worker_actor,
        )
        assignment = self.local_worker._claim_or_resume(assignment)
        lease = assignment.lease
        if lease is None:
            raise AssignmentBoundCliSessionStaleError(
                "CLI assignment requires a live worker lease"
            )
        if assignment.status == AssignmentStatus.CLAIMED:
            assignment = self.local_worker.worker_service.start(
                self.worker_id,
                assignment.id,
                AssignmentStartRequest(
                    lease_token=lease.lease_token,
                    fence=lease.fence,
                ),
                actor=self.local_worker.worker_actor,
            )
            lease = assignment.lease
            if lease is None:
                raise AssignmentBoundCliSessionStaleError(
                    "CLI assignment lost its worker lease at start"
                )
        if assignment.status != AssignmentStatus.RUNNING:
            raise AssignmentBoundCliSessionStaleError(
                "CLI assignment is not running"
            )
        return assignment, workspace_path

    async def start(self) -> "AssignmentBoundCliSession":
        async with self._start_lock:
            if self.fence is not None:
                self.validate_current()
                return self
            assignment, workspace_path = self._prepare_assignment()
            lease = assignment.lease
            if lease is None:
                raise AssignmentBoundCliSessionStaleError(
                    "CLI assignment has no fenced lease"
                )
            self.fence = lease.fence
            self.workspace_path = workspace_path
            self.started_monotonic = self._monotonic()
            self.last_heartbeat_monotonic = self.started_monotonic
            self.watchdog_task = asyncio.create_task(
                self._watchdog(),
                name=f"cli-assignment-watchdog-{self.assignment_id}",
            )
            return self

    def _validate_resource_bounds(
        self,
        assignment: ExecutionAssignment,
    ) -> None:
        if self.workspace_path is None or self.started_monotonic is None:
            raise AssignmentBoundCliSessionStaleError(
                "CLI assignment has no execution workspace"
            )
        if (
            self._monotonic() - self.started_monotonic
            >= assignment.limits.wall_seconds
        ):
            raise AssignmentBoundCliSessionStaleError(
                "CLI assignment exceeded wall_seconds"
            )
        disk_bytes = self.local_worker.backend.execution_disk_usage(
            self.workspace_path,
            None,
        )
        disk_bytes += self.local_worker.readonly_disk_bytes(assignment)
        if disk_bytes > assignment.limits.disk_bytes:
            raise AssignmentBoundCliSessionStaleError(
                "CLI assignment exceeded disk_bytes"
            )

    def validate_current(self) -> ExecutionAssignment:
        if self.fence is None:
            raise AssignmentBoundCliSessionStaleError(
                "CLI assignment has no active fence"
            )
        self._current_worker()
        assignment = self._current_assignment()
        if assignment.status != AssignmentStatus.RUNNING:
            raise AssignmentBoundCliSessionStaleError(
                f"CLI assignment is {assignment.status.value}"
            )
        if (
            assignment.deadline_at is not None
            and assignment.deadline_at <= self._clock()
        ):
            raise AssignmentBoundCliSessionStaleError(
                "CLI assignment deadline expired"
            )
        self._validate_resource_bounds(assignment)
        return assignment

    def status(self) -> AssignmentBoundAgentSessionStatus:
        try:
            assignment = self.validate_current()
            running = assignment.status == AssignmentStatus.RUNNING
            error = self.last_error
        except Exception as exc:
            running = False
            error = self.last_error or str(exc)
        return AssignmentBoundAgentSessionStatus(
            assignment_id=self.assignment_id,
            worker_id=self.worker_id,
            fence=self.fence,
            running=running,
            ready=running,
            last_error=error,
            credential_expires_at=None,
        )

    def _heartbeat_and_renew(
        self,
        assignment: ExecutionAssignment,
    ) -> ExecutionAssignment:
        now_monotonic = self._monotonic()
        if (
            self.last_heartbeat_monotonic is None
            or now_monotonic - self.last_heartbeat_monotonic
            >= self.local_worker.heartbeat_interval_seconds
        ):
            worker = self._current_worker()
            self.local_worker.worker_service.heartbeat(
                self.worker_id,
                WorkerHeartbeatRequest(version=worker.version),
                actor=self.local_worker.worker_actor,
            )
            self.last_heartbeat_monotonic = now_monotonic

        lease = assignment.lease
        if lease is None:
            raise AssignmentBoundCliSessionStaleError(
                "CLI assignment lease disappeared"
            )
        if (
            lease.expires_at - self._clock()
            <= self.local_worker.renew_margin_seconds
        ):
            assignment = self.local_worker.worker_service.renew(
                self.worker_id,
                assignment.id,
                AssignmentRenewRequest(
                    lease_token=lease.lease_token,
                    fence=lease.fence,
                    lease_seconds=120,
                ),
                actor=self.local_worker.worker_actor,
            )
        return assignment

    async def _watchdog(self) -> None:
        while not self._stopping:
            await self._sleep(self.watchdog_interval_seconds)
            if self._stopping:
                return
            try:
                assignment = self.validate_current()
                self._heartbeat_and_renew(assignment)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)
                return

    async def request(self, method: str, params=None) -> dict:
        del method, params
        self.validate_current()
        raise AssignmentBoundCliSessionError(
            "CLI assignment has no app-server request transport"
        )

    async def notify(self, method: str, params=None) -> None:
        del method, params
        self.validate_current()
        raise AssignmentBoundCliSessionError(
            "CLI assignment has no app-server notification transport"
        )

    async def respond_to_server_request(
        self,
        request_id: int | str,
        result: dict,
    ) -> None:
        del request_id, result
        self.validate_current()
        raise AssignmentBoundCliSessionError(
            "CLI assignment does not support interactive server requests"
        )

    async def stop(self) -> None:
        self._stopping = True
        task = self.watchdog_task
        self.watchdog_task = None
        if (
            task is not None
            and task is not asyncio.current_task()
            and not task.done()
        ):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._stopping = False


class AssignmentBoundCliSessionManager:
    def __init__(
        self,
        local_worker: LocalExecutionWorkerRuntime,
        *,
        runtime_binding: ExecutionRuntimeBinding,
        watchdog_interval_seconds: float = 1.0,
    ) -> None:
        self.local_worker = local_worker
        self.runtime_binding = runtime_binding
        self.watchdog_interval_seconds = watchdog_interval_seconds
        self.sessions: dict[str, AssignmentBoundCliSession] = {}
        self._lock = asyncio.Lock()

    async def start(self, assignment_id: str) -> AssignmentBoundCliSession:
        async with self._lock:
            existing = self.sessions.get(assignment_id)
            if existing is not None:
                if existing.status().ready:
                    return existing
                raise AssignmentBoundCliSessionStaleError(
                    "existing CLI assignment session is not reusable"
                )
            session = AssignmentBoundCliSession(
                self.local_worker,
                assignment_id,
                runtime_binding=self.runtime_binding,
                watchdog_interval_seconds=self.watchdog_interval_seconds,
            )
            await session.start()
            self.sessions[assignment_id] = session
            return session

    def get(self, assignment_id: str) -> AssignmentBoundCliSession | None:
        return self.sessions.get(assignment_id)

    async def complete(
        self,
        assignment_id: str,
        *,
        succeeded: bool,
        failure_code: str | None = None,
        failure_message: str | None = None,
        artifact_ids: tuple[str, ...] = (),
        evidence_ids: tuple[str, ...] = (),
    ) -> ExecutionAssignment:
        session = self.sessions.get(assignment_id)
        if session is None:
            raise AssignmentBoundCliSessionStaleError(
                "CLI assignment session is not registered"
            )
        assignment = session.validate_current()
        lease = assignment.lease
        if lease is None or session.fence is None:
            raise AssignmentBoundCliSessionStaleError(
                "CLI assignment has no completable fenced lease"
            )
        completed = self.local_worker.worker_service.complete(
            session.worker_id,
            assignment.id,
            AssignmentCompleteRequest(
                lease_token=lease.lease_token,
                fence=session.fence,
                succeeded=succeeded,
                failure_code=failure_code,
                failure_message=failure_message,
                artifact_ids=artifact_ids,
                evidence_ids=evidence_ids,
            ),
            actor=self.local_worker.worker_actor,
        )
        await self.stop(assignment_id)
        return completed

    async def stop(self, assignment_id: str) -> None:
        async with self._lock:
            session = self.sessions.pop(assignment_id, None)
        if session is not None:
            await session.stop()

    async def stop_all(self) -> None:
        async with self._lock:
            sessions = list(self.sessions.values())
            self.sessions.clear()
        for session in sessions:
            with contextlib.suppress(Exception):
                await session.stop()
