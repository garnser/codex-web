from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from codex_web.execution_workers import ExecutionRuntimeBinding
from codex_web.runtime.claude import ClaudeCodeRuntime
from codex_web.services.agent_model_egress import AgentRuntimeModelEgressEndpoint
from codex_web.services.agent_process_session import (
    AssignmentBoundAgentProcessSession,
    AssignmentBoundAgentProcessSessionError,
    AssignmentBoundAgentProcessSessionManager,
    AssignmentBoundAgentProcessSessionStaleError,
)
from codex_web.services.agent_worker_session import (
    AssignmentBoundAgentSessionStatus,
    AssignmentRuntimeCredentialProvider,
)
from codex_web.services.local_execution_worker import LocalExecutionWorkerRuntime


AssignmentBoundClaudeSessionError = AssignmentBoundAgentProcessSessionError
AssignmentBoundClaudeSessionStaleError = AssignmentBoundAgentProcessSessionStaleError
AssignmentBoundClaudeSessionStatus = AssignmentBoundAgentSessionStatus


class AssignmentBoundClaudeSession(AssignmentBoundAgentProcessSession):
    """Claude specialization of the generic assignment-bound process session."""

    def __init__(
        self,
        local_worker: LocalExecutionWorkerRuntime,
        host: Any,
        assignment_id: str,
        *,
        runtime_factory: Callable[..., Any] = ClaudeCodeRuntime,
        watchdog_interval_seconds: float = 1.0,
        egress_endpoints_resolver: Callable[
            [], tuple[AgentRuntimeModelEgressEndpoint, ...]
        ]
        | None = None,
        credential_provider: AssignmentRuntimeCredentialProvider | None = None,
        runtime_binding: ExecutionRuntimeBinding | None = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        if credential_provider is None:
            raise AssignmentBoundClaudeSessionError(
                "Anthropic runtime credential provider is not configured"
            )
        super().__init__(
            local_worker,
            host,
            assignment_id,
            runtime_factory=runtime_factory,
            credential_provider=credential_provider,
            runtime_binding=runtime_binding,
            watchdog_interval_seconds=watchdog_interval_seconds,
            egress_endpoints_resolver=egress_endpoints_resolver,
            clock=clock,
            monotonic=monotonic,
            sleep=sleep,
        )


class AssignmentBoundClaudeSessionManager(AssignmentBoundAgentProcessSessionManager):
    """Configure the generic worker-session lifecycle for Claude Code."""

    def __init__(
        self,
        local_worker: LocalExecutionWorkerRuntime,
        host: Any,
        *,
        runtime_factory: Callable[..., Any] = ClaudeCodeRuntime,
        watchdog_interval_seconds: float = 1.0,
        egress_endpoints_resolver: Callable[
            [], tuple[AgentRuntimeModelEgressEndpoint, ...]
        ]
        | None = None,
        credential_provider: AssignmentRuntimeCredentialProvider,
        runtime_binding: ExecutionRuntimeBinding | None = None,
    ) -> None:
        super().__init__(
            local_worker,
            host,
            runtime_factory=runtime_factory,
            session_factory=AssignmentBoundClaudeSession,
            watchdog_interval_seconds=watchdog_interval_seconds,
            egress_endpoints_resolver=egress_endpoints_resolver,
            credential_provider=credential_provider,
            runtime_binding=runtime_binding,
        )
