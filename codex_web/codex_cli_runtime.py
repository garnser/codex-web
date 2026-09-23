from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Mapping, Sequence

from codex_web.cli_runtime import (
    CliRuntimeCommand,
    CliRuntimeReadiness,
    CliRuntimeReadinessStatus,
)


class CodexCliAdapter:
    """OpenAI Codex CLI adapter for provider-neutral CLI runtime execution."""

    provider_id = "openai"
    runtime_id = "codex-cli"

    def __init__(
        self,
        *,
        executable: str = "codex",
        sandbox: str = "workspace-write",
        approval_policy: str = "on-request",
    ) -> None:
        self.executable = executable
        self.sandbox = sandbox
        self.approval_policy = approval_policy

    def readiness_command(self, executable: str) -> Sequence[str]:
        return (executable, "login", "status")

    def interpret_readiness(
        self,
        *,
        executable: str,
        resolved_executable: str,
        result: subprocess.CompletedProcess[str] | None,
    ) -> CliRuntimeReadiness:
        if result is None:
            return CliRuntimeReadiness(
                status=CliRuntimeReadinessStatus.UNAVAILABLE,
                executable=executable,
                resolved_executable=resolved_executable,
                message="Codex CLI authentication status could not be determined.",
            )
        if result.returncode == 0:
            return CliRuntimeReadiness(
                status=CliRuntimeReadinessStatus.READY,
                executable=executable,
                resolved_executable=resolved_executable,
                message="Codex CLI reports an authenticated local session.",
            )
        return CliRuntimeReadiness(
            status=CliRuntimeReadinessStatus.UNAUTHENTICATED,
            executable=executable,
            resolved_executable=resolved_executable,
            message="Codex CLI is installed but is not authenticated.",
        )

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
        approval = self._approval_argument(self.approval_policy)
        sandbox = self._sandbox_argument(self.sandbox)

        argv: list[str] = [
            executable,
            "--ask-for-approval",
            approval,
        ]
        if model:
            argv.extend(("--model", model))
        argv.extend(
            (
                "exec",
                "--json",
                "--sandbox",
                sandbox,
                "-C",
                str(cwd),
            )
        )
        argv.extend(extra_args)
        argv.append(prompt)
        return CliRuntimeCommand(
            argv=tuple(argv),
            cwd=cwd,
            environment=dict(environment or {}),
        )

    @staticmethod
    def _approval_argument(value: str) -> str:
        normalized = value.strip().casefold()
        supported = {
            "never": "never",
            "on-request": "on-request",
            "on-failure": "on-failure",
            "untrusted": "untrusted",
        }
        try:
            return supported[normalized]
        except KeyError as exc:
            raise ValueError(
                f"unsupported Codex CLI approval policy: {value!r}"
            ) from exc

    @staticmethod
    def _sandbox_argument(value: str) -> str:
        normalized = value.strip().casefold()
        supported = {
            "read-only": "read-only",
            "workspace-write": "workspace-write",
            "danger-full-access": "danger-full-access",
        }
        try:
            return supported[normalized]
        except KeyError as exc:
            raise ValueError(
                f"unsupported Codex CLI sandbox profile: {value!r}"
            ) from exc
