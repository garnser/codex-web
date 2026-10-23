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
    max_input_tokens: int
    max_output_tokens: int
    validate_current: Callable[[], None] = field(repr=False, compare=False)
    requester: AuthenticationActor | None = field(default=None, repr=False, compare=False)
    input_message_sha256: str | None = None
    max_duration_seconds: float = 120.0
    process_owner: NativeProcessOwnership | None = field(default=None, repr=False, compare=False)
    concrete_model: str | None = None
    system_prompt: str | None = field(default=None, repr=False, compare=False)

    def accepts(self, request: ModelInvocationRequest, actor: AuthenticationActor) -> bool:
        return ((actor.organization_id, actor.workspace_id, actor.identity_id) ==
                (self.organization_id, self.workspace_id, self.actor_id)
                and self.execution_id == f'executive:{self.activation_id}:{self.role_id}'
                and request.execution_id == self.execution_id
                and request.purpose == ('executive-synthesis' if self.role_id == 'synthesis'
                                        else 'executive-role:' + self.role_id)
                and request.allow_fallback is False
                and 0 < request.timeout_seconds <= self.max_duration_seconds
                and request.max_input_tokens is not None
                and 0 < request.max_input_tokens <= self.max_input_tokens
                and 0 < request.max_output_tokens <= self.max_output_tokens)


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
