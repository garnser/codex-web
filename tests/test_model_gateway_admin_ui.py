from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ModelGatewayAdminUiTests(unittest.TestCase):
    def test_model_gateway_admin_exposes_routing_without_credentials(self) -> None:
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "static" / "model_gateway_admin.js").read_text(encoding="utf-8")

        self.assertIn('id="model-provider-list"', html)
        self.assertIn('id="model-definition-list"', html)
        self.assertIn('id="model-policy"', html)
        self.assertIn('apiRequest("/api/model-gateway/providers")', javascript)
        self.assertIn('apiRequest("/api/model-gateway/models")', javascript)
        self.assertIn('apiRequest("/api/model-gateway/prompts")', javascript)
        self.assertIn('apiRequest("/api/model-gateway/policy")', javascript)
        self.assertIn("/api/model-gateway/invocations?limit=", javascript)
        self.assertIn("credential_ref", javascript)
        self.assertIn("model_classes", javascript)
        self.assertIn("required_residency_tags", javascript)
        self.assertIn("required_compliance_tags", javascript)
        self.assertIn("route_reason", javascript)
        self.assertIn("policy_fingerprint_sha256", javascript)
        self.assertIn("prompt_template_checksum_sha256", javascript)
        self.assertIn("work_item_ref", javascript)
        self.assertIn("goal_id", javascript)
        self.assertIn("decision_id", javascript)
        self.assertIn("codex:model-gateway-rendered", javascript)
        self.assertNotIn("credential_value", javascript)
        self.assertNotIn("api_key", javascript)


if __name__ == "__main__":
    unittest.main()
