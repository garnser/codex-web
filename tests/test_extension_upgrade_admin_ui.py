from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ExtensionUpgradeAdminUiTests(unittest.TestCase):
    def test_upgrade_surface_uses_server_verified_package_and_evidence(self) -> None:
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "static" / "extension_upgrade_admin.js").read_text(encoding="utf-8")

        self.assertIn("static/extension_upgrade_admin.js", html)
        self.assertIn('apiRequest("/api/evidence?include_inactive=false")', javascript)
        self.assertIn("policy_evaluation", javascript)
        self.assertIn("artifact_verification", javascript)
        self.assertIn("test_result", javascript)
        self.assertIn("ci_check", javascript)
        self.assertIn("metadata?.extension_id", javascript)
        self.assertIn("metadata?.to_version", javascript)
        self.assertIn(
            '/api/extensions/${encodeURIComponent(installationId)}/packages/${encodeURIComponent(packageRef)}/upgrade',
            javascript,
        )
        self.assertIn("migration_evidence_id", javascript)
        self.assertIn("server-verified package", javascript)
        self.assertIn("window.confirm", javascript)


if __name__ == "__main__":
    unittest.main()
