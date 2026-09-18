from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SecurityTrustDiagnosticsUiTests(unittest.TestCase):
    def test_security_diagnostics_use_canonical_decisions_without_dumping_sensitive_values(self) -> None:
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "static" / "security_trust_diagnostics.js").read_text(
            encoding="utf-8"
        )

        self.assertIn('id="security-trust-zones"', html)
        self.assertIn('id="security-event-list"', html)
        self.assertIn('id="security-governance-summary"', html)
        self.assertIn('id="security-disabled-actions"', html)
        self.assertIn('apiRequest("/api/security/trust-zones")', javascript)
        self.assertIn('apiRequest("/api/security/events")', javascript)
        self.assertIn('apiRequest("/api/data-governance/records")', javascript)
        self.assertIn('apiRequest("/api/action-intents")', javascript)
        self.assertIn("canonical_control", javascript)
        self.assertIn("untrusted data", javascript)
        self.assertIn("Detail fields present", javascript)
        self.assertIn("Object.keys(item.details", javascript)
        self.assertNotIn("JSON.stringify(item.details", javascript)
        self.assertIn("classification", javascript)
        self.assertIn("retention_expires_at", javascript)
        self.assertIn("legal_hold_at", javascript)
        self.assertIn("deny_model_context", javascript)
        self.assertIn("secret/credential-class records", javascript)
        self.assertIn("authority_decision", javascript)
        self.assertIn("policy_decision", javascript)
        self.assertIn("security_decision", javascript)
        self.assertIn("persisted canonical decision state", javascript)
        self.assertIn("does not infer why the action is disabled", javascript)
        self.assertIn("governed object payloads are not loaded or rendered", javascript)
        self.assertNotIn('method: "POST"', javascript)
        self.assertNotIn('method: "PUT"', javascript)
        self.assertNotIn('method: "DELETE"', javascript)
        self.assertNotIn("localStorage", javascript)
        self.assertNotIn("fetch(", javascript)


if __name__ == "__main__":
    unittest.main()
