from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ExtensionObservabilityAdminUiTests(unittest.TestCase):
    def test_observability_surface_uses_canonical_extension_state_and_audit(self) -> None:
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "static" / "extension_observability_admin.js").read_text(encoding="utf-8")

        self.assertIn('id="extension-audit-status"', html)
        self.assertIn('id="extension-audit-list"', html)
        self.assertIn('apiRequest("/api/extensions/events")', javascript)
        self.assertIn("manifest.compatibility?.codex_web", javascript)
        self.assertIn("manifest.provenance", javascript)
        self.assertIn("verification.observed_digest", javascript)
        self.assertIn("events.subscribes", javascript)
        self.assertIn("events.publishes", javascript)
        self.assertIn("consecutive_health_failures", javascript)
        self.assertIn("manifest_history", javascript)
        self.assertIn("migrations?.entrypoint", javascript)
        self.assertIn("MAX_AUDIT_ROWS = 100", javascript)
        self.assertIn("event.actor_id", javascript)


if __name__ == "__main__":
    unittest.main()
