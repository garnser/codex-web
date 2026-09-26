from __future__ import annotations

from codex_web.configuration import (
    ConfigurationScope,
    ConfigurationSpec,
    ConfigurationValueKind,
)
from codex_web.services.configuration import ConfigurationService

MAMMOUTH_WORKER_API_KEY_CONFIG = "mammouth.worker.api_key_secret"


def install_mammouth_worker_configuration(
    service: ConfigurationService,
) -> ConfigurationSpec:
    """Register the reference-only credential selector for Mammouth workers."""
    return service.register_spec(
        ConfigurationSpec(
            key=MAMMOUTH_WORKER_API_KEY_CONFIG,
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
                "Secret reference used for assignment-bound Mammouth Code worker "
                "authentication. Configuration selects only a reference and does "
                "not grant secret-use authority."
            ),
            hot_reloadable=True,
            startup_only=False,
            grants_authority=False,
        )
    )
