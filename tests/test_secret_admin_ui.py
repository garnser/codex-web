from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SecretAdminUiTests(unittest.TestCase):
    def test_secret_admin_uses_metadata_only_broker_surfaces(self) -> None:
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "static" / "secret_admin.js").read_text(encoding="utf-8")

        self.assertIn('id="secret-admin-list"', html)
        self.assertIn('id="secret-audit-list"', html)
        self.assertIn('type="password"', html)
        self.assertIn("Use permission identities", html)
        self.assertIn("Reveal permission identities", html)
        self.assertIn('apiRequest("/api/secrets")', javascript)
        self.assertIn('apiRequest("/api/secrets/audit")', javascript)
        self.assertIn("allowed_identity_ids", javascript)
        self.assertIn("reveal_identity_ids", javascript)
        self.assertIn("item.owner_identity_id", javascript)
        self.assertIn("item.rotation", javascript)
        self.assertIn(
            '/api/secrets/${encodeURIComponent(secretId)}/rotate',
            javascript,
        )
        self.assertIn(
            '/api/secrets/${encodeURIComponent(secretId)}',
            javascript,
        )
        self.assertIn('method: "DELETE"', javascript)
        self.assertIn("valueInput.value = \"\"", javascript)
        self.assertIn("never rendered back", javascript)
        self.assertIn("MAX_AUDIT_ROWS = 100", javascript)
        self.assertNotIn("localStorage", javascript)
        self.assertNotIn("console.log", javascript)


if __name__ == "__main__":
    unittest.main()
