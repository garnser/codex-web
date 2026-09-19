from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from codex_web.execution_workers import ExecutionRuntimeBinding
from codex_web.runtime.codex import CodexRuntime
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


AssignmentBoundCodexSessionError = AssignmentBoundAgentProcessSessionError
AssignmentBoundCodexSessionStaleError = AssignmentBoundAgentProcessSessionStaleError
AssignmentBoundCodexSessionStatus = AssignmentBoundAgentSessionStatus


class AssignmentBoundCodexSession(AssignmentBoundAgentProcessSession):
    """Compatibility specialization of the generic assignment-bound process session."""

    def __init__(
        self,
        local_worker: LocalExecutionWorkerRuntime,
        host: Any,
        assignment_id: str,
        *,
        runtime_factory: Callable[..., Any] = CodexRuntime,
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
        provider = credential_provider or local_worker.codex_auth_delegation
        if provider is None:
            raise AssignmentBoundCodexSessionError(
                "Codex runtime credential provider is not configured"
            )
        super().__init__(
            local_worker,
            host,
            assignment_id,
            runtime_factory=runtime_factory,
            credential_provider=provider,
            runtime_binding=runtime_binding,
            watchdog_interval_seconds=watchdog_interval_seconds,
            egress_endpoints_resolver=egress_endpoints_resolver,
            clock=clock,
            monotonic=monotonic,
            sleep=sleep,
        )


class AssignmentBoundCodexSessionManager(AssignmentBoundAgentProcessSessionManager):
    """Compatibility manager that configures the generic session for Codex."""

    def __init__(
        self,
        local_worker: LocalExecutionWorkerRuntime,
        host: Any,
        *,
        runtime_factory: Callable[..., Any] = CodexRuntime,
        watchdog_interval_seconds: float = 1.0,
        egress_endpoints_resolver: Callable[
            [], tuple[AgentRuntimeModelEgressEndpoint, ...]
        ]
        | None = None,
        credential_provider: AssignmentRuntimeCredentialProvider | None = None,
        runtime_binding: ExecutionRuntimeBinding | None = None,
    ) -> None:
        provider = credential_provider or local_worker.codex_auth_delegation
        if provider is None:
            raise AssignmentBoundCodexSessionError(
                "Codex runtime credential provider is not configured"
            )
        super().__init__(
            local_worker,
            host,
            runtime_factory=runtime_factory,
            credential_provider=provider,
            runtime_binding=runtime_binding,
            session_factory=AssignmentBoundCodexSession,
            watchdog_interval_seconds=watchdog_interval_seconds,
            egress_endpoints_resolver=egress_endpoints_resolver,
        )
