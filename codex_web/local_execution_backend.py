from __future__ import annotations

import hashlib
import os
import resource
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

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
    container_runtime: str | None = None
    container_profile: str | None = None
    remediation: str | None = None

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
    executable: str
    command_digest: str
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

    @staticmethod
    def _container_runtime() -> str | None:
        if Path("/run/.containerenv").exists():
            return "podman"
        if Path("/.dockerenv").exists():
            return "docker"
        return None

    @classmethod
    def _container_profile(cls) -> str | None:
        runtime = cls._container_runtime()
        if runtime is None:
            return None
        return (
            os.environ.get("CODEX_WEB_EXECUTION_CONTAINER_PROFILE")
            or f"{runtime}-default"
        )

    @classmethod
    def _probe_remediation(cls, reason: str | None) -> str | None:
        detail = str(reason or "").casefold()
        runtime = cls._container_runtime()
        if runtime == "podman" and (
            "can't mount proc" in detail
            or "operation not permitted" in detail
        ):
            return (
                "Use the documented rootless Podman Bubblewrap profile "
                "(container-scoped SYS_ADMIN, seccomp=unconfined, "
                "label=disable), or run the execution "
                "worker natively/in a dedicated VM if that syscall relaxation "
                "is not acceptable."
            )
        if runtime is not None:
            return (
                "Verify that the container runtime permits unprivileged user, "
                "PID, mount, and network namespaces required by Bubblewrap."
            )
        return (
            "Install Bubblewrap and enable unprivileged user namespaces for "
            "the execution-worker account."
        )

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
                container_runtime=self._container_runtime(),
                container_profile=self._container_profile(),
                remediation=self._probe_remediation(
                    "bubblewrap executable is unavailable"
                ),
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
            "/usr",
            "/usr",
            "--symlink",
            "usr/bin",
            "/bin",
            "--symlink",
            "usr/lib",
            "/lib",
        ]
        if Path("/usr/lib64").exists():
            command.extend(("--symlink", "usr/lib64", "/lib64"))
        command.extend([
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
            "--",
            "/bin/true",
        ])
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
                container_runtime=self._container_runtime(),
                container_profile=self._container_profile(),
                remediation=self._probe_remediation(str(exc)),
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
                container_runtime=self._container_runtime(),
                container_profile=self._container_profile(),
                remediation=self._probe_remediation(detail),
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
            container_runtime=self._container_runtime(),
            container_profile=self._container_profile(),
            remediation=None,
        )
        return self._status

    @staticmethod
    def _tree_disk_usage(root: Path) -> int:
        total = 0
        if not root.exists():
            return 0
        for path in root.rglob("*"):
            try:
                if path.is_file() and not path.is_symlink():
                    total += path.stat().st_size
            except OSError:
                continue
        return total

    @classmethod
    def _execution_disk_usage(
        cls,
        workspace: Path,
        git_metadata: Path | None,
    ) -> int:
        total = cls._tree_disk_usage(workspace)
        if (
            git_metadata is not None
            and git_metadata.exists()
            and not git_metadata.is_relative_to(workspace)
        ):
            total += cls._tree_disk_usage(git_metadata)
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
        env["PATH"] = "/usr/local/bin:/usr/bin:/bin"
        if extra:
            for key, value in extra.items():
                normalized = str(key).strip()
                if not normalized or "=" in normalized or "\x00" in normalized:
                    raise LocalExecutionPolicyError("invalid execution environment key")
                if "\x00" in str(value):
                    raise LocalExecutionPolicyError("invalid execution environment value")
                env[normalized] = str(value)
        return env

    def validate_assignment(self, assignment: ExecutionAssignment) -> None:
        status = self.probe()
        if not status.ready:
            raise LocalExecutionUnavailableError(
                status.reason or "local execution isolation is unavailable"
            )
        self._validate_network(assignment.network)
        if WorkerCapability.COMMAND_EXECUTION not in assignment.required_capabilities:
            raise LocalExecutionPolicyError(
                "assignment does not authorize command execution capability"
            )

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

    @staticmethod
    def _directory_creation_args(path: Path) -> list[str]:
        resolved = path.resolve()
        current = Path("/")
        args: list[str] = []
        for part in resolved.parts[1:]:
            current = current / part
            if current in {
                Path("/usr"),
                Path("/bin"),
                Path("/lib"),
                Path("/lib64"),
                Path("/proc"),
                Path("/dev"),
                Path("/tmp"),
            }:
                continue
            args.extend(("--dir", str(current)))
        return args

    @staticmethod
    def discover_git_metadata(workspace_path: Path) -> Path | None:
        dot_git = workspace_path.resolve() / ".git"
        if dot_git.is_dir():
            return None
        if not dot_git.is_file():
            return None
        try:
            value = dot_git.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not value.lower().startswith("gitdir:"):
            return None
        target = value.split(":", 1)[1].strip()
        gitdir = Path(target)
        if not gitdir.is_absolute():
            gitdir = (workspace_path / gitdir).resolve()
        else:
            gitdir = gitdir.resolve()
        common_marker = gitdir / "commondir"
        if common_marker.is_file():
            try:
                common_value = common_marker.read_text(encoding="utf-8").strip()
            except OSError:
                common_value = ""
            if common_value:
                common = Path(common_value)
                if not common.is_absolute():
                    common = (gitdir / common).resolve()
                else:
                    common = common.resolve()
                return common
        return gitdir

    def build_command(
        self,
        assignment: ExecutionAssignment,
        *,
        argv: Sequence[str],
        workspace_path: Path,
        git_metadata_path: Path | None = None,
        trusted_readonly_mounts: Sequence[tuple[Path, Path]] = (),
    ) -> list[str]:
        self.validate_assignment(assignment)
        if not argv or not str(argv[0]).strip():
            raise LocalExecutionPolicyError("execution command is empty")

        workspace = workspace_path.resolve(strict=True)
        mount_flag = "--ro-bind" if assignment.sandbox == "read-only" else "--bind"
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
            "/usr",
            "/usr",
            "--symlink",
            "usr/bin",
            "/bin",
            "--symlink",
            "usr/lib",
            "/lib",
        ]
        if Path("/usr/lib64").exists():
            command.extend(("--symlink", "usr/lib64", "/lib64"))
        command.extend(
            (
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--tmpfs",
                "/tmp",
                "--dir",
                "/tmp/codex-worker-home",
            )
        )
        command.extend(self._directory_creation_args(workspace))
        command.extend((mount_flag, str(workspace), str(workspace)))

        for source_raw, destination_raw in trusted_readonly_mounts:
            source = Path(source_raw).resolve(strict=True)
            destination = Path(destination_raw)
            if not source.is_dir():
                raise LocalExecutionPolicyError(
                    "trusted readonly mount source must be an existing directory"
                )
            if not destination.is_absolute():
                raise LocalExecutionPolicyError(
                    "trusted readonly mount destination must be absolute"
                )
            if destination == workspace or destination.is_relative_to(workspace):
                raise LocalExecutionPolicyError(
                    "trusted readonly mount cannot replace or nest inside execution workspace"
                )
            command.extend(self._directory_creation_args(destination))
            command.extend(("--ro-bind", str(source), str(destination)))

        metadata = (
            git_metadata_path.resolve(strict=True)
            if git_metadata_path is not None
            else self.discover_git_metadata(workspace)
        )
        if metadata is not None and not metadata.is_relative_to(workspace):
            command.extend(self._directory_creation_args(metadata))
            # Git worktrees legitimately share target-repository metadata. The
            # canonical repository/workspace lease is what authorizes mutation
            # of that target resource; unrelated control-plane paths stay absent.
            metadata_mount = (
                "--ro-bind"
                if assignment.sandbox == "read-only"
                else "--bind"
            )
            command.extend((metadata_mount, str(metadata), str(metadata)))

        command.extend(
            (
                "--chdir",
                str(workspace),
                "--setenv",
                "HOME",
                "/tmp/codex-worker-home",
                "--",
                *[str(item) for item in argv],
            )
        )
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

    def terminate_process(self, process) -> None:
        self._kill_process_group(process)

    def execution_disk_usage(
        self,
        workspace_path: Path,
        git_metadata_path: Path | None = None,
    ) -> int:
        return self._execution_disk_usage(
            workspace_path.resolve(),
            (
                git_metadata_path.resolve()
                if git_metadata_path is not None
                else self.discover_git_metadata(workspace_path)
            ),
        )

    def spawn_interactive(
        self,
        assignment: ExecutionAssignment,
        *,
        argv: Sequence[str],
        workspace_path: Path,
        environment: Mapping[str, str] | None = None,
        git_metadata_path: Path | None = None,
        trusted_readonly_mounts: Sequence[tuple[Path, Path]] = (),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text: bool = True,
        bufsize: int = 1,
    ):
        """Start one long-lived assignment process inside the canonical sandbox.

        The caller owns process supervision. This method intentionally reuses the
        same Bubblewrap command, minimal environment and POSIX resource limits as
        one-shot execution so interactive runtimes do not become a second,
        weaker execution boundary.
        """
        status = self.probe()
        if not status.ready:
            raise LocalExecutionUnavailableError(
                status.reason or "local execution isolation is unavailable"
            )
        workspace = workspace_path.resolve(strict=True)
        if not workspace.is_dir():
            raise LocalExecutionPolicyError("execution workspace is not a directory")
        resolved_git_metadata = (
            git_metadata_path.resolve(strict=True)
            if git_metadata_path is not None
            else self.discover_git_metadata(workspace)
        )
        command = self.build_command(
            assignment,
            argv=argv,
            workspace_path=workspace,
            git_metadata_path=resolved_git_metadata,
            trusted_readonly_mounts=trusted_readonly_mounts,
        )
        env = self.minimal_environment(extra=environment)
        return self._popen(
            command,
            cwd=str(workspace),
            env=env,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            text=text,
            bufsize=bufsize,
            start_new_session=True,
            preexec_fn=self._limits_preexec(assignment.limits),
        )

    def run(
        self,
        assignment: ExecutionAssignment,
        *,
        argv: Sequence[str],
        workspace_path: Path,
        environment: Mapping[str, str] | None = None,
        poll_hook: Callable[[], None] | None = None,
        git_metadata_path: Path | None = None,
        trusted_readonly_mounts: Sequence[tuple[Path, Path]] = (),
        additional_disk_bytes: int = 0,
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
        resolved_git_metadata = (
            git_metadata_path.resolve(strict=True)
            if git_metadata_path is not None
            else self.discover_git_metadata(workspace)
        )
        command = self.build_command(
            assignment,
            argv=argv,
            workspace_path=workspace,
            git_metadata_path=resolved_git_metadata,
            trusted_readonly_mounts=trusted_readonly_mounts,
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

            def current_disk_bytes() -> int:
                return (
                    self._execution_disk_usage(
                        workspace,
                        resolved_git_metadata,
                    )
                    + max(0, int(additional_disk_bytes))
                )

            disk_bytes = current_disk_bytes()
            while process.poll() is None:
                if poll_hook is not None:
                    poll_hook()
                now = time.monotonic()
                if now >= deadline:
                    timed_out = True
                    limit_breach = "wall_seconds"
                    self._kill_process_group(process)
                    break
                disk_bytes = current_disk_bytes()
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
            disk_bytes = current_disk_bytes()
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

        encoded_argv = "\0".join(str(item) for item in argv).encode("utf-8", errors="replace")
        return LocalExecutionResult(
            executable=Path(str(argv[0])).name,
            command_digest="sha256:" + hashlib.sha256(encoded_argv).hexdigest(),
            exit_code=exit_code,
            stdout=stdout_raw.decode("utf-8", errors="replace"),
            stderr=stderr_raw.decode("utf-8", errors="replace"),
            duration_seconds=max(0.0, time.monotonic() - started),
            timed_out=timed_out,
            limit_breach=limit_breach,
            output_truncated=truncated,
            disk_bytes=disk_bytes,
        )
