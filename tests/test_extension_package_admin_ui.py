from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ExtensionPackageAdminUiTests(unittest.TestCase):
    def test_package_install_surface_uses_server_observed_catalog(self) -> None:
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "static" / "extension_packages_admin.js").read_text(encoding="utf-8")

        self.assertIn('id="extension-package-status"', html)
        self.assertIn('id="extension-package-list"', html)
        self.assertIn('apiRequest("/api/extensions/packages")', javascript)
        self.assertIn('apiRequest("/api/extensions")', javascript)
        self.assertIn('apiRequest("/api/identity/me")', javascript)
        self.assertIn("extension_authority.js", javascript)
        self.assertIn("canMutateExtensions", javascript)
        self.assertIn("MFA/step-up or extensions:admin service authority required", javascript)
        self.assertIn("button.disabled", javascript)
        self.assertIn('apiRequest("/api/identity/me")', javascript)
        self.assertIn("extension_authority.js", javascript)
        self.assertIn("canMutateExtensions", javascript)
        self.assertIn("verification.signature_status", javascript)
        self.assertIn("verification.digest_verified", javascript)
        self.assertIn("Installation does not authorize capabilities", javascript)
        self.assertIn('deployment_mode: "self_hosted"', javascript)
        self.assertIn(
            '/api/extensions/packages/${encodeURIComponent(packageRef)}/install',
            javascript,
        )
        self.assertIn("window.confirm", javascript)
        self.assertIn("data-extension-upgrade-host", javascript)
        self.assertIn("codex:extension-packages-rendered", javascript)
        self.assertIn("canMutateMutation", javascript)
        self.assertIn("MFA/step-up or extensions:admin service authority required", javascript)


if __name__ == "__main__":
    unittest.main()
