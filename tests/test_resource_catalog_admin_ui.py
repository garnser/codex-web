from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ResourceCatalogAdminUiTests(unittest.TestCase):
    def test_resource_catalog_browser_uses_canonical_resource_apis(self) -> None:
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "static" / "resource_catalog_admin.js").read_text(encoding="utf-8")

        self.assertIn('id="resource-catalog-list"', html)
        self.assertIn('id="resource-type-filter"', html)
        self.assertIn('id="resource-lifecycle-filter"', html)
        self.assertIn('apiRequest("/api/resources")', javascript)
        self.assertIn("owner_identity_id", javascript)
        self.assertIn("item.risk", javascript)
        self.assertIn("item.sensitivity", javascript)
        self.assertIn("item.aliases", javascript)
        self.assertIn("item.provenance", javascript)
        self.assertIn(
            '/api/resources/${encodeURIComponent(resourceId)}/relationships?direction=both',
            javascript,
        )
        self.assertIn("relationship.relationship_type", javascript)


if __name__ == "__main__":
    unittest.main()
