from __future__ import annotations

import unittest
from pathlib import Path


EXECUTIVE_UI = Path(__file__).resolve().parents[1] / "static" / "executive-ui.js"


class ExecutiveFrontendBoundaryTests(unittest.TestCase):
    def test_executive_ui_uses_shared_api_client_without_raw_fetch(self) -> None:
        source = EXECUTIVE_UI.read_text()
        self.assertIn("api_client.js", source)
        self.assertIn("executiveApiModule", source)
        self.assertNotIn("fetch(", source)


if __name__ == "__main__":
    unittest.main()
