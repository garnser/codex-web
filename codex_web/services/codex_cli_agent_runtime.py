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
from codex_web.cli_runtime import (
    CliRuntimeOutput,
    CliRuntimeProbe,
    CliRuntimeRunner,
)
from codex_web.codex_cli_runtime import (
    CodexCliAdapter,
    CodexCliJsonEventStream,
)


class CodexCliRuntimeError(RuntimeError):
    pass


class CodexCliAgentRuntimeAdapter:
    """Provider-neutral AgentRuntime adapter backed by the local Codex CLI.

    The adapter owns no provider credentials. The CLI subprocess receives only
    the runner's explicit environment allowlist (HOME/CODEX_HOME/PATH by
    default), allowing the provider CLI to use its existing local login without
    copying credential material into codex-web state.
    """

    provider_id = "openai"
    runtime_id = "codex-cli"
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
        cli: CodexCliAdapter | None = None,
        probe: CliRuntimeProbe | None = None,
        runner: CliRuntimeRunner | None = None,
        timeout_seconds: float = 1800.0,
        start_timeout_seconds: float = 15.0,
    ) -> None:
        self.cli = cli or CodexCliAdapter()
        self.probe = probe or CliRuntimeProbe()
        self.runner = runner or CliRuntimeRunner(
            environment_allowlist=("HOME", "CODEX_HOME", "PATH"),
        )
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.start_timeout_seconds = max(0.1, float(start_timeout_seconds))
        self._listeners: set[Callable[[AgentRuntimeEvent], None]] = set()
        self._session_aliases: dict[str, str] = {}
        self._provider_sessions: set[str] = set()
        self._active_tasks: dict[str, asyncio.Task[None]] = {}

    async def health(self) -> AgentRuntimeHealth:
        readiness = await asyncio.to_thread(self.probe.evaluate, self.cli)
        return (
            AgentRuntimeHealth.HEALTHY
            if readiness.ready
            else AgentRuntimeHealth.UNAVAILABLE
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
                # Event consumers are observational and must not break the
                # provider process or leave it orphaned.
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
                "data": [
                    {"id": session_id}
                    for session_id in sorted(self._provider_sessions)
                ],
                "source": "codex-cli",
                "complete": False,
            }
        )

    async def create_session(
        self,
        request: AgentRuntimeSessionRequest,
    ) -> AgentRuntimeResult:
        # Codex CLI creates its provider-native session on the first exec turn.
        # Returning no native id avoids inventing provider identity.
        return AgentRuntimeResult(
            payload={
                "pending": True,
                "project_id": request.project_id,
                "source": "codex-cli",
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
            raise ValueError("Codex CLI resume requires a session id")
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
                "source": "codex-cli",
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

    async def start_turn(
        self,
        provider_native_session_id: str,
        request: AgentRuntimeTurnRequest,
    ) -> AgentRuntimeResult:
        logical_session_id = provider_native_session_id.strip()
        if not logical_session_id:
            raise ValueError("Codex CLI turn requires a session id")
        if logical_session_id in self._active_tasks:
            raise CodexCliRuntimeError(
                "Codex CLI session already has an active turn"
            )

        readiness = await asyncio.to_thread(self.probe.evaluate, self.cli)
        if not readiness.ready or not readiness.resolved_executable:
            raise CodexCliRuntimeError(
                readiness.message or "Codex CLI is not execution-ready"
            )

        cwd = Path(request.workspace_cwd or ".")
        actual_session_id = self._session_aliases.get(logical_session_id)
        if actual_session_id is None and logical_session_id in self._provider_sessions:
            actual_session_id = logical_session_id

        turn_cli = CodexCliAdapter(
            executable=self.cli.executable,
            sandbox=self.cli.sandbox,
            approval_policy=(
                request.approval_policy or self.cli.approval_policy
            ),
        )
        if actual_session_id:
            command = turn_cli.build_resume_command(
                executable=readiness.resolved_executable,
                cwd=cwd,
                session_id=actual_session_id,
                prompt=request.message,
                model=request.model,
            )
        else:
            command = turn_cli.build_command(
                executable=readiness.resolved_executable,
                cwd=cwd,
                prompt=request.message,
                model=request.model,
            )

        started: asyncio.Future[AgentRuntimeResult] = (
            asyncio.get_running_loop().create_future()
        )
        task = asyncio.create_task(
            self._run_turn(
                logical_session_id,
                command,
                started,
            ),
            name=f"codex-cli-turn-{logical_session_id}",
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
        stream = CodexCliJsonEventStream()
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
            if (
                not started.done()
                and actual_session_id is not None
            ):
                started.set_result(
                    AgentRuntimeResult(
                        provider_native_session_id=actual_session_id,
                        provider_native_turn_id=last_turn_id,
                        payload={
                            "started": True,
                            "source": "codex-cli",
                        },
                    )
                )

        try:
            result = await self.runner.run(
                command,
                on_output=on_output,
                timeout_seconds=self.timeout_seconds,
            )
            if result.exit_code != 0:
                raise CodexCliRuntimeError(
                    f"Codex CLI exited with status {result.exit_code}"
                )
            if not started.done():
                raise CodexCliRuntimeError(
                    "Codex CLI exited before emitting a provider session id"
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
                        payload={
                            "type": "turn.interrupted",
                            "synthetic": True,
                        },
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
