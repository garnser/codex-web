from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ExecutionWorkerAdminUiTests(unittest.TestCase):
    def test_worker_browser_exposes_control_execution_split_and_redacted_leases(self) -> None:
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "static" / "execution_worker_admin.js").read_text(
            encoding="utf-8"
        )

        self.assertIn('id="execution-worker-list"', html)
        self.assertIn('id="execution-assignment-list"', html)
        self.assertIn('id="execution-worker-events"', html)
        self.assertIn('apiRequest("/api/execution-workers")', javascript)
        self.assertIn('apiRequest("/api/execution-workers/assignments")', javascript)
        self.assertIn('apiRequest("/api/execution-workers/events")', javascript)
        self.assertIn("service_identity_id", javascript)
        self.assertIn("capabilities", javascript)
        self.assertIn("max_concurrency", javascript)
        self.assertIn("last_heartbeat_at", javascript)
        self.assertIn("quarantine_reason", javascript)
        self.assertIn("execution_workspace_id", javascript)
        self.assertIn("work_item_ref", javascript)
        self.assertIn("execution_id", javascript)
        self.assertIn("required_capabilities", javascript)
        self.assertIn("sandbox", javascript)
        self.assertIn("approval_policy", javascript)
        self.assertIn("allowed_hosts", javascript)
        self.assertIn("memory_bytes", javascript)
        self.assertIn("secret_refs", javascript)
        self.assertIn("Raw secret material is never present", javascript)
        self.assertIn("lease_token", javascript)
        self.assertIn("fence", javascript)
        self.assertIn("artifact_ids", javascript)
        self.assertIn("evidence_ids", javascript)
        self.assertIn("control-plane state is distinct from execution lease authority", javascript)
        self.assertIn("codex:execution-worker-state-rendered", javascript)
        self.assertNotIn("/drain", javascript)
        self.assertNotIn("/quarantine", javascript)
        self.assertNotIn("/revoke", javascript)
        self.assertNotIn("/recover-expired", javascript)
        self.assertNotIn("localStorage", javascript)
        self.assertNotIn("fetch(", javascript)


if __name__ == "__main__":
    unittest.main()
