"""Finite capacity for complete required procedure guidance, not authority."""
from codex_web.configuration import ConfigurationScope, ConfigurationSpec, ConfigurationValueKind
from codex_web.services.configuration import ConfigurationService
from codex_web.skills import SKILL_MAX_CONTEXT_CHARS


PROCEDURE_CONTEXT_CHARACTERS = ConfigurationSpec(
    key="procedure_discovery.max_context_characters",
    value_kind=ConfigurationValueKind.INTEGER,
    default=12000,
    minimum=1000,
    maximum=SKILL_MAX_CONTEXT_CHARS,
    allowed_scopes=[ConfigurationScope.DEPLOYMENT],
    category="Agents & Execution",
    description=(
        "Maximum complete procedure guidance characters, including required Skill "
        "instructions, full contracts, supporting context and trust wrapper. Resolved "
        "once per discovery; never truncates required guidance or grants authority."
    ),
    startup_only=False,
    hot_reloadable=True,
    grants_authority=False,
)


def install_procedure_configuration(service: ConfigurationService) -> ConfigurationSpec:
    return service.register_spec(PROCEDURE_CONTEXT_CHARACTERS)
