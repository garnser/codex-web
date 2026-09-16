from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"


class FrontendBoundaryTests(unittest.TestCase):
    def test_legacy_app_bundle_cannot_grow(self) -> None:
        # New UI behavior belongs in focused modules. Keep a small allowance for
        # corrective edits, but force substantive feature work out of app.js.
        self.assertLessEqual((STATIC / "app.js").stat().st_size, 90_000)

    def test_executive_ui_has_its_own_budget(self) -> None:
        self.assertLessEqual((STATIC / "executive-ui.js").stat().st_size, 22_000)

    def test_extracted_context_module_uses_shared_api_client(self) -> None:
        source = (STATIC / "context_compaction.js").read_text()
        self.assertIn("api_client.js", source)
        self.assertIn("apiRequest", source)
        self.assertNotIn("await fetch(", source)

    def test_shared_api_client_preserves_structured_http_errors(self) -> None:
        source = (STATIC / "api_client.js").read_text()
        self.assertIn("class CodexApiError", source)
        self.assertIn("this.status", source)
        self.assertIn("this.detail", source)
        self.assertIn("this.path", source)


if __name__ == "__main__":
    unittest.main()
