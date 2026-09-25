from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class GoalWorkspaceUiTests(unittest.TestCase):
    def test_goal_workspace_uses_canonical_apis_without_model_polling_or_shadow_state(self) -> None:
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "static" / "goals_ui.js").read_text(encoding="utf-8")
        styles = (ROOT / "static" / "goals_ui.css").read_text(encoding="utf-8")

        self.assertIn('static/goals_ui.js', html)
        self.assertIn("request('/api/goals')", javascript)
        self.assertIn("/decompositions/generate", javascript)
        self.assertIn("/review", javascript)
        self.assertIn("/commit/reconcile", javascript)
        self.assertIn("/completion-evaluations", javascript)
        self.assertIn("completion_evaluation_id", javascript)
        self.assertIn("model invocation:", javascript)
        self.assertIn("ActionIntent", javascript)
        self.assertIn("health.reasons", javascript)
        self.assertIn("goal_revision", javascript)
        self.assertIn("goal-observation-source", javascript)
        self.assertIn("goal-observation-reference", javascript)
        self.assertIn("goal-runtime-pause", javascript)
        self.assertIn("goal-runtime-resume", javascript)
        self.assertIn("goal-runtime-cancel", javascript)
        self.assertIn("goal-runtime-reconcile", javascript)
        self.assertIn("Attach / rebind to this Goal", javascript)
        self.assertIn("/runtime-objectives/unbound", javascript)
        self.assertIn("/reconcile", javascript)
        self.assertIn("Cancel Goal", javascript)
        self.assertIn("@media (max-width: 850px)", styles)

        # Refresh is deterministic GET-only; model planning is bound to the
        # explicit .goal-generate click handler.
        refresh_start = javascript.index("async function refreshAll()")
        refresh_end = javascript.index("function renderGoalList()", refresh_start)
        refresh_body = javascript[refresh_start:refresh_end]
        self.assertNotIn("/decompositions/generate", refresh_body)
        self.assertNotIn("method: 'POST'", refresh_body)
        self.assertNotIn("setInterval", javascript)
        self.assertNotIn("localStorage", javascript)
        self.assertNotIn("fetch(", javascript)


if __name__ == "__main__":
    unittest.main()
