from __future__ import annotations

from codex_web.configuration import (
    ConfigurationScope,
    ConfigurationSpec,
    ConfigurationValueKind,
)
from codex_web.services.configuration import ConfigurationService


CODEX_WORKER_ACCESS_TOKEN_CONFIG = "codex.worker.access_token_secret"
CODEX_WORKER_API_KEY_CONFIG = "codex.worker.api_key_secret"
CODEX_EXECUTION_AUTHENTICATION_MODE_CONFIG = "codex.execution.authentication_mode"
CODEX_DEFAULT_AUTHENTICATION_MODE = "delegated_worker_token"


def install_codex_worker_configuration(
    service: ConfigurationService,
) -> ConfigurationSpec:
    """Register explicit Codex authentication mode and reference-only credentials."""
    scopes = [
        ConfigurationScope.ORGANIZATION,
        ConfigurationScope.WORKSPACE,
        ConfigurationScope.PROJECT,
    ]
    service.register_spec(
        ConfigurationSpec(
            key=CODEX_EXECUTION_AUTHENTICATION_MODE_CONFIG,
            value_kind=ConfigurationValueKind.STRING,
            required=True,
            default=CODEX_DEFAULT_AUTHENTICATION_MODE,
            allowed_scopes=scopes,
            category="Model & Provider",
            description=(
                "Explicit Codex execution authentication mode. Changing this value "
                "changes authentication/billing semantics and never triggers fallback."
            ),
            hot_reloadable=True,
            startup_only=False,
            grants_authority=False,
        )
    )
    service.register_spec(
        ConfigurationSpec(
            key=CODEX_WORKER_API_KEY_CONFIG,
            value_kind=ConfigurationValueKind.SECRET_REF,
            required=False,
            default=None,
            allowed_scopes=scopes,
            category="Model & Provider",
            description=(
                "Secret reference used only when Codex execution authentication mode "
                "is explicitly api_key."
            ),
            hot_reloadable=True,
            startup_only=False,
            grants_authority=False,
        )
    )
    return service.register_spec(
        ConfigurationSpec(
            key=CODEX_WORKER_ACCESS_TOKEN_CONFIG,
            value_kind=ConfigurationValueKind.SECRET_REF,
            required=False,
            default=None,
            allowed_scopes=scopes,
            category="Model & Provider",
            description=(
                "Secret reference used only when Codex execution authentication mode "
                "is delegated_worker_token. Configuration selects only a reference "
                "and does not grant secret-use authority."
            ),
            hot_reloadable=True,
            startup_only=False,
            grants_authority=False,
        )
    )
