from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ModelGatewayManagementUiTests(unittest.TestCase):
    def test_management_uses_canonical_registry_policy_and_secret_reference_apis(self) -> None:
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "static" / "model_gateway_management.js").read_text(
            encoding="utf-8"
        )
        catalog_fields = (ROOT / "static" / "model_catalog_fields.js").read_text(
            encoding="utf-8"
        )

        self.assertIn('id="model-gateway-management-panel"', html)
        self.assertIn('id="model-definition-workloads"', html)
        self.assertIn('id="model-provider-catalog-discovery"', html)
        self.assertIn('id="model-definition-availability"', html)
        self.assertIn("admin authority and MFA/step-up assurance", html)
        self.assertIn('apiRequest("/api/identity/me")', javascript)
        self.assertIn('apiRequest("/api/secrets")', javascript)
        self.assertIn("/api/model-gateway/providers/", javascript)
        self.assertIn("/api/model-gateway/models/", javascript)
        self.assertIn("/api/model-gateway/prompts/", javascript)
        self.assertIn('apiRequest("/api/model-gateway/policy"', javascript)
        self.assertIn('method: "PUT"', javascript)
        self.assertIn("credential_ref", javascript)
        self.assertIn("residency_tags", javascript)
        self.assertIn("compliance_tags", javascript)
        self.assertIn("input_price_per_million_usd", javascript)
        self.assertIn("output_price_per_million_usd", javascript)
        self.assertIn("workload_classes", javascript)
        self.assertIn("catalog_discovery_enabled", catalog_fields)
        self.assertIn("availability_source", catalog_fields)
        self.assertIn("Existing version content is immutable", javascript)
        self.assertIn("Empty allowlists mean unrestricted", javascript)
        self.assertIn("change where model data is routed", javascript)
        self.assertIn("changes deterministic routing eligibility", javascript)
        self.assertIn("confirmAction", javascript)
        self.assertNotIn("/invoke", javascript)
        self.assertNotIn("secret.value", javascript)
        self.assertNotIn("localStorage", javascript)


if __name__ == "__main__":
    unittest.main()
