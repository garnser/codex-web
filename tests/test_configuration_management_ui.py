from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ConfigurationManagementUiTests(unittest.TestCase):
    def test_configuration_management_is_schema_driven_and_reference_only(self) -> None:
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "static" / "configuration_management.js").read_text(
            encoding="utf-8"
        )

        javascript += (ROOT / "static" / "configuration_lifecycle_ui.js").read_text(encoding="utf-8")
        value_editor = (ROOT / "static" / "configuration_value_editor.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("configuration_value_editor.js", javascript)

        self.assertIn('id="configuration-management-panel"', html)
        self.assertIn('id="configuration-draft-value-host"', html)
        self.assertIn('id="configuration-targeting-panel"', html)
        self.assertIn('id="configuration-force-disabled"', html)
        self.assertIn('operation.request("/api/identity/me")', javascript)
        self.assertIn('operation.request("/api/secrets")', javascript)
        self.assertIn('/api/definitions/records?project_id=', javascript)
        self.assertIn('operation.request("/api/configuration/drafts"', javascript)
        self.assertIn("/validate", javascript)
        self.assertIn("/publish", javascript)
        self.assertIn('request("/api/configuration/rollback"', javascript)
        self.assertIn('request("/api/configuration/reset"', javascript)
        self.assertIn("Revert to inherited/default", javascript)
        self.assertIn("disabled tombstone", javascript)
        self.assertIn("expected_active_revision", javascript)
        self.assertIn("allowed_scopes", javascript)
        self.assertIn("spec.editable", javascript)
        self.assertIn("spec.allowed_values", value_editor)
        self.assertIn("spec.minimum", value_editor)
        self.assertIn("spec.maximum", value_editor)
        self.assertIn("secret_ref", value_editor)
        self.assertIn("SecretBroker reference", value_editor)
        self.assertIn("Raw secret values are never configuration", value_editor)
        self.assertIn("definition_ref", value_editor)
        self.assertIn("Published Definition Registry", value_editor)
        self.assertIn("feature_flag", javascript)
        self.assertIn("kill_switch_capable", javascript)
        self.assertIn("force_disabled", javascript)
        self.assertIn("more-specific published override", javascript)
        self.assertIn("startup-only", javascript)
        self.assertIn("cannot grant authority", javascript)
        self.assertIn("configuration:admin", javascript)
        self.assertIn("configuration:global-admin", javascript)
        self.assertIn('actor.assurance === "local_trusted"', javascript)
        self.assertIn("creates and publishes a new immutable revision", javascript)
        self.assertNotIn("actor: actor.identity_id", javascript)
        self.assertNotIn("localStorage", javascript)
        self.assertNotIn("fetch(", javascript)


if __name__ == "__main__":
    unittest.main()
