from __future__ import annotations

import os
import stat
import unittest
from pathlib import Path


class RecoveryReleaseConfigurationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]

    def test_release_version_advances_beyond_v010_recovery(self) -> None:
        self.assertEqual(
            (self.root / ".release-version").read_text(
                encoding="utf-8"
            ).strip(),
            "0.2.0",
        )

    def test_release_workflow_is_two_phase_and_attested(self) -> None:
        workflow = (
            self.root / ".github/workflows/publish-release.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("immutable-recovery-candidate", workflow)
        self.assertIn("qualified-release-promotion", workflow)
        self.assertIn("--sbom=true", workflow)
        self.assertIn("--provenance=mode=max", workflow)
        self.assertIn("recovery-release", workflow)
        self.assertIn("slack_message_ts", workflow)
        self.assertIn("slack_thread_ts", workflow)
        self.assertIn("gitlab_delivery_id", workflow)
        self.assertIn("stale_turns_accounted", workflow)
        self.assertIn("CODEX_WEB_GIT_REVISION", workflow)
        self.assertIn("release-evidence.json", workflow)

    def test_recovery_overlay_and_nginx_are_repository_controlled(self) -> None:
        compose = (self.root / "compose.recovery.yaml").read_text(
            encoding="utf-8"
        )
        nginx = (
            self.root / "deploy/recovery/nginx.conf"
        ).read_text(encoding="utf-8")

        self.assertIn("CODEX_WEB_IMAGE", compose)
        self.assertIn("deploy/recovery/nginx.conf", compose)
        self.assertIn("proxy_pass http://codex_web", nginx)
        self.assertIn("proxy_set_header Upgrade", nginx)
        self.assertNotIn(".py:", compose)

    def test_recovery_qualification_script_is_executable(self) -> None:
        path = self.root / "scripts/qualify-recovery-image.sh"
        self.assertTrue(path.exists())
        self.assertTrue(
            path.stat().st_mode & stat.S_IXUSR,
            f"{path} must be executable",
        )

    def test_recovery_runbook_forbids_python_compatibility_mounts(self) -> None:
        runbook = (
            self.root
            / "docs/operations/recovery-release-v0.2.0.md"
        ).read_text(encoding="utf-8")

        self.assertIn("v0.1.0", runbook)
        self.assertIn("v0.2.0", runbook)
        self.assertIn("real Slack", runbook)
        self.assertIn("GitLab webhook", runbook)
        self.assertIn("Do not copy the former patched Python files", runbook)


if __name__ == "__main__":
    unittest.main()
