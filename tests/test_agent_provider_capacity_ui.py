from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class AgentProviderCapacityUiTests(unittest.TestCase):
    def test_agent_provider_ui_exposes_runtime_capacity_and_fallback_reason(self) -> None:
        javascript = (ROOT / "static" / "agent_provider_admin.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("apiRequest('/api/provider-capacity')", javascript)
        self.assertIn("capacity_retry_at", javascript)
        self.assertIn("earliest_capacity_retry_at", javascript)
        self.assertIn("capacityWaits", javascript)
        self.assertIn("rejected_reasons", javascript)
        self.assertIn("Allow fallback", javascript)


if __name__ == "__main__":
    unittest.main()
