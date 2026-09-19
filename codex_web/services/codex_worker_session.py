from __future__ import annotations

import asyncio
import contextlib
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from codex_web.execution_workers import (
    AssignmentCompleteRequest,
    AssignmentRenewRequest,
    AssignmentStartRequest,
    AssignmentStatus,
    ExecutionAssignment,
    WorkerHeartbeatRequest,
    WorkerLifecycle,
)
from codex_web.runtime.codex import CodexRuntime
from codex_web.services.agent_worker_session import (
    AssignmentBoundAgentSessionStatus,
    AssignmentRuntimeCredentialGrant,
    AssignmentRuntimeCredentialProvider,
    AssignmentRuntimeLaunchInput,
)
from codex_web.services.codex_model_egress import (
    AssignmentBoundModelEgressBroker,
    CODEX_MODEL_EGRESS_RELAY_SCRIPT,
    CodexModelEgressEndpoint,
)
from codex_web.services.local_execution_worker import (
    LocalExecutionWorkerRuntime,
    LocalExecutionWorkerRuntimeError,
)


class AssignmentBoundCodexSessionError(RuntimeError):
    pass


class AssignmentBoundCodexSessionStaleError(AssignmentBoundCodexSessionError):
    pass


# Compatibility alias while callers migrate to the provider-neutral worker-session contract.
AssignmentBoundCodexSessionStatus = AssignmentBoundAgentSessionStatus


class _OneShotProcessFactory:
    """Return one already-authenticated process and never restart it implicitly."""

    def __init__(self, process) -> None:
        self.process = process
        self.used = False

    def __call__(self, *args, **kwargs):
        if self.used:
            raise AssignmentBoundCodexSessionStaleError(
                "assignment-bound Codex process cannot restart without fresh lease/auth validation"
            )
        self.used = True
        return self.process


class AssignmentBoundCodexSession:
    """One Codex app-server bound to one canonical worker assignment/fence.

    The raw delegated credential is consumed only while Bubblewrap starts the
    trusted Codex process. The session retains the subprocess and metadata-only
    delegation, then continuously validates the canonical lease, worker trust,
    assignment deadline, credential rotation/expiry and local resource bounds.
    """

    def __init__(
        self,
        local_worker: LocalExecutionWorkerRuntime,
        host: Any,
        assignment_id: str,
        *,
        runtime_factory: Callable[..., CodexRuntime] = CodexRuntime,
        watchdog_interval_seconds: float = 1.0,
        egress_endpoints_resolver: Callable[[], tuple[CodexModelEgressEndpoint, ...]] | None = None,
        credential_provider: AssignmentRuntimeCredentialProvider | None = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self.local_worker = local_worker
        self.host = host
        self.assignment_id = assignment_id
        self.runtime_factory = runtime_factory
        self.watchdog_interval_seconds = max(0.05, watchdog_interval_seconds)
        self.egress_endpoints_resolver = egress_endpoints_resolver
        self.credential_provider = credential_provider or local_worker.codex_auth_delegation
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep

        self.runtime: CodexRuntime | None = None
        self.delegation: AssignmentRuntimeCredentialGrant | None = None
        self.fence: int | None = None
        self.workspace_path: Path | None = None
        self.git_metadata_path: Path | None = None
        self.started_monotonic: float | None = None
        self.last_heartbeat_monotonic: float | None = None
        self.last_error: str | None = None
        self.watchdog_task: asyncio.Task[None] | None = None
        self.egress_broker: AssignmentBoundModelEgressBroker | None = None
        self._start_lock = asyncio.Lock()
        self._stopping = False

    @property
    def worker_id(self) -> str:
        return self.local_worker.worker.id

    def status(self) -> AssignmentBoundCodexSessionStatus:
        proc = self.runtime.proc if self.runtime is not None else None
        running = bool(proc is not None and proc.poll() is None)
        ready = bool(running and self.runtime is not None and self.runtime.ready.is_set())
        return AssignmentBoundAgentSessionStatus(
            assignment_id=self.assignment_id,
            worker_id=self.worker_id,
            fence=self.fence,
            running=running,
            ready=ready,
            last_error=self.last_error,
            credential_expires_at=(
                self.delegation.expires_at if self.delegation is not None else None
            ),
        )

    def _current_worker(self):
        worker = next(
            (
                item
                for item in self.local_worker.worker_service.store.load().workers
                if item.id == self.worker_id
                and item.organization_id == self.local_worker.worker_actor.organization_id
                and item.workspace_id == self.local_worker.worker_actor.workspace_id
            ),
            None,
        )
        if worker is None:
            raise AssignmentBoundCodexSessionStaleError(
                "assignment-bound Codex worker no longer exists"
            )
        if worker.lifecycle not in {
            WorkerLifecycle.ACTIVE,
            WorkerLifecycle.DRAINING,
        }:
            raise AssignmentBoundCodexSessionStaleError(
                f"assignment-bound Codex worker is {worker.lifecycle.value}"
            )
        return worker

    def _current_assignment(self) -> ExecutionAssignment:
        assignment = self.local_worker._pending_assignment(self.assignment_id)
        if self.fence is not None:
            lease = assignment.lease
            if (
                assignment.assigned_worker_id != self.worker_id
                or assignment.fence != self.fence
                or lease is None
                or lease.worker_id != self.worker_id
                or lease.fence != self.fence
            ):
                raise AssignmentBoundCodexSessionStaleError(
                    "assignment-bound Codex lease/fence changed"
                )
        return assignment

    def _prepare_assignment(self) -> tuple[ExecutionAssignment, Path]:
        assignment = self.local_worker._pending_assignment(self.assignment_id)
        workspace_path = self.local_worker._workspace_path(assignment)
        self.local_worker.backend.validate_assignment(assignment)

        worker = self._current_worker()
        if (
            assignment.status == AssignmentStatus.PENDING
            and worker.lifecycle != WorkerLifecycle.ACTIVE
        ):
            raise AssignmentBoundCodexSessionStaleError(
                "draining worker cannot claim a new Codex assignment"
            )

        self.local_worker.worker_service.heartbeat(
            self.worker_id,
            WorkerHeartbeatRequest(version=worker.version),
            actor=self.local_worker.worker_actor,
        )
        assignment = self.local_worker._claim_or_resume(assignment)
        lease = assignment.lease
        if lease is None:
            raise AssignmentBoundCodexSessionStaleError(
                "assignment-bound Codex session requires a live worker lease"
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
                raise AssignmentBoundCodexSessionStaleError(
                    "assignment-bound Codex session lost its worker lease at start"
                )

        if assignment.status != AssignmentStatus.RUNNING:
            raise AssignmentBoundCodexSessionStaleError(
                f"assignment-bound Codex session requires running assignment, got {assignment.status.value}"
            )
        return assignment, workspace_path

    def _spawn_delegated_process(
        self,
        assignment: ExecutionAssignment,
        workspace_path: Path,
        broker: AssignmentBoundModelEgressBroker | None,
    ):
        delegation_service = self.credential_provider
        if delegation_service is None:
            raise AssignmentBoundCodexSessionError(
                "Codex auth delegation is not configured"
            )
        lease = assignment.lease
        if lease is None:
            raise AssignmentBoundCodexSessionStaleError(
                "Codex process launch requires an active worker lease"
            )

        def launch(launch_input: AssignmentRuntimeLaunchInput):
            command = launch_input.command
            environment = dict(launch_input.environment)
            trusted_mounts: tuple[tuple[Path, Path], ...] = ()
            if broker is not None:
                environment.update(
                    {
                        "HTTP_PROXY": broker.proxy_url,
                        "HTTPS_PROXY": broker.proxy_url,
                        "ALL_PROXY": "",
                        "NO_PROXY": "",
                        "http_proxy": broker.proxy_url,
                        "https_proxy": broker.proxy_url,
                        "all_proxy": "",
                        "no_proxy": "",
                    }
                )
                command = (
                    "/usr/bin/python3",
                    "-u",
                    "-c",
                    CODEX_MODEL_EGRESS_RELAY_SCRIPT,
                    str(broker.sandbox_socket_path),
                    "8787",
                    *launch_input.command,
                )
                trusted_mounts = (
                    (broker.mount_source, broker.mount_destination),
                )
            process = self.local_worker.backend.spawn_interactive(
                assignment,
                argv=command,
                workspace_path=workspace_path,
                environment=environment,
                trusted_readonly_mounts=trusted_mounts,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            # The returned tuple contains no credential value. SecretBroker.use()
            # still performs its boundary escape check before returning it.
            return (
                process,
                launch_input.delegation,
                tuple(command),
            )

        return delegation_service.use(
            assignment,
            worker_id=self.worker_id,
            fence=lease.fence,
            actor=self.local_worker.worker_actor,
            consumer=launch,
        )

    def _validate_egress_state(self) -> ExecutionAssignment:
        self._current_worker()
        assignment = self._current_assignment()
        if assignment.status != AssignmentStatus.RUNNING:
            raise AssignmentBoundCodexSessionStaleError(
                f"assignment-bound Codex assignment is {assignment.status.value}"
            )
        if (
            assignment.deadline_at is not None
            and assignment.deadline_at <= self._clock()
        ):
            raise AssignmentBoundCodexSessionStaleError(
                "assignment-bound Codex assignment deadline expired"
            )
        if self.delegation is not None:
            delegation_service = self.credential_provider
            if delegation_service is None:
                raise AssignmentBoundCodexSessionStaleError(
                    "Codex auth delegation is unavailable"
                )
            delegation_service.validate_current(
                self.delegation,
                assignment,
                actor=self.local_worker.worker_actor,
            )
        return assignment

    async def _start_egress_broker(
        self,
    ) -> AssignmentBoundModelEgressBroker | None:
        resolver = self.egress_endpoints_resolver
        if resolver is None:
            return None
        endpoints = resolver()
        broker = AssignmentBoundModelEgressBroker(
            endpoints,
            validator=self._validate_egress_state,
        )
        await broker.start()
        return broker

    async def start(self) -> "AssignmentBoundCodexSession":
        async with self._start_lock:
            current = self.status()
            if current.running and current.ready:
                return self
            if self.runtime is not None:
                raise AssignmentBoundCodexSessionStaleError(
                    "assignment-bound Codex session cannot restart in place"
                )

            assignment, workspace_path = self._prepare_assignment()
            lease = assignment.lease
            if lease is None:
                raise AssignmentBoundCodexSessionStaleError(
                    "assignment-bound Codex session requires an active lease"
                )
            process = None
            broker = None
            try:
                self.fence = lease.fence
                broker = await self._start_egress_broker()
                self.egress_broker = broker
                process, delegation, command = self._spawn_delegated_process(
                    assignment,
                    workspace_path,
                    broker,
                )
                self.delegation = delegation
                self.workspace_path = workspace_path
                self.git_metadata_path = self.local_worker.backend.discover_git_metadata(
                    workspace_path
                )
                self.started_monotonic = self._monotonic()
                self.last_heartbeat_monotonic = self.started_monotonic
                self.runtime = self.runtime_factory(
                    self.host,
                    command=command,
                    cwd=workspace_path,
                    popen=_OneShotProcessFactory(process),
                )
                self.runtime.approval_namespace = assignment.id
                await self.runtime.start()
                self.watchdog_task = asyncio.create_task(
                    self._watchdog(),
                    name=f"codex-worker-session-{self.assignment_id}",
                )
                return self
            except Exception as exc:
                self.last_error = str(exc)
                if self.runtime is not None:
                    with contextlib.suppress(Exception):
                        await self.runtime.stop()
                elif process is not None and process.poll() is None:
                    with contextlib.suppress(Exception):
                        self.local_worker.backend.terminate_process(process)
                if broker is not None:
                    with contextlib.suppress(Exception):
                        await broker.stop()
                    if self.egress_broker is broker:
                        self.egress_broker = None
                raise

    def _validate_resource_bounds(self, assignment: ExecutionAssignment) -> None:
        if self.workspace_path is None or self.started_monotonic is None:
            raise AssignmentBoundCodexSessionStaleError(
                "assignment-bound Codex session has no execution workspace"
            )
        if (
            self._monotonic() - self.started_monotonic
            >= assignment.limits.wall_seconds
        ):
            raise AssignmentBoundCodexSessionStaleError(
                "assignment-bound Codex session exceeded wall_seconds"
            )
        disk_bytes = self.local_worker.backend.execution_disk_usage(
            self.workspace_path,
            self.git_metadata_path,
        )
        if disk_bytes > assignment.limits.disk_bytes:
            raise AssignmentBoundCodexSessionStaleError(
                "assignment-bound Codex session exceeded disk_bytes"
            )

    def validate_current(self) -> ExecutionAssignment:
        if self.delegation is None or self.fence is None:
            raise AssignmentBoundCodexSessionStaleError(
                "assignment-bound Codex session has no active delegation"
            )
        self._current_worker()
        assignment = self._current_assignment()
        if assignment.status != AssignmentStatus.RUNNING:
            raise AssignmentBoundCodexSessionStaleError(
                f"assignment-bound Codex assignment is {assignment.status.value}"
            )
        if (
            assignment.deadline_at is not None
            and assignment.deadline_at <= self._clock()
        ):
            raise AssignmentBoundCodexSessionStaleError(
                "assignment-bound Codex assignment deadline expired"
            )
        delegation_service = self.credential_provider
        if delegation_service is None:
            raise AssignmentBoundCodexSessionStaleError(
                "Codex auth delegation is unavailable"
            )
        delegation_service.validate_current(
            self.delegation,
            assignment,
            actor=self.local_worker.worker_actor,
        )
        self._validate_resource_bounds(assignment)
        return assignment

    def _heartbeat_and_renew(self, assignment: ExecutionAssignment) -> ExecutionAssignment:
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
            raise AssignmentBoundCodexSessionStaleError(
                "assignment-bound Codex lease disappeared"
            )
        if lease.expires_at - self._clock() <= self.local_worker.renew_margin_seconds:
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
            runtime = self.runtime
            process = runtime.proc if runtime is not None else None
            if process is None or process.poll() is not None:
                self.last_error = self.last_error or "Codex app-server process exited"
                return
            try:
                assignment = self.validate_current()
                self._heartbeat_and_renew(assignment)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)
                await self._stop_runtime_from_watchdog()
                return

    async def _stop_runtime_from_watchdog(self) -> None:
        self._stopping = True
        try:
            if self.runtime is not None:
                await self.runtime.stop()
        finally:
            self._stopping = False

    async def request(self, method: str, params: Any = None) -> dict[str, Any]:
        self.validate_current()
        if self.runtime is None:
            raise AssignmentBoundCodexSessionStaleError(
                "assignment-bound Codex session is not started"
            )
        return await self.runtime.request(method, params)

    async def notify(self, method: str, params: Any = None) -> None:
        self.validate_current()
        if self.runtime is None:
            raise AssignmentBoundCodexSessionStaleError(
                "assignment-bound Codex session is not started"
            )
        await self.runtime.notify(method, params)

    async def respond_to_server_request(
        self,
        request_id: int | str,
        result: dict[str, Any],
    ) -> None:
        self.validate_current()
        if self.runtime is None:
            raise AssignmentBoundCodexSessionStaleError(
                "assignment-bound Codex session is not started"
            )
        await self.runtime.respond_to_server_request(request_id, result)

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
        if self.runtime is not None:
            await self.runtime.stop()
        broker = self.egress_broker
        self.egress_broker = None
        if broker is not None:
            await broker.stop()
        self._stopping = False


class AssignmentBoundCodexSessionManager:
    """Own at most one assignment-bound Codex session per canonical assignment."""

    def __init__(
        self,
        local_worker: LocalExecutionWorkerRuntime,
        host: Any,
        *,
        runtime_factory: Callable[..., CodexRuntime] = CodexRuntime,
        watchdog_interval_seconds: float = 1.0,
        egress_endpoints_resolver: Callable[[], tuple[CodexModelEgressEndpoint, ...]] | None = None,
        credential_provider: AssignmentRuntimeCredentialProvider | None = None,
    ) -> None:
        self.local_worker = local_worker
        self.host = host
        self.runtime_factory = runtime_factory
        self.watchdog_interval_seconds = watchdog_interval_seconds
        self.egress_endpoints_resolver = egress_endpoints_resolver
        self.credential_provider = credential_provider
        self.sessions: dict[str, AssignmentBoundCodexSession] = {}
        self._lock = asyncio.Lock()

    async def start(self, assignment_id: str) -> AssignmentBoundCodexSession:
        async with self._lock:
            existing = self.sessions.get(assignment_id)
            if existing is not None:
                status = existing.status()
                if status.running and status.ready:
                    return existing
                raise AssignmentBoundCodexSessionStaleError(
                    "existing assignment-bound Codex session is not reusable"
                )
            session = AssignmentBoundCodexSession(
                self.local_worker,
                self.host,
                assignment_id,
                runtime_factory=self.runtime_factory,
                watchdog_interval_seconds=self.watchdog_interval_seconds,
                egress_endpoints_resolver=self.egress_endpoints_resolver,
                credential_provider=self.credential_provider,
            )
            await session.start()
            self.sessions[assignment_id] = session
            return session

    def get(self, assignment_id: str) -> AssignmentBoundCodexSession | None:
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
            raise AssignmentBoundCodexSessionStaleError(
                "assignment-bound Codex session is not registered"
            )
        assignment = session.validate_current()
        lease = assignment.lease
        if lease is None or session.fence is None:
            raise AssignmentBoundCodexSessionStaleError(
                "assignment-bound Codex session has no completable fenced lease"
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
