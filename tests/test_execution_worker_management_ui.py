from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ExecutionWorkerManagementUiTests(unittest.TestCase):
    def test_worker_management_uses_guarded_control_plane_apis(self) -> None:
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "static" / "execution_worker_management.js").read_text(
            encoding="utf-8"
        )

        self.assertIn('id="execution-worker-management-panel"', html)
        self.assertIn('id="recover-stale-workers"', html)
        self.assertIn('id="recover-expired-assignments"', html)
        self.assertIn('apiRequest("/api/identity/me")', javascript)
        self.assertIn("execution-worker:admin", javascript)
        self.assertIn('["mfa", "local_trusted"]', javascript)
        self.assertIn("/activate", javascript)
        self.assertIn("/drain", javascript)
        self.assertIn("/quarantine", javascript)
        self.assertIn("/revoke", javascript)
        self.assertIn('apiRequest("/api/execution-workers/recover-stale-workers"', javascript)
        self.assertIn('apiRequest("/api/execution-workers/assignments/recover-expired"', javascript)
        self.assertIn("/retry", javascript)
        self.assertIn("cannot be reactivated", javascript)
        self.assertIn("lease validation to fail closed", javascript)
        self.assertIn("new fenced lease", javascript)
        self.assertIn("window.confirm", javascript)
        self.assertIn("window.prompt", javascript)
        self.assertNotIn("/heartbeat", javascript)
        self.assertNotIn("/claim", javascript)
        self.assertNotIn("/renew", javascript)
        self.assertNotIn("/complete", javascript)
        self.assertNotIn("lease_token", javascript)
        self.assertNotIn("localStorage", javascript)
        self.assertNotIn("fetch(", javascript)


if __name__ == "__main__":
    unittest.main()
