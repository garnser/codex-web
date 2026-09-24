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

        self.assertIn('id="configuration-management-panel"', html)
        self.assertIn('id="configuration-draft-value-host"', html)
        self.assertIn('id="configuration-targeting-panel"', html)
        self.assertIn('id="configuration-force-disabled"', html)
        self.assertIn('apiRequest("/api/identity/me")', javascript)
        self.assertIn('apiRequest("/api/secrets")', javascript)
        self.assertIn('apiRequest("/api/definitions/records")', javascript)
        self.assertIn('apiRequest("/api/configuration/drafts"', javascript)
        self.assertIn("/validate", javascript)
        self.assertIn("/publish", javascript)
        self.assertIn('apiRequest("/api/configuration/rollback"', javascript)
        self.assertIn('apiRequest("/api/configuration/reset"', javascript)
        self.assertIn("Revert to inherited/default", javascript)
        self.assertIn("disabled tombstone", javascript)
        self.assertIn("expected_active_revision", javascript)
        self.assertIn("allowed_scopes", javascript)
        self.assertIn("secret_ref", javascript)
        self.assertIn("SecretBroker reference", javascript)
        self.assertIn("Raw secret values are never configuration", javascript)
        self.assertIn("definition_ref", javascript)
        self.assertIn("Published Definition Registry", javascript)
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
