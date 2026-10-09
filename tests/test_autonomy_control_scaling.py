from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_web.autonomy import AutonomyControl, AutonomyCycleRecord, AutonomyMode, AutonomyState
from codex_web.storage.autonomy import AutonomyStateStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class AutonomyControlScalingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.sqlite = SQLiteStateStore(Path(self.temp.name) / "state.db")
        self.store = AutonomyStateStore(self.sqlite)

    def test_control_reads_do_not_decode_five_thousand_cycles(self):
        cycles = [AutonomyCycleRecord(cycle_key=f"cycle-{i}", event_id=f"evt-{i}",
                    event_type="test.event", source="test", recursion_depth=0,
                    reasoning_score=0, outcome="deterministic", reason="known") for i in range(5000)]
        state = AutonomyState(cycles=cycles, control=AutonomyControl(mode=AutonomyMode.PAUSED))
        self.sqlite.put(self.store.namespace, state.model_dump(mode="json"))
        self.assertEqual(self.store.control(), state.control)
        with patch.object(self.sqlite, "get", side_effect=AssertionError("whole document")), \
             patch.object(self.sqlite, "record_items", side_effect=AssertionError("whole history")), \
             patch.object(self.sqlite, "record_get", wraps=self.sqlite.record_get) as read:
            for _ in range(20):
                self.assertEqual(self.store.control(), state.control)
            self.assertEqual(self.store.active_break_glass_grants(
                organization_id="org", workspace_id="workspace", now=1), ())
        self.assertEqual({call.args[1] for call in read.call_args_list},
                         {"schema_version", "control", "break_glass_grants"})
        self.assertEqual(len(self.store.load().cycles), 5000)

    def test_another_writer_updates_the_next_control_read(self):
        self.store.control()
        other = AutonomyStateStore(self.sqlite)
        other.set_control(AutonomyControl(mode=AutonomyMode.KILLED), actor_id="operator")
        self.assertEqual(self.store.control().mode, AutonomyMode.KILLED)

    def test_keyed_control_is_paused_by_existing_recovery_sanitizer(self):
        from codex_web.services.recovery import RecoveryService
        self.store.control()
        restored = RecoveryService._sanitize_restored_documents(self.sqlite.documents())
        self.assertEqual(restored["autonomy"]["control"]["mode"], "paused")

    def test_legacy_schema_migrates_and_retains_history(self):
        self.sqlite.put(self.store.namespace, {"schema_version": "1.0", "cycles": [],
                                               "control": {"mode": "paused"}})
        self.assertEqual(self.store.control().mode, AutonomyMode.PAUSED)
        self.assertEqual(self.store.load().schema_version, "2.0")

    def test_unsupported_schema_fails_closed(self):
        self.sqlite.put(self.store.namespace, {"schema_version": "99", "control": {}})
        with self.assertRaises(ValueError):
            self.store.control()
        self.assertFalse(self.sqlite.record_collection_exists(self.store.namespace))
