from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"


class StructuredLogAdminUiTests(unittest.TestCase):
    def test_structured_log_ui_uses_bounded_canonical_query_and_trace_correlation(self) -> None:
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        javascript = (STATIC / "structured_log_admin.js").read_text(encoding="utf-8")
        operations = (STATIC / "operations_observability.js").read_text(encoding="utf-8")

        self.assertIn('id="structured-log-panel"', html)
        self.assertIn('id="structured-log-filters"', html)
        self.assertIn('id="structured-log-correlation"', html)
        self.assertIn('id="structured-log-action-intent"', html)
        self.assertIn('apiRequest(`/api/logs/recent?', javascript)
        self.assertIn("window_seconds", javascript)
        self.assertIn("logger_name", javascript)
        self.assertIn("correlation_id", javascript)
        self.assertIn("causation_id", javascript)
        self.assertIn("work_item_ref", javascript)
        self.assertIn("execution_id", javascript)
        self.assertIn("action_intent_id", javascript)
        self.assertIn(
            "Free-form log message and exception text are intentionally not returned",
            javascript,
        )
        self.assertIn(
            "Telemetry is explanatory, not canonical business state",
            javascript,
        )
        self.assertIn("data-log-correlation", operations)
        self.assertIn("View structured logs", operations)
        self.assertNotIn("localStorage", javascript)
        self.assertNotIn("fetch(", javascript)

    def test_structured_log_module_stays_bounded_and_uses_shared_api_client(self) -> None:
        path = STATIC / "structured_log_admin.js"
        source = path.read_text(encoding="utf-8")
        self.assertLessEqual(path.stat().st_size, 8_000)
        self.assertIn("api_client.js", source)
        self.assertIn("apiRequest", source)
        self.assertNotIn("fetch(", source)


if __name__ == "__main__":
    unittest.main()
