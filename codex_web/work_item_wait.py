"""Explicit unchanged-input wait receipts on canonical Work, not remote proof."""
from __future__ import annotations

import hashlib
import json
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class UnchangedWaitRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    schema_version: Literal['1.0'] = '1.0'
    expected_input_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


class UnchangedWaitCheckpoint(UnchangedWaitRequest):
    actor: str = Field(min_length=1)
    acknowledged_at: float = Field(ge=0, allow_inf_nan=False)


def wait_input_sha256(state):
    # Actual canonical inputs, including source revision, handoff, retry,
    # Artifact/Evidence references and execution prerequisites. Audit notes and
    # receipt/observation clocks are not a remote-read or prerequisite change.
    values = state.model_dump(mode='json', exclude={'wait_checkpoint', 'notes',
        'updated_at', 'last_meaningful_update_at', 'last_owner_activity_at',
        'last_gitlab_event_at', 'source_projection'})
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(',', ':'),
        ensure_ascii=False).encode()).hexdigest()


def current_unchanged_wait(state):
    witness = getattr(state, 'wait_checkpoint', None)
    return bool(witness is not None and not state.closed_at and state.current_stage != 'closed'
        and not (state.handoff and state.handoff.status == 'pending')
        and state.blocker and state.next_action
        and witness.expected_input_sha256 == wait_input_sha256(state))


def wait_context(state):
    return {'schema_version': '1.0', 'input_sha256': wait_input_sha256(state),
        'unchanged_wait_active': current_unchanged_wait(state),
        'remote_prerequisites_verified': False}
