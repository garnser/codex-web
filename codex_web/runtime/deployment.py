from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


LEGACY_GITLAB_BASE_URL = "https://dev.veridataops.com/gitlab"
LEGACY_SUPPORT_PROJECT = "veridataops/support"
LEGACY_HANDOFF_CHANNEL = "C0B9M89AHCY"
GENERIC_GITLAB_BASE_URL = "https://gitlab.com"


@dataclass(frozen=True)
class DeploymentConfiguration:
    legacy_compatibility: bool
    gitlab_base_url: str
    support_project_paths: tuple[str, ...]
    support_sweep_project: str
    handoff_channel_configured: bool


def _configured_support_paths() -> list[str] | None:
    raw = os.environ.get("CODEX_WEB_SUPPORT_SERVICEDESK_PROJECT_PATHS") or os.environ.get(
        "CODEX_WEB_SUPPORT_SERVICEDESK_PROJECT_PATH"
    )
    if raw is None:
        return None
    return [value.strip().lower().strip("/") for value in raw.split(",") if value.strip()]


def _is_legacy_installation(host: Any) -> bool:
    try:
        settings = host._load_gitlab_routing_settings()
    except Exception:
        return False
    for project in getattr(settings, "projects", {}).values():
        for path in getattr(project, "project_paths", []) or []:
            normalized = str(path).strip().lower().strip("/")
            if normalized == "veridataops" or normalized.startswith("veridataops/"):
                return True
    return False


def install_deployment_configuration(app: Any, host: Any) -> DeploymentConfiguration:
    """Move installation-specific fallbacks out of the reusable runtime.

    Fresh installs receive generic/disabled integration defaults. Existing
    installations that still contain explicit VeridataOps routing retain the
    historical implicit defaults until they are configured explicitly. This
    makes upgrading non-breaking while removing those values from new installs.
    """

    existing = getattr(app.state, "deployment_configuration", None)
    if isinstance(existing, DeploymentConfiguration):
        return existing

    legacy = _is_legacy_installation(host)
    original_sweep_interval = host._support_servicedesk_sweep_interval

    def gitlab_base_url() -> str:
        explicit = os.environ.get("CODEX_WEB_GITLAB_BASE_URL") or os.environ.get("GITLAB_BASE_URL")
        return (explicit or (LEGACY_GITLAB_BASE_URL if legacy else GENERIC_GITLAB_BASE_URL)).rstrip("/")

    def support_project_paths() -> list[str]:
        explicit = _configured_support_paths()
        if explicit is not None:
            return explicit
        return [LEGACY_SUPPORT_PROJECT] if legacy else []

    def support_sweep_project() -> str:
        explicit = os.environ.get("CODEX_WEB_SUPPORT_SERVICEDESK_PROJECT_ID") or os.environ.get(
            "CODEX_WEB_SUPPORT_SERVICEDESK_PROJECT_PATH"
        )
        if explicit is not None:
            return explicit.strip()
        return LEGACY_SUPPORT_PROJECT if legacy else ""

    def support_sweep_interval() -> float:
        if not support_sweep_project():
            return 0.0
        return original_sweep_interval()

    host._gitlab_api_base_url = gitlab_base_url
    host._support_servicedesk_project_paths = support_project_paths
    host._support_servicedesk_sweep_project = support_sweep_project
    host._support_servicedesk_sweep_interval = support_sweep_interval

    api_base = os.environ.get("CODEX_WEB_GITLAB_API_BASE")
    host.GITLAB_API_BASE = (api_base or f"{gitlab_base_url()}/api/v4").rstrip("/")

    explicit_handoff_channel = os.environ.get("CODEX_WEB_HANDOFF_COORDINATION_CHANNEL")
    host.HANDOFF_COORDINATION_CHANNEL = (
        explicit_handoff_channel.strip()
        if explicit_handoff_channel is not None
        else (LEGACY_HANDOFF_CHANNEL if legacy else None)
    ) or None

    configuration = DeploymentConfiguration(
        legacy_compatibility=legacy,
        gitlab_base_url=gitlab_base_url(),
        support_project_paths=tuple(support_project_paths()),
        support_sweep_project=support_sweep_project(),
        handoff_channel_configured=bool(host.HANDOFF_COORDINATION_CHANNEL),
    )
    app.state.deployment_configuration = configuration
    return configuration
