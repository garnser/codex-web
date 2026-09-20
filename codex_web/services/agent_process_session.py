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
    ExecutionRuntimeBinding,
    WorkerHeartbeatRequest,
    WorkerLifecycle,
)
from codex_web.services.control_plane_broker import (
    AssignmentBoundControlPlaneBroker,
    CONTROL_PLANE_RELAY_SCRIPT,
    DeferredControlPlaneBrokerFactory,
)
from codex_web.services.agent_worker_session import (
    AssignmentBoundAgentSessionStatus,
    AssignmentRuntimeCredentialGrant,
    AssignmentRuntimeCredentialProvider,
    AssignmentRuntimeLaunchInput,
)
from codex_web.services.agent_model_egress import (
    AGENT_MODEL_EGRESS_RELAY_SCRIPT,
    AgentRuntimeModelEgressEndpoint,
    AssignmentBoundAgentModelEgressBroker,
)
from codex_web.services.local_execution_worker import (
    LocalExecutionWorkerRuntime,
    LocalExecutionWorkerRuntimeError,
)


class AssignmentBoundAgentProcessSessionError(RuntimeError):
    pass


class AssignmentBoundAgentProcessSessionStaleError(AssignmentBoundAgentProcessSessionError):
    pass


class _OneShotProcessFactory:
    """Return one already-authenticated process and never restart it implicitly."""

    def __init__(self, process) -> None:
        self.process = process
        self.used = False

    def __call__(self, *args, **kwargs):
        if self.used:
            raise AssignmentBoundAgentProcessSessionStaleError(
                "assignment-bound agent runtime process cannot restart without fresh lease/auth validation"
            )
        self.used = True
        return self.process


class AssignmentBoundAgentProcessSession:
    """One execution-agent process bound to one canonical worker assignment/fence.

    Runtime-specific credential and command construction stay behind the injected
    credential provider. This class owns the common worker/lease/fence/workspace,
    process, resource-limit, egress, recovery and completion lifecycle.
    """

    def __init__(
        self,
        local_worker: LocalExecutionWorkerRuntime,
        host: Any,
        assignment_id: str,
        *,
        runtime_factory: Callable[..., Any],
        credential_provider: AssignmentRuntimeCredentialProvider,
        runtime_binding: ExecutionRuntimeBinding | None = None,
        watchdog_interval_seconds: float = 1.0,
        egress_endpoints_resolver: Callable[[], tuple[AgentRuntimeModelEgressEndpoint, ...]] | None = None,
        control_plane_broker_factory: DeferredControlPlaneBrokerFactory | None = None,
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
        self.control_plane_broker_factory = control_plane_broker_factory
        if credential_provider is None:
            raise AssignmentBoundAgentProcessSessionError(
                "runtime credential provider is required"
            )
        self.credential_provider = credential_provider
        self.runtime_binding = runtime_binding
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep

        self.runtime: Any | None = None
        self.delegation: AssignmentRuntimeCredentialGrant | None = None
        self.fence: int | None = None
        self.workspace_path: Path | None = None
        self.git_metadata_path: Path | None = None
        self.started_monotonic: float | None = None
        self.last_heartbeat_monotonic: float | None = None
        self.last_error: str | None = None
        self.watchdog_task: asyncio.Task[None] | None = None
        self.egress_broker: AssignmentBoundAgentModelEgressBroker | None = None
        self.control_plane_broker: AssignmentBoundControlPlaneBroker | None = None
        self._start_lock = asyncio.Lock()
        self._stopping = False

    @property
    def worker_id(self) -> str:
        return self.local_worker.worker.id

    def status(self) -> AssignmentBoundAgentSessionStatus:
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
            raise AssignmentBoundAgentProcessSessionStaleError(
                "assignment-bound agent runtime worker no longer exists"
            )
        if worker.lifecycle not in {
            WorkerLifecycle.ACTIVE,
            WorkerLifecycle.DRAINING,
        }:
            raise AssignmentBoundAgentProcessSessionStaleError(
                f"assignment-bound agent runtime worker is {worker.lifecycle.value}"
            )
        return worker

    def _current_assignment(self) -> ExecutionAssignment:
        assignment = self.local_worker._pending_assignment(self.assignment_id)
        if (
            self.runtime_binding is not None
            and assignment.runtime_binding != self.runtime_binding
        ):
            raise AssignmentBoundAgentProcessSessionStaleError(
                "assignment-bound agent runtime binding changed or is incompatible"
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
                raise AssignmentBoundAgentProcessSessionStaleError(
                    "assignment-bound agent runtime lease/fence changed"
                )
        return assignment

    def _prepare_assignment(self) -> tuple[ExecutionAssignment, Path]:
        assignment = self.local_worker._pending_assignment(self.assignment_id)
        if (
            self.runtime_binding is not None
            and assignment.runtime_binding != self.runtime_binding
        ):
            raise AssignmentBoundAgentProcessSessionStaleError(
                "assignment-bound agent runtime binding is incompatible"
            )
        workspace_path = self.local_worker._workspace_path(assignment)
        self.local_worker.backend.validate_assignment(assignment)

        worker = self._current_worker()
        if (
            assignment.status == AssignmentStatus.PENDING
            and worker.lifecycle != WorkerLifecycle.ACTIVE
        ):
            raise AssignmentBoundAgentProcessSessionStaleError(
                "draining worker cannot claim a new agent runtime assignment"
            )

        self.local_worker.worker_service.heartbeat(
            self.worker_id,
            WorkerHeartbeatRequest(version=worker.version),
            actor=self.local_worker.worker_actor,
        )
        assignment = self.local_worker._claim_or_resume(assignment)
        lease = assignment.lease
        if lease is None:
            raise AssignmentBoundAgentProcessSessionStaleError(
                "assignment-bound agent runtime session requires a live worker lease"
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
                raise AssignmentBoundAgentProcessSessionStaleError(
                    "assignment-bound agent runtime session lost its worker lease at start"
                )

        if assignment.status != AssignmentStatus.RUNNING:
            raise AssignmentBoundAgentProcessSessionStaleError(
                f"assignment-bound agent runtime session requires running assignment, got {assignment.status.value}"
            )
        return assignment, workspace_path

    def _spawn_delegated_process(
        self,
        assignment: ExecutionAssignment,
        workspace_path: Path,
        broker: AssignmentBoundAgentModelEgressBroker | None,
        control_broker: AssignmentBoundControlPlaneBroker | None,
    ):
        delegation_service = self.credential_provider
        if delegation_service is None:
            raise AssignmentBoundAgentProcessSessionError(
                "runtime credential provider is not configured"
            )
        lease = assignment.lease
        if lease is None:
            raise AssignmentBoundAgentProcessSessionStaleError(
                "agent runtime process launch requires an active worker lease"
            )

        def launch(launch_input: AssignmentRuntimeLaunchInput):
            command = launch_input.command
            environment = dict(launch_input.environment)
            readonly_mount_resolver = getattr(
                self.local_worker,
                "readonly_mounts",
                None,
            )
            trusted_mounts = (
                tuple(readonly_mount_resolver(assignment))
                if callable(readonly_mount_resolver)
                else ()
            )
            if trusted_mounts:
                environment["CODEX_READONLY_REPOSITORIES"] = ":".join(
                    str(destination)
                    for _source, destination in trusted_mounts
                )
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
                    AGENT_MODEL_EGRESS_RELAY_SCRIPT,
                    str(broker.sandbox_socket_path),
                    "8787",
                    *launch_input.command,
                )
                trusted_mounts = (
                    *trusted_mounts,
                    (broker.mount_source, broker.mount_destination),
                )
            if control_broker is not None:
                environment["CODEX_WEB_CONTROL_PLANE_URL"] = (
                    control_broker.sandbox_url
                )
                command = (
                    "/usr/bin/python3",
                    "-u",
                    "-c",
                    CONTROL_PLANE_RELAY_SCRIPT,
                    str(control_broker.sandbox_socket_path),
                    "8788",
                    *command,
                )
                trusted_mounts = (
                    *trusted_mounts,
                    (
                        control_broker.mount_source,
                        control_broker.mount_destination,
                    ),
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
            raise AssignmentBoundAgentProcessSessionStaleError(
                f"assignment-bound agent runtime assignment is {assignment.status.value}"
            )
        if (
            assignment.deadline_at is not None
            and assignment.deadline_at <= self._clock()
        ):
            raise AssignmentBoundAgentProcessSessionStaleError(
                "assignment-bound agent runtime assignment deadline expired"
            )
        if self.delegation is not None:
            delegation_service = self.credential_provider
            if delegation_service is None:
                raise AssignmentBoundAgentProcessSessionStaleError(
                    "runtime credential provider is unavailable"
                )
            delegation_service.validate_current(
                self.delegation,
                assignment,
                actor=self.local_worker.worker_actor,
            )
        return assignment

    async def _start_egress_broker(
        self,
    ) -> AssignmentBoundAgentModelEgressBroker | None:
        resolver = self.egress_endpoints_resolver
        if resolver is None:
            return None
        endpoints = resolver()
        broker = AssignmentBoundAgentModelEgressBroker(
            endpoints,
            validator=self._validate_egress_state,
        )
        await broker.start()
        return broker

    async def _start_control_plane_broker(
        self,
        assignment: ExecutionAssignment,
    ) -> AssignmentBoundControlPlaneBroker | None:
        factory = self.control_plane_broker_factory
        lease = assignment.lease
        if factory is None or lease is None:
            return None
        worker = self._current_worker()
        return await factory.start(
            assignment=assignment,
            worker_id=worker.id,
            service_identity_id=worker.service_identity_id,
            fence=lease.fence,
            validator=self._validate_egress_state,
        )

    async def start(self) -> "AssignmentBoundAgentProcessSession":
        async with self._start_lock:
            current = self.status()
            if current.running and current.ready:
                return self
            if self.runtime is not None:
                raise AssignmentBoundAgentProcessSessionStaleError(
                    "assignment-bound agent runtime session cannot restart in place"
                )

            assignment, workspace_path = self._prepare_assignment()
            lease = assignment.lease
            if lease is None:
                raise AssignmentBoundAgentProcessSessionStaleError(
                    "assignment-bound agent runtime session requires an active lease"
                )
            process = None
            broker = None
            control_broker = None
            try:
                self.fence = lease.fence
                broker = await self._start_egress_broker()
                self.egress_broker = broker
                control_broker = await self._start_control_plane_broker(
                    assignment
                )
                self.control_plane_broker = control_broker
                process, delegation, command = self._spawn_delegated_process(
                    assignment,
                    workspace_path,
                    broker,
                    control_broker,
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
                    name=f"agent-worker-session-{self.assignment_id}",
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
                if control_broker is not None:
                    with contextlib.suppress(Exception):
                        await control_broker.stop()
                    if self.control_plane_broker is control_broker:
                        self.control_plane_broker = None
                raise

    def _validate_resource_bounds(self, assignment: ExecutionAssignment) -> None:
        if self.workspace_path is None or self.started_monotonic is None:
            raise AssignmentBoundAgentProcessSessionStaleError(
                "assignment-bound agent runtime session has no execution workspace"
            )
        if (
            self._monotonic() - self.started_monotonic
            >= assignment.limits.wall_seconds
        ):
            raise AssignmentBoundAgentProcessSessionStaleError(
                "assignment-bound agent runtime session exceeded wall_seconds"
            )
        disk_bytes = self.local_worker.backend.execution_disk_usage(
            self.workspace_path,
            self.git_metadata_path,
        )
        readonly_disk_bytes = getattr(
            self.local_worker,
            "readonly_disk_bytes",
            lambda _assignment: 0,
        )(assignment)
        disk_bytes += readonly_disk_bytes
        if disk_bytes > assignment.limits.disk_bytes:
            raise AssignmentBoundAgentProcessSessionStaleError(
                "assignment-bound agent runtime session exceeded disk_bytes"
            )

    def validate_current(self) -> ExecutionAssignment:
        if self.delegation is None or self.fence is None:
            raise AssignmentBoundAgentProcessSessionStaleError(
                "assignment-bound agent runtime session has no active delegation"
            )
        self._current_worker()
        assignment = self._current_assignment()
        if assignment.status != AssignmentStatus.RUNNING:
            raise AssignmentBoundAgentProcessSessionStaleError(
                f"assignment-bound agent runtime assignment is {assignment.status.value}"
            )
        if (
            assignment.deadline_at is not None
            and assignment.deadline_at <= self._clock()
        ):
            raise AssignmentBoundAgentProcessSessionStaleError(
                "assignment-bound agent runtime assignment deadline expired"
            )
        delegation_service = self.credential_provider
        if delegation_service is None:
            raise AssignmentBoundAgentProcessSessionStaleError(
                "runtime credential provider is unavailable"
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
            raise AssignmentBoundAgentProcessSessionStaleError(
                "assignment-bound agent runtime lease disappeared"
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
                self.last_error = self.last_error or "agent runtime process exited"
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
            raise AssignmentBoundAgentProcessSessionStaleError(
                "assignment-bound agent runtime session is not started"
            )
        return await self.runtime.request(method, params)

    async def notify(self, method: str, params: Any = None) -> None:
        self.validate_current()
        if self.runtime is None:
            raise AssignmentBoundAgentProcessSessionStaleError(
                "assignment-bound agent runtime session is not started"
            )
        await self.runtime.notify(method, params)

    async def respond_to_server_request(
        self,
        request_id: int | str,
        result: dict[str, Any],
    ) -> None:
        self.validate_current()
        if self.runtime is None:
            raise AssignmentBoundAgentProcessSessionStaleError(
                "assignment-bound agent runtime session is not started"
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
        control_broker = self.control_plane_broker
        self.control_plane_broker = None
        if control_broker is not None:
            await control_broker.stop()
        self._stopping = False


class AssignmentBoundAgentProcessSessionManager:
    """Own at most one assignment-bound agent runtime session per canonical assignment."""

    def __init__(
        self,
        local_worker: LocalExecutionWorkerRuntime,
        host: Any,
        *,
        runtime_factory: Callable[..., Any],
        credential_provider: AssignmentRuntimeCredentialProvider,
        runtime_binding: ExecutionRuntimeBinding | None = None,
        session_factory: Callable[..., AssignmentBoundAgentProcessSession] = AssignmentBoundAgentProcessSession,
        watchdog_interval_seconds: float = 1.0,
        egress_endpoints_resolver: Callable[[], tuple[AgentRuntimeModelEgressEndpoint, ...]] | None = None,
        control_plane_broker_factory: DeferredControlPlaneBrokerFactory | None = None,
    ) -> None:
        self.local_worker = local_worker
        self.host = host
        self.runtime_factory = runtime_factory
        self.session_factory = session_factory
        self.watchdog_interval_seconds = watchdog_interval_seconds
        self.egress_endpoints_resolver = egress_endpoints_resolver
        self.control_plane_broker_factory = control_plane_broker_factory
        self.credential_provider = credential_provider
        self.runtime_binding = runtime_binding
        self.sessions: dict[str, AssignmentBoundAgentProcessSession] = {}
        self._lock = asyncio.Lock()

    async def start(self, assignment_id: str) -> AssignmentBoundAgentProcessSession:
        async with self._lock:
            existing = self.sessions.get(assignment_id)
            if existing is not None:
                status = existing.status()
                if status.running and status.ready:
                    return existing
                raise AssignmentBoundAgentProcessSessionStaleError(
                    "existing assignment-bound agent runtime session is not reusable"
                )
            session = self.session_factory(
                self.local_worker,
                self.host,
                assignment_id,
                runtime_factory=self.runtime_factory,
                watchdog_interval_seconds=self.watchdog_interval_seconds,
                egress_endpoints_resolver=self.egress_endpoints_resolver,
                control_plane_broker_factory=self.control_plane_broker_factory,
                credential_provider=self.credential_provider,
                runtime_binding=self.runtime_binding,
            )
            await session.start()
            self.sessions[assignment_id] = session
            return session

    def get(self, assignment_id: str) -> AssignmentBoundAgentProcessSession | None:
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
            raise AssignmentBoundAgentProcessSessionStaleError(
                "assignment-bound agent runtime session is not registered"
            )
        assignment = session.validate_current()
        lease = assignment.lease
        if lease is None or session.fence is None:
            raise AssignmentBoundAgentProcessSessionStaleError(
                "assignment-bound agent runtime session has no completable fenced lease"
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
