from __future__ import annotations

import asyncio
import contextlib
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Awaitable

from codex_web.cli_runtime import (
    CliRuntimeOutput,
    CliRuntimeResult,
    CliRuntimeTimeoutError,
)
from codex_web.execution_workers import ExecutionRuntimeBinding
from codex_web.services.agent_model_egress import (
    AGENT_MODEL_EGRESS_RELAY_SCRIPT,
    AgentRuntimeModelEgressEndpoint,
    AssignmentBoundAgentModelEgressBroker,
)
from codex_web.services.cli_worker_session import (
    AssignmentBoundCliSession,
    AssignmentBoundCliSessionManager,
    AssignmentBoundCliSessionStaleError,
)
from codex_web.services.local_execution_worker import LocalExecutionWorkerRuntime
from codex_web.services.mammouth_auth_delegation import MammouthAuthDelegationService


MAMMOUTH_SANDBOX_WORKER_HOME = Path("/tmp/codex-worker-home")


class MammouthTurnExecutorError(RuntimeError):
    pass


class MammouthWorkerHomeRegistry:
    """Per-assignment ephemeral Mammouth HOME directories on the worker host.

    Continuation requires Mammouth session state to survive across the turns of
    one assignment, so each claimed assignment receives its own HOME directory
    that is mounted writable into the sandbox and removed with the assignment.
    """

    def __init__(self) -> None:
        self._homes: dict[str, Path] = {}
        self._lock = threading.Lock()

    def home_for(self, assignment_id: str) -> Path:
        with self._lock:
            home = self._homes.get(assignment_id)
            if home is None:
                home = Path(tempfile.mkdtemp(prefix="mammouth-worker-home-"))
                self._homes[assignment_id] = home
            return home

    def discard(self, assignment_id: str) -> None:
        with self._lock:
            home = self._homes.pop(assignment_id, None)
        if home is not None:
            shutil.rmtree(home, ignore_errors=True)


class AssignmentBoundMammouthSessionManager(AssignmentBoundCliSessionManager):
    """CLI assignment session manager with per-assignment Mammouth HOME lifecycle."""

    def __init__(
        self,
        local_worker: LocalExecutionWorkerRuntime,
        *,
        runtime_binding: ExecutionRuntimeBinding,
        worker_homes: MammouthWorkerHomeRegistry,
        watchdog_interval_seconds: float = 1.0,
    ) -> None:
        super().__init__(
            local_worker,
            runtime_binding=runtime_binding,
            watchdog_interval_seconds=watchdog_interval_seconds,
            allow_coordinated_repository_layouts=True,
        )
        self.worker_homes = worker_homes

    async def complete(
        self,
        assignment_id: str,
        *,
        succeeded: bool,
        failure_code: str | None = None,
        failure_message: str | None = None,
        artifact_ids: tuple[str, ...] = (),
        evidence_ids: tuple[str, ...] = (),
    ):
        try:
            return await super().complete(
                assignment_id,
                succeeded=succeeded,
                failure_code=failure_code,
                failure_message=failure_message,
                artifact_ids=artifact_ids,
                evidence_ids=evidence_ids,
            )
        finally:
            self.worker_homes.discard(assignment_id)

    async def stop(self, assignment_id: str) -> None:
        try:
            await super().stop(assignment_id)
        finally:
            self.worker_homes.discard(assignment_id)

    async def stop_all(self) -> None:
        assignment_ids = list(self.sessions)
        try:
            await super().stop_all()
        finally:
            for assignment_id in assignment_ids:
                self.worker_homes.discard(assignment_id)


class MammouthCliSandboxTurnExecutor:
    """Run one Mammouth turn inside the assignment's canonical Bubblewrap sandbox.

    The executor is injected into ``MammouthCliAgentRuntimeAdapter`` as its
    ``run_command`` boundary. Every turn re-validates the fenced assignment,
    starts an assignment-bound model-egress broker, resolves the Mammouth API
    key only inside ``MammouthAuthDelegationService.use()``, and launches the
    CLI through the same Bubblewrap boundary, repository mounts and relay
    wrappers used by the Codex app-server path. There is no host subprocess
    fallback; the executor fails closed when the assignment session, lease or
    egress authority is stale.
    """

    def __init__(
        self,
        local_worker: LocalExecutionWorkerRuntime,
        *,
        session_manager: AssignmentBoundMammouthSessionManager,
        delegation: MammouthAuthDelegationService,
        worker_homes: MammouthWorkerHomeRegistry,
        egress_endpoints_resolver: Callable[
            [], tuple[AgentRuntimeModelEgressEndpoint, ...]
        ],
    ) -> None:
        self.local_worker = local_worker
        self.session_manager = session_manager
        self.delegation = delegation
        self.worker_homes = worker_homes
        self.egress_endpoints_resolver = egress_endpoints_resolver

    def _assignment_session(self, assignment_id: str) -> AssignmentBoundCliSession:
        session = self.session_manager.get(assignment_id)
        if session is None:
            raise MammouthTurnExecutorError(
                "Mammouth assignment session is not claimed"
            )
        return session

    async def __call__(
        self,
        command,
        *,
        binding,
        on_output: Callable[[CliRuntimeOutput], Awaitable[None]],
        timeout_seconds: float,
    ) -> CliRuntimeResult:
        loop = asyncio.get_running_loop()
        session = self._assignment_session(binding.assignment_id)
        assignment = session.validate_current()
        workspace_path = session.workspace_path
        if workspace_path is None:
            raise MammouthTurnExecutorError(
                "Mammouth assignment session has no execution workspace"
            )
        lease = assignment.lease
        if lease is None:
            raise AssignmentBoundCliSessionStaleError(
                "Mammouth turn requires an active worker lease"
            )

        endpoints = tuple(self.egress_endpoints_resolver())
        if not endpoints:
            raise MammouthTurnExecutorError(
                "Mammouth model egress endpoints are not configured"
            )
        broker = AssignmentBoundAgentModelEgressBroker(
            endpoints,
            validator=lambda: session.validate_current(),
        )
        await broker.start()
        argv = tuple(command.argv)
        executable_dir = self._executable_mount(argv)
        worker_home = self.worker_homes.home_for(binding.assignment_id)
        pending_outputs: list[asyncio.Future] = []

        def submit(output: CliRuntimeOutput) -> None:
            pending_outputs.append(
                asyncio.run_coroutine_threadsafe(on_output(output), loop)
            )

        def launch(launch_input):
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
            readonly_mounts, writable_mounts = (
                self.local_worker.repository_mounts(assignment)
            )
            trusted_readonly = (
                *readonly_mounts,
                *(
                    python_environment.readonly_mounts
                    if python_environment is not None
                    else ()
                ),
                *(
                    ((executable_dir, executable_dir),)
                    if executable_dir is not None
                    else ()
                ),
                (broker.mount_source, broker.mount_destination),
            )
            trusted_writable = (
                *writable_mounts,
                (worker_home, MAMMOUTH_SANDBOX_WORKER_HOME),
            )
            if readonly_mounts or executable_dir is not None:
                environment["CODEX_READONLY_REPOSITORIES"] = ":".join(
                    str(destination)
                    for _source, destination in readonly_mounts
                )
            if writable_mounts:
                environment["CODEX_WRITABLE_REPOSITORIES"] = ":".join(
                    str(destination)
                    for _source, destination in writable_mounts
                )
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
            relay_argv = (
                "/usr/bin/python3",
                "-u",
                "-c",
                AGENT_MODEL_EGRESS_RELAY_SCRIPT,
                str(broker.sandbox_socket_path),
                "8787",
                *argv,
            )
            process = self.local_worker.backend.spawn_interactive(
                assignment,
                argv=relay_argv,
                workspace_path=workspace_path,
                environment=environment,
                trusted_readonly_mounts=trusted_readonly,
                trusted_writable_mounts=trusted_writable,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            return (
                process,
                launch_input.delegation,
                relay_argv,
            )

        try:
            process, _delegation, relay_argv = await asyncio.to_thread(
                self.delegation.use,
                assignment,
                worker_id=session.worker_id,
                fence=lease.fence,
                actor=self.local_worker.worker_actor,
                consumer=launch,
            )
        except BaseException:
            await broker.stop()
            raise

        stdout = process.stdout
        stderr = process.stderr
        pump_done = threading.Event()

        def pump() -> None:
            try:
                if stdout is None:
                    return
                for raw in iter(stdout.readline, ""):
                    text = raw.rstrip("\r\n")
                    if text.strip():
                        submit(CliRuntimeOutput(stream="stdout", text=text))
            finally:
                pump_done.set()

        def drain() -> None:
            try:
                if stderr is None:
                    return
                for raw in iter(stderr.readline, ""):
                    text = raw.rstrip("\r\n")
                    if text.strip():
                        submit(CliRuntimeOutput(stream="stderr", text=text))
            except Exception:
                return

        pump_thread = threading.Thread(
            target=pump,
            name=f"mammouth-turn-{binding.assignment_id}",
            daemon=True,
        )
        drain_thread = threading.Thread(
            target=drain,
            name=f"mammouth-turn-stderr-{binding.assignment_id}",
            daemon=True,
        )
        pump_thread.start()
        drain_thread.start()

        async def collect_outputs() -> None:
            for future in tuple(pending_outputs):
                await asyncio.shield(asyncio.wrap_future(future))

        wait_task = asyncio.ensure_future(asyncio.to_thread(process.wait))
        try:
            try:
                exit_code = await asyncio.wait_for(
                    asyncio.shield(wait_task),
                    timeout=max(0.01, float(timeout_seconds)),
                )
            except asyncio.TimeoutError:
                self.local_worker.backend.terminate_process(process)
                exit_code = await wait_task
                raise CliRuntimeTimeoutError(
                    f"Mammouth turn timed out after {timeout_seconds}s"
                ) from None
            await asyncio.to_thread(pump_thread.join)
            await asyncio.to_thread(drain_thread.join)
            await collect_outputs()
            return CliRuntimeResult(exit_code=int(exit_code))
        except asyncio.CancelledError:
            self.local_worker.backend.terminate_process(process)
            with contextlib.suppress(BaseException):
                await wait_task
            await asyncio.to_thread(pump_done.wait, 5)
            with contextlib.suppress(Exception):
                await collect_outputs()
            raise
        except BaseException:
            self.local_worker.backend.terminate_process(process)
            with contextlib.suppress(BaseException):
                await wait_task
            await asyncio.to_thread(pump_done.wait, 5)
            with contextlib.suppress(Exception):
                await collect_outputs()
            raise
        finally:
            await broker.stop()

    @staticmethod
    def _executable_mount(argv: tuple[str, ...]) -> Path | None:
        raw = str(argv[0]) if argv else ""
        if not raw or "/" not in raw:
            return None
        candidate = Path(raw).resolve().parent
        if not candidate.is_dir():
            return None
        return candidate
