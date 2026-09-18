from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"


class FrontendBoundaryTests(unittest.TestCase):
    def test_legacy_app_bundle_cannot_grow(self) -> None:
        # New UI behavior belongs in focused modules. Keep a small allowance for
        # corrective edits, but force substantive feature work out of app.js.
        self.assertLessEqual((STATIC / "app.js").stat().st_size, 90_000)

    def test_executive_ui_has_its_own_budget(self) -> None:
        self.assertLessEqual((STATIC / "executive-ui.js").stat().st_size, 22_000)

    def test_extracted_context_module_uses_shared_api_client(self) -> None:
        source = (STATIC / "context_compaction.js").read_text()
        self.assertIn("api_client.js", source)
        self.assertIn("apiRequest", source)
        self.assertNotIn("await fetch(", source)

    def test_control_plane_ui_uses_shared_api_client(self) -> None:
        source = (STATIC / "control_plane_ui.js").read_text()
        self.assertIn("api_client.js", source)
        self.assertIn("apiRequest", source)
        self.assertNotIn("fetch(", source)

    def test_extension_admin_has_its_own_budget_and_api_client(self) -> None:
        source_path = STATIC / "extension_admin.js"
        source = source_path.read_text()
        self.assertLessEqual(source_path.stat().st_size, 16_000)
        self.assertIn("api_client.js", source)
        self.assertIn("apiRequest", source)
        self.assertNotIn("fetch(", source)

    def test_extension_package_admin_has_its_own_budget_and_api_client(self) -> None:
        source_path = STATIC / "extension_packages_admin.js"
        source = source_path.read_text()
        self.assertLessEqual(source_path.stat().st_size, 8_000)
        self.assertIn("api_client.js", source)
        self.assertIn("apiRequest", source)
        self.assertNotIn("fetch(", source)

    def test_extension_configuration_admin_has_its_own_budget_and_api_client(self) -> None:
        source_path = STATIC / "extension_configuration_admin.js"
        source = source_path.read_text()
        self.assertLessEqual(source_path.stat().st_size, 10_000)
        self.assertIn("api_client.js", source)
        self.assertIn("apiRequest", source)
        self.assertNotIn("fetch(", source)

    def test_extension_upgrade_admin_has_its_own_budget_and_api_client(self) -> None:
        source_path = STATIC / "extension_upgrade_admin.js"
        source = source_path.read_text()
        self.assertLessEqual(source_path.stat().st_size, 8_000)
        self.assertIn("api_client.js", source)
        self.assertIn("apiRequest", source)
        self.assertNotIn("fetch(", source)

    def test_extension_observability_admin_has_its_own_budget_and_api_client(self) -> None:
        source_path = STATIC / "extension_observability_admin.js"
        source = source_path.read_text()
        self.assertLessEqual(source_path.stat().st_size, 6_000)
        self.assertIn("api_client.js", source)
        self.assertIn("apiRequest", source)
        self.assertNotIn("fetch(", source)

    def test_resource_catalog_admin_has_its_own_budget_and_api_client(self) -> None:
        source_path = STATIC / "resource_catalog_admin.js"
        source = source_path.read_text()
        self.assertLessEqual(source_path.stat().st_size, 9_000)
        self.assertIn("api_client.js", source)
        self.assertIn("apiRequest", source)
        self.assertNotIn("fetch(", source)

    def test_resource_catalog_management_has_its_own_budget_and_api_client(self) -> None:
        source_path = STATIC / "resource_catalog_management.js"
        source = source_path.read_text()
        self.assertLessEqual(source_path.stat().st_size, 12_000)
        self.assertIn("api_client.js", source)
        self.assertIn("apiRequest", source)
        self.assertNotIn("fetch(", source)

    def test_identity_admin_has_its_own_budget_and_api_client(self) -> None:
        source_path = STATIC / "identity_admin.js"
        source = source_path.read_text()
        self.assertLessEqual(source_path.stat().st_size, 14_000)
        self.assertIn("api_client.js", source)
        self.assertIn("apiRequest", source)
        self.assertNotIn("fetch(", source)

    def test_identity_authority_admin_has_its_own_budget_and_api_client(self) -> None:
        source_path = STATIC / "identity_authority_admin.js"
        source = source_path.read_text()
        self.assertLessEqual(source_path.stat().st_size, 15_000)
        self.assertIn("api_client.js", source)
        self.assertIn("apiRequest", source)
        self.assertNotIn("fetch(", source)

    def test_secret_admin_has_its_own_budget_and_api_client(self) -> None:
        source_path = STATIC / "secret_admin.js"
        source = source_path.read_text()
        self.assertLessEqual(source_path.stat().st_size, 13_000)
        self.assertIn("api_client.js", source)
        self.assertIn("apiRequest", source)
        self.assertNotIn("fetch(", source)

    def test_crypto_key_admin_has_its_own_budget_and_api_client(self) -> None:
        source_path = STATIC / "crypto_key_admin.js"
        source = source_path.read_text()
        self.assertLessEqual(source_path.stat().st_size, 14_000)
        self.assertIn("api_client.js", source)
        self.assertIn("apiRequest", source)
        self.assertNotIn("fetch(", source)

    def test_model_gateway_admin_has_its_own_budget_and_api_client(self) -> None:
        source_path = STATIC / "model_gateway_admin.js"
        source = source_path.read_text()
        self.assertLessEqual(source_path.stat().st_size, 16_000)
        self.assertIn("api_client.js", source)
        self.assertIn("apiRequest", source)
        self.assertNotIn("fetch(", source)

    def test_model_gateway_management_has_its_own_budget_and_api_client(self) -> None:
        source_path = STATIC / "model_gateway_management.js"
        source = source_path.read_text()
        self.assertLessEqual(source_path.stat().st_size, 22_000)
        self.assertIn("api_client.js", source)
        self.assertIn("apiRequest", source)
        self.assertNotIn("fetch(", source)

    def test_action_provider_admin_has_its_own_budget_and_api_client(self) -> None:
        source_path = STATIC / "action_provider_admin.js"
        source = source_path.read_text()
        self.assertLessEqual(source_path.stat().st_size, 9_000)
        self.assertIn("api_client.js", source)
        self.assertIn("apiRequest", source)
        self.assertNotIn("fetch(", source)

    def test_entitlement_admin_has_its_own_budget_and_api_client(self) -> None:
        source_path = STATIC / "entitlement_admin.js"
        source = source_path.read_text()
        self.assertLessEqual(source_path.stat().st_size, 12_000)
        self.assertIn("api_client.js", source)
        self.assertIn("apiRequest", source)
        self.assertNotIn("fetch(", source)

    def test_shared_api_client_preserves_structured_http_errors(self) -> None:
        source = (STATIC / "api_client.js").read_text()
        self.assertIn("class CodexApiError", source)
        self.assertIn("this.status", source)
        self.assertIn("this.detail", source)
        self.assertIn("this.path", source)


if __name__ == "__main__":
    unittest.main()
