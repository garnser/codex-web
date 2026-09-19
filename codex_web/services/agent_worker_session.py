from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from codex_web.execution_workers import ExecutionAssignment


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
