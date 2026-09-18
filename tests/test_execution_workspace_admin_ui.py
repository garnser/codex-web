from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ExecutionWorkspaceAdminUiTests(unittest.TestCase):
    def test_workspace_inspector_exposes_isolation_lease_and_cleanup_state(self) -> None:
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "static" / "execution_workspace_admin.js").read_text(
            encoding="utf-8"
        )

        self.assertIn('id="execution-workspace-list"', html)
        self.assertIn('id="recover-execution-workspaces"', html)
        self.assertIn('apiRequest("/api/execution-workspaces/inspection")', javascript)
        self.assertIn('apiRequest("/api/identity/me")', javascript)
        self.assertIn('apiRequest("/api/projects")', javascript)
        self.assertIn('apiRequest("/api/resources")', javascript)
        self.assertIn("/events", javascript)
        self.assertIn("/renew", javascript)
        self.assertIn("/release", javascript)
        self.assertIn('apiRequest("/api/execution-workspaces/recover"', javascript)
        self.assertIn("owner_identity_id", javascript)
        self.assertIn("branch_name", javascript)
        self.assertIn("base_revision", javascript)
        self.assertIn("head_revision", javascript)
        self.assertIn("actual_disk_bytes", javascript)
        self.assertIn("lease_active", javascript)
        self.assertIn("lease_expired", javascript)
        self.assertIn("lease.owner_identity_id", javascript)
        self.assertIn("lease.mode", javascript)
        self.assertIn("lease.acquired_at", javascript)
        self.assertIn("lease.renewed_at", javascript)
        self.assertIn("lease.expires_at", javascript)
        self.assertIn("lease.released_at", javascript)
        self.assertIn("release_reason", javascript)
        self.assertIn("integration.conflicts", javascript)
        self.assertIn("execution-workspace:admin", javascript)
        self.assertIn('["mfa", "local_trusted"]', javascript)
        self.assertIn("without claiming successful integration", javascript)
        self.assertIn("window.confirm", javascript)
        self.assertIn("window.prompt", javascript)
        self.assertNotIn("localStorage", javascript)
        self.assertNotIn("fetch(", javascript)


if __name__ == "__main__":
    unittest.main()
