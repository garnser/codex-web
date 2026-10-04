from __future__ import annotations

import unittest

from codex_web.api.skills import _security_matches


class SkillApiSecurityFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.item = {"security": {"status": "warning", "scan": {"severity": "medium", "provider_id": "nvidia-skillspector", "risk_score": 42, "findings": [{"category": "tool-misuse"}]}}}

    def matches(self, **updates) -> bool:
        filters = {"status": "", "severity": "", "category": "", "provider": "", "risk_min": None, "risk_max": None}
        filters.update(updates)
        return _security_matches(self.item, **filters)

    def test_all_security_facets_match(self) -> None:
        self.assertTrue(self.matches(status="warning", severity="medium", category="tool-misuse", provider="nvidia-skillspector", risk_min=40, risk_max=50))

    def test_mismatched_facets_exclude_the_skill(self) -> None:
        for facet in ({"status": "passed"}, {"severity": "critical"}, {"category": "prompt-injection"}, {"provider": "other"}, {"risk_min": 43}, {"risk_max": 41}):
            self.assertFalse(self.matches(**facet))

    def test_unscanned_skill_does_not_match_risk_filter(self) -> None:
        self.item = {"security": {"status": "not_scanned", "scan": None}}
        self.assertTrue(self.matches(status="not_scanned"))
        self.assertFalse(self.matches(risk_max=100))


if __name__ == "__main__":
    unittest.main()
