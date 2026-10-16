"""Ephemeral readiness admission derived only by the canonical intent executor.

This carries no authority token or persisted ownership. The provider must re-enter
canonical admission around network awaits; request parameters cannot set context.
"""
import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Awaitable, Callable

from codex_web.action_providers import ActionRequest
from codex_web.services.code_host_continuation import request_identity
from codex_web.services.action_providers import ActionRequirementError


@dataclass(frozen=True)
class ReadyExecution:
    binding_id: str
    request_identity: str
    checkpoint: Callable[[], Awaitable[None]]
    settlement_guard: Callable[[object], dict] | None = None
    admitted_intent: Callable[[], object] | None = None


_current: ContextVar[ReadyExecution | None] = ContextVar("code_host_ready_execution", default=None)


@contextmanager
def readiness_execution(context: ReadyExecution | None):
    token = _current.set(context)
    try:
        yield
    finally:
        _current.reset(token)


async def readiness_checkpoint(request: ActionRequest, binding_id: str) -> None:
    context = _current.get()
    if context is None or context.binding_id != binding_id or context.request_identity != request_identity(request):
        raise ActionRequirementError("Readiness requires current canonical ActionIntent admission")
    try:
        await context.checkpoint()
    except ActionRequirementError:
        raise
    except Exception as exc:
        raise ActionRequirementError("Current readiness admission is unavailable") from exc


def readiness_binding_checksum(binding, provider):
    """Exact non-secret binding/provider attribution, not a second config store."""
    payload = {"binding": binding.model_dump(mode="json"),
        "provider": {name: getattr(provider, name, None) for name in
                     ("provider_type", "provider_instance", "contract_version", "api_base", "web_base")}}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def readiness_verification_checkpoint(binding_id: str) -> None:
    context = _current.get()
    if context is None or context.binding_id != binding_id:
        raise ActionRequirementError("Readiness verification requires canonical originating intent context")
    await context.checkpoint()
