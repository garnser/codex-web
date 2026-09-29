from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from codex_web.execution_workers import (
    AssignmentStatus,
    ExecutionAssignment,
    ExecutionRuntimeBinding,
)
from codex_web.identity import AuthenticationActor, PrincipalKind
from codex_web.runtime.codex import (
    CODEX_MINIMUM_ADDRESS_SPACE_BYTES,
    CODEX_MINIMUM_PROCESS_COUNT,
    CodexRuntime,
    trusted_local_codex_command,
)
from codex_web.services.agent_model_egress import AgentRuntimeModelEgressEndpoint
from codex_web.services.agent_process_session import (
    AssignmentBoundAgentProcessSession,
    AssignmentBoundAgentProcessSessionError,
    AssignmentBoundAgentProcessSessionManager,
    AssignmentBoundAgentProcessSessionStaleError,
)
from codex_web.services.control_plane_broker import DeferredControlPlaneBrokerFactory
from codex_web.services.local_execution_worker import LocalExecutionWorkerRuntime
from codex_web.services.agent_worker_session import (
    AssignmentBoundAgentSessionStatus,
    AssignmentRuntimeCredentialProvider,
    AssignmentRuntimeLaunchInput,
)


AssignmentBoundCodexSessionError = AssignmentBoundAgentProcessSessionError
AssignmentBoundCodexSessionStaleError = AssignmentBoundAgentProcessSessionStaleError
AssignmentBoundCodexSessionStatus = AssignmentBoundAgentSessionStatus

CODEX_TRUSTED_LOCAL_MODE = "trusted_local_session"


@dataclass(frozen=True, slots=True)
class CodexTrustedLocalGrant:
    """Metadata-only trusted-local grant bound to one worker assignment/fence."""

    assignment_id: str
    worker_id: str
    fence: int
    expires_at: float
    codex_home: str
    mode: str = CODEX_TRUSTED_LOCAL_MODE

    @property
    def secret_id(self) -> str:
        return ""

    @property
    def secret_rotation(self) -> int:
        return 0


@dataclass(frozen=True, slots=True)
class CodexTrustedLocalLaunch:
    delegation: CodexTrustedLocalGrant
    command: tuple[str, ...]
    environment: dict[str, str]
    trusted_writable_mounts: tuple[tuple[Path, Path], ...]


class TrustedLocalCodexCredentialProvider:
    """Launch assignment-bound Codex with the operator's ambient Codex auth.

    Trusted-local mode intentionally reuses the operator's own Codex session
    instead of a delegated secret. The ambient Codex home is mounted into the
    canonical sandbox so the app-server authenticates exactly as it does on
    the host, while the worker boundary keeps bounding repository and network
    access. There is no credential copy: the mount is the same directory.
    """

    def __init__(
        self,
        *,
        codex_home: Path,
        executable: str,
        max_grant_seconds: int = 24 * 60 * 60,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.codex_home = codex_home
        self.executable = executable
        self.max_grant_seconds = max(60, int(max_grant_seconds))
        self._clock = clock

    def _require_worker_actor(self, actor: AuthenticationActor) -> None:
        if (
            actor.principal_kind != PrincipalKind.SERVICE
            or "execution-worker:run" not in actor.service_scopes
        ):
            raise AssignmentBoundCodexSessionError(
                "trusted-local Codex launch requires execution-worker:run "
                "service authority"
            )

    def _grant(
        self,
        assignment: ExecutionAssignment,
        *,
        worker_id: str,
        fence: int,
    ) -> CodexTrustedLocalGrant:
        now = self._clock()
        lease = assignment.lease
        if assignment.status not in {
            AssignmentStatus.CLAIMED,
            AssignmentStatus.RUNNING,
        }:
            raise AssignmentBoundCodexSessionStaleError(
                "trusted-local Codex launch requires a claimed or running "
                "assignment"
            )
        if (
            assignment.assigned_worker_id != worker_id
            or assignment.fence != fence
            or lease is None
            or lease.worker_id != worker_id
            or lease.fence != fence
            or lease.expires_at <= now
        ):
            raise AssignmentBoundCodexSessionStaleError(
                "trusted-local Codex lease/fence is stale or invalid"
            )
        expires_at = min(
            lease.expires_at,
            now + self.max_grant_seconds,
        )
        if assignment.deadline_at is not None:
            expires_at = min(expires_at, assignment.deadline_at)
        return CodexTrustedLocalGrant(
            assignment_id=assignment.id,
            worker_id=worker_id,
            fence=fence,
            expires_at=expires_at,
            codex_home=str(self.codex_home),
        )

    def use(
        self,
        assignment: ExecutionAssignment,
        *,
        worker_id: str,
        fence: int,
        actor: AuthenticationActor,
        consumer: Callable[[AssignmentRuntimeLaunchInput], Any],
    ) -> Any:
        self._require_worker_actor(actor)
        grant = self._grant(
            assignment,
            worker_id=worker_id,
            fence=fence,
        )
        return consumer(
            CodexTrustedLocalLaunch(
                delegation=grant,
                command=trusted_local_codex_command(
                    executable=self.executable,
                    subcommand=("app-server",),
                ),
                environment={"CODEX_HOME": str(self.codex_home)},
                trusted_writable_mounts=((self.codex_home, self.codex_home),),
            )
        )

    def validate_current(
        self,
        delegation: CodexTrustedLocalGrant,
        assignment: ExecutionAssignment,
        *,
        actor: AuthenticationActor,
    ) -> None:
        self._require_worker_actor(actor)
        now = self._clock()
        lease = assignment.lease
        if assignment.id != delegation.assignment_id:
            raise AssignmentBoundCodexSessionStaleError(
                "trusted-local Codex grant assignment changed"
            )
        if (
            assignment.assigned_worker_id != delegation.worker_id
            or assignment.fence != delegation.fence
            or lease is None
            or lease.worker_id != delegation.worker_id
            or lease.fence != delegation.fence
            or lease.expires_at <= now
        ):
            raise AssignmentBoundCodexSessionStaleError(
                "trusted-local Codex lease/fence is stale or invalid"
            )
        if assignment.deadline_at is not None and assignment.deadline_at <= now:
            raise AssignmentBoundCodexSessionStaleError(
                "trusted-local Codex assignment deadline has expired"
            )


class DispatchingCodexCredentialProvider:
    """Route Codex launches by the assignment's configured authentication mode."""

    def __init__(
        self,
        *,
        delegated: AssignmentRuntimeCredentialProvider,
        trusted_local: AssignmentRuntimeCredentialProvider | None,
    ) -> None:
        self.delegated = delegated
        self.trusted_local = trusted_local

    def _provider(self, assignment: ExecutionAssignment):
        mode = getattr(
            assignment.runtime_binding,
            "authentication_mode",
            None,
        )
        if mode == CODEX_TRUSTED_LOCAL_MODE:
            if self.trusted_local is None:
                raise AssignmentBoundCodexSessionError(
                    "trusted-local Codex authentication is not configured"
                )
            return self.trusted_local
        return self.delegated

    def use(
        self,
        assignment: ExecutionAssignment,
        *,
        worker_id: str,
        fence: int,
        actor: AuthenticationActor,
        consumer: Callable[[AssignmentRuntimeLaunchInput], Any],
    ) -> Any:
        return self._provider(assignment).use(
            assignment,
            worker_id=worker_id,
            fence=fence,
            actor=actor,
            consumer=consumer,
        )

    def validate_current(
        self,
        delegation: Any,
        assignment: ExecutionAssignment,
        *,
        actor: AuthenticationActor,
    ) -> None:
        mode = getattr(delegation, "mode", None)
        provider = (
            self.trusted_local
            if mode == CODEX_TRUSTED_LOCAL_MODE and self.trusted_local is not None
            else self.delegated
        )
        provider.validate_current(delegation, assignment, actor=actor)


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
        control_plane_broker_factory: DeferredControlPlaneBrokerFactory | None = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        provider = credential_provider
        if provider is None:
            delegated = local_worker.codex_auth_delegation
            if delegated is None:
                raise AssignmentBoundCodexSessionError(
                    "Codex runtime credential provider is not configured"
                )
            provider = DispatchingCodexCredentialProvider(
                delegated=delegated,
                trusted_local=local_worker.trusted_local_codex_delegation,
            )
        super().__init__(
            local_worker,
            host,
            assignment_id,
            runtime_factory=runtime_factory,
            credential_provider=provider,
            runtime_binding=runtime_binding,
            minimum_address_space_bytes=CODEX_MINIMUM_ADDRESS_SPACE_BYTES,
            minimum_process_count=CODEX_MINIMUM_PROCESS_COUNT,
            restart_runtime_on_timeout=False,
            watchdog_interval_seconds=watchdog_interval_seconds,
            egress_endpoints_resolver=egress_endpoints_resolver,
            control_plane_broker_factory=control_plane_broker_factory,
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
        control_plane_broker_factory: DeferredControlPlaneBrokerFactory | None = None,
    ) -> None:
        provider = credential_provider
        if provider is None:
            delegated = local_worker.codex_auth_delegation
            if delegated is None:
                raise AssignmentBoundCodexSessionError(
                    "Codex runtime credential provider is not configured"
                )
            provider = DispatchingCodexCredentialProvider(
                delegated=delegated,
                trusted_local=local_worker.trusted_local_codex_delegation,
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
            control_plane_broker_factory=control_plane_broker_factory,
        )
