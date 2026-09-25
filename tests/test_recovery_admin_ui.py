from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RecoveryAdminUiTests(unittest.TestCase):
    def test_recovery_management_uses_reference_safe_canonical_contracts(self) -> None:
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "static" / "recovery_admin.js").read_text(encoding="utf-8")
        matrix = (
            ROOT / "docs" / "administration" / "configuration-capability-matrix.md"
        ).read_text(encoding="utf-8")

        self.assertIn('id="recovery-management-panel"', html)
        self.assertIn('id="recovery-policy-json"', html)
        self.assertIn('id="save-recovery-policy"', html)
        self.assertIn('id="create-recovery-backup"', html)
        self.assertIn('src="static/recovery_admin.js"', html)
        self.assertIn('request("/api/recovery/status")', javascript)
        self.assertIn('request("/api/recovery/policy"', javascript)
        self.assertIn('request("/api/recovery/backups"', javascript)
        self.assertIn('/verify', javascript)
        self.assertIn('method: "PUT"', javascript)
        self.assertIn('method: "POST"', javascript)
        self.assertIn("backup_key_id", javascript)
        self.assertNotIn("key_material", javascript)
        self.assertIn("Review and confirm recovery-policy impact", javascript)
        self.assertIn("isolated restore", javascript)
        self.assertIn(
            "| Recovery / operational policy | Y | Y | Y | explicit policy version + fingerprint |",
            matrix,
        )
        self.assertIn("no manual delete API", matrix)
        self.assertIn("Production restore remains deliberately outside", matrix)


if __name__ == "__main__":
    unittest.main()
