from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DefinitionRegistryManagementUiTests(unittest.TestCase):
    def test_definition_lifecycle_uses_secured_canonical_apis_and_optimistic_revision(self) -> None:
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "static" / "definition_registry_management.js").read_text(
            encoding="utf-8"
        )

        self.assertIn('id="definition-lifecycle-panel"', html)
        self.assertIn('id="definition-draft-payload"', html)
        self.assertIn('apiRequest("/api/identity/me")', javascript)
        self.assertIn('apiRequest("/api/definitions/drafts"', javascript)
        self.assertIn("/validate", javascript)
        self.assertIn("/publish", javascript)
        self.assertIn("/quarantine", javascript)
        self.assertIn('apiRequest("/api/definitions/rollback"', javascript)
        self.assertIn("expected_active_revision", javascript)
        self.assertIn("approval_metadata", javascript)
        self.assertIn("/publication-preflight", javascript)
        self.assertIn("/publication-approvals", javascript)
        self.assertIn("publication_approval_id", javascript)
        self.assertIn("preflightSummary", javascript)
        self.assertIn("sensitive authority/definition expansion requires approval", javascript)
        self.assertIn("canApprove(scopeType)", javascript)
        self.assertIn("definitions:approve", javascript)
        self.assertIn("definitions:global-approve", javascript)
        self.assertIn("cannot replace required canonical approval evidence", javascript)
        self.assertIn("activeFor(record)", javascript)
        self.assertIn("usage impact could not be loaded", javascript)
        self.assertIn("creates a new immutable revision", javascript)
        self.assertIn("code-owned schema", javascript)
        self.assertIn("code-owned security invariants", javascript)
        self.assertIn("definitions:admin", javascript)
        self.assertIn("definitions:global-admin", javascript)
        self.assertIn('actor.assurance === "local_trusted"', javascript)
        self.assertIn("JSON.parse(rawPayload)", javascript)
        self.assertIn("window.confirm", javascript)
        self.assertIn("window.prompt", javascript)
        self.assertIn('id="definition-transfer-panel"', html)
        self.assertIn('id="definition-transfer-document"', html)
        self.assertIn('apiRequest("/api/definitions/export")', javascript)
        self.assertIn('apiRequest("/api/definitions/import"', javascript)
        self.assertIn("canManage(record.scope_type)", javascript)
        self.assertIn("nothing was submitted", javascript)
        self.assertIn("inactive draft revisions", javascript)
        self.assertIn("none are published automatically", javascript)
        self.assertNotIn("actor: actor.identity_id", javascript)
        self.assertNotIn("localStorage", javascript)


if __name__ == "__main__":
    unittest.main()
