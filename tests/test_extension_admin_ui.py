from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ExtensionAdminUiTests(unittest.TestCase):
    def test_extension_admin_surface_uses_canonical_apis(self) -> None:
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "static" / "extension_admin.js").read_text(encoding="utf-8")

        self.assertIn('id="extension-admin-list"', html)
        self.assertIn('id="refresh-extensions"', html)
        self.assertIn('apiRequest("/api/extensions")', javascript)
        self.assertIn('apiRequest("/api/identity/me")', javascript)
        self.assertIn("extension_authority.js", javascript)
        self.assertIn("canMutateExtensions", javascript)
        self.assertIn("extensionMutationAuthorityText", javascript)
        self.assertIn("MFA/step-up or extensions:admin service authority required", javascript)
        self.assertIn('apiRequest("/api/identity/me")', javascript)
        self.assertIn("extension_authority.js", javascript)
        self.assertIn("canMutateExtensions", javascript)
        self.assertIn("extensionMutationAuthorityText", javascript)
        self.assertIn("package_verification", javascript)
        self.assertIn("configuration_record_ids", javascript)
        self.assertIn("secret_bindings", javascript)

        self.assertIn('data-extension-action="', javascript)
        self.assertIn('["installed", "configured", "disabled"]', javascript)
        self.assertIn('["disable", "quarantine", "clear-quarantine", "remove"]', javascript)
        self.assertIn("window.confirm", javascript)
        self.assertIn("window.prompt", javascript)
        self.assertIn('/api/extensions/${encodeURIComponent(installationId)}/${action}', javascript)

        self.assertIn('apiRequest("/api/resources")', javascript)
        self.assertIn('/api/extensions/${encodeURIComponent(item.id)}/grants', javascript)
        self.assertIn('data-extension-grant-action="grant"', javascript)
        self.assertIn('data-extension-grant-action="revoke"', javascript)
        self.assertIn("resource_ids", javascript)
        self.assertIn("mandatory capability", javascript)
        self.assertIn("data-extension-config-host", javascript)
        self.assertIn("codex:extension-state-rendered", javascript)
        self.assertIn("preserve_tombstone", javascript)
        self.assertIn("hard deletion is not supported", javascript)
        self.assertIn("data-extension-details-host", javascript)
        self.assertIn("MFA/step-up or extensions:admin service authority required", javascript)
        self.assertIn("canMutateMutation", javascript)


if __name__ == "__main__":
    unittest.main()
