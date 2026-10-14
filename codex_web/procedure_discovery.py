"""Structured discovery evidence; requirements never confer action authority."""
from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from codex_web.definitions import DefinitionReference


class ProcedureCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    reference: DefinitionReference
    outcome: Literal["selected", "rejected", "blocked", "conflict"]
    reasons: tuple[str, ...]


class ProcedureDiscovery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["1.0"] = "1.0"
    actor_identity_id: str
    organization_id: str
    workspace_id: str
    project_id: str | None
    resource_ids: tuple[str, ...]
    operation: str | None
    context_checksum: str
    evaluated_at: float
    outcome: Literal["matched", "no_match", "failed", "ambiguous", "inaccessible", "unsupported_enforcement"]
    selected: tuple[DefinitionReference, ...] = ()
    candidates: tuple[ProcedureCandidate, ...] = ()
    blockers: tuple[str, ...] = ()
    policy_source: str
    policy_allows: bool
    instruction_checksum: str | None = None
    instruction_characters: int = Field(default=0, ge=0)
    supporting_sources: tuple[str, ...] = ()
    capability_sources: tuple[str, ...] = ()

    @property
    def admitted(self) -> bool:
        return self.policy_allows and self.outcome in {"matched", "no_match"} and not self.blockers


# This process-local context is installed only by the canonical ActionIntent
# executor. No serialized request or procedure can supply it.
from contextlib import contextmanager
from contextvars import ContextVar
_action_intent_context = ContextVar("procedure_action_intent", default=None)

@contextmanager
def procedure_action_intent(intent_id):
    token = _action_intent_context.set(intent_id)
    try:
        yield
    finally:
        _action_intent_context.reset(token)


def current_procedure_action_intent():
    return _action_intent_context.get()
