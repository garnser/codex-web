from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ExtensionAdminUiTests(unittest.TestCase):
    def test_extension_admin_surface_uses_canonical_apis(self) -> None:
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

        self.assertIn('id="extension-admin-list"', html)
        self.assertIn('id="refresh-extensions"', html)
        self.assertIn('api("/api/extensions")', javascript)
        self.assertIn('api("/api/extensions/packages")', javascript)
        self.assertIn("package_verification", javascript)
        self.assertIn("configuration_record_ids", javascript)
        self.assertIn("secret_bindings", javascript)

        self.assertIn('data-extension-action="', javascript)
        self.assertIn('["installed", "configured", "disabled"]', javascript)
        self.assertIn('["disable", "quarantine", "clear-quarantine"]', javascript)
        self.assertIn("window.confirm", javascript)
        self.assertIn("window.prompt", javascript)
        self.assertIn('/api/extensions/${encodeURIComponent(installationId)}/${action}', javascript)


if __name__ == "__main__":
    unittest.main()
