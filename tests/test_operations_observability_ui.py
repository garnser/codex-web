from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class OperationsObservabilityUiTests(unittest.TestCase):
    def test_operations_ui_projects_canonical_operator_telemetry_safely(self) -> None:
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "static" / "operations_observability.js").read_text(
            encoding="utf-8"
        )

        self.assertIn('id="operations-health"', html)
        self.assertIn('id="operations-runtime"', html)
        self.assertIn('id="operations-providers"', html)
        self.assertIn('id="operations-metrics"', html)
        self.assertIn('id="operations-traces"', html)
        self.assertIn('id="operations-diagnostics"', html)
        self.assertIn('id="resume-runtime-recovery"', html)
        self.assertIn('apiRequest("/api/observability")', javascript)
        self.assertIn("/api/operations?window_seconds=", javascript)
        self.assertIn('apiRequest("/api/execution-workers/assignments")', javascript)
        self.assertIn('apiRequest("/api/identity/me")', javascript)
        self.assertIn("/api/diagnostics", javascript)
        self.assertIn('apiRequest("/api/recovery/resume"', javascript)
        self.assertIn("autonomousExecutionEligible", javascript)
        self.assertIn("requiredForReadiness", javascript)
        self.assertIn("requiredForAutonomy", javascript)
        self.assertIn("supervisorTasks", javascript)
        self.assertIn("oldestQueueAgeSeconds", javascript)
        self.assertIn("lease?.expires_at", javascript)
        self.assertIn("gitlabSyncConsecutiveFailures", javascript)
        self.assertIn("correlationId", javascript)
        self.assertIn("causationId", javascript)
        self.assertIn("parentSpanId", javascript)
        self.assertIn("averageSeconds", javascript)
        self.assertIn("runtime:admin", javascript)
        self.assertIn('["mfa", "local_trusted"]', javascript)
        self.assertIn("does not mark Work Items successful", javascript)
        self.assertIn("Telemetry is explanatory, not canonical state", javascript)
        self.assertIn("Bounded/low-cardinality telemetry", javascript)
        self.assertNotIn("OPENAI_API_KEY", javascript)
        self.assertNotIn("localStorage", javascript)
        self.assertNotIn("fetch(", javascript)


if __name__ == "__main__":
    unittest.main()
