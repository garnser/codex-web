from __future__ import annotations

import os
import resource
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from codex_web.execution_workers import (
    ExecutionAssignment,
    NetworkPolicy,
    WorkerCapability,
    WorkerResourceLimits,
)


class LocalExecutionBackendError(RuntimeError):
    pass


class LocalExecutionUnavailableError(LocalExecutionBackendError):
    pass


class LocalExecutionPolicyError(LocalExecutionBackendError):
    pass


@dataclass(frozen=True, slots=True)
class LocalIsolationStatus:
    backend: str
    ready: bool
    reason: str | None
    supports_network_disabled: bool
    supports_network_allowlist: bool
    supports_resource_limits: bool

    @property
    def capabilities(self) -> tuple[WorkerCapability, ...]:
        values = [
            WorkerCapability.GIT,
            WorkerCapability.ARTIFACT_UPLOAD,
        ]
        if self.ready:
            values.append(WorkerCapability.COMMAND_EXECUTION)
        if self.ready and self.supports_network_allowlist:
            values.append(WorkerCapability.NETWORK)
        return tuple(values)


@dataclass(frozen=True, slots=True)
class LocalExecutionResult:
    argv: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False
    limit_breach: str | None = None
    output_truncated: bool = False
    disk_bytes: int = 0

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and self.limit_breach is None


class BubblewrapExecutionBackend:
    """Run one assignment command inside an OS-enforced local sandbox.

    The control plane supplies an already-authorized assignment and isolated
    workspace. Ambient process environment is not inherited except for a small
    non-sensitive allowlist. Bubblewrap owns filesystem/network namespace
    isolation while POSIX rlimits bound CPU, address space, process count and
    file size. Total workspace usage is monitored by the parent and terminates
    the whole process group on breach.
    """

    SAFE_ENV_KEYS = (
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "TZ",
    )

    def __init__(
        self,
        *,
        executable: str | None = None,
        probe_runner=subprocess.run,
        popen=subprocess.Popen,
        poll_interval_seconds: float = 0.05,
        max_output_bytes: int = 2 * 1024 * 1024,
    ) -> None:
        self.executable = executable or shutil.which("bwrap") or ""
        self._probe_runner = probe_runner
        self._popen = popen
        self.poll_interval_seconds = max(0.01, poll_interval_seconds)
        self.max_output_bytes = max(1024, max_output_bytes)
        self._status: LocalIsolationStatus | None = None

    def probe(self, *, refresh: bool = False) -> LocalIsolationStatus:
        if self._status is not None and not refresh:
            return self._status
        if not self.executable:
            self._status = LocalIsolationStatus(
                backend="bubblewrap",
                ready=False,
                reason="bubblewrap executable is unavailable",
                supports_network_disabled=False,
                supports_network_allowlist=False,
                supports_resource_limits=True,
            )
            return self._status
        command = [
            self.executable,
            "--die-with-parent",
            "--new-session",
            "--unshare-user",
            "--unshare-pid",
            "--unshare-uts",
            "--unshare-ipc",
            "--unshare-net",
            "--ro-bind",
            "/",
            "/",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--",
            "/bin/true",
        ]
        try:
            completed = self._probe_runner(
                command,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self._status = LocalIsolationStatus(
                backend="bubblewrap",
                ready=False,
                reason=f"bubblewrap probe failed: {exc}",
                supports_network_disabled=False,
                supports_network_allowlist=False,
                supports_resource_limits=True,
            )
            return self._status
        if completed.returncode != 0:
            detail = (completed.stderr or "").strip()
            self._status = LocalIsolationStatus(
                backend="bubblewrap",
                ready=False,
                reason=detail or f"bubblewrap probe exited {completed.returncode}",
                supports_network_disabled=False,
                supports_network_allowlist=False,
                supports_resource_limits=True,
            )
            return self._status
        self._status = LocalIsolationStatus(
            backend="bubblewrap",
            ready=True,
            reason=None,
            supports_network_disabled=True,
            # Bubblewrap can isolate the network namespace but cannot enforce a
            # DNS/host allowlist by itself. Never advertise a capability we
            # cannot enforce.
            supports_network_allowlist=False,
            supports_resource_limits=True,
        )
        return self._status

    @staticmethod
    def _workspace_disk_usage(root: Path) -> int:
        total = 0
        for path in root.rglob("*"):
            try:
                if path.is_file() and not path.is_symlink():
                    total += path.stat().st_size
            except OSError:
                continue
        return total

    @staticmethod
    def _limits_preexec(limits: WorkerResourceLimits):
        def apply() -> None:
            resource.setrlimit(resource.RLIMIT_CPU, (limits.cpu_seconds, limits.cpu_seconds))
            resource.setrlimit(resource.RLIMIT_AS, (limits.memory_bytes, limits.memory_bytes))
            resource.setrlimit(resource.RLIMIT_NPROC, (limits.process_count, limits.process_count))
            resource.setrlimit(resource.RLIMIT_FSIZE, (limits.disk_bytes, limits.disk_bytes))

        return apply

    @classmethod
    def minimal_environment(
        cls,
        *,
        extra: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        env = {
            key: value
            for key in cls.SAFE_ENV_KEYS
            if (value := os.environ.get(key))
        }
        env["PATH"] = os.environ.get(
            "PATH",
            "/usr/local/bin:/usr/bin:/bin",
        )
        if extra:
            for key, value in extra.items():
                normalized = str(key).strip()
                if not normalized or "=" in normalized or "\x00" in normalized:
                    raise LocalExecutionPolicyError("invalid execution environment key")
                if "\x00" in str(value):
                    raise LocalExecutionPolicyError("invalid execution environment value")
                env[normalized] = str(value)
        return env

    @staticmethod
    def _validate_network(policy: NetworkPolicy) -> None:
        if not policy.enabled:
            return
        if policy.allowed_hosts:
            raise LocalExecutionPolicyError(
                "local bubblewrap worker cannot enforce host allowlists"
            )
        raise LocalExecutionPolicyError(
            "local worker does not advertise unrestricted network execution"
        )

    def build_command(
        self,
        assignment: ExecutionAssignment,
        *,
        argv: Sequence[str],
        workspace_path: Path,
        home_path: Path,
    ) -> list[str]:
        status = self.probe()
        if not status.ready:
            raise LocalExecutionUnavailableError(
                status.reason or "local execution isolation is unavailable"
            )
        if assignment.sandbox == "danger-full-access":
            raise LocalExecutionPolicyError(
                "danger-full-access is not allowed on the isolated local worker"
            )
        self._validate_network(assignment.network)
        if WorkerCapability.COMMAND_EXECUTION not in assignment.required_capabilities:
            raise LocalExecutionPolicyError(
                "assignment does not authorize command execution capability"
            )
        if not argv or not str(argv[0]).strip():
            raise LocalExecutionPolicyError("execution command is empty")

        workspace = workspace_path.resolve(strict=True)
        home = home_path.resolve(strict=True)
        mount_flag = "--bind" if assignment.sandbox == "workspace-write" else "--ro-bind"
        command = [
            self.executable,
            "--die-with-parent",
            "--new-session",
            "--unshare-user",
            "--unshare-pid",
            "--unshare-uts",
            "--unshare-ipc",
            "--unshare-net",
            "--ro-bind",
            "/",
            "/",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
            mount_flag,
            str(workspace),
            str(workspace),
            "--bind",
            str(home),
            str(home),
            "--chdir",
            str(workspace),
            "--setenv",
            "HOME",
            str(home),
            "--",
            *[str(item) for item in argv],
        ]
        return command

    @staticmethod
    def _kill_process_group(process) -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                process.kill()
            except (ProcessLookupError, OSError):
                pass

    def run(
        self,
        assignment: ExecutionAssignment,
        *,
        argv: Sequence[str],
        workspace_path: Path,
        environment: Mapping[str, str] | None = None,
    ) -> LocalExecutionResult:
        status = self.probe()
        if not status.ready:
            raise LocalExecutionUnavailableError(
                status.reason or "local execution isolation is unavailable"
            )
        workspace = workspace_path.resolve(strict=True)
        if not workspace.is_dir():
            raise LocalExecutionPolicyError("execution workspace is not a directory")

        started = time.monotonic()
        timed_out = False
        limit_breach: str | None = None
        with tempfile.TemporaryDirectory(prefix="codex-worker-home-") as raw_home:
            home = Path(raw_home)
            command = self.build_command(
                assignment,
                argv=argv,
                workspace_path=workspace,
                home_path=home,
            )
            env = self.minimal_environment(extra=environment)
            with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
                process = self._popen(
                    command,
                    cwd=str(workspace),
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    start_new_session=True,
                    preexec_fn=self._limits_preexec(assignment.limits),
                )
                deadline = started + assignment.limits.wall_seconds
                disk_bytes = self._workspace_disk_usage(workspace)
                while process.poll() is None:
                    now = time.monotonic()
                    if now >= deadline:
                        timed_out = True
                        limit_breach = "wall_seconds"
                        self._kill_process_group(process)
                        break
                    disk_bytes = self._workspace_disk_usage(workspace)
                    if disk_bytes > assignment.limits.disk_bytes:
                        limit_breach = "disk_bytes"
                        self._kill_process_group(process)
                        break
                    time.sleep(
                        min(
                            self.poll_interval_seconds,
                            max(0.0, deadline - now),
                        )
                    )
                exit_code = process.wait()
                disk_bytes = self._workspace_disk_usage(workspace)
                if limit_breach is None and exit_code < 0:
                    signum = -exit_code
                    if signum == signal.SIGXCPU:
                        limit_breach = "cpu_seconds"
                    elif signum == signal.SIGXFSZ:
                        limit_breach = "disk_bytes"

                stdout_file.seek(0)
                stdout_raw = stdout_file.read(self.max_output_bytes + 1)
                stderr_file.seek(0)
                stderr_raw = stderr_file.read(self.max_output_bytes + 1)
                truncated = (
                    len(stdout_raw) > self.max_output_bytes
                    or len(stderr_raw) > self.max_output_bytes
                )
                stdout_raw = stdout_raw[: self.max_output_bytes]
                stderr_raw = stderr_raw[: self.max_output_bytes]

        return LocalExecutionResult(
            argv=tuple(str(item) for item in argv),
            exit_code=exit_code,
            stdout=stdout_raw.decode("utf-8", errors="replace"),
            stderr=stderr_raw.decode("utf-8", errors="replace"),
            duration_seconds=max(0.0, time.monotonic() - started),
            timed_out=timed_out,
            limit_breach=limit_breach,
            output_truncated=truncated,
            disk_bytes=disk_bytes,
        )
