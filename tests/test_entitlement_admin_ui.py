from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class EntitlementAdminUiTests(unittest.TestCase):
    def test_entitlement_browser_separates_service_access_from_authority(self) -> None:
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "static" / "entitlement_admin.js").read_text(encoding="utf-8")

        self.assertIn('id="entitlement-mode"', html)
        self.assertIn('id="entitlement-capabilities"', html)
        self.assertIn('id="entitlement-quotas"', html)
        self.assertIn('id="entitlement-usage"', html)
        self.assertIn('id="entitlement-preview-result"', html)
        self.assertIn('apiRequest("/api/entitlements/mode")', javascript)
        self.assertIn('apiRequest("/api/entitlements/capabilities")', javascript)
        self.assertIn('apiRequest("/api/entitlements/quotas")', javascript)
        self.assertIn('apiRequest("/api/entitlements/usage")', javascript)
        self.assertIn("/api/entitlements/status?", javascript)
        self.assertIn("decision.reason", javascript)
        self.assertIn("quota.projected_usage", javascript)
        self.assertIn("quota.window_start", javascript)
        self.assertIn("quota.window_end", javascript)
        self.assertIn("quota.behavior", javascript)
        self.assertIn("idempotency_key", javascript)
        self.assertIn("action_intent_id", javascript)
        self.assertIn("self_hosted_unlimited", javascript)
        self.assertIn("does not grant human RBAC", javascript)
        self.assertIn("codex:entitlement-state-rendered", javascript)
        self.assertNotIn('method: "PUT"', javascript)
        self.assertNotIn('method: "POST"', javascript)


if __name__ == "__main__":
    unittest.main()
