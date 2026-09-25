from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ProjectSetupCapabilityParityTests(unittest.TestCase):
    def test_task_source_readiness_remediation_routes_to_canonical_work_surface(self) -> None:
        javascript = (ROOT / "static" / "project_setup_ui.js").read_text(
            encoding="utf-8"
        )
        matrix = (
            ROOT / "docs" / "administration" / "configuration-capability-matrix.md"
        ).read_text(encoding="utf-8")

        self.assertIn('route.includes("task-source")', javascript)
        self.assertIn('openWorkspace?.("work")', javascript)
        self.assertIn(
            "| Project readiness / bootstrap / task-source authority | Y | Y | Y |",
            matrix,
        )
        self.assertIn("authoritative task-source create/update/clear", matrix)


if __name__ == "__main__":
    unittest.main()
