from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ExecutionProfileControlsUiTests(unittest.TestCase):
    def test_execution_profile_selector_exposes_canonical_management_path(self) -> None:
        javascript = (ROOT / "static" / "execution_profile_controls.js").read_text(
            encoding="utf-8"
        )
        matrix = (
            ROOT / "docs" / "administration" / "configuration-capability-matrix.md"
        ).read_text(encoding="utf-8")

        self.assertIn("activeProjectId", javascript)
        self.assertIn('id="manage-execution-profiles"', javascript)
        self.assertIn("Manage profiles in Definitions", javascript)
        self.assertIn("/projects/", javascript)
        self.assertIn("/definitions", javascript)
        self.assertIn("catalog.definition", javascript)
        self.assertIn("Canonical definition:", javascript)
        self.assertIn(
            "| Execution profiles | Y | Y via Definition Registry |",
            matrix,
        )
        self.assertIn("execution-profile-catalog", matrix)


if __name__ == "__main__":
    unittest.main()
