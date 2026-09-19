from __future__ import annotations

from typing import Iterable

from codex_web.services.agent_model_egress import (
    AGENT_MODEL_EGRESS_RELAY_SCRIPT,
    AgentRuntimeModelEgressDeniedError,
    AgentRuntimeModelEgressEndpoint,
    AgentRuntimeModelEgressError,
    AssignmentBoundAgentModelEgressBroker,
    model_egress_endpoints_from_base_urls,
)


# Compatibility aliases for callers/tests migrating from the original Codex-specific
# names. The implementation and enforcement boundary are provider-neutral.
CodexModelEgressError = AgentRuntimeModelEgressError
CodexModelEgressDeniedError = AgentRuntimeModelEgressDeniedError
CodexModelEgressEndpoint = AgentRuntimeModelEgressEndpoint
AssignmentBoundModelEgressBroker = AssignmentBoundAgentModelEgressBroker
CODEX_MODEL_EGRESS_RELAY_SCRIPT = AGENT_MODEL_EGRESS_RELAY_SCRIPT


def endpoints_from_provider_base_urls(
    base_urls: Iterable[str | None],
    *,
    include_default_openai: bool = True,
) -> tuple[AgentRuntimeModelEgressEndpoint, ...]:
    """Compatibility helper retaining the legacy Codex/OpenAI defaults."""

    defaults = (
        (
            AgentRuntimeModelEgressEndpoint("api.openai.com", 443),
            AgentRuntimeModelEgressEndpoint("chatgpt.com", 443),
        )
        if include_default_openai
        else ()
    )
    return model_egress_endpoints_from_base_urls(
        base_urls,
        default_endpoints=defaults,
    )


__all__ = [
    "AssignmentBoundModelEgressBroker",
    "CODEX_MODEL_EGRESS_RELAY_SCRIPT",
    "CodexModelEgressDeniedError",
    "CodexModelEgressEndpoint",
    "CodexModelEgressError",
    "endpoints_from_provider_base_urls",
]
