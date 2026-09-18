from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class InputPluginAdminUiTests(unittest.TestCase):
    def test_input_plugin_ui_exposes_versions_provenance_and_safe_rejections(self) -> None:
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "static" / "input_plugin_admin.js").read_text(
            encoding="utf-8"
        )

        self.assertIn('id="input-plugin-definition"', html)
        self.assertIn('id="input-plugin-registrations"', html)
        self.assertIn('id="input-plugin-invocations"', html)
        self.assertIn('id="input-plugin-security-events"', html)
        self.assertIn('apiRequest("/api/input-plugins")', javascript)
        self.assertIn(
            'apiRequest("/api/model-gateway/invocations?limit=100")',
            javascript,
        )
        self.assertIn('apiRequest("/api/security/events")', javascript)
        self.assertIn("implementation_available", javascript)
        self.assertIn("implementation_transport", javascript)
        self.assertIn("installed versions", javascript)
        self.assertIn("failure_policy", javascript)
        self.assertIn("max_patch_bytes", javascript)
        self.assertIn("max_added_characters", javascript)
        self.assertIn("definition_ref", javascript)
        self.assertIn("input_sha256", javascript)
        self.assertIn("patch_sha256", javascript)
        self.assertIn("output_sha256", javascript)
        self.assertIn("applied_fields", javascript)
        self.assertIn("proposed_gated_fields", javascript)
        self.assertIn("rejected_fields", javascript)
        self.assertIn("estimated_tokens_before", javascript)
        self.assertIn("estimated_tokens_after", javascript)
        self.assertIn("input_characters_before", javascript)
        self.assertIn("input_characters_after", javascript)
        self.assertIn("value_sha256", javascript)
        self.assertIn("The proposed value itself is intentionally not rendered", javascript)
        self.assertIn("Warning bodies are not rendered", javascript)
        self.assertIn("SKILL.md transport metadata only", javascript)
        self.assertIn("SAFE_EVENT_DETAIL_KEYS", javascript)
        self.assertNotIn("JSON.stringify(item.details", javascript)
        self.assertIn("Prompt/context/warning/transport payload bodies", javascript)
        self.assertIn("Configuration changes must use the canonical Definition Registry", javascript)
        self.assertNotIn('method: "POST"', javascript)
        self.assertNotIn('method: "PUT"', javascript)
        self.assertNotIn('method: "DELETE"', javascript)
        self.assertNotIn("localStorage", javascript)
        self.assertNotIn("fetch(", javascript)


if __name__ == "__main__":
    unittest.main()
