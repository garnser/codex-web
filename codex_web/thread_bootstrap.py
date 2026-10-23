from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from codex_web.compatibility import ContractSpec
from codex_web.execution_subjects import ExecutionSubject, ExecutionSubjectKind


@dataclass(frozen=True)
class RecipientBootstrapOrigin:
    """Internal controller correlation, never accepted by the public create DTO."""

    logical_id: str
    generation: int
    attribution_sha256: str
    predecessor_assignment_id: str | None = None
    work_item_ref: str | None = None
    handoff_requested_at: float | None = None
    binding_id: str | None = None
    actor_id: str | None = None


@dataclass(frozen=True)
class BootstrapPreworkerRefusal:
    """Produced only at ThreadService's before-worker-start exception boundary."""

    bootstrap_id: str
    execution_id: str
    error_type: str
    guard_deadline_exceeded: bool
    origin: RecipientBootstrapOrigin | None = None
    error_http_status: int | None = None


THREAD_BOOTSTRAP_BINDING_CONTRACT = ContractSpec(
    "thread-bootstrap-binding-state",
    "1.0",
    ("1.0",),
)


def deterministic_thread_bootstrap_binding_id(
    organization_id: str,
    workspace_id: str,
    bootstrap_id: str,
) -> str:
    raw = f"{organization_id}\n{workspace_id}\n{bootstrap_id}".encode()
    return f"threadbootstrap-{hashlib.sha256(raw).hexdigest()[:20]}"


class ThreadBootstrapBinding(BaseModel):
    """Immutable bridge from pre-thread execution identity to the returned Codex thread."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    id: str = Field(min_length=1)
    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    bootstrap_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    execution_id: str = Field(min_length=1)
    assignment_id: str = Field(min_length=1)
    execution_workspace_id: str = Field(min_length=1)
    created_by: str = Field(min_length=1)
    created_at: float = Field(default_factory=time.time)
    # ``thread/start`` returns the provider-native ID before Codex writes the
    # first rollout.  The first turn must therefore go straight to
    # ``turn/start``; trying ``thread/resume`` first treats the not-yet-created
    # rollout as stale and can trigger an unbounded replacement loop.  Old
    # bindings default to False because their first turn already predates this
    # lifecycle marker.
    initial_turn_pending: bool = False

    @property
    def subject(self) -> ExecutionSubject:
        return ExecutionSubject(
            kind=ExecutionSubjectKind.THREAD_BOOTSTRAP,
            ref=self.bootstrap_id,
        )


class ThreadBootstrapBindingState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = THREAD_BOOTSTRAP_BINDING_CONTRACT.current
    bindings: list[ThreadBootstrapBinding] = Field(default_factory=list)
