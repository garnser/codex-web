from __future__ import annotations

import asyncio
import contextlib
import inspect
import time
from pathlib import Path
from typing import Awaitable, Callable

from codex_web.cli_runtime import (
    CliRuntimeCommand,
    CliRuntimeOutput,
    CliRuntimeOutputHandler,
    CliRuntimeResult,
    CliRuntimeTimeoutError,
)
from codex_web.services.cli_worker_session import AssignmentBoundCliSession
from codex_web.services.mammouth_auth_delegation import (
    MammouthAuthDelegation,
    MammouthAuthDelegationService,
    MammouthDelegatedLaunch,
)


class MammouthAssignmentRunnerError(RuntimeError):
    pass


class MammouthAssignmentCliRunner:
    """Run Mammouth inside one canonical assignment Bubblewrap boundary.

    Credential material is resolved only while spawning the child process inside
    MammouthAuthDelegationService.use. The parent retains only metadata needed
    to revalidate the delegation while the process is alive.
    """

    def __init__(
        self,
        session: AssignmentBoundCliSession,
        credential_provider: MammouthAuthDelegationService,
        *,
        poll_interval_seconds: float = 0.1,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.session = session
        self.credential_provider = credential_provider
        self.poll_interval_seconds = max(0.01, float(poll_interval_seconds))
        self._monotonic = monotonic
        self._sleep = sleep

    def _require_workspace(self, command: CliRuntimeCommand) -> Path:
        self.session.validate_current()
        workspace = self.session.workspace_path
        if workspace is None or self.session.fence is None:
            raise MammouthAssignmentRunnerError(
                "Mammouth assignment has no active canonical workspace/fence"
            )
        try:
            expected = workspace.resolve(strict=True)
            actual = command.cwd.resolve(strict=True)
        except OSError as exc:
            raise MammouthAssignmentRunnerError(
                "Mammouth assignment workspace is unavailable"
            ) from exc
        if actual != expected:
            raise MammouthAssignmentRunnerError(
                "Mammouth command cwd does not match the canonical assignment workspace"
            )
        return expected

    def _spawn(
        self,
        command: CliRuntimeCommand,
    ) -> tuple[object, MammouthAuthDelegation]:
        workspace = self._require_workspace(command)
        assignment = self.session.validate_current()
        local_worker = self.session.local_worker
        readonly_mounts, writable_mounts = local_worker.repository_mounts(assignment)

        def spawn(launch: MammouthDelegatedLaunch):
            environment = dict(command.environment)
            environment.update(launch.environment)
            process = local_worker.backend.spawn_interactive(
                assignment,
                argv=command.argv,
                workspace_path=workspace,
                environment=environment,
                trusted_readonly_mounts=readonly_mounts,
                trusted_writable_mounts=writable_mounts,
            )
            return process, launch.delegation

        return self.credential_provider.use(
            assignment,
            worker_id=self.session.worker_id,
            fence=self.session.fence,
            actor=local_worker.worker_actor,
            consumer=spawn,
        )

    async def _pump(
        self,
        stream,
        name: str,
        on_output: CliRuntimeOutputHandler | None,
    ) -> None:
        if stream is None:
            return
        while True:
            line = await asyncio.to_thread(stream.readline)
            if line == "":
                return
            if on_output is None:
                continue
            result = on_output(CliRuntimeOutput(stream=name, text=line.rstrip("\n")))
            if inspect.isawaitable(result):
                await result

    def _validate_delegation(self, delegation: MammouthAuthDelegation) -> None:
        assignment = self.session.validate_current()
        self.credential_provider.validate_current(
            delegation,
            assignment,
            actor=self.session.local_worker.worker_actor,
        )

    async def _terminate(self, process) -> None:
        with contextlib.suppress(Exception):
            self.session.local_worker.backend.terminate_process(process)
        with contextlib.suppress(Exception):
            await asyncio.to_thread(process.wait)

    async def run(
        self,
        command: CliRuntimeCommand,
        *,
        on_output: CliRuntimeOutputHandler | None = None,
        timeout_seconds: float | None = None,
    ) -> CliRuntimeResult:
        process, delegation = await asyncio.to_thread(self._spawn, command)
        pumps = (
            asyncio.create_task(
                self._pump(getattr(process, "stdout", None), "stdout", on_output),
                name="mammouth-assignment-stdout",
            ),
            asyncio.create_task(
                self._pump(getattr(process, "stderr", None), "stderr", on_output),
                name="mammouth-assignment-stderr",
            ),
        )
        started = self._monotonic()
        try:
            while True:
                exit_code = process.poll()
                if exit_code is not None:
                    break
                self._validate_delegation(delegation)
                if (
                    timeout_seconds is not None
                    and self._monotonic() - started >= max(0.01, float(timeout_seconds))
                ):
                    await self._terminate(process)
                    raise CliRuntimeTimeoutError(
                        f"CLI runtime timed out after {timeout_seconds}s"
                    )
                await self._sleep(self.poll_interval_seconds)

            await asyncio.gather(*pumps)
            return CliRuntimeResult(exit_code=int(exit_code))
        except asyncio.CancelledError:
            await self._terminate(process)
            raise
        except Exception:
            await self._terminate(process)
            raise
        finally:
            for task in pumps:
                if not task.done():
                    task.cancel()
            for task in pumps:
                if not task.done():
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
