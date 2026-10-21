"""Ephemeral readiness admission derived only by the canonical intent executor.

This carries no authority token or persisted ownership. The provider must re-enter
canonical admission around network awaits; request parameters cannot set context.
"""
import hashlib
import json
import math
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Awaitable, Callable

from codex_web.action_providers import ActionRequest
from codex_web.identity import IdentityState
from codex_web.secrets import SecretState
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


@dataclass(frozen=True)
class ReadyIdentityObservation:
    """One checkpoint's canonical identity inputs, never an authorization cache."""
    state: IdentityState
    observed_at: float

    def matches(self, current: IdentityState, *, now: float) -> bool:
        if not math.isfinite(now) or not math.isfinite(self.observed_at) or now < self.observed_at:
            return False
        # These exact fields are authentication activity, not membership/grants.
        # Idle deadlines are checked separately so extensions cannot prolong this
        # observation. Every other field, including revocation and policy, stays
        # exact. Do not remove fields recursively or normalize entire identities.
        activity = {'sessions': {'__all__': {'last_seen_at', 'idle_expires_at'}},
                    'service_tokens': {'__all__': {'last_used_at'}}}
        if self.state.model_dump(exclude=activity) != current.model_dump(exclude=activity):
            return False
        for previous, latest in zip(self.state.sessions, current.sessions):
            old_idle, new_idle = previous.idle_expires_at, latest.idle_expires_at
            absolute = previous.absolute_expires_at
            if not all(math.isfinite(value) for value in (old_idle, new_idle, absolute)):
                return False
            was_live = (previous.revoked_at is None
                        and min(old_idle, absolute) > self.observed_at)
            if was_live:
                # A touch may extend the stored deadline, but the original pin
                # still expires. Shortening always changes admission, even if
                # the new deadline has not yet elapsed.
                if new_idle < old_idle or now >= min(old_idle, absolute):
                    return False
                if previous.step_up_until is not None:
                    if (not math.isfinite(previous.step_up_until)
                            or self.observed_at < previous.step_up_until <= now):
                        return False
            elif new_idle != old_idle:
                # Historical expired/revoked sessions cannot be resurrected by
                # treating a deadline edit as a harmless activity update.
                return False
        for token in self.state.service_tokens:
            if (token.revoked_at is None and token.expires_at is not None
                    and (not math.isfinite(token.expires_at)
                         or self.observed_at < token.expires_at <= now)):
                return False
        return True


@dataclass(frozen=True)
class ReadySecretObservation:
    """Exact credential authority at one checkpoint, excluding audit appends only."""
    state: SecretState
    observed_at: float

    def matches(self, current: SecretState, *, now: float) -> bool:
        if not math.isfinite(now) or not math.isfinite(self.observed_at) or now < self.observed_at:
            return False
        if self.state.model_dump(exclude={'audit'}) != current.model_dump(exclude={'audit'}):
            return False
        for reference in self.state.references:
            if (reference.revoked_at is None and reference.expires_at is not None
                    and (not math.isfinite(reference.expires_at)
                         or self.observed_at < reference.expires_at <= now)):
                return False
        return True
