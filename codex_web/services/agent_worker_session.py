from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol, TypeVar, runtime_checkable

from codex_web.execution_workers import (
    AssignmentCancelRequest, AssignmentStatus, ExecutionAssignment, ExecutionRuntimeBinding,
)
from codex_web.identity import AuthenticationActor


T = TypeVar("T")


class AssignmentBoundAgentSessionError(RuntimeError):
    """Base error for provider-neutral assignment session failures."""


class AssignmentBoundAgentSessionStaleError(AssignmentBoundAgentSessionError):
    """The cached session no longer owns a valid assignment lease/fence."""


def runtime_binding_identity_matches(
    assignment_binding: ExecutionRuntimeBinding | None,
    session_binding: ExecutionRuntimeBinding | None,
) -> bool:
    """Compare the runtime identity a session serves against an assignment.

    Routing enriches assignment bindings with per-execution details such as
    sandbox profiles and Codex authentication mode. Those are execution
    facts, not runtime identity: a session pins the provider, runtime and
    capability revision and tolerates the enriched fields. A session without
    a pinned binding constrains nothing.
    """

    if session_binding is None:
        return True
    if assignment_binding is None:
        return False
    return (
        assignment_binding.provider_id == session_binding.provider_id
        and assignment_binding.runtime_id == session_binding.runtime_id
        and assignment_binding.capability_revision
        == session_binding.capability_revision
    )


@dataclass(frozen=True, slots=True)
class AssignmentBoundAgentSessionStatus:
    """Provider-neutral status for one assignment-bound execution-agent session."""

    assignment_id: str
    worker_id: str
    fence: int | None
    running: bool
    ready: bool
    last_error: str | None
    credential_expires_at: float | None

    @property
    def delegation_expires_at(self) -> float | None:
        """Compatibility alias for the original Codex-specific status field."""

        return self.credential_expires_at

    def public(self) -> dict[str, str | int | float | bool | None]:
        return {
            "assignment_id": self.assignment_id,
            "worker_id": self.worker_id,
            "fence": self.fence,
            "running": self.running,
            "ready": self.ready,
            "last_error": self.last_error,
            "credential_expires_at": self.credential_expires_at,
            "delegation_expires_at": self.credential_expires_at,
        }


@runtime_checkable
class AssignmentBoundAgentSession(Protocol):
    """Runtime-neutral worker-session boundary for one canonical assignment."""

    assignment_id: str
    fence: int | None
    workspace_path: Path | None

    @property
    def worker_id(self) -> str: ...

    def status(self) -> AssignmentBoundAgentSessionStatus: ...

    async def start(self) -> "AssignmentBoundAgentSession": ...

    def validate_current(self) -> ExecutionAssignment: ...

    async def request(self, method: str, params: Any = None) -> dict[str, Any]: ...

    async def notify(self, method: str, params: Any = None) -> None: ...

    async def respond_to_server_request(
        self,
        request_id: int | str,
        result: dict[str, Any],
    ) -> None: ...

    async def stop(self) -> None: ...


@runtime_checkable
class AssignmentBoundAgentSessionManager(Protocol):
    """Provider-neutral lifecycle boundary for assignment-bound agent sessions."""

    async def start(self, assignment_id: str) -> AssignmentBoundAgentSession: ...

    def get(self, assignment_id: str) -> AssignmentBoundAgentSession | None: ...

    async def checkpoint(self, assignment_id: str) -> tuple[Any, ...]: ...

    async def complete(
        self,
        assignment_id: str,
        *,
        succeeded: bool,
        failure_code: str | None = None,
        failure_message: str | None = None,
        artifact_ids: tuple[str, ...] = (),
        evidence_ids: tuple[str, ...] = (),
    ) -> ExecutionAssignment: ...

    async def stop(self, assignment_id: str) -> None: ...

    async def stop_all(self) -> None: ...


@runtime_checkable
class AssignmentRuntimeCredentialGrant(Protocol):
    """Metadata-only credential grant bound to one worker assignment/fence."""

    assignment_id: str
    worker_id: str
    fence: int
    secret_id: str
    secret_rotation: int
    expires_at: float


@runtime_checkable
class AssignmentRuntimeLaunchInput(Protocol):
    """Ephemeral launch material exposed only inside the credential boundary."""

    delegation: AssignmentRuntimeCredentialGrant
    command: tuple[str, ...]
    environment: Any


@runtime_checkable
class AssignmentRuntimeCredentialProvider(Protocol):
    """Runtime-specific credential/launch boundary used by the generic worker session."""

    def use(
        self,
        assignment: ExecutionAssignment,
        *,
        worker_id: str,
        fence: int,
        actor: AuthenticationActor,
        consumer: Callable[[AssignmentRuntimeLaunchInput], T],
    ) -> T: ...

    def validate_current(
        self,
        delegation: AssignmentRuntimeCredentialGrant,
        assignment: ExecutionAssignment,
        *,
        actor: AuthenticationActor,
    ) -> None: ...


def bootstrap_cancellation_reason(reason: str | None) -> str:
    """Keep internal failure metadata within the strict cancellation contract."""
    return (str(reason or "").strip() or "bootstrap failed")[:500]


async def start_assignment_session(session: Any, local_worker: Any, assignment_id: str) -> None:
    """Settle an owned bootstrap start before fencing failed/cancelled startup.

    Cancellation cannot interrupt a synchronous claim or spawn already running
    in an executor. Shield that ownership boundary, then revoke only the fence
    captured by this attempt and retain the complete recovery workspace.
    """
    if local_worker is None:
        await session.start()
        return
    original_task = asyncio.create_task(
        asyncio.to_thread(local_worker._pending_assignment, assignment_id)
    )
    original = None
    startup_task = None
    try:
        original = await asyncio.shield(original_task)
        if original.subject.kind != "thread_bootstrap" or original.status != AssignmentStatus.PENDING:
            await session.start()
            return
        # A same-worker lease can otherwise be resumed by _claim_or_resume.
        # This pending bootstrap attempt owns only a new claim it actually wins.
        session.require_new_claim = True
        startup_task = asyncio.create_task(session.start())
        await asyncio.shield(startup_task)
    except (Exception, asyncio.CancelledError) as startup_error:
        # A cancelled await leaves its executor operation running. Resolve its
        # result before deciding which fence this attempt actually owns.
        if original is None:
            try:
                original = await original_task
            except Exception as lookup_error:
                raise lookup_error from startup_error
        if startup_task is not None and not startup_task.done():
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await startup_task
        if original.subject.kind == "thread_bootstrap" and original.status == AssignmentStatus.PENDING:
            try:
                await asyncio.to_thread(
                    local_worker.worker_service.cancel_bootstrap,
                    assignment_id,
                    AssignmentCancelRequest(
                        expected_fence=session.fence if session.fence is not None else original.fence,
                        reason="assignment-bound bootstrap session startup failed or cancelled",
                    ),
                    actor=local_worker.control_actor,
                )
            except Exception as cleanup_error:
                raise cleanup_error from startup_error
            finally:
                # A shielded start can succeed after its caller times out.
                # Its unregistered process/watchdog must still be stopped.
                await session.stop()
        raise
