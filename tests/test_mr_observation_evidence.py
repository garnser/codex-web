"""Freshness cannot become retention or accept malformed operator metadata."""
import unittest
from types import SimpleNamespace

from codex_web.mr_observation_evidence import METHOD, current_observation


class MRObservationFreshnessTests(unittest.TestCase):
    def proof(self, *, observed=1000, expiry=1030):
        return SimpleNamespace(observed_at=observed,
            metadata={"verification_method": METHOD, "observation_expires_at": expiry})

    def test_exact_bound_is_finite_and_expired_is_not_current(self):
        self.assertTrue(current_observation(self.proof(), now=1001))
        self.assertFalse(current_observation(self.proof(), now=999))
        self.assertFalse(current_observation(self.proof(), now=1030))
        self.assertFalse(current_observation(self.proof(expiry=1031), now=1001))

    def test_malformed_observation_expiry_or_clock_fails_closed_without_throwing(self):
        for value in (None, True, "1000", float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                self.assertFalse(current_observation(self.proof(observed=value), now=1001))
                self.assertFalse(current_observation(self.proof(expiry=value), now=1001))
                self.assertFalse(current_observation(self.proof(), now=value))

    def test_legacy_evidence_is_not_relabeled_or_given_new_expiry(self):
        original = SimpleNamespace(observed_at=1, metadata={"legacy": "kept"})
        self.assertTrue(current_observation(original, now=100000))
        self.assertEqual(original.metadata, {"legacy": "kept"})
