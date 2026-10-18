from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import socket
import subprocess
import threading
import time
import weakref
import contextvars
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Sequence

from codex_web.execution_workspaces import ExecutionWorkspaceRelease
from codex_web.storage.state_store import StorageOperationBudget, storage_operation_budget, check_operation_budget, operation_lock
from codex_web.services.managed_validation_custody import ManagedValidationCustody
from codex_web.observability import log_event
from codex_web.runtime import managed_validation_runner
from codex_web.artifact_evidence import EvidenceCreate, EvidenceResult, EvidenceType
from codex_web.execution_workers import (
    AssignmentCancelRequest,
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
    start_assignment_session,
    bootstrap_cancellation_reason,
)
from codex_web.services.agent_model_egress import (
    AGENT_MODEL_EGRESS_RELAY_SCRIPT,
    AgentRuntimeModelEgressEndpoint,
    AssignmentBoundAgentModelEgressBroker,
)
from codex_web.services.codex_auth_delegation import CodexAuthDelegationStaleError
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
            "CODEX_WEB_MANAGED_VALIDATION_DIR=",
            "CODEX_WEB_MANAGED_VALIDATION_RUNNER=",
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
        for name in ("CODEX_WEB_MANAGED_VALIDATION_DIR", "CODEX_WEB_MANAGED_VALIDATION_RUNNER"):
            if environment.get(name):
                entries.append(f"{name}={json.dumps(environment[name])}")
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
        self._stop_task = None
        self._stopping = False
        # One serial supervision lane per admitted session. Browser projections
        # and repository tools must not exhaust the executor needed to renew it.
        # This is process lifecycle infrastructure, never admission authority.
        self._supervision_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="agent-supervision")
        self._diagnostic_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="agent-diagnostic")
        self._bound_assignment: ExecutionAssignment | None = None
        self._executor_work = set()
        self.diagnostic_state = "none"
        self._failure_observation = {}
        self._retirement_lock = asyncio.Lock()
        self._retired = False
        self._owned_process = None
        self._process_cleanup_done = False
        self.managed_validation = None
        self._supervision_timings: dict[str, float] = {}

    @property
    def worker_id(self) -> str:
        return self.local_worker.worker.id

    def status(self) -> AssignmentBoundAgentSessionStatus:
        proc = (self.runtime.proc if self.runtime is not None else None) or self._owned_process
        running = bool(proc is not None and proc.poll() is None)
        ready = bool(running and self.runtime is not None and self.runtime.ready.is_set())
        return AssignmentBoundAgentSessionStatus(
            assignment_id=self.assignment_id,
            worker_id=self.worker_id,
            fence=self.fence,
            running=running,
            ready=ready,
            last_error=self.last_error,
            diagnostic_state=self.diagnostic_state,
            pending_executor_work=len(self._executor_work),
            validation_custody_state=self.managed_validation.state if self.managed_validation else "unavailable",
            failure_observation={**self._failure_observation,
                "timings": dict(self._supervision_timings)} if self._failure_observation else None,
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
        original=getattr(self,'_bound_assignment',None)
        if original is not None and getattr(original,'resource_context',None)!=getattr(assignment,'resource_context',None):
            raise AssignmentBoundAgentProcessSessionStaleError('Immutable Resource context changed')
        return assignment

    def _prepare_assignment(self) -> tuple[ExecutionAssignment, Path]:
        assignment = self.local_worker._pending_assignment(self.assignment_id)
        if getattr(self, "require_new_claim", False) and assignment.status != AssignmentStatus.PENDING:
            raise AssignmentBoundAgentProcessSessionStaleError(
                "bootstrap startup lost its pending assignment to another claim"
            )
        if not runtime_binding_identity_matches(
            assignment.runtime_binding,
            self.runtime_binding,
        ):
            raise AssignmentBoundAgentProcessSessionStaleError(
                "assignment-bound agent runtime binding is incompatible"
            )
        if getattr(assignment,'resource_context',None) is not None:
            guard=getattr(self.local_worker.worker_service,'resource_context_validator',None)
            if guard is None:
                raise AssignmentBoundAgentProcessSessionStaleError('Resource context validator unavailable')
            guard(assignment)
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
        # Capture ownership before start() can fail, so failure cleanup cannot
        # revoke a later claimant or a session this attempt never claimed.
        self.fence = lease.fence

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
        if getattr(assignment,'resource_context',None) is not None:
            guard=getattr(self.local_worker.worker_service,'resource_context_validator',None)
            if guard is None:
                raise AssignmentBoundAgentProcessSessionStaleError('Resource context validator unavailable')
            guard(assignment)
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
            if self.managed_validation and self.managed_validation.registration:
                environment["CODEX_WEB_MANAGED_VALIDATION_DIR"] = str(self.managed_validation.directory)
                environment["CODEX_WEB_MANAGED_VALIDATION_RUNNER"] = "/opt/codex-managed-validation.py"
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
                ((Path(managed_validation_runner.__file__), Path('/opt/codex-managed-validation.py')),)
                    if self.managed_validation and self.managed_validation.registration else (),
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
            if self._stop_task is not None:
                raise AssignmentBoundAgentProcessSessionStaleError("stopped process session cannot restart")
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
                self._bound_assignment = assignment
                evidence = self.local_worker.artifact_evidence
                if evidence is not None and evidence.content is not None:
                    await asyncio.to_thread(self.local_worker.python_environment, assignment)
                    self.managed_validation = ManagedValidationCustody(evidence, assignment, workspace_path,
                        self.local_worker.worker_actor)
                    await asyncio.to_thread(self.managed_validation.prepare)
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
                self._owned_process = process
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

    @contextlib.contextmanager
    def _resource_timing(self, name):
        started = time.monotonic()
        try:
            yield
        finally:
            self._supervision_timings[name] = round(time.monotonic() - started, 6)

    def _validate_resource_bounds(self, assignment: ExecutionAssignment) -> None:
        # Reset skipped stages so cached validation cannot retain old timings.
        for name in ("resource_disk_lock_seconds", "resource_disk_seconds",
                     "resource_readonly_seconds", "resource_rss_seconds"):
            self._supervision_timings[name] = 0.0
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
        with contextlib.ExitStack() as stack:
            with self._resource_timing("resource_disk_lock_seconds"):
                stack.enter_context(operation_lock(self._disk_validation_lock))
            if (
                self.last_disk_validation_monotonic is None
                or now_monotonic - self.last_disk_validation_monotonic
                >= self.disk_validation_interval_seconds
            ):
                with self._resource_timing("resource_disk_seconds"):
                    disk_bytes = self.local_worker.backend.execution_disk_usage(
                        self.workspace_path,
                        self.git_metadata_path,
                    )
                with self._resource_timing("resource_readonly_seconds"):
                    readonly_disk_bytes = getattr(
                        self.local_worker,
                        "readonly_disk_bytes",
                        lambda _assignment: 0,
                    )(assignment)
                check_operation_budget()
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
            with self._resource_timing("resource_rss_seconds"):
                rss_bytes = rss_reader(process.pid) if callable(rss_reader) else 0
            if rss_bytes > assignment.limits.memory_bytes:
                raise AssignmentBoundAgentProcessSessionStaleError(
                    "assignment-bound agent runtime session exceeded memory_bytes"
                )

    def _validate_current_authority(self) -> ExecutionAssignment:
        """Resolve current authority without repeating the resource scan."""
        if self.delegation is None or self.fence is None:
            raise AssignmentBoundAgentProcessSessionStaleError(
                "assignment-bound agent runtime session has no active delegation"
            )
        self._current_worker()
        assignment = self._current_assignment()
        if getattr(assignment,'resource_context',None) is not None:
            guard=getattr(self.local_worker.worker_service,'resource_context_validator',None)
            if guard is None:
                raise AssignmentBoundAgentProcessSessionStaleError('Resource context validator unavailable')
            guard(assignment)
        if assignment.status != AssignmentStatus.RUNNING:
            raise AssignmentBoundAgentProcessSessionStaleError(
                f"assignment-bound agent runtime assignment is {assignment.status.value}"
            )
        if assignment.lease is None or assignment.lease.expires_at <= self._clock():
            raise AssignmentBoundAgentProcessSessionStaleError(
                "assignment-bound agent runtime lease expired"
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
        return assignment

    def validate_current(self) -> ExecutionAssignment:
        identity_started = time.monotonic()
        assignment = self._validate_current_authority()
        self._supervision_timings["identity_seconds"] = round(time.monotonic() - identity_started, 6)
        resource_started = time.monotonic()
        try:
            self._validate_resource_bounds(assignment)
        finally:
            self._supervision_timings["resource_seconds"] = round(time.monotonic() - resource_started, 6)
        return assignment

    def _heartbeat_and_renew(self, assignment: ExecutionAssignment) -> ExecutionAssignment:
        # A completed validation may only be *observed* after a delayed event
        # loop resumes. Never renew from its old snapshot: this existing next
        # phase resolves the same assignment's current authority/fence first.
        assignment = self._validate_current_authority()
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

    async def _supervise(self, phase, function, *args, seconds, reconcile_late=True):
        budget = StorageOperationBudget(seconds)
        context = contextvars.copy_context()
        context.run(storage_operation_budget.set, budget)
        submitted = time.monotonic()
        self._supervision_phase = phase
        completed_at = None
        def run():
            def invoke():
                nonlocal completed_at
                check_operation_budget()
                started = time.monotonic()
                self._supervision_timings[f"{phase}_queue_seconds"] = round(started - submitted, 6)
                try:
                    result = function(*args)
                    check_operation_budget()
                    completed_at = time.monotonic()
                    return result
                finally:
                    self._supervision_timings[f"{phase}_seconds"] = round(time.monotonic() - started, 6)
            return context.run(invoke)
        future = self._supervision_executor.submit(run)
        self._executor_work.add(future)
        future.add_done_callback(self._executor_work.discard)
        try:
            result = await asyncio.wait_for(asyncio.wrap_future(future), seconds)
            # Callback ordering may deliver a completed result before a delayed
            # timeout callback. That is still late observation, including for
            # the one-shot authority gate, which must not accept stale success.
            if time.monotonic() >= budget.deadline:
                raise TimeoutError("supervision result observed after its deadline")
            return result
        except TimeoutError:
            if not reconcile_late:
                # Authority reconciliation itself gets one bounded attempt,
                # never a recursive sequence of late-success probes.
                raise
            # asyncio's wrapper and timeout callbacks both need the main loop.
            # A stalled observer can time out after the actual worker has
            # already completed successfully within its unchanged real budget.
            # Only inspect this exact terminal future; never resubmit the work.
            if not future.done() or future.cancelled():
                raise
            result = future.result()  # Preserve the actual worker exception.
            if completed_at is None or completed_at >= budget.deadline:
                raise
            # Timely work is not a lease extension. The last validated lease
            # and assignment deadline must still be live at observation.
            self._lease_budget()
            # The same successful work may have finished before authority was
            # revoked while the observer loop remained blocked. Check current
            # canonical authority for *both* validation and renewal results,
            # off-loop in the existing serial executor. Do not repeat resource
            # validation, heartbeat or renewal, or install the old result yet.
            await self._supervise(
                f"{phase}_authority", self._validate_current_authority,
                seconds=self._lease_budget(), reconcile_late=False,
            )
            self._supervision_phase = phase
            return result
        finally:
            self._supervision_timings[f"{phase}_wait_seconds"] = round(time.monotonic() - submitted, 6)
            budget.cancelled.set()
            future.cancel()

    def _lease_budget(self):
        assignment = self._bound_assignment
        lease = assignment.lease if assignment else None
        now = self._clock()
        expires = min(lease.expires_at, assignment.deadline_at or lease.expires_at) if lease else now
        if expires <= now:
            raise TimeoutError("last validated lease or assignment deadline expired")
        return min(10.0, expires - now)

    async def _retire_failed_runtime(self, code, message):
        async with self._retirement_lock:
            if self._retired:
                return
            self._retired = True
            await self._retire_failed_runtime_once(code, message)

    async def _retire_failed_runtime_once(self, code, message):
        # Kill the owned process group before any storage call. Metadata-only
        # diagnostics use a separate bounded attempt; unavailable storage cannot
        # keep the sandbox alive or acquire newer credentials/fencing authority.
        process = (self.runtime.proc if self.runtime else None) or self._owned_process
        assignment = self._bound_assignment
        self._failure_observation = {"observed_at": time.time(), "process_pid": process.pid if process else None,
            "code": code, "assignment_id": self.assignment_id, "worker_id": self.worker_id,
            "fence": self.fence,
            "execution_id": assignment.execution_id if assignment else None,
            "execution_workspace_id": assignment.execution_workspace_id if assignment else None,
            "organization_id": assignment.organization_id if assignment else None,
            "workspace_id": assignment.workspace_id if assignment else None,
            "project_id": assignment.project_id if assignment else None,
            "requester_identity_id": assignment.created_by if assignment else None,
            "resource_ids": list(assignment.resource_ids) if assignment else [],
            "definition": assignment.execution_profile_definition.model_dump(mode="json") if assignment and assignment.execution_profile_definition else None,
            "agent_profile_record_id": assignment.agent_profile.profile_record_id if assignment and assignment.agent_profile else None,
            "outcome": "unknown", "cleanup_state": "pending",
            "process_exit_code": process.poll() if process else None,
            "phase": getattr(self, "_supervision_phase", "process"),
            "limit_kind": next((name for name in ('disk_bytes','memory_bytes','wall_seconds')
                                if f'exceeded {name}' in (self.last_error or '')), None),
            "observed_disk_bytes": self.last_disk_bytes,
            "lease_expires_at": self._bound_assignment.lease.expires_at if self._bound_assignment and self._bound_assignment.lease else None,
            "execution_profile_id": assignment.execution_profile_id if assignment else None,
            "agent_profile_id": assignment.agent_profile.profile_id if assignment and assignment.agent_profile else None,
            "agent_profile_revision": assignment.agent_profile.profile_revision if assignment and assignment.agent_profile else None,
            "mutable_repository_id": assignment.repository_target.mutable_repository_id if assignment and assignment.repository_target else None,
            "base_revision": assignment.base_revision if assignment else None,
            "validation_registration_id": self.managed_validation.registration.id if self.managed_validation and self.managed_validation.registration else None}
        cleanup_started = time.monotonic()
        try:
            await self._stop_runtime_from_watchdog()
            self._failure_observation['cleanup_state'] = 'stopped' if process is None or process.poll() is not None else 'unavailable'
        except Exception:
            self._failure_observation['cleanup_state'] = 'unavailable'
        self._failure_observation['cleanup_seconds'] = round(time.monotonic() - cleanup_started, 6)
        self._failure_observation['process_exit_code'] = process.poll() if process else None
        self._log_failure_observation("pending")
        budget = StorageOperationBudget(1.0)
        context = contextvars.copy_context()
        context.run(storage_operation_budget.set, budget)
        self.diagnostic_state = "pending"
        future = None
        try:
            future = self._diagnostic_executor.submit(context.run,
                lambda: self._record_runtime_failure(failure_code=code,
                    failure_message=message, release_reason=message))
            self._executor_work.add(future)
            future.add_done_callback(self._executor_work.discard)
            await asyncio.wait_for(asyncio.wrap_future(future), 1.0)
        except Exception:
            if self.diagnostic_state != "recorded":
                self.diagnostic_state = "unavailable"
        finally:
            if self.diagnostic_state == "pending":
                self.diagnostic_state = "unavailable"
            budget.cancelled.set()
            if future is not None:
                future.cancel()
            self._diagnostic_executor.shutdown(wait=False, cancel_futures=True)
            self._failure_observation["validation_custody_state"] = self.managed_validation.state if self.managed_validation else "unavailable"
            self._log_failure_observation(self.diagnostic_state)

    def _log_failure_observation(self, diagnostic_state):
        """Storage-independent, scoped metadata; never an action outcome receipt.

        Same-instance status retains this observation if the bounded canonical
        diagnostic write fails. The existing structured journal is the durable
        fallback; no new store, retry loop or execution authority is created.
        """
        observation = {**self._failure_observation, "timings": dict(self._supervision_timings)}
        with contextlib.suppress(Exception):
            log_event(logging.getLogger(__name__), logging.ERROR,
                "agent.runtime_supervision_failed", "Assignment-bound runtime supervision failed",
                tenant_id=observation.get("organization_id"), workspace_id=observation.get("workspace_id"),
                project_id=observation.get("project_id"), execution_id=observation.get("execution_id"),
                assignment_id=self.assignment_id, diagnostic_state=diagnostic_state,
                phase=observation.get("phase"), failure_code=observation.get("code"),
                cleanup_state=observation.get("cleanup_state"),
                validation_custody_state=observation.get("validation_custody_state", "unavailable"),
                timings=dict(self._supervision_timings), observation=observation)

    async def _watchdog(self) -> None:
        while not self._stopping:
            try:
                delay = min(self.watchdog_interval_seconds, self._lease_budget())
            except TimeoutError:
                self._supervision_phase = "lease"
                await self._retire_failed_runtime("execution_timeout", "last validated lease or deadline expired")
                return
            await self._sleep(delay)
            if self._stopping:
                return
            runtime = self.runtime
            process = (runtime.proc if runtime is not None else None) or self._owned_process
            if process is None or process.poll() is not None:
                self._supervision_phase = "process"
                self.last_error = "agent runtime process exited"
                await self._retire_failed_runtime("agent_runtime_process_exited", self.last_error)
                return
            try:
                assignment = await self._supervise("validation", self.validate_current, seconds=self._lease_budget())
                self._bound_assignment = await self._supervise("renewal",
                    self._heartbeat_and_renew, assignment, seconds=self._lease_budget())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = (str(exc) if isinstance(exc, (AssignmentBoundAgentSessionStaleError, CodexAuthDelegationStaleError))
                                   else "runtime supervision unavailable")
                code = "execution_timeout" if isinstance(exc, TimeoutError) else "agent_runtime_validation_failed"
                if "exceeded " in self.last_error and isinstance(exc, AssignmentBoundAgentProcessSessionStaleError):
                    code = "resource_limit"
                await self._retire_failed_runtime(code, "assignment-bound runtime supervision failed")
                return

    def _record_runtime_failure(
        self,
        *,
        failure_code: str,
        failure_message: str,
        release_reason: str,
    ) -> None:
        # Never borrow the latest claimant's token/fence to report an old
        # session's failure. Observations survive loss; completion still requires
        # the exact original lease and canonical worker validation.
        assignment = self._bound_assignment
        if assignment is None:
            return
        check_operation_budget()
        if self.managed_validation:
            try:
                self.managed_validation.capture()
            except Exception:
                self.managed_validation.state = 'unavailable'
        self._failure_observation["validation_custody_state"] = self.managed_validation.state if self.managed_validation else "unavailable"
        evidence_ids = []
        metadata = {"observed_fence": self.fence,
                    "failure_code": failure_code,
                    **{key: value for key, value in self._failure_observation.items()
                       if value is None or isinstance(value, (str, int, float, bool))},
                    **{f"definition_{key}": value for key, value in (self._failure_observation.get("definition") or {}).items()
                       if value is None or isinstance(value, (str, int, float, bool))},
                    **dict(self._supervision_timings)}
        evidence_service = self.local_worker.artifact_evidence
        if evidence_service is not None:
            try:
                check_operation_budget()
                evidence = evidence_service.create_evidence(EvidenceCreate(
                    project_id=assignment.project_id, work_item_ref=assignment.work_item_ref,
                    execution_id=assignment.execution_id, execution_workspace_id=assignment.execution_workspace_id,
                    resource_ids=assignment.resource_ids, evidence_type=EvidenceType.RUNTIME_RESULT,
                    provider="local-execution-worker", source="agent-runtime-supervision",
                    result=EvidenceResult.FAIL, summary=failure_message,
                    deny_model_context=True,
                    metadata={"assignment_id": assignment.id, "worker_id": self.worker_id, **metadata}),
                    actor=self.local_worker.worker_actor)
                evidence_ids.append(evidence.id)
                self._failure_observation["evidence_id"] = evidence.id
                self.diagnostic_state = "recorded"
            except Exception:
                pass
        try:
            check_operation_budget()
            self.local_worker.worker_service.record_runtime_observation(
                self.worker_id, assignment.id, observed_fence=self.fence,
                failure_code=failure_code, timings=dict(self._supervision_timings),
                evidence_id=evidence_ids[0] if evidence_ids else None,
                phase=self._failure_observation.get("phase"),
                cleanup_state=self._failure_observation.get("cleanup_state"),
                validation_custody_state=self._failure_observation.get("validation_custody_state"),
                cleanup_seconds=self._failure_observation.get("cleanup_seconds"),
                actor=self.local_worker.worker_actor)
        except Exception:
            pass
        check_operation_budget()
        if self._failure_observation.get('cleanup_state') == 'unavailable':
            return  # No assertion of retirement while an owned process may live.
        lease = assignment.lease
        if (
            lease is None
            or assignment.status
            not in {AssignmentStatus.CLAIMED, AssignmentStatus.RUNNING}
        ):
            return
        try:
            check_operation_budget()
            self.local_worker.worker_service.complete(
                self.worker_id,
                assignment.id,
                AssignmentCompleteRequest(
                    lease_token=lease.lease_token,
                    fence=assignment.fence,
                    succeeded=False,
                    failure_code=failure_code,
                    failure_message=failure_message,
                    evidence_ids=tuple(evidence_ids),
                ),
                actor=self.local_worker.worker_actor,
            )
        except Exception:
            return
        if assignment.execution_workspace_id:
            with contextlib.suppress(Exception):
                check_operation_budget()
                self.local_worker.workspace_service.release_failed_assignment(
                    assignment.execution_workspace_id, assignment,
                    actor=self.local_worker.control_actor)

    def _record_unexpected_process_exit(self) -> None:
        self._record_runtime_failure(
            failure_code="agent_runtime_process_exited",
            failure_message="assignment-bound agent runtime process exited",
            release_reason="agent runtime process exited",
        )

    async def _stop_runtime_from_watchdog(self) -> None:
        self._stopping = True
        self._supervision_executor.shutdown(wait=False, cancel_futures=True)
        try:
            process = (self.runtime.proc if self.runtime else None) or self._owned_process
            if process is not None and not self._process_cleanup_done:
                # killpg is independent of Python executors and store locks.
                self.local_worker.backend.terminate_process(process)
                deadline = time.monotonic() + .5
                while process.poll() is None and time.monotonic() < deadline:
                    await asyncio.sleep(.01)
                self._process_cleanup_done = process.poll() is not None
            if self.runtime is not None:
                await asyncio.wait_for(self.runtime.stop(), 1.0)
        finally:
            self._stopping = False

    async def request(self, method: str, params: Any = None) -> dict[str, Any]:
        return await self._request(method, params)

    async def request_with_start_capture(self, method: str, params: Any, capture) -> dict[str, Any]:
        if method not in {'turn/start', 'thread/resume'} or method != capture.rpc_method:
            raise ValueError('native capture method does not match its typed contract')
        return await self._request(method, params, start_capture=capture)

    async def _request(self, method: str, params: Any, *, start_capture=None) -> dict[str, Any]:
        assignment = await asyncio.to_thread(self.validate_current)
        if self.runtime is None:
            raise AssignmentBoundAgentProcessSessionStaleError(
                "assignment-bound agent runtime session is not started"
            )
        try:
            if start_capture is not None:
                capture = start_capture.bind_assignment(assignment, worker_id=self.worker_id,
                    fence=self.fence, validator=self.validate_current)
                return await self.runtime.request(method, params, start_capture=capture)
            return await self.runtime.request(method, params)
        except Exception as exc:
            # A timed-out interrupt has an unknown outcome; it does not prove
            # the running turn or its fenced process is unusable. Keep that
            # session so a failed steering request can preserve the queue and
            # await the actual terminal notification. Start/mutation timeouts
            # still require canonical retirement and fresh recovery.
            if (
                getattr(exc, "status_code", None) == 504
                and method not in {"thread/read", "turn/interrupt"}
            ):
                self._supervision_phase = "request"
                self.last_error = f"agent runtime RPC timed out: {method}"
                await self._retire_failed_runtime("agent_runtime_rpc_timeout", self.last_error)
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
        # One owned cleanup task survives a cancelled caller and is shared by
        # direct, manager and teardown callers. Never resubmit to shut executors.
        if self._stop_task is None:
            self._stop_task = asyncio.create_task(self._stop_once())
        await asyncio.shield(self._stop_task)

    async def _stop_once(self) -> None:
        async with self._start_lock:
            self._stopping = True
            errors = []
            task = self.watchdog_task
            self.watchdog_task = None
            if task is not None and task is not asyncio.current_task() and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            try:
                await self._stop_runtime_from_watchdog()
            except Exception as exc:
                errors.append(exc)
            if self.managed_validation and not self._retired:
                budget = StorageOperationBudget(1.0)
                context = contextvars.copy_context()
                context.run(storage_operation_budget.set, budget)
                future = None
                try:
                    future = self._diagnostic_executor.submit(context.run, self.managed_validation.capture)
                    self._executor_work.add(future)
                    future.add_done_callback(self._executor_work.discard)
                    await asyncio.wait_for(asyncio.wrap_future(future),1.0)
                except Exception:
                    self.managed_validation.state = 'unavailable'
                finally:
                    budget.cancelled.set()
                    if future is not None:
                        future.cancel()
            # Attempt every owned broker even if process cleanup/another broker
            # fails. A failed cleanup is retained, never reported as successful.
            for name in ('egress_broker','control_plane_broker'):
                broker = getattr(self,name)
                setattr(self,name,None)
                if broker is not None:
                    try:
                        await broker.stop()
                    except Exception as exc:
                        errors.append(exc)
            self._supervision_executor.shutdown(wait=False, cancel_futures=True)
            self._diagnostic_executor.shutdown(wait=False, cancel_futures=True)
            self._stopping = False
            if errors:
                self.last_error = 'runtime cleanup unavailable'
                raise RuntimeError(self.last_error) from errors[0]


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
            await start_assignment_session(session, self.local_worker, assignment_id)
            self.sessions[assignment_id] = session
            return session

    def get(self, assignment_id: str) -> AssignmentBoundAgentProcessSession | None:
        return self.sessions.get(assignment_id)

    async def cancel_bootstrap(self, assignment_id: str, *, reason: str) -> ExecutionAssignment:
        async with self._assignment_lock(assignment_id):
            session = self.sessions.get(assignment_id)
            if session is None or session.fence is None:
                raise AssignmentBoundAgentProcessSessionStaleError(
                    "bootstrap cancellation requires the registered fenced session"
                )
            cancelled = await asyncio.to_thread(
                self.local_worker.worker_service.cancel_bootstrap,
                assignment_id,
                AssignmentCancelRequest(
                    expected_fence=session.fence,
                    reason=bootstrap_cancellation_reason(reason),
                ),
                actor=self.local_worker.control_actor,
            )
            self.sessions.pop(assignment_id, None)
            await session.stop()
            return cancelled

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
        if not succeeded:
            current = await asyncio.to_thread(
                self.local_worker._pending_assignment, assignment_id,
            )
            if current.subject.kind == "thread_bootstrap":
                return await self.cancel_bootstrap(
                    assignment_id,
                    reason=failure_message or failure_code or "bootstrap failed",
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
                    actor=self.local_worker.control_actor,
                    preserve_files=True,
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
