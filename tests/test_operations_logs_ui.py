from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class OperationsLogsUiTests(unittest.TestCase):
    def test_operations_log_drilldown_uses_bounded_query_api_without_browser_store(self) -> None:
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        logs = (ROOT / "static" / "operations_logs.js").read_text(encoding="utf-8")
        operations = (ROOT / "static" / "operations_observability.js").read_text(
            encoding="utf-8"
        )

        self.assertIn('id="operations-log-status"', html)
        self.assertIn('id="operations-log-correlation"', html)
        self.assertIn('id="operations-log-work-item"', html)
        self.assertIn('id="operations-log-execution"', html)
        self.assertIn('id="operations-log-action-intent"', html)
        self.assertIn('id="load-structured-logs"', html)
        self.assertIn("/api/logs?", logs)
        self.assertIn("window_seconds", logs)
        self.assertIn('limit: "100"', logs)
        self.assertIn("correlation_id", logs)
        self.assertIn("causation_id", logs)
        self.assertIn("work_item_ref", logs)
        self.assertIn("execution_id", logs)
        self.assertIn("action_intent_id", logs)
        self.assertIn("payloadPolicy", logs)
        self.assertIn("bounded runtime telemetry", logs)
        self.assertIn("not canonical Work/Goal/Decision/ActionIntent state", logs)
        self.assertIn("data-operations-log-correlation", operations)
        self.assertIn("Logs for this correlation", operations)
        self.assertNotIn("localStorage", logs)
        self.assertNotIn("sessionStorage", logs)
        self.assertNotIn("fetch(", logs)


if __name__ == "__main__":
    unittest.main()
