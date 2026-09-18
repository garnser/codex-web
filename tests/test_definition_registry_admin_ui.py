from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DefinitionRegistryAdminUiTests(unittest.TestCase):
    def test_definition_registry_browser_uses_canonical_read_apis(self) -> None:
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "static" / "definition_registry_admin.js").read_text(
            encoding="utf-8"
        )

        self.assertIn('id="definition-registry-list"', html)
        self.assertIn('id="definition-bootstrap"', html)
        self.assertIn('id="definition-resolve-result"', html)
        self.assertIn('id="definition-diff-result"', html)
        self.assertIn('apiRequest("/api/definitions/schemas")', javascript)
        self.assertIn('apiRequest("/api/definitions/bootstrap")', javascript)
        self.assertIn('apiRequest("/api/definitions/records")', javascript)
        self.assertIn('apiRequest("/api/projects")', javascript)
        self.assertIn('apiRequest("/api/definitions/resolve"', javascript)
        self.assertIn("/api/definitions/diff?left=", javascript)
        self.assertIn("/usage", javascript)
        self.assertIn("approval_metadata", javascript)
        self.assertIn("supersedes_record_id", javascript)
        self.assertIn("rollback_of_record_id", javascript)
        self.assertIn("definition_schema_version", javascript)
        self.assertIn("min_engine_version", javascript)
        self.assertIn("max_engine_version", javascript)
        self.assertIn("record.payload", javascript)
        self.assertIn("changed_paths", javascript)
        self.assertIn("codex:definition-registry-rendered", javascript)
        self.assertIn("schema/interpreter/security engines remain code-owned", javascript)
        self.assertNotIn("/publish", javascript)
        self.assertNotIn("/rollback", javascript)
        self.assertNotIn("/quarantine", javascript)
        self.assertNotIn("/drafts", javascript)


if __name__ == "__main__":
    unittest.main()
