from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WorkGraphAdminUiTests(unittest.TestCase):
    def test_work_graph_ui_uses_canonical_graph_facts_and_server_validated_edits(self) -> None:
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "static" / "work_graph_admin.js").read_text(
            encoding="utf-8"
        )
        styles = (ROOT / "static" / "styles.css").read_text(encoding="utf-8")

        self.assertIn('id="work-graph-panel"', html)
        self.assertIn('id="work-graph-svg"', html)
        self.assertIn('id="work-graph-management-panel"', html)
        self.assertIn('id="work-graph-node-detail"', html)
        self.assertIn('apiRequest("/api/projects")', javascript)
        self.assertIn("/api/work-graph/projects/", javascript)
        self.assertIn("/api/work-graph/events?", javascript)
        self.assertIn("/api/work-graph/traverse?", javascript)
        self.assertIn('apiRequest("/api/work-graph/edges"', javascript)
        self.assertIn('method: "POST"', javascript)
        self.assertIn('method: "DELETE"', javascript)
        self.assertIn("work-graph:admin", javascript)
        self.assertIn('["mfa", "local_trusted"]', javascript)
        self.assertIn("critical_path", javascript)
        self.assertIn("runnable_refs", javascript)
        self.assertIn("failure_impacts", javascript)
        self.assertIn("readiness.reasons", javascript)
        self.assertIn("blocking_refs", javascript)
        self.assertIn("downstream", javascript)
        self.assertIn("upstream", javascript)
        self.assertIn("window.confirm", javascript)
        self.assertIn("server will reject cycles", javascript)
        self.assertIn("graph-runnable does not grant execution authority", javascript)
        self.assertIn("Open canonical Work Item operator detail", javascript)
        self.assertIn('role="button"', javascript)
        self.assertIn("keydown", javascript)
        self.assertIn(".work-graph-edge.critical", styles)
        self.assertIn(".work-graph-node.status-runnable", styles)
        self.assertIn(".work-graph-node.status-blocked", styles)
        self.assertNotIn("localStorage", javascript)
        self.assertNotIn("fetch(", javascript)


if __name__ == "__main__":
    unittest.main()
