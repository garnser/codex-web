from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, Mapping, Sequence

from codex_web.agent_runtime import AgentRuntimeEvent
from codex_web.cli_runtime import (
    CliRuntimeCommand,
    CliRuntimeReadiness,
    CliRuntimeReadinessStatus,
)


_MAMMOUTH_MODEL_NAME_OVERRIDES = {
    "glm": "GLM",
    "gpt": "GPT",
    "llm": "LLM",
    "o1": "o1",
    "o3": "o3",
    "o4": "o4",
    "qwen": "Qwen",
}


def humanize_mammouth_model_name(model_id: str) -> str:
    tokens = [token for token in model_id.replace("_", "-").split("-") if token]
    rendered: list[str] = []
    for index, token in enumerate(tokens):
        lowered = token.lower()
        if lowered in _MAMMOUTH_MODEL_NAME_OVERRIDES:
            rendered.append(_MAMMOUTH_MODEL_NAME_OVERRIDES[lowered])
        elif token.replace(".", "").isdigit() and index > 0:
            rendered.append(token)
        else:
            rendered.append(token[:1].upper() + token[1:] if token else token)
    return " ".join(rendered)


class MammouthModelCatalog:
    """Bounded Mammouth model-catalog probe for operator-facing model lists.

    The probe runs Mammouth's own read-only catalog command with an explicitly
    allowlisted environment and returns provider-qualified model entries; it
    never exposes provider output beyond the model identifiers.
    """

    def __init__(
        self,
        *,
        executable: str = "mammouth",
        ttl_seconds: float = 300.0,
        timeout_seconds: float = 15.0,
        environment_allowlist: Sequence[str] = (
            "HOME",
            "PATH",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
        ),
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.executable = executable
        self.ttl_seconds = max(5.0, float(ttl_seconds))
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.environment_allowlist = tuple(environment_allowlist)
        self._environ = os.environ if environ is None else environ
        self._clock = clock
        self._entries: tuple[dict, ...] | None = None
        self._expires_at = 0.0

    def _environment(self) -> dict[str, str]:
        return {
            key: self._environ[key]
            for key in self.environment_allowlist
            if key in self._environ
        }

    def _probe(self) -> tuple[dict, ...]:
        resolved = shutil.which(self.executable)
        if resolved is None:
            return ()
        try:
            result = subprocess.run(
                (resolved, "models", "mammouth-ai"),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
                env=self._environment(),
            )
        except (OSError, subprocess.SubprocessError):
            return ()
        if result.returncode != 0:
            return ()
        entries: list[dict] = []
        seen: set[str] = set()
        for raw_line in result.stdout.splitlines():
            model_id = raw_line.strip()
            if not model_id:
                continue
            qualified = (
                model_id
                if "/" in model_id
                else f"mammouth-ai/{model_id}"
            )
            if qualified in seen:
                continue
            seen.add(qualified)
            entries.append(
                {
                    "id": qualified,
                    "model": qualified,
                    "displayName": humanize_mammouth_model_name(
                        qualified.partition("/")[2]
                    ),
                    "description": "Mammouth Code model",
                    "provider": "mammouth-ai",
                    "runtime": "mammouth-cli",
                }
            )
        return tuple(entries)

    def models(self) -> tuple[dict, ...]:
        now = self._clock()
        if self._entries is not None and now < self._expires_at:
            return self._entries
        entries = self._probe()
        if entries:
            self._entries = entries
            self._expires_at = now + self.ttl_seconds
        return entries


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
    """Project Mammouth run JSONL into canonical thread/turn/item events."""

    _event_types = {
        "step_start",
        "step_finish",
        "text",
        "reasoning",
        "tool_use",
        "error",
    }

    def __init__(self) -> None:
        self.session_id: str | None = None
        self._turn_started = False

    def parse(self, line: str) -> tuple[AgentRuntimeEvent, ...]:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError("Mammouth Code emitted invalid JSONL output") from exc
        if not isinstance(payload, dict):
            raise ValueError("Mammouth Code JSONL event must be an object")

        event_type = str(payload.get("type") or "").strip()
        if event_type not in self._event_types:
            raise ValueError("Mammouth Code JSONL event type is unsupported")

        session_id = str(payload.get("sessionID") or self.session_id or "").strip()
        if not session_id:
            raise ValueError("Mammouth Code JSONL event has no session id")
        self.session_id = session_id
        part = payload.get("part")
        if not isinstance(part, dict):
            raise ValueError("Mammouth Code JSONL event has no part payload")
        message_id = str(part.get("messageID") or "").strip() or None
        if not message_id:
            raise ValueError("Mammouth Code JSONL event part has no message id")
        turn_id = message_id
        if self.session_id and not self._turn_started:
            self._turn_started = True
            turn_start = self._event(
                "turn/started",
                turn_id,
                {
                    "type": "turn/started",
                    "turn": {
                        "id": turn_id,
                        "items": [],
                        "itemsView": "full",
                        "status": "inProgress",
                        "error": None,
                        "startedAt": None,
                        "completedAt": None,
                        "durationMs": None,
                    },
                },
            )
        else:
            turn_start = None
        base_payload = {"type": event_type}

        if event_type == "error":
            events = []
            if turn_start is not None:
                events.append(turn_start)
            events.append(
                self._event(
                    "turn/failed",
                    turn_id,
                    {"type": "turn/failed", "error": "agent runtime turn failed"},
                )
            )
            return tuple(events)
        if event_type == "step_start":
            events = []
            if turn_start is not None:
                events.append(turn_start)
            events.append(
                self._event(
                    "item/started",
                    turn_id,
                    {
                        **base_payload,
                        "item": {
                            "id": message_id,
                            "type": "agentMessage",
                            "text": "",
                            "phase": None,
                            "memoryCitation": None,
                        },
                        "itemId": message_id,
                    },
                )
            )
            return tuple(events)
        if event_type == "step_finish":
            events = []
            if turn_start is not None:
                events.append(turn_start)
            events.append(
                self._event(
                    "item/completed",
                    turn_id,
                    {
                        **base_payload,
                        "item": {
                            "id": message_id,
                            "type": "agentMessage",
                            "text": "",
                            "phase": None,
                            "memoryCitation": None,
                        },
                        "itemId": message_id,
                    },
                )
            )
            return tuple(events)
        if event_type in {"text", "reasoning"}:
            text = part.get("text")
            if not isinstance(text, str):
                return ()
            method = (
                "item/agentMessage/delta"
                if event_type == "text"
                else "item/reasoning/summaryText/delta"
            )
            events = []
            if turn_start is not None:
                events.append(turn_start)
            events.append(
                self._event(
                    method,
                    turn_id,
                    {
                        **base_payload,
                        "itemId": message_id,
                        "delta": text,
                        **(
                            {"summaryIndex": int(part.get("summaryIndex") or 0)}
                            if event_type == "reasoning"
                            else {}
                        ),
                    },
                )
            )
            return tuple(events)
        if event_type == "tool_use":
            tool_state = part.get("state")
            tool_state = tool_state if isinstance(tool_state, dict) else {}
            tool_status = str(tool_state.get("status") or "").casefold()
            item_status = (
                "failed"
                if tool_status in {"error", "failed"}
                else "completed"
                if tool_status in {"completed", "success"}
                else "inProgress"
            )
            item = {
                "id": part.get("callID") or part.get("id"),
                "type": "dynamicToolCall",
                "namespace": part.get("namespace"),
                "tool": part.get("name") or part.get("tool") or "tool",
                "arguments": tool_state.get(
                    "input",
                    tool_state.get("arguments", part.get("input", {})),
                ),
                "status": item_status,
                "contentItems": (
                    [{"type": "inputText", "text": output}]
                    if isinstance(
                        output := tool_state.get("output", tool_state.get("result")),
                        str,
                    )
                    else output
                    if isinstance(output, list)
                    else None
                ),
                "success": (
                    item_status == "completed"
                    if item_status != "inProgress"
                    else None
                ),
            }
            method = (
                "item/completed"
                if item_status != "inProgress"
                else "item/started"
            )
            events = []
            if turn_start is not None:
                events.append(turn_start)
            events.append(
                self._event(
                    method,
                    turn_id,
                    {
                        **base_payload,
                        "item": item,
                        "itemId": item["id"],
                    },
                )
            )
            return tuple(events)
        return ()

    def _event(
        self,
        event_type: str,
        turn_id: str | None,
        payload: dict,
    ) -> AgentRuntimeEvent:
        return AgentRuntimeEvent(
            event_type=event_type,
            provider_native_session_id=self.session_id,
            provider_native_turn_id=turn_id,
            payload=payload,
        )
