from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ExtensionConfigurationAdminUiTests(unittest.TestCase):
    def test_configuration_surface_uses_reference_only_canonical_apis(self) -> None:
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "static" / "extension_configuration_admin.js").read_text(encoding="utf-8")

        self.assertIn("static/extension_configuration_admin.js", html)
        self.assertIn('apiRequest("/api/secrets")', javascript)
        self.assertIn('apiRequest("/api/configuration/records")', javascript)
        self.assertIn("configuration_record_ids", javascript)
        self.assertIn("secret_bindings", javascript)
        self.assertIn("canMutateMutation", javascript)
        self.assertIn("MFA/local-trusted assurance or extensions:admin service authority", javascript)
        self.assertIn("button.disabled", javascript)
        self.assertIn(
            '/api/extensions/${encodeURIComponent(installationId)}/configuration',
            javascript,
        )
        self.assertIn('method: "PUT"', javascript)
        self.assertIn("does not grant authority or reveal secret material", javascript)
        self.assertIn("canMutateMutation", javascript)
        self.assertIn("requires tenant admin/owner plus MFA/local-trusted assurance", javascript)
        self.assertNotIn("secret.value", javascript)
        self.assertNotIn("/reveal", javascript)


if __name__ == "__main__":
    unittest.main()
