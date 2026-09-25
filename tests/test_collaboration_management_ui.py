from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CollaborationManagementUiTests(unittest.TestCase):
    def test_management_surface_keeps_identity_provenance_read_only(self) -> None:
        javascript = (ROOT / "static" / "collaboration_management.js").read_text(
            encoding="utf-8"
        )
        workspace = (ROOT / "static" / "collaboration_workspace.js").read_text(
            encoding="utf-8"
        )
        matrix = (
            ROOT / "docs" / "administration" / "configuration-capability-matrix.md"
        ).read_text(encoding="utf-8")

        self.assertLess(len(javascript.encode("utf-8")), 12_000)
        self.assertIn("EDITABLE_PROFILE_FIELDS", javascript)
        self.assertIn("EDITABLE_TEAM_FIELDS", javascript)
        self.assertNotIn('"profile_id", "revision"', javascript)
        self.assertNotIn('"record_id"', javascript.split("EDITABLE_PROFILE_FIELDS", 1)[1].split("];", 1)[0])
        self.assertIn("reason is required for a revision", javascript)
        self.assertIn("Confirm the impact before applying", javascript)
        self.assertIn("/revisions", javascript)
        self.assertIn("managementActions", workspace)
        self.assertIn("Create Agent Profile", workspace)
        self.assertIn("Create Team", workspace)
        self.assertIn(
            "| Agent Profiles: identity, role/instruction refs, runtime/model policy, execution profile, sandbox/network requirements, budgets | Y | Y | Y | Y via immutable revision |",
            matrix,
        )
        self.assertIn(
            "| Teams: membership, delegation policy, budgets, lifecycle | Y | Y | Y | Y via immutable revision |",
            matrix,
        )
        self.assertIn("Hard delete is intentionally unsupported", matrix)


if __name__ == "__main__":
    unittest.main()
