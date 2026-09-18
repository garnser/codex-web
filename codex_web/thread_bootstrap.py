from __future__ import annotations

import hashlib
import time

from pydantic import BaseModel, ConfigDict, Field

from codex_web.compatibility import ContractSpec
from codex_web.execution_subjects import ExecutionSubject, ExecutionSubjectKind


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
