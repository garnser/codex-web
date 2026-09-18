from __future__ import annotations

from codex_web.configuration import (
    ConfigurationScope,
    ConfigurationSpec,
    ConfigurationValueKind,
)
from codex_web.services.configuration import ConfigurationService


CODEX_WORKER_ACCESS_TOKEN_CONFIG = "codex.worker.access_token_secret"


def install_codex_worker_configuration(
    service: ConfigurationService,
) -> ConfigurationSpec:
    """Register the reference-only credential selector for isolated Codex workers."""
    return service.register_spec(
        ConfigurationSpec(
            key=CODEX_WORKER_ACCESS_TOKEN_CONFIG,
            value_kind=ConfigurationValueKind.SECRET_REF,
            required=True,
            default=None,
            allowed_scopes=[
                ConfigurationScope.ORGANIZATION,
                ConfigurationScope.WORKSPACE,
                ConfigurationScope.PROJECT,
            ],
            description=(
                "Secret reference used for assignment-bound Codex worker authentication. "
                "Configuration selects only a reference and does not grant secret-use authority."
            ),
            hot_reloadable=True,
            startup_only=False,
            grants_authority=False,
        )
    )
