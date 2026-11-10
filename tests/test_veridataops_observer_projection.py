"""Mocked canonical HTTP DTO through the actual standalone producer/reducer."""
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


def load_script(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[1] / "scripts/operator" / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


observer = load_script("projection_observer", "veridataops_observer.py")
monitor = load_script("projection_supervisor", "mission_supervisor.py")


class ObserverProjectionTests(unittest.TestCase):
    tid = "01a16582-cf57-7003-84d1-159c324e70f7"

    def sample(self, response):
        responses = [response, {"queued": [], "queueDepth": 0, "automationWaitReason": None}]
        with patch.object(observer, "api", side_effect=responses) as api:
            tid, sample = observer._sample_thread_current(self.tid, "project")
        self.assertEqual(tid, self.tid)
        self.assertEqual(api.call_count, 2)
        return sample

    def reduce(self, sample, *, complete=True):
        return monitor.reduce_observer({"checked_at": 10000, "inspection_complete": complete,
            "conditions": {}, "threads": {self.tid: sample}})

    def test_canonical_projection_states_survive_producer_to_reducer(self):
        for state in ("unknown", "cancelled", "idle", "failed", "lost", "waiting", "running", "recovering", "stopped"):
            with self.subTest(state=state):
                sample = self.sample({"runner": {"state": state}, "activity": {"state": "idle"}})
                self.assertEqual(sample["runner_projection_status"], "present")
                reduced = self.reduce(sample)
                self.assertEqual(reduced["inspection_status"], "complete")
                self.assertTrue(reduced["threads"][self.tid]["inspection_ok"])
                self.assertEqual(reduced["threads"][self.tid]["runner"]["state"], state)

    def test_explicit_canonical_null_survives_legacy_flattening_without_false_failure(self):
        sample = self.sample({"runner": None, "activity": {"state": "unknown", "turnId": None},
            "status": {"type": "notLoaded"}})
        self.assertEqual(sample["runner_projection_status"], "absent")
        self.assertTrue(all(value is None for value in sample["runner"].values()))
        reduced = self.reduce(sample)
        self.assertEqual(reduced["inspection_status"], "complete")
        self.assertEqual(reduced["failed_thread_count"], 0)
        self.assertIsNone(reduced["threads"][self.tid]["runner"])
        snapshot = {"service": {"ActiveState": "active"}, "observer": reduced}
        self.assertEqual(monitor.observe(snapshot, {}, now=10000), [])

    def test_missing_or_malformed_canonical_projection_cannot_become_successful_absence(self):
        for response in ({}, {"runner": []}, {"runner": "private"}, {"runner": {}},
                         {"runner": {"state": "not-a-canonical-state"}}, None, []):
            with self.subTest(response=response):
                sample = self.sample(response)
                self.assertEqual(sample["runner_projection_status"], "invalid")
                reduced = self.reduce(sample)
                self.assertEqual(reduced["inspection_status"], "incomplete")
                snapshot = {"service": {"ActiveState": "active"}, "observer": reduced}
                self.assertEqual([event.kind for event in monitor.observe(snapshot, {}, now=10000)],
                    ["observer_inspection_incomplete"])

    def test_absence_marker_does_not_override_failed_read_or_legacy_unknown_coverage(self):
        sample = self.sample({"runner": None})
        sample["read_error_type"] = "HTTPError"
        self.assertEqual(self.reduce(sample)["inspection_status"], "incomplete")
        sample.pop("read_error_type")
        reduced = self.reduce(sample, complete=None)
        self.assertEqual(reduced["inspection_status"], "unknown")
        self.assertTrue(reduced["threads"][self.tid]["inspection_ok"])


if __name__ == "__main__":
    unittest.main()
