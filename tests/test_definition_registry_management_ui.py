from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DefinitionRegistryManagementUiTests(unittest.TestCase):
    def test_definition_management_uses_guarded_versioned_lifecycle_apis(self) -> None:
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "static" / "definition_registry_management.js").read_text(
            encoding="utf-8"
        )

        self.assertIn('id="definition-management-panel"', html)
        self.assertIn('id="definition-draft-payload"', html)
        self.assertIn('id="definition-transfer-document"', html)
        self.assertIn('apiRequest("/api/identity/me")', javascript)
        self.assertIn('apiRequest("/api/definitions/drafts"', javascript)
        self.assertIn("/validate", javascript)
        self.assertIn("/publish", javascript)
        self.assertIn("/quarantine", javascript)
        self.assertIn('apiRequest("/api/definitions/rollback"', javascript)
        self.assertIn('apiRequest("/api/definitions/export")', javascript)
        self.assertIn('apiRequest("/api/definitions/import"', javascript)
        self.assertIn("expected_active_revision", javascript)
        self.assertIn("approval_metadata", javascript)
        self.assertIn("activeFor(record)", javascript)
        self.assertIn("creates and publishes a new immutable revision", javascript)
        self.assertIn("runtime resolution can fail closed", javascript)
        self.assertIn("imported records are never activated automatically", javascript)
        self.assertIn("Definition payload must be a JSON object", javascript)
        self.assertIn("definitions:global-admin", javascript)
        self.assertIn("MFA/step-up", javascript)
        self.assertIn("window.confirm", javascript)
        self.assertNotIn('"actor":', javascript)
        self.assertNotIn("localStorage", javascript)
        self.assertNotIn("fetch(", javascript)


if __name__ == "__main__":
    unittest.main()
