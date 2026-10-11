"""Ephemeral provider context derived from canonical ActionIntent evidence.

Workers submit an origin intent reference, never a marker or authority token.
Only the claimed ActionIntent executor establishes this context after checking
the original receipt and both immutable execution identities. It is scoped to
the exact request and binding and is not a second ownership store.
"""
from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from codex_web.action_providers import ActionRequest


def request_identity(request: ActionRequest) -> str:
    return hashlib.sha256(json.dumps(
        request.model_dump(mode="json", exclude={"correlation_id", "requested_by"}),
        sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


@dataclass(frozen=True)
class ChangeRequestContinuation:
    origin_intent_id: str
    binding_id: str
    request_identity: str
    marker: str
    external_id: str
    number: int


_context: ContextVar[ChangeRequestContinuation | None] = ContextVar(
    "code_host_change_request_continuation", default=None,
)


@contextmanager
def continuation_context(proof: ChangeRequestContinuation | None):
    token = _context.set(proof)
    try:
        yield
    finally:
        _context.reset(token)


def continuation_for(request: ActionRequest, binding_id: str):
    origin = request.parameters.get("origin_intent_id")
    if origin is None:
        return None
    proof = _context.get()
    if (
        proof is None or proof.origin_intent_id != origin
        or proof.binding_id != binding_id
        or proof.request_identity != request_identity(request)
    ):
        raise ValueError("change request continuation requires verified ActionIntent execution context")
    return proof
