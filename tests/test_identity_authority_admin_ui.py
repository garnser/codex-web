from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class IdentityAuthorityAdminUiTests(unittest.TestCase):
    def test_sensitive_identity_mutations_use_canonical_apis(self) -> None:
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "static" / "identity_authority_admin.js").read_text(encoding="utf-8")

        self.assertIn('id="identity-authority-panel"', html)
        self.assertIn('id="identity-created-token-result"', html)
        self.assertIn("admin authority and MFA/step-up assurance", javascript)
        self.assertIn('"/api/identity/organizations"', javascript)
        self.assertIn('"/api/identity/workspaces"', javascript)
        self.assertIn('"/api/identity/humans"', javascript)
        self.assertIn('"/api/identity/services"', javascript)
        self.assertIn('"/api/identity/memberships"', javascript)
        self.assertIn('"/api/identity/service-tokens"', javascript)
        self.assertIn('method: "POST"', javascript)
        self.assertIn("principal_kind", javascript)
        self.assertIn("organization_id", javascript)
        self.assertIn("workspace_id", javascript)
        self.assertIn("team_ids", javascript)
        self.assertIn("credentials.token", javascript)
        self.assertIn("token.textContent = credentials.token", javascript)
        self.assertIn("token.textContent = \"\"", javascript)
        self.assertIn("will not appear in list APIs", javascript)
        self.assertIn("window.confirm", javascript)
        self.assertNotIn("localStorage", javascript)
        self.assertNotIn("console.log", javascript)


if __name__ == "__main__":
    unittest.main()
