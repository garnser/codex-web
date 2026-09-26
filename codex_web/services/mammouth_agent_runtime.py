from __future__ import annotations

import asyncio
import contextlib
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Protocol

from codex_web.agent_providers import AgentProviderCapability
from codex_web.agent_runtime import (
    AgentRuntimeEvent,
    AgentRuntimeHealth,
    AgentRuntimeListRequest,
    AgentRuntimeResult,
    AgentRuntimeSessionRequest,
    AgentRuntimeTurnRequest,
    AgentRuntimeUnsupportedCapability,
)
from codex_web.cli_runtime import (
    CliRuntimeCommand,
    CliRuntimeOutput,
    CliRuntimeProbe,
    CliRuntimeResult,
)
from codex_web.mammouth_cli_runtime import (
    MammouthCliAdapter,
    MammouthCliJsonEventStream,
)


class MammouthCliRuntimeError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MammouthCliSessionBinding:
    canonical_session_id: str
    provider_native_session_id: str | None
    assignment_id: str | None
    execution_id: str | None
    execution_workspace_id: str | None
    worker_id: str | None


class MammouthCliCommandExecutor(Protocol):
    async def __call__(
        self,
        command: CliRuntimeCommand,
        *,
        binding: MammouthCliSessionBinding,
        on_output: Callable[[CliRuntimeOutput], Awaitable[None]],
        timeout_seconds: float,
    ) -> CliRuntimeResult: ...


class MammouthCliAgentRuntimeAdapter:
    provider_id = "mammouth-ai"
    runtime_id = "mammouth-cli"
    runtime_type = "cli"
    capabilities = (
        AgentProviderCapability.AGENT_EXECUTION,
        AgentProviderCapability.PERSISTENT_SESSIONS,
        AgentProviderCapability.STREAMING,
        AgentProviderCapability.INTERRUPT_CANCEL,
        AgentProviderCapability.FILESYSTEM_EDITING,
        AgentProviderCapability.SHELL_TOOLS,
        AgentProviderCapability.GIT_OPERATIONS,
        AgentProviderCapability.USAGE_PARTIAL,
    )

    _terminal_events = {
        "turn/failed",
        "turn/interrupted",
    }
    _redacted_keys = (
        "api_key",
        "apikey",
        "authorization",
        "credential",
        "diagnostic",
        "error",
        "password",
        "private_key",
        "privatekey",
        "secret",
        "stderr",
        "token",
    )
    _credential_pattern = re.compile(
        r"(?i)(?:api[_-]?key|authorization|bearer|access[_-]?token|secret)"
        r"\s*[:=]\s*[^\s,;]+"
    )
    _known_credential_keys = {
        "api_key",
        "mammouth_api_key",
        "mammouth_session_api_key",
        "mammouth_session_access_token",
        "mammouth_session_refresh_token",
        "mammouth_session_id_token",
        "mammouth_session_password",
        "mammouth_session_secret",
        "mammouth_session_token",
    }

    def __init__(
        self,
        *,
        cli: MammouthCliAdapter | None = None,
        probe: CliRuntimeProbe | None = None,
        run_command: MammouthCliCommandExecutor | None = None,
        timeout_seconds: float = 1800.0,
        start_timeout_seconds: float = 15.0,
    ) -> None:
        self.cli = cli or MammouthCliAdapter()
        self.probe = probe
        self.run_command = run_command
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.start_timeout_seconds = max(0.1, float(start_timeout_seconds))
        self._listeners: set[Callable[[AgentRuntimeEvent], None]] = set()
        self._session_aliases: dict[str, str] = {}
        self._canonical_sessions: dict[str, str] = {}
        self._session_requests: dict[str, AgentRuntimeSessionRequest] = {}
        self._session_payloads: dict[str, dict] = {}
        self._active_tasks: dict[str, asyncio.Task[None]] = {}

    async def health(self) -> AgentRuntimeHealth:
        if self.probe is None or self.run_command is None:
            return AgentRuntimeHealth.UNAVAILABLE
        try:
            readiness = await asyncio.to_thread(self.probe.evaluate, self.cli)
        except Exception:
            return AgentRuntimeHealth.UNAVAILABLE
        return (
            AgentRuntimeHealth.HEALTHY
            if readiness.ready
            else AgentRuntimeHealth.UNAVAILABLE
        )

    async def recover(self) -> AgentRuntimeHealth:
        return await self.health()

    async def shutdown(self) -> None:
        tasks = set(self._active_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._active_tasks.clear()

    def subscribe_events(
        self,
        listener: Callable[[AgentRuntimeEvent], None],
    ) -> Callable[[], None]:
        self._listeners.add(listener)

        def unsubscribe() -> None:
            self._listeners.discard(listener)

        return unsubscribe

    def _emit(self, event: AgentRuntimeEvent) -> None:
        for listener in tuple(self._listeners):
            try:
                listener(event)
            except Exception:
                continue

    @classmethod
    def _safe_value(cls, value):
        if isinstance(value, dict):
            return {
                key: cls._safe_value(item)
                for key, item in value.items()
                if str(key).casefold() not in cls._known_credential_keys
                and not any(
                    marker in str(key).casefold()
                    for marker in cls._redacted_keys
                )
            }
        if isinstance(value, list):
            return [cls._safe_value(item) for item in value]
        if isinstance(value, str):
            return cls._credential_pattern.sub("[redacted]", value)
        return value

    @classmethod
    def _safe_metadata(cls, payload: dict) -> dict:
        metadata = payload.get("metadata")
        return cls._safe_value(metadata) if isinstance(metadata, dict) else {}

    async def list_sessions(
        self,
        request: AgentRuntimeListRequest,
    ) -> AgentRuntimeResult:
        del request
        sessions = []
        for canonical_id in sorted(self._session_requests):
            payload = self._session_payloads.get(canonical_id, {})
            sessions.append(
                {
                    "id": canonical_id,
                    "provider_native_session_id": self._session_aliases.get(
                        canonical_id
                    ),
                    "metadata": self._safe_metadata(payload),
                }
            )
        return AgentRuntimeResult(
            payload={"data": sessions, "source": self.runtime_id, "complete": False}
        )

    async def create_session(
        self,
        request: AgentRuntimeSessionRequest,
    ) -> AgentRuntimeResult:
        canonical_id = f"mammouth-session-{uuid.uuid4().hex}"
        self._session_requests[canonical_id] = request
        return AgentRuntimeResult(
            provider_native_session_id=canonical_id,
            payload={
                "pending": True,
                "canonical_session_id": canonical_id,
                "provider_native_session_id": None,
                "source": self.runtime_id,
            },
        )

    async def resume_session(
        self,
        provider_native_session_id: str,
        request: AgentRuntimeSessionRequest,
    ) -> AgentRuntimeResult:
        native_id = provider_native_session_id.strip()
        if not native_id:
            raise ValueError("Mammouth Code resume requires a session id")
        if native_id in self._session_requests:
            canonical_id = native_id
            native_id = self._session_aliases.get(canonical_id)
        else:
            canonical_id = self._canonical_sessions.get(native_id, native_id)
            self._session_aliases[canonical_id] = native_id
            self._canonical_sessions[native_id] = canonical_id
        self._session_requests[canonical_id] = request
        return AgentRuntimeResult(
            provider_native_session_id=canonical_id,
            payload={
                "resumed": True,
                "canonical_session_id": canonical_id,
                "provider_native_session_id": native_id,
            },
        )

    async def read_session(
        self,
        provider_native_session_id: str,
    ) -> AgentRuntimeResult:
        canonical_id, native_id = self._resolve_session(provider_native_session_id)
        metadata = self._session_payloads.get(canonical_id, {})
        return AgentRuntimeResult(
            provider_native_session_id=canonical_id,
            payload={
                "id": canonical_id,
                "provider_native_session_id": native_id,
                "active": canonical_id in self._active_tasks,
                "known": canonical_id in self._session_requests,
                "metadata": self._safe_metadata(metadata),
                "source": self.runtime_id,
            },
        )

    async def close_session(
        self,
        provider_native_session_id: str,
    ) -> AgentRuntimeResult:
        canonical_id, native_id = self._resolve_session(provider_native_session_id)
        await self.interrupt(canonical_id)
        self._session_requests.pop(canonical_id, None)
        self._session_payloads.pop(canonical_id, None)
        self._session_aliases.pop(canonical_id, None)
        if native_id:
            self._canonical_sessions.pop(native_id, None)
        return AgentRuntimeResult(
            provider_native_session_id=canonical_id,
            payload={"closed": True},
        )

    async def restore_session(
        self,
        provider_native_session_id: str,
    ) -> AgentRuntimeResult:
        canonical_id, native_id = self._resolve_session(provider_native_session_id)
        return AgentRuntimeResult(
            provider_native_session_id=canonical_id,
            payload={
                "restored": canonical_id in self._session_requests,
                "provider_native_session_id": native_id,
            },
        )

    async def compact_session(
        self,
        provider_native_session_id: str,
    ) -> AgentRuntimeResult:
        del provider_native_session_id
        raise AgentRuntimeUnsupportedCapability(
            AgentProviderCapability.NATIVE_CONTEXT_COMPACTION
        )

    async def respond_approval(
        self,
        request_id: int | str,
        result: dict,
    ) -> None:
        del request_id, result
        raise AgentRuntimeUnsupportedCapability(
            AgentProviderCapability.INTERACTIVE_APPROVALS
        )

    def _resolve_session(self, session_id: str) -> tuple[str, str | None]:
        value = session_id.strip()
        canonical_id = self._canonical_sessions.get(value, value)
        return canonical_id, self._session_aliases.get(canonical_id)

    def logical_session_id_for(
        self,
        provider_native_session_id: str | None,
    ) -> str | None:
        native_id = str(provider_native_session_id or "").strip()
        if not native_id:
            return None
        return self._canonical_sessions.get(native_id)

    async def start_turn(
        self,
        provider_native_session_id: str,
        request: AgentRuntimeTurnRequest,
    ) -> AgentRuntimeResult:
        if self.run_command is None:
            raise MammouthCliRuntimeError(
                "Mammouth Code execution requires an injected sandbox executor"
            )
        if self.probe is None:
            raise MammouthCliRuntimeError("Mammouth Code runtime is not execution-ready")
        canonical_id, native_id = self._resolve_session(provider_native_session_id)
        if not canonical_id:
            raise ValueError("Mammouth Code turn requires a session id")
        if canonical_id in self._active_tasks:
            raise MammouthCliRuntimeError(
                "Mammouth Code session already has an active turn"
            )
        try:
            readiness_details = await asyncio.to_thread(
                self.probe.evaluate,
                self.cli,
            )
        except Exception:
            raise MammouthCliRuntimeError(
                "Mammouth Code runtime is not execution-ready"
            ) from None
        if not readiness_details.ready or not readiness_details.resolved_executable:
            raise MammouthCliRuntimeError("Mammouth Code runtime is not execution-ready")

        session_request = self._session_requests.get(canonical_id)
        if session_request is None:
            session_request = AgentRuntimeSessionRequest(
                project_id="runtime-session",
                workspace_cwd=request.workspace_cwd,
            )
            self._session_requests[canonical_id] = session_request
        cwd = Path(request.workspace_cwd or session_request.workspace_cwd or ".")
        if native_id:
            command = self.cli.build_resume_command(
                executable=readiness_details.resolved_executable,
                cwd=cwd,
                session_id=native_id,
                prompt=request.message,
                model=request.model or session_request.model,
            )
        else:
            command = self.cli.build_command(
                executable=readiness_details.resolved_executable,
                cwd=cwd,
                prompt=request.message,
                model=request.model or session_request.model,
            )

        binding = MammouthCliSessionBinding(
            canonical_session_id=canonical_id,
            provider_native_session_id=native_id,
            assignment_id=session_request.assignment_id,
            execution_id=session_request.execution_id,
            execution_workspace_id=session_request.execution_workspace_id,
            worker_id=session_request.worker_id,
        )
        started: asyncio.Future[AgentRuntimeResult] = (
            asyncio.get_running_loop().create_future()
        )
        task = asyncio.create_task(
            self._run_turn(canonical_id, native_id, command, binding, started),
            name=f"mammouth-cli-turn-{canonical_id}",
        )
        self._active_tasks[canonical_id] = task
        try:
            return await asyncio.wait_for(
                asyncio.shield(started),
                timeout=self.start_timeout_seconds,
            )
        except BaseException:
            if not task.done():
                task.cancel()
            with contextlib.suppress(BaseException):
                await task
            raise

    def _clear_active_task(
        self,
        canonical_id: str,
        task: asyncio.Task[None],
    ) -> None:
        if self._active_tasks.get(canonical_id) is task:
            self._active_tasks.pop(canonical_id, None)

    async def _run_turn(
        self,
        canonical_id: str,
        native_id: str | None,
        command: CliRuntimeCommand,
        binding: MammouthCliSessionBinding,
        started: asyncio.Future[AgentRuntimeResult],
    ) -> None:
        stream = MammouthCliJsonEventStream()
        last_turn_id: str | None = None
        terminal_seen = False
        if native_id:
            stream.session_id = native_id

        async def on_output(output: CliRuntimeOutput) -> None:
            nonlocal native_id, last_turn_id, terminal_seen
            if output.stream != "stdout" or not output.text.strip():
                return
            try:
                parsed_events = stream.parse(output.text)
            except (TypeError, ValueError) as exc:
                if not started.done():
                    started.set_exception(
                        MammouthCliRuntimeError(
                            "Mammouth Code emitted invalid JSON output"
                        )
                    )
                    return
                raise ValueError(
                    "Mammouth Code emitted invalid JSONL output"
                ) from exc

            if stream.session_id:
                native_id = stream.session_id
                self._session_aliases[canonical_id] = native_id
                self._canonical_sessions[native_id] = canonical_id
                self._session_payloads[canonical_id] = self._safe_value(
                    {
                        "sessionID": native_id,
                        "type": "session",
                    }
                )

            if not started.done() and native_id:
                started.set_result(
                    AgentRuntimeResult(
                        provider_native_session_id=canonical_id,
                        provider_native_turn_id=last_turn_id,
                        payload={
                            "started": True,
                            "canonical_session_id": canonical_id,
                            "provider_native_session_id": native_id,
                            "source": self.runtime_id,
                        },
                    )
                )
                await asyncio.sleep(0)

            for parsed in parsed_events:
                event = AgentRuntimeEvent(
                    event_type=parsed.event_type,
                    provider_native_session_id=parsed.provider_native_session_id,
                    provider_native_turn_id=parsed.provider_native_turn_id,
                    payload=self._safe_value(parsed.payload),
                )
                if event.provider_native_turn_id:
                    last_turn_id = event.provider_native_turn_id
                if event.event_type in self._terminal_events:
                    terminal_seen = True
                self._emit(event)
            await asyncio.sleep(0)

        try:
            result = await self.run_command(
                command,
                binding=binding,
                on_output=on_output,
                timeout_seconds=self.timeout_seconds,
            )
            if not started.done():
                if result.exit_code == 0:
                    started.set_exception(
                        MammouthCliRuntimeError(
                            "Mammouth Code exited before emitting a session id"
                        )
                    )
                else:
                    started.set_exception(
                        MammouthCliRuntimeError("Mammouth Code execution failed")
                    )
                return
            if result.exit_code == 0:
                if not terminal_seen:
                    self._emit(
                        AgentRuntimeEvent(
                            event_type="turn/completed",
                            provider_native_session_id=native_id,
                            provider_native_turn_id=last_turn_id,
                            payload={
                                "type": "turn/completed",
                                "synthetic": True,
                                "exit_code": 0,
                            },
                        )
                    )
            elif not terminal_seen:
                self._emit(
                    AgentRuntimeEvent(
                        event_type="turn/failed",
                        provider_native_session_id=native_id,
                        provider_native_turn_id=last_turn_id,
                        payload={"type": "turn/failed", "synthetic": True},
                    )
                )
        except asyncio.CancelledError:
            if not started.done():
                started.cancel()
            else:
                self._emit(
                    AgentRuntimeEvent(
                        event_type="turn/interrupted",
                        provider_native_session_id=native_id,
                        provider_native_turn_id=last_turn_id,
                        payload={"type": "turn/interrupted", "synthetic": True},
                    )
                )
            raise
        except Exception:
            if not started.done():
                started.set_exception(
                    MammouthCliRuntimeError("Mammouth Code execution failed")
                )
            elif not terminal_seen:
                self._emit(
                    AgentRuntimeEvent(
                        event_type="turn/failed",
                        provider_native_session_id=native_id,
                        provider_native_turn_id=last_turn_id,
                        payload={"type": "turn/failed", "synthetic": True},
                    )
                )
        finally:
            current = asyncio.current_task()
            for key, value in tuple(self._active_tasks.items()):
                if value is current:
                    self._active_tasks.pop(key, None)

    async def interrupt(
        self,
        provider_native_session_id: str,
    ) -> AgentRuntimeResult:
        canonical_id, native_id = self._resolve_session(provider_native_session_id)
        task = self._active_tasks.get(canonical_id)
        interrupted = task is not None and not task.done()
        if interrupted:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        return AgentRuntimeResult(
            provider_native_session_id=canonical_id,
            payload={
                "interrupted": interrupted,
                "provider_native_session_id": native_id,
            },
        )
