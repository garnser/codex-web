from __future__ import annotations

import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI

from codex_web.runtime.deployment import (
    GENERIC_GITLAB_BASE_URL,
    LEGACY_GITLAB_BASE_URL,
    LEGACY_HANDOFF_CHANNEL,
    LEGACY_SUPPORT_PROJECT,
    install_deployment_configuration,
)


class _Host:
    def __init__(self, project_paths: list[str] | None = None) -> None:
        self.project_paths = project_paths or []
        self.GITLAB_API_BASE = "legacy-placeholder"
        self.HANDOFF_COORDINATION_CHANNEL = "legacy-placeholder"

    def _load_gitlab_routing_settings(self):
        projects = {}
        if self.project_paths:
            projects["project"] = SimpleNamespace(project_paths=self.project_paths)
        return SimpleNamespace(projects=projects)

    @staticmethod
    def _support_servicedesk_sweep_interval() -> float:
        return 300.0


class DeploymentConfigurationTests(unittest.TestCase):
    def test_fresh_install_is_generic_and_optional_features_are_disabled(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            host = _Host()
            app = FastAPI()

            config = install_deployment_configuration(app, host)

            self.assertFalse(config.legacy_compatibility)
            self.assertEqual(host._gitlab_api_base_url(), GENERIC_GITLAB_BASE_URL)
            self.assertEqual(host.GITLAB_API_BASE, f"{GENERIC_GITLAB_BASE_URL}/api/v4")
            self.assertEqual(host._support_servicedesk_project_paths(), [])
            self.assertEqual(host._support_servicedesk_sweep_project(), "")
            self.assertEqual(host._support_servicedesk_sweep_interval(), 0.0)
            self.assertIsNone(host.HANDOFF_COORDINATION_CHANNEL)

    def test_legacy_routing_preserves_historical_implicit_defaults(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            host = _Host(["veridataops/product"])
            app = FastAPI()

            config = install_deployment_configuration(app, host)

            self.assertTrue(config.legacy_compatibility)
            self.assertEqual(host._gitlab_api_base_url(), LEGACY_GITLAB_BASE_URL)
            self.assertEqual(host._support_servicedesk_project_paths(), [LEGACY_SUPPORT_PROJECT])
            self.assertEqual(host._support_servicedesk_sweep_project(), LEGACY_SUPPORT_PROJECT)
            self.assertEqual(host._support_servicedesk_sweep_interval(), 300.0)
            self.assertEqual(host.HANDOFF_COORDINATION_CHANNEL, LEGACY_HANDOFF_CHANNEL)

    def test_explicit_configuration_wins_on_fresh_install(self) -> None:
        env = {
            "CODEX_WEB_GITLAB_BASE_URL": "https://gitlab.example/internal/",
            "CODEX_WEB_GITLAB_API_BASE": "https://api.gitlab.example/v4/",
            "CODEX_WEB_SUPPORT_SERVICEDESK_PROJECT_PATHS": "group/support, group/helpdesk",
            "CODEX_WEB_SUPPORT_SERVICEDESK_PROJECT_ID": "1234",
            "CODEX_WEB_HANDOFF_COORDINATION_CHANNEL": "C123",
        }
        with patch.dict(os.environ, env, clear=True):
            host = _Host()
            app = FastAPI()

            install_deployment_configuration(app, host)

            self.assertEqual(host._gitlab_api_base_url(), "https://gitlab.example/internal")
            self.assertEqual(host.GITLAB_API_BASE, "https://api.gitlab.example/v4")
            self.assertEqual(
                host._support_servicedesk_project_paths(),
                ["group/support", "group/helpdesk"],
            )
            self.assertEqual(host._support_servicedesk_sweep_project(), "1234")
            self.assertEqual(host._support_servicedesk_sweep_interval(), 300.0)
            self.assertEqual(host.HANDOFF_COORDINATION_CHANNEL, "C123")

    def test_runtime_environment_paths_remain_dynamic(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            host = _Host()
            app = FastAPI()
            install_deployment_configuration(app, host)

            os.environ["CODEX_WEB_SUPPORT_SERVICEDESK_PROJECT_PATH"] = "new/support"
            try:
                self.assertEqual(host._support_servicedesk_project_paths(), ["new/support"])
                self.assertEqual(host._support_servicedesk_sweep_project(), "new/support")
                self.assertEqual(host._support_servicedesk_sweep_interval(), 300.0)
            finally:
                os.environ.pop("CODEX_WEB_SUPPORT_SERVICEDESK_PROJECT_PATH", None)


    def test_recovery_compose_uses_immutable_image_and_no_python_bind_mounts(self) -> None:
        root = Path(__file__).resolve().parents[1]
        base = (root / "compose.yaml").read_text(encoding="utf-8")
        recovery = (root / "compose.recovery.yaml").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            "CODEX_WEB_IMAGE:?CODEX_WEB_IMAGE must reference the qualified immutable image",
            recovery,
        )
        self.assertIn("deploy/recovery/nginx.conf", recovery)
        self.assertNotIn(".py:", base)
        self.assertNotIn(".py:", recovery)
        self.assertNotIn("/app/codex_web", recovery)

    def test_release_dockerfile_embeds_explicit_build_identity(self) -> None:
        root = Path(__file__).resolve().parents[1]
        dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn("ARG CODEX_WEB_RELEASE_VERSION=dev", dockerfile)
        self.assertIn("ARG CODEX_WEB_GIT_REVISION=unknown", dockerfile)
        self.assertIn("CODEX_WEB_RELEASE_VERSION=", dockerfile)
        self.assertIn("CODEX_WEB_GIT_REVISION=", dockerfile)



if __name__ == "__main__":
    unittest.main()
