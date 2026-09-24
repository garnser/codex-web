from __future__ import annotations

from codex_web.configuration import (
    ConfigurationScope,
    ConfigurationSpec,
    ConfigurationValueKind,
)
from codex_web.services.configuration import ConfigurationService


ARTIFACT_CONTENT_BACKEND_CONFIG = "artifact.content.backend"


def install_artifact_content_configuration(
    service: ConfigurationService,
) -> ConfigurationSpec:
    return service.register_spec(
        ConfigurationSpec(
            key=ARTIFACT_CONTENT_BACKEND_CONFIG,
            value_kind=ConfigurationValueKind.STRING,
            required=True,
            default="local",
            allowed_scopes=[
                ConfigurationScope.DEPLOYMENT,
                ConfigurationScope.ORGANIZATION,
                ConfigurationScope.WORKSPACE,
                ConfigurationScope.PROJECT,
            ],
            category="Advanced",
            description=(
                "Artifact byte-content backend id. The value selects a registered "
                "backend only and never contains credentials, bucket secrets, or keys."
            ),
            hot_reloadable=True,
            startup_only=False,
            grants_authority=False,
        )
    )
