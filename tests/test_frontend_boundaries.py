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

    def test_input_plugin_admin_has_its_own_budget_and_api_client(self) -> None:
        source_path = STATIC / "input_plugin_admin.js"
        source = source_path.read_text()
        self.assertLessEqual(source_path.stat().st_size, 17_000)
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

    def test_configuration_admin_has_its_own_budget_and_api_client(self) -> None:
        source_path = STATIC / "configuration_admin.js"
        source = source_path.read_text()
        self.assertLessEqual(source_path.stat().st_size, 16_000)
        self.assertIn("api_client.js", source)
        self.assertIn("apiRequest", source)
        self.assertNotIn("fetch(", source)

    def test_configuration_management_has_its_own_budget_and_api_client(self) -> None:
        source_path = STATIC / "configuration_management.js"
        source = source_path.read_text()
        self.assertLessEqual(source_path.stat().st_size, 20_000)
        self.assertIn("api_client.js", source)
        self.assertIn("apiRequest", source)
        self.assertNotIn("fetch(", source)

    def test_execution_worker_admin_has_its_own_budget_and_api_client(self) -> None:
        source_path = STATIC / "execution_worker_admin.js"
        source = source_path.read_text()
        self.assertLessEqual(source_path.stat().st_size, 13_000)
        self.assertIn("api_client.js", source)
        self.assertIn("apiRequest", source)
        self.assertNotIn("fetch(", source)

    def test_legacy_project_migration_admin_is_focused_and_uses_shared_client(self) -> None:
        source_path = STATIC / "legacy_project_migration_admin.js"
        source = source_path.read_text(encoding="utf-8")
        html = (STATIC / "index.html").read_text(encoding="utf-8")

        self.assertLessEqual(source_path.stat().st_size, 8_000)
        self.assertIn("api_client.js", source)
        self.assertIn("apiRequest", source)
        self.assertNotIn("fetch(", source)
        self.assertIn("approve_material_authority_changes", source)
        self.assertIn("authority_difference", source)
        self.assertIn("rollback_boundary", source)
        self.assertIn("legacy_project_migration_admin.js", html)
        self.assertIn('id="legacy-project-migration-panel"', html)

    def test_control_plane_broker_admin_has_focused_budget_and_no_secret_surface(self) -> None:
        source_path = STATIC / "control_plane_broker_admin.js"
        source = source_path.read_text(encoding="utf-8")
        html = (STATIC / "index.html").read_text(encoding="utf-8")

        self.assertLessEqual(source_path.stat().st_size, 7_000)
        self.assertIn("api_client.js", source)
        self.assertIn("apiRequest", source)
        self.assertNotIn("fetch(", source)
        self.assertIn("credential_exposed", source)
        self.assertNotIn("lease_token", source)
        self.assertNotIn("capability_token", source)
        self.assertIn("brokered_control_plane", source)
        self.assertIn("control_plane_broker_admin.js", html)

    def test_execution_worker_management_has_its_own_budget_and_api_client(self) -> None:
        source_path = STATIC / "execution_worker_management.js"
        source = source_path.read_text()
        self.assertLessEqual(source_path.stat().st_size, 11_000)
        self.assertIn("api_client.js", source)
        self.assertIn("apiRequest", source)
        self.assertNotIn("fetch(", source)

    def test_execution_workspace_admin_has_its_own_budget_and_api_client(self) -> None:
        source_path = STATIC / "execution_workspace_admin.js"
        source = source_path.read_text()
        self.assertLessEqual(source_path.stat().st_size, 17_000)
        self.assertIn("api_client.js", source)
        self.assertIn("apiRequest", source)
        self.assertNotIn("fetch(", source)

    def test_artifact_evidence_admin_has_its_own_budget_and_api_client(self) -> None:
        source_path = STATIC / "artifact_evidence_admin.js"
        source = source_path.read_text()
        self.assertLessEqual(source_path.stat().st_size, 20_000)
        self.assertIn("api_client.js", source)
        self.assertIn("apiRequest", source)
        self.assertNotIn("fetch(", source)

    def test_action_intent_admin_has_its_own_budget_and_api_client(self) -> None:
        source_path = STATIC / "action_intent_admin.js"
        source = source_path.read_text()
        self.assertLessEqual(source_path.stat().st_size, 17_000)
        self.assertIn("api_client.js", source)
        self.assertIn("apiRequest", source)
        self.assertNotIn("fetch(", source)

    def test_work_graph_admin_has_its_own_budget_and_api_client(self) -> None:
        source_path = STATIC / "work_graph_admin.js"
        source = source_path.read_text()
        self.assertLessEqual(source_path.stat().st_size, 23_000)
        self.assertIn("api_client.js", source)
        self.assertIn("apiRequest", source)
        self.assertNotIn("fetch(", source)

    def test_security_trust_diagnostics_has_its_own_budget_and_api_client(self) -> None:
        source_path = STATIC / "security_trust_diagnostics.js"
        source = source_path.read_text()
        self.assertLessEqual(source_path.stat().st_size, 14_000)
        self.assertIn("api_client.js", source)
        self.assertIn("apiRequest", source)
        self.assertNotIn("fetch(", source)

    def test_operations_observability_has_its_own_budget_and_api_client(self) -> None:
        source_path = STATIC / "operations_observability.js"
        source = source_path.read_text()
        self.assertLessEqual(source_path.stat().st_size, 16_000)
        self.assertIn("api_client.js", source)
        self.assertIn("apiRequest", source)
        self.assertNotIn("fetch(", source)

    def test_definition_registry_admin_has_its_own_budget_and_api_client(self) -> None:
        source_path = STATIC / "definition_registry_admin.js"
        source = source_path.read_text()
        self.assertLessEqual(source_path.stat().st_size, 17_000)
        self.assertIn("api_client.js", source)
        self.assertIn("apiRequest", source)
        self.assertNotIn("fetch(", source)

    def test_definition_registry_management_has_its_own_budget_and_api_client(self) -> None:
        source_path = STATIC / "definition_registry_management.js"
        source = source_path.read_text()
        self.assertLessEqual(source_path.stat().st_size, 18_000)
        self.assertIn("api_client.js", source)
        self.assertIn("apiRequest", source)
        self.assertNotIn("fetch(", source)

    def test_typed_definition_editor_modules_have_focused_budgets(self) -> None:
        budgets = {
            "definition_typed_editor.js": 12_000,
            "definition_typed_authority_editor.js": 13_000,
            "definition_typed_execution_editor.js": 6_000,
            "definition_typed_editor_shared.js": 4_000,
        }
        for name, limit in budgets.items():
            with self.subTest(name=name):
                source_path = STATIC / name
                self.assertLessEqual(source_path.stat().st_size, limit)
                self.assertNotIn("fetch(", source_path.read_text())
        coordinator = (STATIC / "definition_typed_editor.js").read_text()
        self.assertIn("api_client.js", coordinator)
        self.assertIn("apiRequest", coordinator)

    def test_danger_full_access_is_explicitly_warned_in_execution_controls(self) -> None:
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        css = (STATIC / "styles.css").read_text(encoding="utf-8")

        self.assertIn('value="danger-full-access">Full access (high risk)', html)
        self.assertIn('id="sandbox-danger-warning"', html)
        self.assertIn("disables the Codex inner sandbox", html)
        self.assertIn(
            '#sandbox:has(option[value="danger-full-access"]:checked) + .sandbox-danger-warning',
            css,
        )

    def test_execution_profile_controls_are_focused_and_explain_authority(self) -> None:
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        module_path = STATIC / "execution_profile_controls.js"
        module = module_path.read_text(encoding="utf-8")
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        work_items = (STATIC / "work_items_ui.js").read_text(encoding="utf-8")

        self.assertLessEqual(module_path.stat().st_size, 6_000)
        self.assertIn("api_client.js", module)
        self.assertIn("apiRequest", module)
        self.assertNotIn("fetch(", module)
        self.assertIn('id="execution-profile"', html)
        self.assertIn('id="execution-profile-summary"', html)
        self.assertIn("No mutable Git worktree is created", module)
        self.assertIn("execution_profile_id", app)
        self.assertIn("contract.execution_profile?.id", work_items)
        self.assertIn("required_worker_capabilities", work_items)

    def test_repository_target_controls_are_exposed_for_thread_bootstrap(self) -> None:
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        project_state = (STATIC / "project_ui_state.js").read_text(
            encoding="utf-8"
        )

        self.assertIn('id="repository-target"', html)
        self.assertIn('id="repository-read-context"', html)
        self.assertIn("repository_resource_id", app)
        self.assertIn("read_only_repository_resource_id", app)
        self.assertIn("/ui-state", project_state)
        self.assertNotIn("/api/bots/bindings", project_state)
        self.assertNotIn("/api/thread-settings", project_state)

    def test_project_ui_state_loader_has_focused_budget(self) -> None:
        source_path = STATIC / "project_ui_state.js"
        source = source_path.read_text(encoding="utf-8")
        self.assertLessEqual(source_path.stat().st_size, 8_000)
        self.assertIn("/ui-state", source)
        self.assertNotIn("fetch(", source)

    def test_project_ui_event_reconciler_has_focused_budget(self) -> None:
        source_path = STATIC / "project_ui_events.js"
        source = source_path.read_text(encoding="utf-8")
        self.assertLessEqual(source_path.stat().st_size, 12_000)
        self.assertIn("/ui-state/bindings", source)
        self.assertIn("/api/threads", source)
        self.assertNotIn("fetch(", source)

    def test_shared_api_client_preserves_structured_http_errors(self) -> None:
        source = (STATIC / "api_client.js").read_text()
        self.assertIn("class CodexApiError", source)
        self.assertIn("this.status", source)
        self.assertIn("this.detail", source)
        self.assertIn("this.path", source)


    def test_authority_policy_explorer_has_focused_budget_and_shared_api_client(self) -> None:
        source_path = STATIC / "authority_policy_explorer.js"
        source = source_path.read_text(encoding="utf-8")
        self.assertLessEqual(source_path.stat().st_size, 12_000)
        self.assertIn("api_client.js", source)
        self.assertIn("apiRequest", source)
        self.assertNotIn("fetch(", source)


    def test_work_item_operator_surfaces_exact_execution_definition_provenance(self) -> None:
        source = (STATIC / "work_items_ui.js").read_text(encoding="utf-8")
        self.assertIn("contract.definition_refs", source)
        self.assertIn("Definition:", source)
        self.assertIn("ref.checksum", source)
        self.assertIn("ref.revision", source)


if __name__ == "__main__":
    unittest.main()
