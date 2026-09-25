from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Mapping, Sequence

from codex_web.agent_runtime import AgentRuntimeEvent
from codex_web.cli_runtime import (
    CliRuntimeCommand,
    CliRuntimeReadiness,
    CliRuntimeReadinessStatus,
)


class MammouthCliAdapter:
    """Mammouth Code adapter for the provider-neutral CLI runtime."""

    provider_id = "mammouth-ai"
    runtime_id = "mammouth-cli"

    def __init__(self, *, executable: str = "mammouth") -> None:
        self.executable = executable

    def readiness_command(self, executable: str) -> Sequence[str]:
        # models mammouth-ai is non-mutating and exercises local
        # configuration/authentication plus provider reachability.
        return (executable, "models", "mammouth-ai")

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
                message="Mammouth Code readiness could not be determined.",
            )
        if result.returncode == 0:
            return CliRuntimeReadiness(
                status=CliRuntimeReadinessStatus.READY,
                executable=executable,
                resolved_executable=resolved_executable,
                message="Mammouth Code can access the Mammouth model catalog.",
            )
        return CliRuntimeReadiness(
            status=CliRuntimeReadinessStatus.UNAUTHENTICATED,
            executable=executable,
            resolved_executable=resolved_executable,
            message=(
                "Mammouth Code is installed but its Mammouth provider "
                "configuration/authentication is not ready."
            ),
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
        argv: list[str] = [
            executable,
            "run",
            "--format",
            "json",
            "--dir",
            str(cwd),
        ]
        if model:
            argv.extend(("--model", self._model_argument(model)))
        argv.extend(extra_args)
        argv.append(prompt)
        return CliRuntimeCommand(
            argv=tuple(argv),
            cwd=cwd,
            environment=dict(environment or {}),
        )

    def build_resume_command(
        self,
        *,
        executable: str,
        cwd: Path,
        session_id: str,
        prompt: str,
        model: str | None = None,
        extra_args: Sequence[str] = (),
        environment: Mapping[str, str] | None = None,
    ) -> CliRuntimeCommand:
        normalized_session = session_id.strip()
        if not normalized_session:
            raise ValueError("Mammouth Code resume requires a session id")

        argv: list[str] = [
            executable,
            "run",
            "--format",
            "json",
            "--dir",
            str(cwd),
            "--session",
            normalized_session,
        ]
        if model:
            argv.extend(("--model", self._model_argument(model)))
        argv.extend(extra_args)
        argv.append(prompt)
        return CliRuntimeCommand(
            argv=tuple(argv),
            cwd=cwd,
            environment=dict(environment or {}),
        )

    @staticmethod
    def _model_argument(value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Mammouth Code model cannot be empty")
        if "/" in normalized:
            return normalized
        return f"mammouth-ai/{normalized}"


class MammouthCliJsonEventStream:
    """Project Mammouth Code JSONL into canonical agent-runtime events."""

    def __init__(self) -> None:
        self.session_id: str | None = None

    def parse(self, line: str) -> AgentRuntimeEvent:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError("Mammouth Code emitted invalid JSONL output") from exc
        if not isinstance(payload, dict):
            raise ValueError("Mammouth Code JSONL event must be an object")

        event_type = str(payload.get("type") or "").strip()
        if not event_type:
            raise ValueError("Mammouth Code JSONL event has no type")

        session_id = str(
            payload.get("sessionID")
            or payload.get("session_id")
            or self.session_id
            or ""
        ).strip()
        if session_id:
            self.session_id = session_id

        turn_id = payload.get("turnID") or payload.get("turn_id") or payload.get("messageID")
        return AgentRuntimeEvent(
            event_type=event_type,
            provider_native_session_id=self.session_id,
            provider_native_turn_id=str(turn_id) if turn_id is not None else None,
            payload=payload,
        )
