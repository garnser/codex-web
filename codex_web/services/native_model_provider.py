"""Scoped native reasoning adapter for the existing ModelGateway registry.

Opaque authentication remains in the canonical Codex credential owner. A public
ModelInvocationRequest is insufficient to select this execution path.
"""
from __future__ import annotations

import contextvars
import asyncio
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
    stop: Callable | None = field(default=None, repr=False)

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
        try:
            await PostTurnReadinessService._run_owned(
                lambda: asyncio.to_thread(context.validate_current),
                seconds=5., name='native-model-reservation')
        except Exception as exc:
            raise ModelProviderAdapterError('Native consultation reservation unavailable') from exc
        # Caller-context binding is pure. Blocking canonical validation belongs
        # in the bridge's owned off-loop preparation and final native guards.
        context = replace(context, concrete_model=model.concrete_model, system_prompt=request.system_prompt,
                          process_owner=NativeProcessOwnership())
        token = _current_native_model_invocation.set(context)
        try:
            return await self.bridge.invoke(provider, model, request, actor=actor, context=context)
        finally:
            _current_native_model_invocation.reset(token)
