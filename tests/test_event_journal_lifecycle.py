from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from codex_web.services.event_journal import EventJournal


class _Clock:
    def __init__(self, value: float = 1_800_000_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class EventJournalLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "bot_events.jsonl"
        self.clock = _Clock()

    def _journal(self) -> EventJournal:
        return EventJournal(
            self.path,
            archive_directory=self.root / "archive",
            clock=self.clock,
        )

    def test_explicit_rotation_preserves_recent_order_and_cursor_sequence(self) -> None:
        journal = self._journal()
        first = [
            journal.append({"type": "fixture", "index": index})
            for index in range(5)
        ]
        segment = journal.rotate()
        self.assertIsNotNone(segment)
        self.clock.advance(1)
        second = [
            journal.append({"type": "fixture", "index": index})
            for index in range(5, 10)
        ]

        recent = journal.recent(8, chunk_size=128)

        self.assertEqual(
            [item["index"] for item in recent],
            list(range(2, 10)),
        )
        first_sequence = int(
            first[-1]["journal_cursor"].split(":", 1)[0]
        )
        second_sequence = int(
            second[0]["journal_cursor"].split(":", 1)[0]
        )
        self.assertEqual(second_sequence, first_sequence + 1)
        self.assertEqual(
            len(
                {
                    item["journal_cursor"]
                    for item in [*first, *second]
                }
            ),
            10,
        )

    def test_size_rotation_bounds_active_file(self) -> None:
        journal = self._journal()
        with patch.object(
            EventJournal,
            "max_active_bytes",
            return_value=512,
        ):
            for index in range(20):
                journal.append(
                    {
                        "type": "fixture",
                        "index": index,
                        "payload": "x" * 100,
                    }
                )

        status = journal.status()
        self.assertGreater(status["segmentCount"], 0)
        self.assertLessEqual(status["activeBytes"], 512)

    def test_age_rotation_occurs_during_maintenance_without_new_append(self) -> None:
        journal = self._journal()
        journal.append({"type": "fixture", "index": 1})
        self.clock.advance(20)

        with patch.object(
            EventJournal,
            "max_active_age_seconds",
            return_value=10.0,
        ):
            result = journal.maintain_once()

        self.assertIsNotNone(result["rotated"])
        self.assertEqual(journal.status()["activeBytes"], 0)

    def test_concurrent_appends_are_not_lost_or_duplicated(self) -> None:
        journal = self._journal()

        def append(index: int) -> dict:
            return journal.append(
                {
                    "type": "concurrent",
                    "index": index,
                    "payload": "x" * 32,
                }
            )

        with ThreadPoolExecutor(max_workers=12) as pool:
            values = list(pool.map(append, range(200)))

        lines = [
            json.loads(line)
            for line in self.path.read_text().splitlines()
            if line.strip()
        ]
        self.assertEqual(len(lines), 200)
        self.assertEqual(
            {item["index"] for item in lines},
            set(range(200)),
        )
        self.assertEqual(
            len({item["journal_cursor"] for item in values}),
            200,
        )

    def test_restart_recovers_segment_renamed_before_manifest_update(self) -> None:
        journal = self._journal()
        journal.append({"type": "fixture", "index": 1})
        sequence = journal.status()["activeSequence"]
        segment_name = journal._segment_name(
            sequence,
            self.clock(),
        )
        segment_path = self.root / segment_name

        os.replace(self.path, segment_path)
        self.path.touch()
        journal.manifest_file.unlink()

        recovered = self._journal()
        status = recovered.status()

        self.assertEqual(status["segmentCount"], 1)
        self.assertEqual(
            [item["index"] for item in recovered.recent(5)],
            [1],
        )

    def test_restart_removes_stale_compression_temp_without_losing_source(self) -> None:
        journal = self._journal()
        journal.append({"type": "fixture", "index": 1})
        segment = journal.rotate()
        assert segment is not None

        archive = self.root / "archive"
        archive.mkdir()
        temporary = archive / f"{segment.filename}.gz.tmp"
        temporary.write_bytes(b"partial archive")

        recovered = self._journal()

        self.assertFalse(temporary.exists())
        self.assertTrue(
            (self.root / segment.filename).exists()
        )
        self.assertEqual(
            [item["index"] for item in recovered.recent(5)],
            [1],
        )

    def test_archive_compression_runs_for_non_hot_closed_segment(self) -> None:
        journal = self._journal()
        journal.append({"type": "fixture", "index": 1})
        first = journal.rotate()
        assert first is not None
        self.clock.advance(1)
        journal.append({"type": "fixture", "index": 2})
        second = journal.rotate()
        assert second is not None

        with patch.object(
            EventJournal,
            "hot_segments",
            return_value=1,
        ):
            result = journal.maintain_once()

        self.assertIsNotNone(result["archived"])
        status = journal.status()
        self.assertEqual(status["archivedSegmentCount"], 1)
        self.assertTrue(
            any((self.root / "archive").glob("*.gz"))
        )

    def test_retention_never_deletes_protected_or_unknown_segments(self) -> None:
        journal = self._journal()

        journal.append(
            {
                "type": "security.audit",
                "retention_class": "audit",
                "index": 1,
            }
        )
        protected = journal.rotate()
        assert protected is not None
        self.clock.advance(1)

        journal.append({"type": "fixture", "index": 2})
        ordinary = journal.rotate()
        assert ordinary is not None
        self.clock.advance(1)

        journal.append({"type": "fixture", "index": 3})
        hot = journal.rotate()
        assert hot is not None
        self.clock.advance(100)

        # First pass indexes the protected segment. Second indexes ordinary.
        journal.maintain_once()
        journal.maintain_once()

        with (
            patch.object(
                EventJournal,
                "retention_seconds",
                return_value=0.0,
            ),
            patch.object(
                EventJournal,
                "retention_max_segments",
                return_value=2,
            ),
            patch.object(
                EventJournal,
                "hot_segments",
                return_value=1,
            ),
            patch.object(
                EventJournal,
                "compression_enabled",
                return_value=False,
            ),
        ):
            result = journal.maintain_once()

        remaining = {
            item.sequence
            for item in journal._manifest.segments
        }
        self.assertIn(protected.sequence, remaining)
        self.assertNotIn(ordinary.sequence, remaining)
        self.assertIn(hot.sequence, remaining)
        self.assertIn(ordinary.id, result["removed"])
        self.assertGreaterEqual(
            journal.status()["protectedSegmentCount"],
            1,
        )

    def test_unknown_segment_protection_blocks_cleanup_until_indexed(self) -> None:
        legacy = {
            "created_at": self.clock(),
            "type": "fixture",
            "index": 1,
        }
        self.path.write_text(
            json.dumps(legacy) + "\n",
            encoding="utf-8",
        )
        journal = self._journal()
        segment = journal.rotate()
        assert segment is not None
        self.assertFalse(segment.indexed)

        with (
            patch.object(
                EventJournal,
                "retention_seconds",
                return_value=0.0,
            ),
            patch.object(
                EventJournal,
                "retention_max_segments",
                return_value=2,
            ),
            patch.object(
                EventJournal,
                "compression_enabled",
                return_value=False,
            ),
        ):
            # The same maintenance pass indexes before cleanup, so force an
            # unknown manifest record and assert the candidate filter itself
            # fails closed.
            segment.indexed = False
            segment.protected_records = None
            candidates = journal._retention_candidates(
                self.clock() + 1000
            )

        self.assertEqual(candidates, [])

    async def test_slow_archive_work_offloaded_does_not_starve_event_loop(self) -> None:
        journal = self._journal()
        journal.append({"type": "fixture", "index": 1})
        journal.rotate()
        self.clock.advance(1)
        journal.append({"type": "fixture", "index": 2})
        journal.rotate()

        original = journal._compress_and_archive

        def slow_archive(segment):
            time.sleep(0.15)
            return original(segment)

        with (
            patch.object(
                journal,
                "_compress_and_archive",
                side_effect=slow_archive,
            ),
            patch.object(
                EventJournal,
                "hot_segments",
                return_value=1,
            ),
        ):
            task = asyncio.create_task(
                asyncio.to_thread(journal.maintain_once)
            )
            started = time.perf_counter()
            await asyncio.sleep(0.01)
            elapsed = time.perf_counter() - started
            await task

        self.assertLess(elapsed, 0.08)

    def test_cursor_remains_monotonic_through_multiple_rotations(self) -> None:
        journal = self._journal()
        cursors = []
        for index in range(4):
            value = journal.append(
                {"type": "fixture", "index": index}
            )
            cursors.append(value["journal_cursor"])
            journal.rotate()
            self.clock.advance(1)

        sequences = [
            int(cursor.split(":", 1)[0])
            for cursor in cursors
        ]
        self.assertEqual(sequences, sorted(sequences))
        self.assertEqual(len(set(sequences)), 4)


if __name__ == "__main__":
    unittest.main()
