from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Callable

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
from codex_web.cli_runtime import CliRuntimeOutput, CliRuntimeProbe, CliRuntimeRunner
from codex_web.mammouth_cli_runtime import MammouthCliAdapter, MammouthCliJsonEventStream


class MammouthCliRuntimeError(RuntimeError):
    pass


class MammouthCliAgentRuntimeAdapter:
    """Provider-neutral Mammouth Code AgentRuntime adapter.

    This adapter intentionally defaults to fail-closed execution. A caller must
    supply an execution_authorizer that proves the selected cwd/process launch is
    inside codex-web's canonical assignment/worker containment boundary. Merely
    having a usable Mammouth CLI on the control-plane host is not sufficient.
    """

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

    def __init__(
        self,
        *,
        cli: MammouthCliAdapter | None = None,
        probe: CliRuntimeProbe | None = None,
        runner: CliRuntimeRunner | None = None,
        execution_authorizer: Callable[[Path], bool] | None = None,
        timeout_seconds: float = 1800.0,
        start_timeout_seconds: float = 15.0,
    ) -> None:
        self.cli = cli or MammouthCliAdapter()
        self.probe = probe or CliRuntimeProbe(environment_allowlist=("HOME", "PATH"))
        self.runner = runner or CliRuntimeRunner(environment_allowlist=("HOME", "PATH"))
        self.execution_authorizer = execution_authorizer
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.start_timeout_seconds = max(0.1, float(start_timeout_seconds))
        self._listeners: set[Callable[[AgentRuntimeEvent], None]] = set()
        self._session_aliases: dict[str, str] = {}
        self._provider_sessions: set[str] = set()
        self._active_tasks: dict[str, asyncio.Task[None]] = {}

    def logical_session_id_for(
        self,
        provider_native_session_id: str | None,
    ) -> str | None:
        native = str(provider_native_session_id or "").strip()
        if not native:
            return None
        for logical, actual in self._session_aliases.items():
            if actual == native:
                return logical
        return native if native in self._session_aliases else None

    async def health(self) -> AgentRuntimeHealth:
        readiness = await asyncio.to_thread(self.probe.evaluate, self.cli)
        if not readiness.ready:
            return AgentRuntimeHealth.UNAVAILABLE
        # A ready CLI is still not execution-ready unless the canonical
        # assignment/worker launcher explicitly authorizes containment.
        return (
            AgentRuntimeHealth.HEALTHY
            if self.execution_authorizer is not None
            else AgentRuntimeHealth.DEGRADED
        )

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

    async def recover(self) -> AgentRuntimeHealth:
        return await self.health()

    async def shutdown(self) -> None:
        tasks = set(self._active_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._active_tasks.clear()

    async def list_sessions(
        self,
        request: AgentRuntimeListRequest,
    ) -> AgentRuntimeResult:
        del request
        return AgentRuntimeResult(
            payload={
                "data": [{"id": item} for item in sorted(self._provider_sessions)],
                "source": "mammouth-cli",
                "complete": False,
            }
        )

    async def create_session(
        self,
        request: AgentRuntimeSessionRequest,
    ) -> AgentRuntimeResult:
        return AgentRuntimeResult(
            payload={
                "pending": True,
                "project_id": request.project_id,
                "source": "mammouth-cli",
            }
        )

    async def resume_session(
        self,
        provider_native_session_id: str,
        request: AgentRuntimeSessionRequest,
    ) -> AgentRuntimeResult:
        del request
        session_id = provider_native_session_id.strip()
        if not session_id:
            raise ValueError("Mammouth Code resume requires a session id")
        actual = self._session_aliases.get(session_id, session_id)
        return AgentRuntimeResult(
            provider_native_session_id=actual,
            payload={"resumed": actual in self._provider_sessions},
        )

    async def read_session(
        self,
        provider_native_session_id: str,
    ) -> AgentRuntimeResult:
        actual = self._session_aliases.get(
            provider_native_session_id,
            provider_native_session_id,
        )
        return AgentRuntimeResult(
            provider_native_session_id=actual,
            payload={
                "id": actual,
                "active": actual in self._active_tasks,
                "known": actual in self._provider_sessions,
                "source": "mammouth-cli",
            },
        )

    async def close_session(
        self,
        provider_native_session_id: str,
    ) -> AgentRuntimeResult:
        await self.interrupt(provider_native_session_id)
        actual = self._session_aliases.pop(
            provider_native_session_id,
            provider_native_session_id,
        )
        self._provider_sessions.discard(actual)
        return AgentRuntimeResult(
            provider_native_session_id=actual,
            payload={"closed": True},
        )

    async def restore_session(
        self,
        provider_native_session_id: str,
    ) -> AgentRuntimeResult:
        actual = self._session_aliases.get(
            provider_native_session_id,
            provider_native_session_id,
        )
        return AgentRuntimeResult(
            provider_native_session_id=actual,
            payload={"restored": actual in self._provider_sessions},
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

    def _require_contained_launch(self, cwd: Path) -> None:
        if self.execution_authorizer is None:
            raise MammouthCliRuntimeError(
                "Mammouth Code execution is blocked until a canonical "
                "assignment/worker containment authorizer is configured"
            )
        try:
            authorized = bool(self.execution_authorizer(cwd))
        except Exception as exc:
            raise MammouthCliRuntimeError(
                "Mammouth Code containment authorization failed"
            ) from exc
        if not authorized:
            raise MammouthCliRuntimeError(
                "Mammouth Code execution cwd is outside the authorized "
                "assignment/worker containment boundary"
            )

    async def start_turn(
        self,
        provider_native_session_id: str,
        request: AgentRuntimeTurnRequest,
    ) -> AgentRuntimeResult:
        logical_session_id = provider_native_session_id.strip()
        if not logical_session_id:
            raise ValueError("Mammouth Code turn requires a session id")
        if logical_session_id in self._active_tasks:
            raise MammouthCliRuntimeError(
                "Mammouth Code session already has an active turn"
            )

        cwd = Path(request.workspace_cwd or ".")
        self._require_contained_launch(cwd)

        readiness = await asyncio.to_thread(self.probe.evaluate, self.cli)
        if not readiness.ready or not readiness.resolved_executable:
            raise MammouthCliRuntimeError(
                readiness.message or "Mammouth Code is not execution-ready"
            )

        actual_session_id = self._session_aliases.get(logical_session_id)
        if actual_session_id is None and logical_session_id in self._provider_sessions:
            actual_session_id = logical_session_id

        if actual_session_id:
            command = self.cli.build_resume_command(
                executable=readiness.resolved_executable,
                cwd=cwd,
                session_id=actual_session_id,
                prompt=request.message,
                model=request.model,
            )
        else:
            command = self.cli.build_command(
                executable=readiness.resolved_executable,
                cwd=cwd,
                prompt=request.message,
                model=request.model,
            )

        started: asyncio.Future[AgentRuntimeResult] = (
            asyncio.get_running_loop().create_future()
        )
        task = asyncio.create_task(
            self._run_turn(logical_session_id, command, started),
            name=f"mammouth-cli-turn-{logical_session_id}",
        )
        self._active_tasks[logical_session_id] = task
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

    async def _run_turn(
        self,
        logical_session_id: str,
        command,
        started: asyncio.Future[AgentRuntimeResult],
    ) -> None:
        stream = MammouthCliJsonEventStream()
        last_turn_id: str | None = None
        terminal_seen = False
        actual_session_id = self._session_aliases.get(logical_session_id)

        async def on_output(output: CliRuntimeOutput) -> None:
            nonlocal actual_session_id, last_turn_id, terminal_seen
            if output.stream != "stdout" or not output.text.strip():
                return
            event = stream.parse(output.text)
            if event.provider_native_session_id:
                actual_session_id = event.provider_native_session_id
                self._provider_sessions.add(actual_session_id)
                self._session_aliases[logical_session_id] = actual_session_id
                self._active_tasks[actual_session_id] = asyncio.current_task()
            if event.provider_native_turn_id:
                last_turn_id = event.provider_native_turn_id
            if event.event_type in {
                "turn.completed",
                "turn.failed",
                "turn.interrupted",
            }:
                terminal_seen = True
            self._emit(event)
            if not started.done() and actual_session_id is not None:
                started.set_result(
                    AgentRuntimeResult(
                        provider_native_session_id=actual_session_id,
                        provider_native_turn_id=last_turn_id,
                        payload={"started": True, "source": "mammouth-cli"},
                    )
                )
                await asyncio.sleep(0)

        try:
            result = await self.runner.run(
                command,
                on_output=on_output,
                timeout_seconds=self.timeout_seconds,
            )
            if result.exit_code != 0:
                raise MammouthCliRuntimeError(
                    f"Mammouth Code exited with status {result.exit_code}"
                )
            if not started.done():
                raise MammouthCliRuntimeError(
                    "Mammouth Code exited before emitting a provider session id"
                )
            if not terminal_seen:
                self._emit(
                    AgentRuntimeEvent(
                        event_type="turn.completed",
                        provider_native_session_id=actual_session_id,
                        provider_native_turn_id=last_turn_id,
                        payload={
                            "type": "turn.completed",
                            "synthetic": True,
                            "exit_code": result.exit_code,
                        },
                    )
                )
        except asyncio.CancelledError:
            if not started.done():
                started.cancel()
            else:
                self._emit(
                    AgentRuntimeEvent(
                        event_type="turn.interrupted",
                        provider_native_session_id=actual_session_id,
                        provider_native_turn_id=last_turn_id,
                        payload={"type": "turn.interrupted", "synthetic": True},
                    )
                )
            raise
        except Exception as exc:
            if not started.done():
                started.set_exception(exc)
            else:
                self._emit(
                    AgentRuntimeEvent(
                        event_type="turn.failed",
                        provider_native_session_id=actual_session_id,
                        provider_native_turn_id=last_turn_id,
                        payload={
                            "type": "turn.failed",
                            "synthetic": True,
                            "error": type(exc).__name__,
                        },
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
        actual = self._session_aliases.get(
            provider_native_session_id,
            provider_native_session_id,
        )
        task = (
            self._active_tasks.get(provider_native_session_id)
            or self._active_tasks.get(actual)
        )
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        return AgentRuntimeResult(
            provider_native_session_id=actual,
            payload={"interrupted": task is not None},
        )
