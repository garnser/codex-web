"""Scoped native reasoning adapter for the existing ModelGateway registry.

Opaque authentication remains in the canonical Codex credential owner. A public
ModelInvocationRequest is insufficient to select this execution path.
"""
from __future__ import annotations

import contextvars
import asyncio
import hashlib
import json
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Callable, Protocol

from codex_web.identity import AuthenticationActor
from codex_web.model_gateway import (
    ModelAccessSource, ModelUsageSemantics, ModelInvocationRequest,
)
from codex_web.model_providers import ModelProviderAdapterError
from codex_web.services.codex_reasoning_policy import NativeReasoningPolicyError


@dataclass(slots=True)
class NativeProcessOwnership:
    """Ephemeral join handle, never permission, idempotency or result authority."""
    assignment_id: str | None = None
    workspace_path: str | None = None
    native_session_id: str | None = None
    bootstrap_sent: bool = False
    deadline_at: float | None = None
    cancelled: threading.Event = field(default_factory=threading.Event, repr=False)
    stop: Callable | None = field(default=None, repr=False)

    def validate_current(self):
        if self.cancelled.is_set() or self.deadline_at is None or time.monotonic() >= self.deadline_at:
            raise NativeReasoningPolicyError('Native invocation cancelled or deadline expired')

    def bind_workspace(self, path):
        value = str(path)
        if self.workspace_path is not None and self.workspace_path != value:
            raise NativeReasoningPolicyError('Original native workspace changed')
        self.workspace_path = value

    def bind_native_session(self, native_id):
        if not native_id or (self.native_session_id is not None and self.native_session_id != native_id):
            raise NativeReasoningPolicyError('Original native session changed')
        self.native_session_id = native_id

    def attach(self, assignment_id, stop):
        if self.assignment_id is not None or not callable(stop):
            raise NativeReasoningPolicyError('One physical native process owner required')
        self.assignment_id, self.stop = assignment_id, stop


@dataclass(frozen=True, slots=True)
class NativeModelInvocationContext:
    organization_id: str
    workspace_id: str
    actor_id: str
    project_id: str
    activation_id: str
    role_id: str
    execution_id: str
    max_input_tokens: int | None
    max_output_tokens: int | None
    validate_current: Callable[[], None] = field(repr=False, compare=False)
    requester: AuthenticationActor | None = field(default=None, repr=False, compare=False)
    input_message_sha256: str | None = None
    max_duration_seconds: float = 120.0
    process_owner: NativeProcessOwnership | None = field(default=None, repr=False, compare=False)
    concrete_model: str | None = None
    system_prompt: str | None = field(default=None, repr=False, compare=False)
    # Bound from the selected canonical ModelDefinition at the adapter edge.
    # These are declared model capacities, not application-budget replacements.
    model_context_window_tokens: int | None = None
    model_output_capacity_tokens: int | None = None

    def accepts(self, request: ModelInvocationRequest, actor: AuthenticationActor) -> bool:
        return ((actor.organization_id, actor.workspace_id, actor.identity_id) ==
                (self.organization_id, self.workspace_id, self.actor_id)
                and self.execution_id == f'executive:{self.activation_id}:{self.role_id}'
                and request.execution_id == self.execution_id
                and request.purpose == ('executive-synthesis' if self.role_id == 'synthesis'
                                        else 'executive-role:' + self.role_id)
                and request.allow_fallback is False
                and 0 < request.timeout_seconds <= self.max_duration_seconds
                and self._matches_ceiling(request.max_input_tokens, self.max_input_tokens)
                and self._matches_ceiling(request.max_output_tokens, self.max_output_tokens))

    def observed_tokens_allowed(self, input_tokens: int | None, output_tokens: int | None) -> bool:
        # Partial events are not completion evidence; the result owner separately
        # requires both exact counters. Null application ceilings need a finite
        # selected-model context declaration, never an invented infinity value.
        if ((self.max_input_tokens is None or self.max_output_tokens is None)
                and (type(self.model_context_window_tokens) is not int
                     or self.model_context_window_tokens <= 0)):
            return False
        for value, limit in ((input_tokens, self.max_input_tokens),
                             (output_tokens, self.max_output_tokens),
                             (output_tokens, self.model_output_capacity_tokens),
                             (input_tokens, self.model_context_window_tokens),
                             (output_tokens, self.model_context_window_tokens)):
            if value is not None and limit is not None and value > limit:
                return False
        return (input_tokens is None or output_tokens is None
                or self.model_context_window_tokens is None
                or input_tokens + output_tokens <= self.model_context_window_tokens)

    @staticmethod
    def _matches_ceiling(requested: int | None, authorized: int | None) -> bool:
        # None is an exact opt-out marker, never a wildcard for a finite value.
        if requested is None or authorized is None:
            return requested is None and authorized is None
        return 0 < requested <= authorized


_current_native_model_invocation = contextvars.ContextVar('canonical_native_model_invocation', default=None)


def current_native_model_invocation() -> NativeModelInvocationContext:
    context = _current_native_model_invocation.get()
    if not isinstance(context, NativeModelInvocationContext):
        raise NativeReasoningPolicyError('Canonical native model invocation context required')
    return context


class NativeReasoningBridge(Protocol):
    """Composition of existing Thread/Turn/custody/usage owners, never a new engine."""

    def supports(self, provider, model, context: NativeModelInvocationContext) -> bool: ...
    async def invoke(self, provider, model, request, *, actor, context): ...


class NativeCodexModelProviderAdapter:
    adapter_type = 'codex-native-reasoning'

    def __init__(self, bridge: NativeReasoningBridge):
        self.bridge = bridge

    def entitlement_route(self, provider, model, request, *, actor, context) -> bool:
        return (isinstance(context, NativeModelInvocationContext)
                and context.accepts(request, actor)
                and provider.adapter_type == self.adapter_type
                and provider.access_source == ModelAccessSource.CHATGPT_SUBSCRIPTION
                and provider.usage_semantics == ModelUsageSemantics.ENTITLEMENT
                and provider.runtime_provider == 'codex'
                and provider.credential_ref is None and provider.credential_required is False
                and provider.base_url is None
                and self.bridge.supports(provider, model, context))

    async def invoke(self, provider, model, request, *, credential):
        raise ModelProviderAdapterError('Canonical scoped native invocation required')

    async def invoke_scoped(self, provider, model, request, *, actor, context, credential):
        if credential is not None or not self.entitlement_route(provider, model, request,
                                                                actor=actor, context=context):
            raise ModelProviderAdapterError('Native reasoning entitlement/context not qualified')
        # Keep the exact authorized application markers. Routing's output
        # reservation estimate is not an observed-input ceiling.
        context = replace(context, model_context_window_tokens=model.context_window_tokens,
                          model_output_capacity_tokens=model.max_output_tokens)
        from codex_web.services.post_turn_readiness import PostTurnReadinessService
        owner = NativeProcessOwnership(deadline_at=time.monotonic() + request.timeout_seconds)
        original_guard = context.validate_current
        def invocation_current():
            owner.validate_current()
            original_guard()
            owner.validate_current()
        context = replace(context, validate_current=invocation_current)
        try:
            await PostTurnReadinessService._run_owned(
                lambda: asyncio.to_thread(context.validate_current),
                seconds=5., name='native-model-reservation')
        except Exception as exc:
            raise ModelProviderAdapterError('Native consultation reservation unavailable') from exc
        # Caller-context binding is pure. Blocking canonical validation belongs
        # in the bridge's owned off-loop preparation and final native guards.
        context = replace(context, concrete_model=model.concrete_model, system_prompt=request.system_prompt,
                          process_owner=owner, requester=actor,
                          input_message_sha256=hashlib.sha256(json.dumps(
                              [item.model_dump(mode='json') for item in request.messages],
                              ensure_ascii=False, separators=(',', ':')).encode()).hexdigest())
        token = _current_native_model_invocation.set(context)
        try:
            return await self.bridge.invoke(provider, model, request, actor=actor, context=context)
        finally:
            _current_native_model_invocation.reset(token)
