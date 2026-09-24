from __future__ import annotations

from codex_web.configuration import (
    ConfigurationScope,
    ConfigurationSpec,
    ConfigurationValueKind,
)
from codex_web.services.configuration import ConfigurationService


AGENT_ROUTING_PREFERRED_PROVIDERS = "agent.routing.preferred_provider_ids"
AGENT_ROUTING_PREFERRED_RUNTIMES = "agent.routing.preferred_runtime_ids"
AGENT_ROUTING_ALLOWED_PROVIDERS = "agent.routing.allowed_provider_ids"
AGENT_ROUTING_ALLOWED_RUNTIMES = "agent.routing.allowed_runtime_ids"
AGENT_ROUTING_REQUIRED_RESIDENCY = "agent.routing.required_residency_tags"
AGENT_ROUTING_REQUIRED_COMPLIANCE = "agent.routing.required_compliance_tags"
AGENT_ROUTING_ALLOW_FALLBACK = "agent.routing.allow_fallback"
AGENT_ROUTING_MAX_RUNTIME_COST_USD = "agent.routing.max_runtime_cost_usd"

AGENT_ROUTING_CONFIGURATION_KEYS = (
    AGENT_ROUTING_PREFERRED_PROVIDERS,
    AGENT_ROUTING_PREFERRED_RUNTIMES,
    AGENT_ROUTING_ALLOWED_PROVIDERS,
    AGENT_ROUTING_ALLOWED_RUNTIMES,
    AGENT_ROUTING_REQUIRED_RESIDENCY,
    AGENT_ROUTING_REQUIRED_COMPLIANCE,
    AGENT_ROUTING_ALLOW_FALLBACK,
    AGENT_ROUTING_MAX_RUNTIME_COST_USD,
)


def install_agent_routing_configuration(
    service: ConfigurationService,
) -> tuple[ConfigurationSpec, ...]:
    scopes = [
        ConfigurationScope.GLOBAL,
        ConfigurationScope.ORGANIZATION,
        ConfigurationScope.WORKSPACE,
        ConfigurationScope.PROJECT,
    ]
    specs = (
        ConfigurationSpec(
            key=AGENT_ROUTING_PREFERRED_PROVIDERS,
            value_kind=ConfigurationValueKind.STRING_LIST,
            default=[],
            allowed_scopes=scopes,
            category="Model & Provider",
            description="Ordered preferred execution-agent provider IDs.",
        ),
        ConfigurationSpec(
            key=AGENT_ROUTING_PREFERRED_RUNTIMES,
            value_kind=ConfigurationValueKind.STRING_LIST,
            default=[],
            allowed_scopes=scopes,
            category="Model & Provider",
            description="Ordered preferred execution-agent runtime IDs.",
        ),
        ConfigurationSpec(
            key=AGENT_ROUTING_ALLOWED_PROVIDERS,
            value_kind=ConfigurationValueKind.STRING_LIST,
            default=[],
            allowed_scopes=scopes,
            category="Model & Provider",
            description="Optional execution-agent provider allowlist.",
        ),
        ConfigurationSpec(
            key=AGENT_ROUTING_ALLOWED_RUNTIMES,
            value_kind=ConfigurationValueKind.STRING_LIST,
            default=[],
            allowed_scopes=scopes,
            category="Model & Provider",
            description="Optional execution-agent runtime allowlist.",
        ),
        ConfigurationSpec(
            key=AGENT_ROUTING_REQUIRED_RESIDENCY,
            value_kind=ConfigurationValueKind.STRING_LIST,
            default=[],
            allowed_scopes=scopes,
            category="Model & Provider",
            description="Required residency tags for execution-agent routing.",
        ),
        ConfigurationSpec(
            key=AGENT_ROUTING_REQUIRED_COMPLIANCE,
            value_kind=ConfigurationValueKind.STRING_LIST,
            default=[],
            allowed_scopes=scopes,
            category="Model & Provider",
            description="Required compliance tags for execution-agent routing.",
        ),
        ConfigurationSpec(
            key=AGENT_ROUTING_ALLOW_FALLBACK,
            value_kind=ConfigurationValueKind.BOOLEAN,
            default=True,
            allowed_scopes=scopes,
            category="Model & Provider",
            description="Whether execution-agent routing may use an eligible fallback.",
        ),
        ConfigurationSpec(
            key=AGENT_ROUTING_MAX_RUNTIME_COST_USD,
            value_kind=ConfigurationValueKind.NUMBER,
            default=None,
            minimum=0,
            allowed_scopes=scopes,
            category="Model & Provider",
            description=(
                "Optional maximum declared execution-agent session cost. "
                "Runtimes without pricing metadata fail closed when set."
            ),
        ),
    )
    return tuple(service.register_spec(spec) for spec in specs)
