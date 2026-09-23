from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence


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
    ) -> None:
        self._which = which
        self._run = run

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
                env={},
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
