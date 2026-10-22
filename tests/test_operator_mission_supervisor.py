from __future__ import annotations

import importlib.util
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location("mission_supervisor", Path(__file__).parents[1] / "scripts/operator/mission_supervisor.py")
monitor = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = monitor
SPEC.loader.exec_module(monitor)


class MissionSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = {"schema_version": "1.0", "signals": {}, "jobs": {}}
        self.now = 10000
        self.config = {"mission_enabled": True, "automatic_investigations": True}

    def sample(self, **runner):
        return {"checked_at": self.now, "service": {"ActiveState": "active"},
                "observer": {"checked_at": self.now, "threads": {"thread": {
                    "queue_depth": 0, "activity": "idle", "runner": runner}}}}

    def test_retired_idle_runner_is_not_new_work_failure(self):
        snapshot = self.sample(state="lost", assignmentStatus="cancelled", leaseValid=False, processRunning=False)
        self.assertEqual(monitor.observe(snapshot, {}, now=self.now), [])
        snapshot["observer"]["threads"]["thread"]["queue_depth"] = 1
        self.assertEqual([s.kind for s in monitor.observe(snapshot, {}, now=self.now)], ["current_runner_failure"])

    def test_maintenance_suppresses_failures_and_launch(self):
        self.assertEqual(monitor.observe({}, {}, now=self.now, maintenance=True), [])
        monitor.retain_signals(self.state, [monitor.Signal("bug:7", "open_bug", {}, 7)], now=self.now)
        self.config["maintenance_until"] = self.now + 1
        self.assertIsNone(monitor.choose_signal(self.state, self.config, now=self.now))

    def test_historical_failed_turn_is_not_new_failure(self):
        snapshot = self.sample()
        sample = snapshot["observer"]["threads"]["thread"]
        sample["progress"] = {"status": "failed", "turn_id": "old"}
        self.assertEqual(monitor.observe(snapshot, {}, now=self.now), [])
        previous = json.loads(json.dumps(snapshot))
        sample["progress"]["turn_id"] = "new"
        self.assertEqual([s.kind for s in monitor.observe(snapshot, previous, now=self.now)], ["new_native_failure"])

    def test_root_lease_capacity_and_daily_budget_inhibit_launch(self):
        monitor.retain_signals(self.state, [monitor.Signal("bug:7", "open_bug", {}, 7)], now=self.now)
        self.assertEqual(monitor.choose_signal(self.state, self.config, now=self.now), "bug:7")
        self.config["root_lease_until"] = self.now + 1
        self.assertIsNone(monitor.choose_signal(self.state, self.config, now=self.now))
        del self.config["root_lease_until"]
        for status in ("launch_requested", "running", "unknown"):
            self.state["jobs"] = {"job": {"created_at": 1, "status": status}}
            self.assertIsNone(monitor.choose_signal(self.state, self.config, now=self.now))
        self.state["jobs"] = {str(i): {"created_at": self.now - 4000, "status": "failed"} for i in range(6)}
        self.assertIsNone(monitor.choose_signal(self.state, self.config, now=self.now))

    def test_host_failure_requires_consecutive_samples(self):
        signals = [monitor.Signal("host:test", "service_inactive", {})]
        for now in (10000, 10060):
            monitor.retain_signals(self.state, signals, now=now)
            self.assertIsNone(monitor.choose_signal(self.state, self.config, now=now))
        monitor.retain_signals(self.state, signals, now=10120)
        self.assertEqual(monitor.choose_signal(self.state, self.config, now=10120), "host:test")
        monitor.retain_signals(self.state, signals, now=11000)
        self.assertEqual(self.state["signals"]["host:test"]["samples"], 1)

    def test_uncertain_issue_creation_is_read_back_never_repeated(self):
        monitor.retain_signals(self.state, [monitor.Signal("host:test", "service_inactive", {})], now=self.now)
        calls = []
        def run(argv):
            calls.append(argv)
            if "create" in argv:
                raise subprocess.TimeoutExpired(argv, 15)
            return subprocess.CompletedProcess(argv, 0, "[]", "")
        with self.assertRaises(subprocess.TimeoutExpired):
            monitor.ensure_issue(self.root, self.state, "host:test", run=run)
        restored = monitor.load_state(self.root)
        self.assertIsNone(monitor.ensure_issue(self.root, restored, "host:test", run=run))
        self.assertEqual(sum("create" in argv for argv in calls), 1)

    def test_launch_persists_identity_before_uncertain_unit_start(self):
        monitor.retain_signals(self.state, [monitor.Signal("bug:7", "open_bug", {}, 7)], now=self.now)
        def run(argv):
            saved = monitor.load_state(self.root)
            self.assertEqual(len(saved["jobs"]), 1)
            job_id = saved["signals"]["bug:7"]["job_id"]
            self.assertTrue((self.root / "jobs" / job_id / "request.json").is_file())
            raise subprocess.TimeoutExpired(argv, 15)
        with self.assertRaises(subprocess.TimeoutExpired):
            monitor.launch(self.root, self.state, "bug:7", self.config, now=self.now, run=run)
        self.assertIsNone(monitor.choose_signal(monitor.load_state(self.root), self.config, now=self.now + 2000))

    def test_positive_reaping_retains_same_signal_without_blocking_unrelated_work(self):
        self.state["jobs"]["a"] = {"id": "a", "status": "running", "unit": "same.service", "created_at": 1}
        def run(argv):
            return subprocess.CompletedProcess(argv, 0, "ActiveState=failed\nMainPID=0\nLoadState=loaded\nJob=\n", "")
        monitor.reconcile_jobs(self.root, self.state, run=run)
        self.assertEqual(self.state["jobs"]["a"]["status"], "interrupted_requires_reconciliation")
        monitor.retain_signals(self.state, [monitor.Signal("bug:7", "open_bug", {}, 7)], now=self.now)
        self.assertEqual(monitor.choose_signal(self.state, self.config, now=self.now), "bug:7")

    def test_unknown_unit_state_blocks_replacement(self):
        self.state["jobs"]["a"] = {"id": "a", "status": "running", "unit": "same.service", "created_at": 1}
        def run(argv):
            raise RuntimeError("bus_unavailable")
        monitor.reconcile_jobs(self.root, self.state, run=run)
        self.assertEqual(self.state["jobs"]["a"]["status"], "unknown")

    def test_inactive_unit_with_queued_start_job_still_owns_capacity(self):
        self.state["jobs"]["a"] = {"id": "a", "status": "launch_requested", "unit": "same.service", "created_at": 1}
        def run(argv):
            return subprocess.CompletedProcess(argv, 0, "ActiveState=inactive\nMainPID=0\nLoadState=loaded\nJob=123\n", "")
        monitor.reconcile_jobs(self.root, self.state, run=run)
        self.assertEqual(self.state["jobs"]["a"]["status"], "unknown")
        monitor.retain_signals(self.state, [monitor.Signal("bug:7", "open_bug", {}, 7)], now=self.now)
        self.assertIsNone(monitor.choose_signal(self.state, self.config, now=self.now))

    def test_github_outage_retains_host_collection_and_last_inventory(self):
        config = {"repository_path": str(self.root), "observer_snapshot": str(self.root / "missing"),
                  "maintenance_until": self.now + 1}
        def run(argv):
            if argv[0] == "gh":
                raise RuntimeError("offline")
            return subprocess.CompletedProcess(argv, 0, "ActiveState=active\nMainPID=42\nNRestarts=0\n", "")
        previous = {"bugs": [{"number": 7}], "github_checked_at": 100}
        with patch.object(monitor, "urlopen", side_effect=AssertionError("maintenance must not probe app")):
            snapshot = monitor.collect(config, previous, now=self.now, run=run)
        self.assertEqual(snapshot["bugs"], [{"number": 7}])
        self.assertEqual(snapshot["github_checked_at"], 100)
        self.assertEqual(snapshot["service"]["MainPID"], "42")
        self.assertEqual(snapshot["github_error"], "RuntimeError")
        self.assertGreater(snapshot["disk_free"], 0)

    def test_observer_reduction_omits_raw_output_and_names(self):
        reduced = monitor.reduce_observer({"checked_at": 1, "secret": "raw",
            "threads": {"t": {"name": "private", "output": "private", "progress": {"turn_id": "a", "text": "raw"}}}})
        self.assertNotIn("private", json.dumps(reduced))
        self.assertNotIn("raw", json.dumps(reduced))

    def test_receipt_is_not_inferred_issue_closure(self):
        job = {"id": "a", "status": "running", "unit": "same.service"}
        self.state["jobs"]["a"] = job
        monitor.atomic_json(self.root / "jobs/a/finished.json", {
            "status": "investigation_finished", "finished_at": 123, "result": {"status": "fixed"}})
        def run(argv):
            return subprocess.CompletedProcess(argv, 0, "ActiveState=inactive\nMainPID=0\nLoadState=loaded\nJob=\n", "")
        monitor.reconcile_jobs(self.root, self.state, run=run)
        self.assertEqual(job["status"], "investigation_finished")
        self.assertNotIn("issue_closed", job)

    def test_finished_receipt_waits_for_unit_cleanup_before_releasing_capacity(self):
        self.state["jobs"]["a"] = {"id": "a", "status": "running", "unit": "same.service", "created_at": 1}
        monitor.atomic_json(self.root / "jobs/a/finished.json", {
            "job_id": "a", "status": "investigation_finished", "finished_at": 123, "result": {"status": "fixed"}})
        def run(argv):
            return subprocess.CompletedProcess(argv, 0, "ActiveState=deactivating\nMainPID=0\nLoadState=loaded\nJob=123\n", "")
        monitor.reconcile_jobs(self.root, self.state, run=run)
        self.assertEqual(self.state["jobs"]["a"]["status"], "running")
        monitor.retain_signals(self.state, [monitor.Signal("bug:7", "open_bug", {}, 7)], now=self.now)
        self.assertIsNone(monitor.choose_signal(self.state, self.config, now=self.now))

    def test_start_rechecks_mission_and_root_lease_before_checkout(self):
        job_id = "a" * 32
        directory = self.root / "jobs" / job_id
        monitor.atomic_json(directory / "request.json", {"id": job_id, "issue_number": 7})
        self.config["root_lease_until"] = monitor.time.time() + 900
        monitor.atomic_json(self.root / "config.json", self.config)
        monitor.investigate(self.root, job_id, run=lambda _: self.fail("must not checkout under Root lease"))
        self.assertFalse((directory / "started.json").exists())
        self.assertTrue(monitor.read_json(directory / "finished.json")["model_not_started"])

    def test_maintenance_arriving_during_checkout_prevents_model_start(self):
        job_id = "a" * 32
        directory = self.root / "jobs" / job_id
        monitor.atomic_json(directory / "request.json", {"id": job_id, "issue_number": 7})
        self.config["repository_path"] = str(self.root)
        monitor.atomic_json(self.root / "config.json", self.config)
        def run(argv):
            Path(argv[-2]).mkdir()
            self.config["maintenance_until"] = monitor.time.time() + 3600
            monitor.atomic_json(self.root / "config.json", self.config)
        with patch.object(monitor.subprocess, "run", side_effect=AssertionError("model must not start")):
            monitor.investigate(self.root, job_id, run=run)
        self.assertFalse((directory / "started.json").exists())
        self.assertEqual(monitor.read_json(directory / "finished.json")["status"], "deferred_before_model")

    def test_untyped_private_metadata_cannot_reach_issue_body(self):
        tid = "01a16582-cf57-7003-84d1-159c324e70f7"
        value = {"checked_at": 1, "threads": {tid: {"runner": {"assignmentId": "private raw provider error", "fence": "private"},
            "progress": {"turn_id": {"text": "private"}, "error_code": "private"}}}}
        reduced = monitor.reduce_observer(value)
        self.assertNotIn("private", json.dumps(reduced))
        item = {"kind": "new_native_failure", "samples": 3, "metadata": {
            "thread_id": tid, "turn_id": "private", "assignment_id": "private", "error_code": "private"}}
        body = monitor.issue_body("key", item, "marker")
        self.assertNotIn('"private"', body)
        self.assertIn(tid, body)

    def test_known_no_model_deferral_requalifies_once_after_root_lease_expires(self):
        old_id = "a" * 32
        monitor.retain_signals(self.state, [monitor.Signal("bug:7", "open_bug", {}, 7)], now=self.now)
        self.state["signals"]["bug:7"]["job_id"] = old_id
        job = {"id": old_id, "status": "deferred_before_model", "signal_key": "bug:7",
            "unit": "same.service", "created_at": self.now - 2000, "issue_number": 7}
        self.state["jobs"][old_id] = job
        monitor.atomic_json(self.root / "jobs" / old_id / "finished.json", {
            "job_id": old_id, "finished_at": self.now - 1000,
            "status": "deferred_before_model", "model_not_started": True})
        self.config["root_lease_until"] = self.now + 1
        monitor.requalify_known_deferrals(self.root, self.state, self.config, now=self.now,
            run=lambda _: self.fail("must not requalify under current Root lease"))
        self.assertEqual(self.state["signals"]["bug:7"]["job_id"], old_id)
        calls = []
        def run(argv):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "LoadState=loaded\nActiveState=inactive\nMainPID=0\nJob=\n", "")
        monitor.requalify_known_deferrals(self.root, self.state, self.config, now=self.now + 2, run=run)
        self.assertNotIn("job_id", self.state["signals"]["bug:7"])
        new_id = monitor.launch(self.root, self.state, "bug:7", self.config, now=self.now + 2, run=run)
        self.assertNotEqual(new_id, old_id)
        monitor.requalify_known_deferrals(self.root, self.state, self.config, now=self.now + 3, run=run)
        self.assertEqual(sum("start" in argv for argv in calls), 1)
        self.assertIsNone(monitor.choose_signal(self.state, self.config, now=self.now + 3000))
        self.assertTrue((self.root / "jobs" / old_id / "finished.json").is_file())

    def test_unknown_or_started_job_never_enters_known_deferral_requalification(self):
        for status, started in (("unknown", False), ("deferred_before_model", True)):
            job_id = "b" * 32
            monitor.retain_signals(self.state, [monitor.Signal("bug:7", "open_bug", {}, 7)], now=self.now)
            self.state["signals"]["bug:7"]["job_id"] = job_id
            self.state["jobs"][job_id] = {"id": job_id, "status": status, "signal_key": "bug:7",
                "unit": "same.service", "created_at": self.now - 2000}
            monitor.atomic_json(self.root / "jobs" / job_id / "finished.json", {"model_not_started": True})
            if started:
                monitor.atomic_json(self.root / "jobs" / job_id / "started.json", {"started_at": 1})
            monitor.requalify_known_deferrals(self.root, self.state, self.config, now=self.now,
                run=lambda _: self.fail("must not retry uncertain/started job"))
            self.assertEqual(self.state["signals"]["bug:7"]["job_id"], job_id)

    def test_ci_continuation_requires_current_positive_required_checks(self):
        self.state["jobs"]["a"] = {"id": "a", "created_at": 1, "issue_number": 7,
            "status": "investigation_finished", "result": {"status": "awaiting_ci",
                "pull_request_url": "https://github.com/garnser/codex-web/pull/8"}}
        pr = {"state": "OPEN", "isDraft": False, "headRefOid": "a" * 40,
              "mergeStateStatus": "CLEAN", "mergeable": "MERGEABLE",
              "statusCheckRollup": [{"name": "CodeQL", "conclusion": "SUCCESS", "status": "COMPLETED"}]}
        def run(argv):
            data = {"contexts": ["CodeQL", "unit-static"]} if "api" in argv else pr
            return subprocess.CompletedProcess(argv, 0, json.dumps(data), "")
        self.assertEqual(monitor.continuation_signals(self.state, run=run), [])
        pr["statusCheckRollup"].append({"name": "unit-static", "conclusion": "SUCCESS", "status": "COMPLETED"})
        events = monitor.continuation_signals(self.state, run=run)
        self.assertEqual([s.kind for s in events], ["pr_ready"])
        monitor.retain_signals(self.state, events, now=self.now)
        key = events[0].key
        self.state["signals"][key]["job_id"] = "already-launched"
        monitor.retain_signals(self.state, monitor.continuation_signals(self.state, run=run), now=self.now + 60)
        self.assertIsNone(monitor.choose_signal(self.state, self.config, now=self.now + 60))

    def test_ci_failure_fingerprint_changes_only_for_changed_evidence(self):
        self.state["jobs"]["a"] = {"id": "a", "created_at": 1, "issue_number": 7,
            "status": "investigation_finished", "result": {"status": "awaiting_ci",
                "pull_request_url": "https://github.com/garnser/codex-web/pull/8"}}
        pr = {"state": "OPEN", "isDraft": False, "headRefOid": "a" * 40,
              "statusCheckRollup": [{"name": "unit-static", "conclusion": "FAILURE"}]}
        def run(argv):
            return subprocess.CompletedProcess(argv, 0, json.dumps(pr), "")
        first = monitor.continuation_signals(self.state, run=run)[0]
        self.assertEqual(first.kind, "ci_failed")
        self.assertEqual(first.key, monitor.continuation_signals(self.state, run=run)[0].key)
        pr["headRefOid"] = "b" * 40
        self.assertNotEqual(first.key, monitor.continuation_signals(self.state, run=run)[0].key)

    def test_same_issue_is_limited_to_four_investigation_rounds(self):
        monitor.retain_signals(self.state, [monitor.Signal("continue:7", "ci_failed", {}, 7)], now=self.now)
        self.state["jobs"] = {str(i): {"created_at": 1, "issue_number": 7,
            "status": "investigation_finished"} for i in range(4)}
        self.assertIsNone(monitor.choose_signal(self.state, self.config, now=self.now))

    def test_tick_recovers_checkpoint_and_counts_one_sample_per_observation(self):
        self.config["automatic_investigations"] = False
        monitor.atomic_json(self.root / "config.json", self.config)
        snapshot = {"checked_at": self.now, "github_checked_at": self.now, "service": {"ActiveState": "inactive"},
            "disk_free": 9 * 1024**3, "memory_available": 4 * 1024**3, "bugs": []}
        with patch.object(monitor, "collect", return_value=snapshot), redirect_stdout(io.StringIO()):
            monitor.tick(self.root, now=self.now)
            self.assertEqual(monitor.load_state(self.root)["signals"]["host:web_service_inactive"]["samples"], 1)
            monitor.tick(self.root, now=self.now + 60)
        restored = monitor.load_state(self.root)
        self.assertEqual(restored["signals"]["host:web_service_inactive"]["samples"], 2)
        self.assertEqual(restored["jobs"], {})
        self.assertEqual(monitor.read_json(self.root / "summary.json")["service_state"], "inactive")

    def test_new_service_restart_is_not_conflated_with_initial_history(self):
        snapshot = self.sample()
        snapshot["service"]["NRestarts"] = "3"
        self.assertEqual(monitor.observe(snapshot, {}, now=self.now), [])
        previous = {"service": {"NRestarts": "2"}}
        self.assertEqual([s.kind for s in monitor.observe(snapshot, previous, now=self.now)], ["new_service_restart"])


if __name__ == "__main__":
    unittest.main()
