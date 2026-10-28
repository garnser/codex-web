"""Code-owned bounds for the canonical Executive deployment setting."""

from codex_web.configuration import ConfigurationScope, ConfigurationSpec, ConfigurationValueKind
from codex_web.services.configuration import ConfigurationService


EXECUTIVE_INVOCATION_TIMEOUT = ConfigurationSpec(
    key="executive.invocation_timeout_seconds",
    value_kind=ConfigurationValueKind.INTEGER,
    default=120,
    minimum=1,
    maximum=600,
    allowed_scopes=[ConfigurationScope.DEPLOYMENT],
    category="Model & Provider",
    description=(
        "Finite seconds per Executive role or synthesis invocation, including native "
        "preparation and completion. Resolved once per consultation; it does "
        "not extend leases, authorize retries or change call/token/cost limits."
    ),
    startup_only=False,
    hot_reloadable=True,
    grants_authority=False,
)


def install_executive_configuration(service: ConfigurationService) -> ConfigurationSpec:
    return service.register_spec(EXECUTIVE_INVOCATION_TIMEOUT)
