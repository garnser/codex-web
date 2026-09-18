from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ResourceCatalogManagementUiTests(unittest.TestCase):
    def test_resource_management_uses_canonical_resource_and_identity_apis(self) -> None:
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "static" / "resource_catalog_management.js").read_text(encoding="utf-8")

        self.assertIn('id="resource-create-panel"', html)
        self.assertIn('id="resource-relationship-panel"', html)
        self.assertIn('apiRequest("/api/identity")', javascript)
        self.assertIn('apiRequest("/api/identity/me")', javascript)
        self.assertIn('apiRequest("/api/resources"', javascript)
        self.assertIn('method: "POST"', javascript)
        self.assertIn('method: "PATCH"', javascript)
        self.assertIn('apiRequest("/api/resources/relationships"', javascript)
        self.assertIn("owner_identity_id", javascript)
        self.assertIn("membership.organization_id === actor.organization_id", javascript)
        self.assertIn("membership.workspace_id === actor.workspace_id", javascript)
        self.assertIn("unavailable for privileged resolution", javascript)
        self.assertIn("window.confirm", javascript)


if __name__ == "__main__":
    unittest.main()
