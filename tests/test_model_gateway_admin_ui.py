from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ModelGatewayAdminUiTests(unittest.TestCase):
    def test_model_gateway_browser_exposes_routing_provenance_without_invocation(self) -> None:
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "static" / "model_gateway_admin.js").read_text(encoding="utf-8")
        provider_cards = (ROOT / "static" / "model_provider_cards.js").read_text(encoding="utf-8")
        catalog_ui = (ROOT / "static" / "model_catalog_ui.js").read_text(encoding="utf-8")
        route_controls = (ROOT / "static" / "model_gateway_route_controls.js").read_text(
            encoding="utf-8"
        )
        invocation_ui = (ROOT / "static" / "model_gateway_invocation_ui.js").read_text(
            encoding="utf-8"
        )

        self.assertIn('id="model-gateway-policy"', html)
        self.assertIn('id="model-provider-list"', html)
        self.assertIn('id="model-catalog-list"', html)
        self.assertIn('id="model-definition-list"', html)
        self.assertIn('id="model-prompt-list"', html)
        self.assertIn('id="model-invocation-list"', html)
        self.assertIn('id="model-route-preview-result"', html)
        self.assertIn('id="model-route-workload"', html)
        self.assertIn('id="model-route-pinned-model"', html)
        self.assertIn('id="model-route-preferred-latency"', html)
        self.assertIn('id="model-route-low-cost"', html)
        self.assertIn('apiRequest("/api/model-gateway/providers")', javascript)
        self.assertIn('apiRequest("/api/model-gateway/models")', javascript)
        self.assertIn('apiRequest("/api/model-gateway/catalogs")', javascript)
        self.assertIn("catalog/refresh", catalog_ui)
        self.assertIn("availability_source", catalog_ui)
        self.assertIn("model_gateway_invocation_ui.js", javascript)
        self.assertIn("selected_catalog_revision", invocation_ui)
        self.assertIn('apiRequest("/api/model-gateway/prompts")', javascript)
        self.assertIn('apiRequest("/api/model-gateway/policy")', javascript)
        self.assertIn('apiRequest("/api/provider-capacity")', javascript)
        self.assertIn("/api/model-gateway/invocations?limit=", javascript)
        self.assertIn('apiRequest("/api/model-gateway/route"', javascript)
        self.assertIn('purpose: "ui.route-preview"', javascript)
        self.assertIn("routing_reason", javascript)
        self.assertIn("policy_fingerprint_sha256", javascript)
        self.assertIn("prompt_template_checksum_sha256", javascript)
        self.assertIn("selected_provider_id", invocation_ui)
        self.assertIn("selected_model_id", invocation_ui)
        self.assertIn("credential_ref", provider_cards)
        self.assertIn("residency_tags", javascript)
        self.assertIn("compliance_tags", javascript)
        self.assertIn("input_price_per_million_usd", catalog_ui)
        self.assertIn("output_price_per_million_usd", catalog_ui)
        self.assertIn("model_gateway_route_controls.js", javascript)
        self.assertIn("workload_class", route_controls)
        self.assertIn("pinned_model_id", route_controls)
        self.assertIn("preferred_latency_classes", route_controls)
        self.assertIn("prefer_lower_cost", route_controls)
        self.assertIn("capacity.retry_at", provider_cards)
        self.assertIn("capacityWaits", javascript)
        self.assertIn("no model/provider invocation occurred", javascript)
        self.assertIn("codex:model-gateway-rendered", javascript)
        self.assertNotIn("/invoke", javascript)
        self.assertNotIn("OPENAI_API_KEY", javascript)


if __name__ == "__main__":
    unittest.main()
