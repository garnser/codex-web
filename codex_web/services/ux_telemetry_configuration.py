from __future__ import annotations

from codex_web.configuration import (
    ConfigurationScope,
    ConfigurationSpec,
    ConfigurationValueKind,
)
from codex_web.services.configuration import ConfigurationService


UX_TELEMETRY_ENABLED_CONFIG = "ux.telemetry.enabled"
UX_TELEMETRY_RETENTION_DAYS_CONFIG = "ux.telemetry.retention_days"


def install_ux_telemetry_configuration(
    service: ConfigurationService,
) -> tuple[ConfigurationSpec, ConfigurationSpec]:
    enabled = service.register_spec(
        ConfigurationSpec(
            key=UX_TELEMETRY_ENABLED_CONFIG,
            value_kind=ConfigurationValueKind.BOOLEAN,
            default=False,
            allowed_scopes=[
                ConfigurationScope.DEPLOYMENT,
                ConfigurationScope.ORGANIZATION,
                ConfigurationScope.WORKSPACE,
            ],
            category="Privacy & Telemetry",
            description=(
                "Collect the closed, content-free UX event taxonomy. Disabled by default; "
                "this flag never grants authority and can be force-disabled as a kill switch."
            ),
            hot_reloadable=True,
            feature_flag=True,
            kill_switch_capable=True,
            grants_authority=False,
        )
    )
    retention = service.register_spec(
        ConfigurationSpec(
            key=UX_TELEMETRY_RETENTION_DAYS_CONFIG,
            value_kind=ConfigurationValueKind.INTEGER,
            default=30,
            minimum=1,
            maximum=365,
            allowed_scopes=[
                ConfigurationScope.DEPLOYMENT,
                ConfigurationScope.ORGANIZATION,
                ConfigurationScope.WORKSPACE,
            ],
            category="Privacy & Telemetry",
            description="Retention window in days for content-free UX telemetry events.",
            hot_reloadable=True,
            grants_authority=False,
        )
    )
    return enabled, retention
