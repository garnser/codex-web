from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ModelGatewayAdminUiTests(unittest.TestCase):
    def test_model_gateway_browser_exposes_routing_provenance_without_invocation(self) -> None:
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "static" / "model_gateway_admin.js").read_text(encoding="utf-8")

        self.assertIn('id="model-gateway-policy"', html)
        self.assertIn('id="model-provider-list"', html)
        self.assertIn('id="model-definition-list"', html)
        self.assertIn('id="model-prompt-list"', html)
        self.assertIn('id="model-invocation-list"', html)
        self.assertIn('id="model-route-preview-result"', html)
        self.assertIn('apiRequest("/api/model-gateway/providers")', javascript)
        self.assertIn('apiRequest("/api/model-gateway/models")', javascript)
        self.assertIn('apiRequest("/api/model-gateway/prompts")', javascript)
        self.assertIn('apiRequest("/api/model-gateway/policy")', javascript)
        self.assertIn('apiRequest("/api/provider-capacity")', javascript)
        self.assertIn("/api/model-gateway/invocations?limit=", javascript)
        self.assertIn('apiRequest("/api/model-gateway/route"', javascript)
        self.assertIn('purpose: "ui.route-preview"', javascript)
        self.assertIn("routing_reason", javascript)
        self.assertIn("policy_fingerprint_sha256", javascript)
        self.assertIn("prompt_template_checksum_sha256", javascript)
        self.assertIn("selected_provider_id", javascript)
        self.assertIn("selected_model_id", javascript)
        self.assertIn("credential_ref", javascript)
        self.assertIn("residency_tags", javascript)
        self.assertIn("compliance_tags", javascript)
        self.assertIn("input_price_per_million_usd", javascript)
        self.assertIn("output_price_per_million_usd", javascript)
        self.assertIn("capacity.retry_at", javascript)
        self.assertIn("capacityWaits", javascript)
        self.assertIn("no model/provider invocation occurred", javascript)
        self.assertIn("codex:model-gateway-rendered", javascript)
        self.assertNotIn("/invoke", javascript)
        self.assertNotIn("OPENAI_API_KEY", javascript)


if __name__ == "__main__":
    unittest.main()
