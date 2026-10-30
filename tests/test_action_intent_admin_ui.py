from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ActionIntentAdminUiTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is required for UI contract tests")
    def test_failure_phase_view_node_contracts(self) -> None:
        result = subprocess.run(
            [shutil.which("node"), "--test", "tests/js/action_intent_outcome_view.test.mjs"],
            cwd=ROOT, capture_output=True, text=True, timeout=20, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_action_intent_timeline_uses_durable_history_without_dumping_provider_payloads(self) -> None:
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "static" / "action_intent_admin.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("action_intent_outcome_view.js", javascript)
        javascript += (ROOT / "static" / "action_intent_outcome_view.js").read_text(encoding="utf-8")

        self.assertIn("action_intent_merge_view.js", javascript)
        javascript += (ROOT / "static" / "action_intent_merge_view.js").read_text(encoding="utf-8")

        self.assertIn('id="action-intent-list"', html)
        self.assertIn('id="action-intent-recovery-panel"', html)
        self.assertIn('id="recover-stale-action-intents"', html)
        self.assertIn('apiRequest("/api/action-intents")', javascript)
        self.assertIn("/history", javascript)
        self.assertIn('apiRequest("/api/identity/me")', javascript)
        self.assertIn('apiRequest("/api/action-intents/recover-stale"', javascript)
        self.assertIn("authority_decision", javascript)
        self.assertIn("policy_decision", javascript)
        self.assertIn("security_decision", javascript)
        self.assertIn("credential_ref", javascript)
        self.assertIn("resource_ids", javascript)
        self.assertIn("idempotency_key", javascript)
        self.assertIn("provider_idempotency_supported", javascript)
        self.assertIn("correlation_id", javascript)
        self.assertIn("causation_id", javascript)
        self.assertIn("last_receipt_id", javascript)
        self.assertIn("last_verification_id", javascript)
        self.assertIn("requires_reconciliation", javascript)
        self.assertIn("failure.reason_code", javascript)
        self.assertIn("failure.remediation_key", javascript)
        self.assertIn("retry authority is exhausted", javascript)
        self.assertIn("newly authorized replacement ActionIntent", javascript)
        self.assertIn("current policy and provider gates", javascript)
        self.assertIn("Provider output body and rollback token are intentionally not rendered", javascript)
        self.assertIn("Callback payload body is intentionally not rendered", javascript)
        self.assertIn("Evidence-evaluation payload is retained canonically but not dumped", javascript)
        self.assertIn("Request parameter keys only", javascript)
        self.assertIn("Publication recovery provenance", javascript)
        self.assertIn("existing_change_request_number", javascript)
        self.assertIn("action-intent:admin", javascript)
        self.assertIn('["mfa", "local_trusted"]', javascript)
        self.assertIn("Expired EXECUTING intents become UNCERTAIN", javascript)
        self.assertNotIn("/execute", javascript)
        self.assertNotIn("/claim", javascript)
        self.assertNotIn("/renew", javascript)
        self.assertNotIn("/rollback", javascript)
        self.assertNotIn("localStorage", javascript)
        self.assertNotIn("fetch(", javascript)


if __name__ == "__main__":
    unittest.main()
