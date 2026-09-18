from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ArtifactEvidenceAdminUiTests(unittest.TestCase):
    def test_explorer_uses_canonical_artifact_evidence_and_governance_apis(self) -> None:
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "static" / "artifact_evidence_admin.js").read_text(
            encoding="utf-8"
        )

        self.assertIn('id="artifact-list"', html)
        self.assertIn('id="evidence-list"', html)
        self.assertIn('id="verification-list"', html)
        self.assertIn('id="evidence-gate-result"', html)
        self.assertIn('id="artifact-evidence-admin-panel"', html)
        self.assertIn('apiRequest("/api/artifacts")', javascript)
        self.assertIn('apiRequest("/api/evidence")', javascript)
        self.assertIn('apiRequest("/api/verifications")', javascript)
        self.assertIn('apiRequest("/api/data-governance/records")', javascript)
        self.assertIn('apiRequest("/api/identity/me")', javascript)
        self.assertIn("/api/evidence-requirements/work-items/", javascript)
        self.assertIn("/api/evidence-evaluations/work-items/", javascript)
        self.assertIn("/invalidate", javascript)
        self.assertIn('apiRequest("/api/artifact-evidence/governance/sync"', javascript)
        self.assertIn('apiRequest("/api/artifact-evidence/expire-retention"', javascript)
        self.assertIn("classification", javascript)
        self.assertIn("retention_action", javascript)
        self.assertIn("legal_hold_at", javascript)
        self.assertIn("deny_model_context", javascript)
        self.assertIn("governance_record_id", javascript)
        self.assertIn("independent", javascript)
        self.assertIn("matching_evidence_ids", javascript)
        self.assertIn("verification_ids", javascript)
        self.assertIn("Dependent evidence and verifications will be invalidated transitively", javascript)
        self.assertIn("artifact-evidence:admin", javascript)
        self.assertIn('["mfa", "local_trusted"]', javascript)
        self.assertIn("payload contents are not rendered here", javascript)
        self.assertIn("codex:artifact-evidence-state-rendered", javascript)
        self.assertNotIn("localStorage", javascript)
        self.assertNotIn("fetch(", javascript)


if __name__ == "__main__":
    unittest.main()
