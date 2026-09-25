from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ConfigurationAdminUiTests(unittest.TestCase):
    def test_configuration_browser_projects_typed_state_without_mutating_it(self) -> None:
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "static" / "configuration_admin.js").read_text(
            encoding="utf-8"
        )

        self.assertIn('id="configuration-spec-list"', html)
        self.assertIn('id="configuration-category-filter"', html)
        self.assertIn('id="configuration-record-list"', html)
        self.assertIn('id="configuration-resolve-result"', html)
        self.assertIn('apiRequest("/api/configuration/specs")', javascript)
        self.assertIn('apiRequest("/api/configuration/records")', javascript)
        self.assertIn('apiRequest("/api/configuration/resolve"', javascript)
        self.assertIn("/impact", javascript)
        self.assertIn('apiRequest("/api/projects")', javascript)
        self.assertIn('apiRequest("/api/resources")', javascript)
        self.assertIn("SCOPE_PRECEDENCE", javascript)
        self.assertIn("deployment", javascript)
        self.assertIn("resource", javascript)
        self.assertIn("feature_targeting", javascript)
        self.assertIn("force_disabled", javascript)
        self.assertIn("kill switch", javascript)
        self.assertIn("startup_only", javascript)
        self.assertIn("hot_reloadable", javascript)
        self.assertIn("spec.category", javascript)
        self.assertIn("spec.editable", javascript)
        self.assertIn("spec.sensitive", javascript)
        self.assertIn("spec.allowed_values", javascript)
        self.assertIn("spec.minimum", javascript)
        self.assertIn("spec.maximum", javascript)
        self.assertIn("secret_ref", javascript)
        self.assertIn("Secret reference only", javascript)
        self.assertIn("definition_ref", javascript)
        self.assertIn("Definition reference", javascript)
        self.assertIn("more_specific_overrides", javascript)
        self.assertIn("published_by", javascript)
        self.assertIn("codex:configuration-state-rendered", javascript)
        self.assertIn("does not grant RBAC", javascript)
        self.assertNotIn("/api/configuration/drafts", javascript)
        self.assertNotIn("/publish", javascript)
        self.assertNotIn("/rollback", javascript)
        self.assertNotIn("localStorage", javascript)
        self.assertNotIn("fetch(", javascript)


if __name__ == "__main__":
    unittest.main()
