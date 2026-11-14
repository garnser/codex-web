"""Typed controller observations for the existing queued bootstrap recovery.

These are before-input observations, never provider outcomes or an input grant.
The private observation is accepted only at the controller's same-claim commit.
"""
from dataclasses import dataclass
import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class NativePreinputProof(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    version: Literal['1.0'] = '1.0'
    phase: Literal['before_native_turn_input'] = 'before_native_turn_input'
    code: Literal['native_context_delivery_limit'] = 'native_context_delivery_limit'
    origin: Literal['context_renderer'] = 'context_renderer'
    attempt_id: str = Field(min_length=1)
    claim_id: str = Field(min_length=1)
    snapshot_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    thread_id: str = Field(min_length=1)
    bootstrap_id: str = Field(min_length=1)
    bootstrap_binding_id: str = Field(min_length=1)
    assignment_id: str = Field(min_length=1)
    execution_id: str = Field(min_length=1)
    execution_workspace_id: str = Field(min_length=1)
    worker_id: str = Field(min_length=1)
    fence: int = Field(ge=1)
    assignment_authority_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    observed_at: float = Field(ge=0, allow_inf_nan=False)


@dataclass(frozen=True)
class _NativePreinputObservation:
    proof: NativePreinputProof


def retry_custody_fingerprint(raw):
    """Same custody may renew its finite lease while policy is inspected."""
    value = dict(raw)
    value.pop('updated_at', None)
    if isinstance(value.get('lease'), dict):
        lease = dict(value['lease'])
        lease.pop('expires_at', None)
        lease.pop('renewed_at', None)
        value['lease'] = lease
    return value


def same_retry_custody(expected, actual):
    for raw in (expected, actual):
        if not isinstance(raw, dict) or not isinstance(raw.get('lease'), dict):
            return False
        lease = raw['lease']
        required = (raw.get('updated_at'), lease.get('acquired_at'), lease.get('expires_at'))
        optional = (lease.get('renewed_at'), raw.get('deadline_at'))
        if not all(finite_timestamp(value) for value in required):
            return False
        if not all(value is None or finite_timestamp(value) for value in optional):
            return False
    if retry_custody_fingerprint(expected) != retry_custody_fingerprint(actual):
        return False
    before, after = expected.get('lease'), actual.get('lease')
    if not isinstance(before, dict) or not isinstance(after, dict):
        return False
    old_renewed, new_renewed = before.get('renewed_at'), after.get('renewed_at')
    return (actual.get('updated_at', 0) >= expected.get('updated_at', 0)
        and after.get('expires_at', 0) >= before.get('expires_at', 0)
        and (old_renewed is None or (new_renewed is not None and new_renewed >= old_renewed)))


def finite_timestamp(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def no_native_capture(attempt):
    return (attempt.scheduling_retirement is None and attempt.started_at is None
        and not attempt.queued_resume_history and all(getattr(attempt, name) is None for name in (
            'queued_start_correlation', 'queued_start_evidence', 'queued_start_receipt',
            'queued_resume_correlation', 'queued_resume_evidence')))


def observe_renderer_refusal(service, *, attempt_id, claim_id, thread_id, bootstrap, assignment):
    """Called only by the real renderer exception boundary, before any RPC."""
    if not service or not attempt_id or not claim_id or bootstrap is None or assignment is None:
        return None
    current = service.store.get(attempt_id)
    if (current is None or current.status != 'reconciliation_required'
            or current.queued_admission_claim_id != claim_id or not no_native_capture(current)
            or (current.replacement_thread_id or current.thread_id) != thread_id
            or not current.queued_turn_snapshot
            or (bootstrap.assignment_id, bootstrap.execution_id, bootstrap.execution_workspace_id)
                != (assignment.id, assignment.execution_id, assignment.execution_workspace_id)
            or assignment.lease is None or assignment.assigned_worker_id != assignment.lease.worker_id
            or assignment.fence != assignment.lease.fence):
        return None
    pin = current.queued_turn_snapshot.get('owner_routing_pin') or {}
    recipient = pin.get('recipient') or {}
    if (not current.queued_turn_snapshot.get('work_item_ref')
            or (recipient.get('thread_id'), recipient.get('bootstrap_id'), recipient.get('assignment_id'),
                recipient.get('execution_id'), recipient.get('worker_id'), recipient.get('fence'))
                != (thread_id, bootstrap.id, assignment.id, assignment.execution_id,
                    assignment.assigned_worker_id, assignment.fence)):
        return None  # This recovery slice owns only the exact pinned Work recipient.
    from codex_web.services.owner_routing import OwnerRoutingService
    return _NativePreinputObservation(NativePreinputProof(
        attempt_id=current.id, claim_id=claim_id,
        snapshot_sha256=service.queued_snapshot_digest(current.queued_turn_snapshot),
        thread_id=thread_id, bootstrap_id=bootstrap.bootstrap_id, bootstrap_binding_id=bootstrap.id,
        assignment_id=assignment.id, execution_id=assignment.execution_id,
        execution_workspace_id=assignment.execution_workspace_id,
        worker_id=assignment.assigned_worker_id, fence=assignment.fence,
        assignment_authority_sha256=OwnerRoutingService._authority_checksum(assignment),
        observed_at=float(service.clock())))


def matches_original(service, attempt, proof, *, current_claim=True):
    return (proof.attempt_id == attempt.id and attempt.queued_turn_snapshot is not None
        and proof.snapshot_sha256 == service.queued_snapshot_digest(attempt.queued_turn_snapshot)
        and (not current_claim or proof.claim_id == attempt.queued_admission_claim_id)
        and proof.thread_id == (attempt.replacement_thread_id or attempt.thread_id))


def recorded_proof(service, attempt):
    if attempt.status != 'blocked' or not no_native_capture(attempt):
        return None
    for blocker in attempt.blockers:
        raw = blocker.model_dump(mode='json').get('native_preinput_refusal')
        if raw is None:
            continue
        try:
            proof = NativePreinputProof.model_validate(raw)
        except (ValueError, TypeError):
            continue
        if blocker.code == proof.code and matches_original(service, attempt, proof):
            return proof
    return None
