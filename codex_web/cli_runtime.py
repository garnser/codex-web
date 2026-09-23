from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
import signal
import shutil
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Awaitable, Callable, Mapping, Protocol, Sequence


class CliRuntimeReadinessStatus(StrEnum):
    READY = "ready"
    NOT_INSTALLED = "not_installed"
    MISCONFIGURED = "misconfigured"
    UNAUTHENTICATED = "unauthenticated"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class CliRuntimeReadiness:
    status: CliRuntimeReadinessStatus
    executable: str
    resolved_executable: str | None = None
    message: str | None = None

    @property
    def ready(self) -> bool:
        return self.status == CliRuntimeReadinessStatus.READY

    def public(self) -> dict[str, str | bool | None]:
        return {
            "ready": self.ready,
            "status": self.status.value,
            "executable": self.executable,
            "resolved_executable": self.resolved_executable,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class CliRuntimeCommand:
    argv: tuple[str, ...]
    cwd: Path
    environment: Mapping[str, str]


class CliRuntimeAdapter(Protocol):
    """Provider-specific command/readiness contract for a CLI-backed runtime."""

    provider_id: str
    runtime_id: str
    executable: str

    def readiness_command(self, executable: str) -> Sequence[str] | None:
        ...

    def interpret_readiness(
        self,
        *,
        executable: str,
        resolved_executable: str,
        result: subprocess.CompletedProcess[str] | None,
    ) -> CliRuntimeReadiness:
        ...

    def build_command(
        self,
        *,
        executable: str,
        cwd: Path,
        prompt: str,
        model: str | None = None,
        extra_args: Sequence[str] = (),
        environment: Mapping[str, str] | None = None,
    ) -> CliRuntimeCommand:
        ...


class CliRuntimeProbe:
    """Detect a CLI and evaluate adapter-specific authentication/readiness.

    The probe never reads provider credential files. Authentication is inferred
    only from a provider-supported, non-mutating readiness command.
    """

    def __init__(
        self,
        *,
        which: Callable[[str], str | None] = shutil.which,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        environment_allowlist: Sequence[str] = (),
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._which = which
        self._run = run
        self.environment_allowlist = tuple(
            dict.fromkeys(item for item in environment_allowlist if item)
        )
        self.environ = os.environ if environ is None else environ

    def environment(self) -> dict[str, str]:
        return {
            key: self.environ[key]
            for key in self.environment_allowlist
            if key in self.environ
        }

    def evaluate(self, adapter: CliRuntimeAdapter) -> CliRuntimeReadiness:
        requested = adapter.executable.strip()
        if not requested:
            return CliRuntimeReadiness(
                status=CliRuntimeReadinessStatus.MISCONFIGURED,
                executable=adapter.executable,
                message="CLI executable is empty.",
            )

        if os.path.sep in requested or (os.path.altsep and os.path.altsep in requested):
            path = Path(requested).expanduser()
            resolved = str(path) if path.is_file() and os.access(path, os.X_OK) else None
        else:
            resolved = self._which(requested)

        if resolved is None:
            return CliRuntimeReadiness(
                status=CliRuntimeReadinessStatus.NOT_INSTALLED,
                executable=requested,
                message="CLI executable was not found or is not executable.",
            )

        command = adapter.readiness_command(resolved)
        if not command:
            return adapter.interpret_readiness(
                executable=requested,
                resolved_executable=resolved,
                result=None,
            )

        try:
            result = self._run(
                tuple(command),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
                check=False,
                env=self.environment(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return CliRuntimeReadiness(
                status=CliRuntimeReadinessStatus.UNAVAILABLE,
                executable=requested,
                resolved_executable=resolved,
                message=f"CLI readiness probe failed: {exc}",
            )

        return adapter.interpret_readiness(
            executable=requested,
            resolved_executable=resolved,
            result=result,
        )


@dataclass(frozen=True, slots=True)
class CliRuntimeOutput:
    stream: str
    text: str


@dataclass(frozen=True, slots=True)
class CliRuntimeResult:
    exit_code: int


CliRuntimeOutputHandler = Callable[
    [CliRuntimeOutput],
    None | Awaitable[None],
]


class CliRuntimeTimeoutError(RuntimeError):
    pass


class CliRuntimeRunner:
    """Run CLI-backed providers with bounded environment and process cleanup."""

    def __init__(
        self,
        *,
        environment_allowlist: Sequence[str] = (),
        environ: Mapping[str, str] | None = None,
        create_subprocess: Callable[..., Awaitable[asyncio.subprocess.Process]] = (
            asyncio.create_subprocess_exec
        ),
        terminate_process_tree: Callable[
            [asyncio.subprocess.Process], Awaitable[None]
        ]
        | None = None,
        terminate_timeout_seconds: float = 5.0,
    ) -> None:
        self.environment_allowlist = tuple(
            dict.fromkeys(item for item in environment_allowlist if item)
        )
        self.environ = os.environ if environ is None else environ
        self._create_subprocess = create_subprocess
        self._terminate_process_tree = (
            terminate_process_tree or self._terminate_tree
        )
        self.terminate_timeout_seconds = max(
            0.1,
            float(terminate_timeout_seconds),
        )

    def environment(self, command: CliRuntimeCommand) -> dict[str, str]:
        environment = {
            key: self.environ[key]
            for key in self.environment_allowlist
            if key in self.environ
        }
        environment.update(command.environment)
        return environment

    async def run(
        self,
        command: CliRuntimeCommand,
        *,
        on_output: CliRuntimeOutputHandler | None = None,
        timeout_seconds: float | None = None,
    ) -> CliRuntimeResult:
        kwargs: dict[str, object] = {
            "cwd": str(command.cwd),
            "env": self.environment(command),
            "stdin": asyncio.subprocess.DEVNULL,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
        }
        if os.name == "posix":
            kwargs["start_new_session"] = True
        elif os.name == "nt":
            kwargs["creationflags"] = getattr(
                subprocess,
                "CREATE_NEW_PROCESS_GROUP",
                0,
            )

        process = await self._create_subprocess(*command.argv, **kwargs)
        pumps = (
            asyncio.create_task(
                self._pump(process.stdout, "stdout", on_output),
                name=f"cli-runtime-{process.pid}-stdout",
            ),
            asyncio.create_task(
                self._pump(process.stderr, "stderr", on_output),
                name=f"cli-runtime-{process.pid}-stderr",
            ),
        )
        try:
            if timeout_seconds is None:
                exit_code = await process.wait()
            else:
                try:
                    exit_code = await asyncio.wait_for(
                        process.wait(),
                        timeout=max(0.01, float(timeout_seconds)),
                    )
                except asyncio.TimeoutError as exc:
                    await self._terminate_process_tree(process)
                    raise CliRuntimeTimeoutError(
                        f"CLI runtime timed out after {timeout_seconds}s"
                    ) from exc
            await asyncio.gather(*pumps)
            return CliRuntimeResult(exit_code=int(exit_code))
        except asyncio.CancelledError:
            await self._terminate_process_tree(process)
            raise
        finally:
            for task in pumps:
                if not task.done():
                    task.cancel()
            for task in pumps:
                if not task.done():
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

    @staticmethod
    async def _pump(
        stream: asyncio.StreamReader | None,
        name: str,
        on_output: CliRuntimeOutputHandler | None,
    ) -> None:
        if stream is None:
            return
        while True:
            raw = await stream.readline()
            if not raw:
                return
            text = raw.decode(errors="replace").rstrip("\r\n")
            if on_output is None:
                continue
            result = on_output(CliRuntimeOutput(stream=name, text=text))
            if inspect.isawaitable(result):
                await result

    async def _terminate_tree(
        self,
        process: asyncio.subprocess.Process,
    ) -> None:
        if process.returncode is not None:
            return
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
        else:
            process.terminate()

        try:
            await asyncio.wait_for(
                process.wait(),
                timeout=self.terminate_timeout_seconds,
            )
            return
        except asyncio.TimeoutError:
            pass

        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                return
        else:
            process.kill()
        await process.wait()
