from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ActionProviderAdminUiTests(unittest.TestCase):
    def test_action_provider_browser_exposes_contracts_without_preparing_or_executing(self) -> None:
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "static" / "action_provider_admin.js").read_text(encoding="utf-8")

        self.assertIn('id="action-provider-list"', html)
        self.assertIn('apiRequest("/api/action-providers")', javascript)
        self.assertIn('apiRequest("/api/resources")', javascript)
        self.assertIn('apiRequest("/api/projects")', javascript)
        self.assertIn("binding.credential_ref", javascript)
        self.assertIn("required_authority", javascript)
        self.assertIn("required_resource_types", javascript)
        self.assertIn("credential_purpose", javascript)
        self.assertIn("expected_evidence", javascript)
        self.assertIn("capabilities?.verification", javascript)
        self.assertIn("capabilities?.rollback", javascript)
        self.assertIn("retry_max_attempts", javascript)
        self.assertIn("binding.security_policy", javascript)
        self.assertIn("allowed_hosts", javascript)
        self.assertIn("allowed_read_roots", javascript)
        self.assertIn("allowed_executables", javascript)
        self.assertIn("does not prepare or execute actions", javascript)
        self.assertNotIn("/prepare", javascript)
        self.assertNotIn("/execute", javascript)


if __name__ == "__main__":
    unittest.main()
