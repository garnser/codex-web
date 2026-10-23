"""Server-produced reference to existing canonical interrupted-turn recovery."""
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class HomeRestartInputProof(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    schema_version: Literal['1.3'] = '1.3'
    recovery_id: str = Field(min_length=1, max_length=512)
    original_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    thread_id: str = Field(min_length=1, max_length=200)
    requested_by: str = Field(min_length=1, max_length=200)
    selection_revision: int = Field(ge=1, strict=True)
    message_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


class HomeGoalRestartInputProof(HomeRestartInputProof):
    """Goal-derived continuation; old restart readers must reject this grant."""
    schema_version: Literal['1.5'] = '1.5'
    binding_id: str = Field(min_length=1,max_length=200)
    delivery_id: str = Field(min_length=1,max_length=200)
    goal_revision: int = Field(ge=1, strict=True)


def parse_restart_proof(value):
    model = HomeGoalRestartInputProof if value.get('schema_version') == '1.5' else HomeRestartInputProof
    return model.model_validate(value)


_current = ContextVar('canonical_home_restart_input', default=None)


def current_home_restart():
    return _current.get()


@contextmanager
def home_restart_context(authorizer):
    token = _current.set(authorizer)
    try:
        yield
    finally:
        _current.reset(token)


class HomeUpgradeInputProof(BaseModel):
    """Canonical completed supervisor handoff; never project delegation."""
    model_config = ConfigDict(extra='forbid', frozen=True)
    schema_version: Literal['1.4'] = '1.4'
    checkpoint_id: str = Field(min_length=1)
    checkpoint_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    upgrade_plan_id: str = Field(min_length=1)
    completion_evidence_id: str = Field(min_length=1)
    resume_evidence_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    requested_by: str = Field(min_length=1)
    message_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
