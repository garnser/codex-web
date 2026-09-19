from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DefinitionRegistryManagementUiTests(unittest.TestCase):
    def test_definition_lifecycle_uses_secured_canonical_apis_and_optimistic_revision(self) -> None:
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        management = (ROOT / "static" / "definition_registry_management.js").read_text(
            encoding="utf-8"
        )
        approvals = (ROOT / "static" / "definition_registry_approvals.js").read_text(
            encoding="utf-8"
        )
        transfer = (ROOT / "static" / "definition_registry_transfer.js").read_text(
            encoding="utf-8"
        )
        javascript = "\n".join((management, approvals, transfer))

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
        self.assertIn("activeFor(record)", javascript)
        self.assertIn("usage impact could not be loaded", javascript)
        self.assertIn("creates a new immutable revision", javascript)
        self.assertIn("code-owned schema", javascript)
        self.assertIn("code-owned security invariants", javascript)
        self.assertIn("definitions:admin", javascript)
        self.assertIn("definitions:global-admin", javascript)
        self.assertIn("definition_registry_approvals.js", management)
        self.assertIn("definition_registry_transfer.js", management)
        self.assertIn("definitions:approve", javascript)
        self.assertIn("definitions:global-approve", javascript)
        self.assertIn("/publication-assessment", javascript)
        self.assertIn("/publication-approvals", javascript)
        self.assertIn("requires_independent_approval", javascript)
        self.assertIn("different identity", javascript)
        self.assertIn("definition_approval_required", javascript)
        self.assertIn("Rollback prepared draft", javascript)
        self.assertIn('actor.assurance === "local_trusted"', javascript)
        self.assertIn("JSON.parse(rawPayload)", javascript)
        self.assertIn("window.confirm", javascript)
        self.assertIn("window.prompt", javascript)
        self.assertIn('id="definition-transfer-panel"', html)
        self.assertIn('id="definition-transfer-document"', html)
        self.assertIn("/api/definitions/export", javascript)
        self.assertIn("/api/definitions/import", javascript)
        self.assertIn("canManage(record.scope_type)", javascript)
        self.assertIn("nothing was submitted", javascript)
        self.assertIn("inactive draft revisions", javascript)
        self.assertIn("none are published automatically", javascript)
        self.assertNotIn("actor: actor.identity_id", javascript)
        self.assertNotIn("localStorage", javascript)

    def test_typed_role_editor_creates_derived_drafts_only(self) -> None:
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        coordinator = (ROOT / "static" / "definition_typed_editor.js").read_text(
            encoding="utf-8"
        )
        authority = (
            ROOT / "static" / "definition_typed_authority_editor.js"
        ).read_text(encoding="utf-8")
        execution = (
            ROOT / "static" / "definition_typed_execution_editor.js"
        ).read_text(encoding="utf-8")
        shared = (
            ROOT / "static" / "definition_typed_editor_shared.js"
        ).read_text(encoding="utf-8")
        javascript = "\n".join((coordinator, authority, execution, shared))

        self.assertIn('id="definition-typed-editor-panel"', html)
        self.assertIn('id="definition-typed-source"', html)
        self.assertIn('id="definition-typed-save"', html)
        self.assertIn("authority-role-catalog", coordinator)
        self.assertIn("execution-role-catalog", coordinator)
        self.assertIn("/api/definitions/drafts", coordinator)
        self.assertIn("derived_from_record_id", coordinator)
        self.assertIn("nothing was activated", coordinator)
        self.assertIn("clone-role", javascript)
        self.assertIn("typed-role-lifecycle", javascript)
        self.assertIn("typed-grant-capability", authority)
        self.assertIn("typed-grant-level", authority)
        self.assertIn("typed-grant-environments", authority)
        self.assertIn("typed-binding-kind", authority)
        self.assertIn("typed-delegation-identity", authority)
        self.assertIn("typed-exec-shared-rules", execution)
        self.assertIn("typed-role-artifacts", execution)
        self.assertIn("typed-role-hands-to", execution)
        self.assertIn("STRUCTURAL_EXECUTION_ROLES", execution)
        self.assertNotIn("/publish", coordinator)
        self.assertNotIn("localStorage", javascript)


if __name__ == "__main__":
    unittest.main()
