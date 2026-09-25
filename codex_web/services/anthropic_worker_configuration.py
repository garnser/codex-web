from __future__ import annotations

from codex_web.configuration import (
    ConfigurationScope,
    ConfigurationSpec,
    ConfigurationValueKind,
)
from codex_web.services.configuration import ConfigurationService


ANTHROPIC_WORKER_API_KEY_CONFIG = "anthropic.worker.api_key_secret"


def install_anthropic_worker_configuration(
    service: ConfigurationService,
) -> ConfigurationSpec:
    """Register the reference-only credential selector for Claude workers."""
    return service.register_spec(
        ConfigurationSpec(
            key=ANTHROPIC_WORKER_API_KEY_CONFIG,
            value_kind=ConfigurationValueKind.SECRET_REF,
            required=False,
            default=None,
            allowed_scopes=[
                ConfigurationScope.ORGANIZATION,
                ConfigurationScope.WORKSPACE,
                ConfigurationScope.PROJECT,
            ],
            category="Model & Provider",
            description=(
                "Secret reference used for assignment-bound Claude worker authentication. "
                "Configuration selects only a reference and does not grant secret-use authority."
            ),
            hot_reloadable=True,
            startup_only=False,
            grants_authority=False,
        )
    )
