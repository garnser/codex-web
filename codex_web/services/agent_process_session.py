from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import subprocess
import threading
import time
import weakref
from pathlib import Path
from typing import Any, Callable, Sequence

from codex_web.execution_workspaces import ExecutionWorkspaceRelease
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
    AssignmentBoundAgentSessionError,
    AssignmentBoundAgentSessionStaleError,
    AssignmentBoundAgentSessionStatus,
    AssignmentRuntimeCredentialGrant,
    AssignmentRuntimeCredentialProvider,
    AssignmentRuntimeLaunchInput,
    runtime_binding_identity_matches,
)
from codex_web.services.agent_model_egress import (
    AGENT_MODEL_EGRESS_RELAY_SCRIPT,
    AgentRuntimeModelEgressEndpoint,
    AssignmentBoundAgentModelEgressBroker,
)
from codex_web.services.local_execution_worker import (
    LocalExecutionWorkerRuntime,
)


class AssignmentBoundAgentProcessSessionError(AssignmentBoundAgentSessionError):
    pass


class AssignmentBoundAgentProcessSessionStaleError(
    AssignmentBoundAgentProcessSessionError,
    AssignmentBoundAgentSessionStaleError,
):
    pass


def _available_loopback_port(*, exclude: frozenset[int] = frozenset()) -> int:
    """Select a relay port when the sandbox shares the host network namespace."""
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            port = int(listener.getsockname()[1])
        if port not in exclude:
            return port


def _executable_mount_destination(command: tuple[str, ...]) -> Path | None:
    raw = str(command[0]) if command else ""
    if not raw or "/" not in raw:
        return None
    candidate = Path(raw).resolve().parent
    if not candidate.is_dir():
        return None
    return candidate


def _merge_trusted_mounts(
    *groups: Sequence[tuple[Path, Path]],
) -> tuple[tuple[Path, Path], ...]:
    merged: list[tuple[Path, Path]] = []
    seen: set[Path] = set()
    for group in groups:
        for source, destination in group:
            if destination in seen:
                continue
            seen.add(destination)
            merged.append((source, destination))
    return tuple(merged)


def _with_execution_python_environment(
    command: tuple[str, ...],
    environment: dict[str, str],
) -> tuple[str, ...]:
    """Project the worker venv into Codex's explicit child environment policy."""

    path = environment.get("PATH")
    virtual_env = environment.get("VIRTUAL_ENV")
    if not path or not virtual_env:
        return command
    updated = list(command)
    for index, argument in enumerate(updated):
        prefix = "shell_environment_policy.set={"
        if not argument.startswith(prefix) or not argument.endswith("}"):
            continue
        body = argument[len(prefix):-1]
        managed = (
            "PATH=",
            "VIRTUAL_ENV=",
            "PYTHONNOUSERSITE=",
            "PIP_DISABLE_PIP_VERSION_CHECK=",
            "PIP_REQUIRE_VIRTUALENV=",
        )
        entries = [
            item
            for item in body.split(",")
            if not item.startswith(managed)
        ]
        entries.extend(
            (
                f"PATH={json.dumps(path)}",
                f"VIRTUAL_ENV={json.dumps(virtual_env)}",
                'PYTHONNOUSERSITE="1"',
                'PIP_DISABLE_PIP_VERSION_CHECK="1"',
                'PIP_REQUIRE_VIRTUALENV="1"',
            )
        )
        updated[index] = prefix + ",".join(entries) + "}"
        break
    return tuple(updated)


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
        minimum_address_space_bytes: int = 0,
        minimum_process_count: int = 0,
        restart_runtime_on_timeout: bool | None = None,
        watchdog_interval_seconds: float = 1.0,
        egress_endpoints_resolver: Callable[[], tuple[AgentRuntimeModelEgressEndpoint, ...]] | None = None,
        control_plane_broker_factory: DeferredControlPlaneBrokerFactory | None = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Any] = asyncio.sleep,
        disk_validation_interval_seconds: float = 30.0,
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
        self.minimum_address_space_bytes = max(0, minimum_address_space_bytes)
        self.minimum_process_count = max(0, minimum_process_count)
        self.restart_runtime_on_timeout = restart_runtime_on_timeout
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep
        self.disk_validation_interval_seconds = max(
            1.0,
            disk_validation_interval_seconds,
        )

        self.runtime: Any | None = None
        self.delegation: AssignmentRuntimeCredentialGrant | None = None
        self.fence: int | None = None
        self.workspace_path: Path | None = None
        self.git_metadata_path: Path | None = None
        self.git_worktree_metadata_path: Path | None = None
        self.started_monotonic: float | None = None
        self.last_heartbeat_monotonic: float | None = None
        self.last_error: str | None = None
        self.last_disk_validation_monotonic: float | None = None
        self.last_disk_bytes: int | None = None
        self._disk_validation_lock = threading.Lock()
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
        worker = self.local_worker.worker_service.store.worker(self.worker_id)
        if worker is not None and (
            worker.organization_id
            != self.local_worker.worker_actor.organization_id
            or worker.workspace_id != self.local_worker.worker_actor.workspace_id
        ):
            worker = None
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
        if not runtime_binding_identity_matches(
            assignment.runtime_binding,
            self.runtime_binding,
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
        if not runtime_binding_identity_matches(
            assignment.runtime_binding,
            self.runtime_binding,
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
            python_environment_resolver = getattr(
                self.local_worker,
                "python_environment",
                None,
            )
            python_environment = (
                python_environment_resolver(assignment)
                if callable(python_environment_resolver)
                else None
            )
            if python_environment is not None:
                environment.update(python_environment.environment)
                command = _with_execution_python_environment(
                    tuple(command),
                    environment,
                )
            repository_mount_resolver = getattr(
                self.local_worker,
                "repository_mounts",
                None,
            )
            if callable(repository_mount_resolver):
                trusted_mounts, trusted_writable_mounts = (
                    repository_mount_resolver(assignment)
                )
            else:
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
                trusted_writable_mounts = ()
            trusted_mounts = _merge_trusted_mounts(
                trusted_mounts,
                tuple(getattr(launch_input, "trusted_mounts", ()) or ()),
            )
            if python_environment is not None:
                trusted_mounts = _merge_trusted_mounts(
                    trusted_mounts,
                    python_environment.readonly_mounts,
                )
            trusted_writable_mounts = _merge_trusted_mounts(
                trusted_writable_mounts,
                tuple(
                    getattr(launch_input, "trusted_writable_mounts", ()) or (),
                ),
            )
            executable_dir = _executable_mount_destination(command)
            if executable_dir is not None:
                trusted_mounts = _merge_trusted_mounts(
                    trusted_mounts,
                    ((executable_dir, executable_dir),),
                )
            if trusted_mounts:
                environment["CODEX_READONLY_REPOSITORIES"] = ":".join(
                    str(destination)
                    for _source, destination in trusted_mounts
                )
            runtime_tool_mount_resolver = getattr(
                self.local_worker,
                "runtime_tool_mounts",
                None,
            )
            if callable(runtime_tool_mount_resolver):
                trusted_mounts = _merge_trusted_mounts(
                    trusted_mounts,
                    tuple(runtime_tool_mount_resolver()),
                )
            writable_destinations = [
                Path(destination)
                for _source, destination in trusted_writable_mounts
            ]
            git_metadata = self.local_worker.backend.discover_git_metadata(
                workspace_path
            )
            worktree_metadata_resolver = getattr(
                self.local_worker.backend,
                "discover_git_worktree_metadata",
                None,
            )
            git_worktree_metadata = (
                worktree_metadata_resolver(workspace_path)
                if callable(worktree_metadata_resolver)
                else git_metadata
            )
            if (
                assignment.sandbox != "read-only"
                and git_metadata is not None
                and git_metadata not in writable_destinations
            ):
                writable_destinations.append(git_metadata)
            if (
                assignment.sandbox != "read-only"
                and git_worktree_metadata is not None
                and git_worktree_metadata not in writable_destinations
            ):
                writable_destinations.append(git_worktree_metadata)
            if writable_destinations:
                environment["CODEX_WRITABLE_REPOSITORIES"] = ":".join(
                    str(destination) for destination in writable_destinations
                )
            model_egress_port: int | None = None
            if broker is not None:
                model_egress_port = (
                    _available_loopback_port()
                    if assignment.network.enabled
                    else 8787
                )
                model_proxy_url = broker.proxy_url.replace(
                    ":8787", f":{model_egress_port}"
                )
                environment.update(
                    {
                        "HTTP_PROXY": model_proxy_url,
                        "HTTPS_PROXY": model_proxy_url,
                        "ALL_PROXY": "",
                        "NO_PROXY": "",
                        "http_proxy": model_proxy_url,
                        "https_proxy": model_proxy_url,
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
                    str(model_egress_port),
                    *command,
                )
                trusted_mounts = (
                    *trusted_mounts,
                    (broker.mount_source, broker.mount_destination),
                )
            if control_broker is not None:
                control_plane_port = (
                    _available_loopback_port(
                        exclude=frozenset(
                            (model_egress_port,)
                            if model_egress_port is not None
                            else ()
                        )
                    )
                    if assignment.network.enabled
                    else 8788
                )
                environment["CODEX_WEB_CONTROL_PLANE_URL"] = (
                    f"http://127.0.0.1:{control_plane_port}"
                )
                if assignment.network.enabled:
                    command = tuple(
                        str(argument).replace(
                            "http://127.0.0.1:8788",
                            f"http://127.0.0.1:{control_plane_port}",
                        )
                        for argument in command
                    )
                command = (
                    "/usr/bin/python3",
                    "-u",
                    "-c",
                    CONTROL_PLANE_RELAY_SCRIPT,
                    str(control_broker.sandbox_socket_path),
                    str(control_plane_port),
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
                trusted_writable_mounts=trusted_writable_mounts,
                minimum_address_space_bytes=self.minimum_address_space_bytes,
                minimum_process_count=self.minimum_process_count,
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
        # CONNECT admission verifies authority but must not mutate the worker
        # catalog. The independent session watchdog owns heartbeats and lease
        # renewal, so model connection bursts cannot amplify durable writes.
        return self.validate_current()

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
        worker = await asyncio.to_thread(self._current_worker)
        return await factory.start(
            assignment=assignment,
            worker_id=worker.id,
            worker_actor=self.local_worker.worker_actor,
            fence=lease.fence,
            validator=self._validate_egress_state,
            worker_service_identity_validator=lambda: (
                self._current_worker().service_identity_id
            ),
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

            assignment, workspace_path = await asyncio.to_thread(
                self._prepare_assignment
            )
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
                process, delegation, command = await asyncio.to_thread(
                    self._spawn_delegated_process,
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
                worktree_metadata_resolver = getattr(
                    self.local_worker.backend,
                    "discover_git_worktree_metadata",
                    None,
                )
                self.git_worktree_metadata_path = (
                    worktree_metadata_resolver(workspace_path)
                    if callable(worktree_metadata_resolver)
                    else self.git_metadata_path
                )
                self.started_monotonic = self._monotonic()
                self.last_heartbeat_monotonic = self.started_monotonic
                self.runtime = self.runtime_factory(
                    self.host,
                    command=command,
                    cwd=workspace_path,
                    popen=_OneShotProcessFactory(process),
                )
                if (
                    self.restart_runtime_on_timeout is not None
                    and hasattr(self.runtime, "restart_on_timeout")
                ):
                    self.runtime.restart_on_timeout = self.restart_runtime_on_timeout
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
        now_monotonic = self._monotonic()
        with self._disk_validation_lock:
            if (
                self.last_disk_validation_monotonic is None
                or now_monotonic - self.last_disk_validation_monotonic
                >= self.disk_validation_interval_seconds
            ):
                disk_bytes = self.local_worker.backend.execution_disk_usage(
                    self.workspace_path,
                    self.git_metadata_path,
                )
                readonly_disk_bytes = getattr(
                    self.local_worker,
                    "readonly_disk_bytes",
                    lambda _assignment: 0,
                )(assignment)
                self.last_disk_bytes = disk_bytes + readonly_disk_bytes
                self.last_disk_validation_monotonic = now_monotonic
            disk_bytes = self.last_disk_bytes
        if disk_bytes is None:
            raise AssignmentBoundAgentProcessSessionStaleError(
                "assignment-bound agent runtime disk usage is unavailable"
            )
        if disk_bytes > assignment.limits.disk_bytes:
            raise AssignmentBoundAgentProcessSessionStaleError(
                "assignment-bound agent runtime session exceeded disk_bytes"
            )
        runtime = self.runtime
        process = runtime.proc if runtime is not None else None
        if process is not None and process.poll() is None:
            rss_reader = getattr(
                self.local_worker.backend,
                "process_tree_rss_bytes",
                None,
            )
            rss_bytes = rss_reader(process.pid) if callable(rss_reader) else 0
            if rss_bytes > assignment.limits.memory_bytes:
                raise AssignmentBoundAgentProcessSessionStaleError(
                    "assignment-bound agent runtime session exceeded memory_bytes"
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
                await asyncio.to_thread(self._record_unexpected_process_exit)
                return
            try:
                assignment = await asyncio.to_thread(self.validate_current)
                await asyncio.to_thread(
                    self._heartbeat_and_renew,
                    assignment,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)
                await self._stop_runtime_from_watchdog()
                return

    def _record_runtime_failure(
        self,
        *,
        failure_code: str,
        failure_message: str,
        release_reason: str,
    ) -> None:
        try:
            assignment = self._current_assignment()
        except Exception:
            return
        lease = assignment.lease
        if (
            lease is None
            or assignment.status
            not in {AssignmentStatus.CLAIMED, AssignmentStatus.RUNNING}
        ):
            return
        try:
            self.local_worker.worker_service.complete(
                self.worker_id,
                assignment.id,
                AssignmentCompleteRequest(
                    lease_token=lease.lease_token,
                    fence=assignment.fence,
                    succeeded=False,
                    failure_code=failure_code,
                    failure_message=failure_message,
                ),
                actor=self.local_worker.worker_actor,
            )
        except Exception:
            return
        if assignment.execution_workspace_id:
            with contextlib.suppress(Exception):
                self.local_worker.workspace_service.release(
                    assignment.execution_workspace_id,
                    ExecutionWorkspaceRelease(
                        discard=False,
                        reason=release_reason,
                    ),
                    actor=self.local_worker.worker_actor,
                )

    def _record_unexpected_process_exit(self) -> None:
        self._record_runtime_failure(
            failure_code="agent_runtime_process_exited",
            failure_message="assignment-bound agent runtime process exited",
            release_reason="agent runtime process exited",
        )

    async def _stop_runtime_from_watchdog(self) -> None:
        self._stopping = True
        try:
            if self.runtime is not None:
                await self.runtime.stop()
        finally:
            self._stopping = False

    async def request(self, method: str, params: Any = None) -> dict[str, Any]:
        await asyncio.to_thread(self.validate_current)
        if self.runtime is None:
            raise AssignmentBoundAgentProcessSessionStaleError(
                "assignment-bound agent runtime session is not started"
            )
        try:
            return await self.runtime.request(method, params)
        except Exception as exc:
            # Assignment runtimes intentionally use a one-shot authenticated
            # process and cannot be restarted in place. A transport timeout
            # therefore makes this fenced assignment unusable even when its
            # process is still alive. Retire it immediately so recovery can
            # claim the preserved queue with a fresh process and lease.
            if (
                getattr(exc, "status_code", None) == 504
                and method != "thread/read"
            ):
                self.last_error = f"agent runtime RPC timed out: {method}"
                await asyncio.to_thread(
                    self._record_runtime_failure,
                    failure_code="agent_runtime_rpc_timeout",
                    failure_message=self.last_error,
                    release_reason="agent runtime RPC timed out",
                )
                await self.stop()
            raise

    async def notify(self, method: str, params: Any = None) -> None:
        await asyncio.to_thread(self.validate_current)
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
        await asyncio.to_thread(self.validate_current)
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
        self._assignment_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    def _assignment_lock(self, assignment_id: str) -> asyncio.Lock:
        lock = self._assignment_locks.get(assignment_id)
        if lock is None:
            lock = asyncio.Lock()
            self._assignment_locks[assignment_id] = lock
        return lock

    async def start(self, assignment_id: str) -> AssignmentBoundAgentProcessSession:
        async with self._assignment_lock(assignment_id):
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

    async def checkpoint(self, assignment_id: str):
        session = self.sessions.get(assignment_id)
        if session is None:
            raise AssignmentBoundAgentProcessSessionStaleError(
                "assignment-bound agent runtime session is not registered"
            )
        session.validate_current()
        return await asyncio.to_thread(
            self.local_worker.checkpoint_assignment,
            assignment_id,
        )

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
        await self.checkpoint(assignment_id)
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
        if assignment.execution_workspace_id:
            with contextlib.suppress(Exception):
                self.local_worker.workspace_service.release(
                    assignment.execution_workspace_id,
                    ExecutionWorkspaceRelease(
                        discard=False,
                        reason="assignment completed",
                    ),
                    actor=self.local_worker.worker_actor,
                )
        return completed

    async def stop(self, assignment_id: str) -> None:
        async with self._assignment_lock(assignment_id):
            session = self.sessions.pop(assignment_id, None)
            if session is not None:
                await session.stop()

    async def stop_all(self) -> None:
        for assignment_id in set(self.sessions) | set(self._assignment_locks):
            with contextlib.suppress(Exception):
                await self.stop(assignment_id)
