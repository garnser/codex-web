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
MODEL_ROUTING_PINNED_MODEL = "model.routing.pinned_model_id"
MODEL_ROUTING_PREFERRED_PROVIDERS = "model.routing.preferred_provider_ids"
MODEL_ROUTING_PREFERRED_LATENCIES = "model.routing.preferred_latency_classes"
MODEL_ROUTING_PREFER_LOWER_COST = "model.routing.prefer_lower_cost"
MODEL_ROUTING_ALLOW_FALLBACK = "model.routing.allow_fallback"
MODEL_ROUTING_MAX_COST_USD = "model.routing.max_cost_usd"

AGENT_ROUTING_CONFIGURATION_KEYS = (
    AGENT_ROUTING_PREFERRED_PROVIDERS,
    AGENT_ROUTING_PREFERRED_RUNTIMES,
    AGENT_ROUTING_ALLOWED_PROVIDERS,
    AGENT_ROUTING_ALLOWED_RUNTIMES,
    AGENT_ROUTING_REQUIRED_RESIDENCY,
    AGENT_ROUTING_REQUIRED_COMPLIANCE,
    AGENT_ROUTING_ALLOW_FALLBACK,
    AGENT_ROUTING_MAX_RUNTIME_COST_USD,
    MODEL_ROUTING_PINNED_MODEL,
    MODEL_ROUTING_PREFERRED_PROVIDERS,
    MODEL_ROUTING_PREFERRED_LATENCIES,
    MODEL_ROUTING_PREFER_LOWER_COST,
    MODEL_ROUTING_ALLOW_FALLBACK,
    MODEL_ROUTING_MAX_COST_USD,
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
        ConfigurationSpec(
            key=MODEL_ROUTING_PINNED_MODEL,
            value_kind=ConfigurationValueKind.STRING,
            default="",
            allowed_scopes=scopes,
            category="Model & Provider",
            description=(
                "Optional concrete Model Gateway definition pin. Explicit workflow/turn "
                "and Agent Profile pins take precedence; tenant policy still constrains it."
            ),
        ),
        ConfigurationSpec(
            key=MODEL_ROUTING_PREFERRED_PROVIDERS,
            value_kind=ConfigurationValueKind.STRING_LIST,
            default=[],
            allowed_scopes=scopes,
            category="Model & Provider",
            description="Ordered preferred Model Gateway provider IDs.",
        ),
        ConfigurationSpec(
            key=MODEL_ROUTING_PREFERRED_LATENCIES,
            value_kind=ConfigurationValueKind.STRING_LIST,
            default=[],
            allowed_scopes=scopes,
            category="Model & Provider",
            description="Ordered preferred model latency classes: low, standard, high.",
        ),
        ConfigurationSpec(
            key=MODEL_ROUTING_PREFER_LOWER_COST,
            value_kind=ConfigurationValueKind.BOOLEAN,
            default=False,
            allowed_scopes=scopes,
            category="Model & Provider",
            description="Prefer the lowest estimated cost among eligible models.",
        ),
        ConfigurationSpec(
            key=MODEL_ROUTING_ALLOW_FALLBACK,
            value_kind=ConfigurationValueKind.BOOLEAN,
            default=True,
            allowed_scopes=scopes,
            category="Model & Provider",
            description="Whether model routing may try another eligible model.",
        ),
        ConfigurationSpec(
            key=MODEL_ROUTING_MAX_COST_USD,
            value_kind=ConfigurationValueKind.NUMBER,
            default=None,
            minimum=0.000001,
            allowed_scopes=scopes,
            category="Model & Provider",
            description="Optional maximum estimated cost for one model invocation.",
        ),
    )
    return tuple(service.register_spec(spec) for spec in specs)
