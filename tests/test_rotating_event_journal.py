from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from codex_web.services.rotating_journal import RotatingJsonlJournal


class _Clock:
    def __init__(self, value: float = 1_800_000_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class RotatingJsonlJournalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "bot_events.jsonl"
        self.clock = _Clock()
        self.journal = RotatingJsonlJournal(
            self.path,
            clock=self.clock,
        )

    @staticmethod
    def _event(index: int, **extra):
        return {
            "created_at": 1_700_000_000.0 + index,
            "type": "fixture",
            "index": index,
            **extra,
        }

    def test_size_threshold_requests_rotation_and_maintenance_bounds_active_file(self) -> None:
        payload = "x" * 600_000
        with patch.dict(
            os.environ,
            {
                "CODEX_WEB_BOT_EVENT_ROTATE_BYTES": str(
                    1024 * 1024
                ),
                "CODEX_WEB_BOT_EVENT_ROTATE_SECONDS": "86400",
            },
            clear=False,
        ):
            self.journal.append(self._event(1, payload=payload))
            self.journal.append(self._event(2, payload=payload))
            before = self.journal.status()
            self.assertTrue(before["rotationRequested"])
            self.assertGreater(
                before["activeSizeBytes"],
                1024 * 1024,
            )

            result = self.journal.maintain()

        self.assertIsNotNone(result["rotated"])
        status = self.journal.status()
        self.assertEqual(status["segmentCount"], 1)
        self.assertEqual(status["activeSizeBytes"], 0)
        self.assertFalse(status["rotationRequested"])
        self.assertEqual(
            [item["index"] for item in self.journal.recent(10)],
            [1, 2],
        )

    def test_age_threshold_rotates_without_requiring_append_at_threshold(self) -> None:
        self.journal.append(self._event(1))
        self.clock.advance(61)
        with patch.dict(
            os.environ,
            {
                "CODEX_WEB_BOT_EVENT_ROTATE_SECONDS": "60",
                "CODEX_WEB_BOT_EVENT_ROTATE_BYTES": str(
                    1024 * 1024 * 1024
                ),
            },
            clear=False,
        ):
            result = self.journal.maintain()

        self.assertIsNotNone(result["rotated"])
        self.assertEqual(
            self.journal.status()["segmentCount"],
            1,
        )

    def test_recent_crosses_rotation_boundary_in_chronological_order(self) -> None:
        for index in range(3):
            self.journal.append(self._event(index))
        self.journal.rotate_if_needed(force=True)
        for index in range(3, 6):
            self.journal.append(self._event(index))

        values = self.journal.recent(5)

        self.assertEqual(
            [item["index"] for item in values],
            [1, 2, 3, 4, 5],
        )

    def test_concurrent_append_and_rotation_loses_no_acknowledged_event(self) -> None:
        total = 150
        barrier = threading.Barrier(5)

        def writer(offset: int) -> None:
            barrier.wait()
            for index in range(offset, total, 4):
                self.journal.append(self._event(index))

        def rotator() -> None:
            barrier.wait()
            for _ in range(5):
                with contextlib.suppress(Exception):
                    self.journal.rotate_if_needed(force=True)
                time.sleep(0.001)

        import contextlib

        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = [
                pool.submit(writer, offset)
                for offset in range(4)
            ]
            futures.append(pool.submit(rotator))
            for future in futures:
                future.result(timeout=10)

        values = self.journal.recent(300)
        indices = [int(item["index"]) for item in values]

        self.assertEqual(len(indices), total)
        self.assertEqual(len(set(indices)), total)
        self.assertEqual(set(indices), set(range(total)))

    def test_recent_and_rotation_share_one_consistent_segment_view(self) -> None:
        self.journal.append(self._event(1))
        entered = threading.Event()
        release = threading.Event()
        original = self.journal._read_plain_tail

        def blocking_read(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(timeout=5))
            return original(*args, **kwargs)

        with patch.object(
            self.journal,
            "_read_plain_tail",
            side_effect=blocking_read,
        ), ThreadPoolExecutor(max_workers=2) as pool:
            recent_future = pool.submit(self.journal.recent, 10)
            self.assertTrue(entered.wait(timeout=5))
            rotate_future = pool.submit(
                self.journal.rotate_if_needed,
                force=True,
            )
            time.sleep(0.02)
            self.assertFalse(rotate_future.done())
            release.set()
            values = recent_future.result(timeout=5)
            rotate_future.result(timeout=5)

        self.assertEqual(
            [item["index"] for item in values],
            [1],
        )
        self.journal.append(self._event(2))
        self.assertEqual(
            [item["index"] for item in self.journal.recent(10)],
            [1, 2],
        )

    def test_restart_recovers_segment_after_crash_between_rename_and_manifest(self) -> None:
        self.journal.append(self._event(1))
        with self.assertRaisesRegex(
            RuntimeError,
            "after rename",
        ):
            self.journal.rotate_if_needed(
                force=True,
                fail_at="after_rename",
            )

        restarted = RotatingJsonlJournal(
            self.path,
            clock=self.clock,
        )
        restarted.append(self._event(2))
        restarted.maintain()

        self.assertEqual(
            [item["index"] for item in restarted.recent(10)],
            [1, 2],
        )
        status = restarted.status()
        self.assertGreaterEqual(status["segmentCount"], 1)
        self.assertGreaterEqual(status["rotationFailures"], 1)

    def test_restart_recovers_manifest_committed_rotation_without_duplicate(self) -> None:
        self.journal.append(self._event(1))
        with self.assertRaisesRegex(
            RuntimeError,
            "after manifest",
        ):
            self.journal.rotate_if_needed(
                force=True,
                fail_at="after_manifest",
            )

        restarted = RotatingJsonlJournal(
            self.path,
            clock=self.clock,
        )
        restarted.append(self._event(2))

        values = restarted.recent(10)
        self.assertEqual(
            [item["index"] for item in values],
            [1, 2],
        )

    def test_compressed_segment_tail_remains_visible_across_boundary(self) -> None:
        with patch.dict(
            os.environ,
            {
                "CODEX_WEB_BOT_EVENT_COMPRESS": "1",
                "CODEX_WEB_BOT_EVENT_HOT_SEGMENTS": "1",
                "CODEX_WEB_BOT_EVENT_ARCHIVE_MAX_SEGMENTS_PER_CYCLE": "2",
            },
            clear=False,
        ):
            for index in range(3):
                self.journal.append(self._event(index))
            self.journal.rotate_if_needed(force=True)
            for index in range(3, 6):
                self.journal.append(self._event(index))
            self.journal.rotate_if_needed(force=True)
            for index in range(6, 8):
                self.journal.append(self._event(index))

            result = self.journal.maintain()
            values = self.journal.recent(7)
            status = self.journal.status()

        self.assertGreaterEqual(len(result["archived"]), 1)
        self.assertGreaterEqual(status["archiveCount"], 1)
        self.assertEqual(
            [item["index"] for item in values],
            [1, 2, 3, 4, 5, 6, 7],
        )

    def test_archive_crash_before_manifest_discards_uncommitted_archive_on_restart(self) -> None:
        with patch.dict(
            os.environ,
            {"CODEX_WEB_BOT_EVENT_COMPRESS": "1"},
            clear=False,
        ):
            self.journal.append(self._event(1))
            segment = self.journal.rotate_if_needed(force=True)
            assert segment is not None

            with self.assertRaisesRegex(
                RuntimeError,
                "after archive files",
            ):
                self.journal._compress_segment(
                    segment,
                    fail_at="after_archive_files",
                )

            restarted = RotatingJsonlJournal(
                self.path,
                clock=self.clock,
            )
            status = restarted.status()
            values = restarted.recent(10)

        self.assertEqual([item["index"] for item in values], [1])
        self.assertEqual(status["segmentCount"], 1)
        segment_files = list(
            (self.root / "bot_events.segments").glob(
                f"{segment.id}.jsonl*"
            )
        )
        self.assertTrue(
            any(path.name == f"{segment.id}.jsonl" for path in segment_files)
        )
        self.assertFalse(
            any(path.name.endswith(".jsonl.gz") for path in segment_files)
        )
        self.assertFalse(
            any(path.name.endswith(".tail.jsonl") for path in segment_files)
        )

    def test_archive_crash_after_manifest_recovers_and_removes_duplicate_source(self) -> None:
        with patch.dict(
            os.environ,
            {"CODEX_WEB_BOT_EVENT_COMPRESS": "1"},
            clear=False,
        ):
            self.journal.append(self._event(1))
            segment = self.journal.rotate_if_needed(force=True)
            assert segment is not None

            with self.assertRaisesRegex(
                RuntimeError,
                "after archive manifest",
            ):
                self.journal._compress_segment(
                    segment,
                    fail_at="after_archive_manifest",
                )

            restarted = RotatingJsonlJournal(
                self.path,
                clock=self.clock,
            )
            status = restarted.status()
            values = restarted.recent(10)

        self.assertEqual([item["index"] for item in values], [1])
        self.assertEqual(status["segmentCount"], 1)
        segment_files = list(
            (self.root / "bot_events.segments").glob(
                f"{segment.id}.jsonl*"
            )
        )
        self.assertTrue(
            any(path.name.endswith(".jsonl.gz") for path in segment_files)
        )
        self.assertFalse(
            any(path.name == f"{segment.id}.jsonl" for path in segment_files)
        )

    def test_retention_removes_old_operational_segments_but_preserves_protected(self) -> None:
        with patch.dict(
            os.environ,
            {
                "CODEX_WEB_BOT_EVENT_RETENTION_SEGMENTS": "2",
                "CODEX_WEB_BOT_EVENT_MIN_SEGMENTS": "1",
                "CODEX_WEB_BOT_EVENT_MIN_RETENTION_SECONDS": "300",
                "CODEX_WEB_BOT_EVENT_RETENTION_DAYS": "3650",
                "CODEX_WEB_BOT_EVENT_RETENTION_BYTES": str(
                    64 * 1024 * 1024 * 1024
                ),
                "CODEX_WEB_BOT_EVENT_PROTECTED_RETENTION_DAYS": "0",
            },
            clear=False,
        ):
            self.journal.append(self._event(1))
            protected = self.journal.rotate_if_needed(force=True)
            assert protected is not None
            # Mark the first closed segment as protected by replacing its event
            # with a canonical/audit-class record and re-indexing.
            protected_path = (
                self.root
                / "bot_events.segments"
                / protected.path
            )
            protected_path.write_text(
                json.dumps(
                    self._event(
                        1,
                        retention_class="audit",
                    ),
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
            with self.journal._lock:
                self.journal._manifest.segments = [
                    item.model_copy(update={"indexed": False})
                    if item.id == protected.id
                    else item
                    for item in self.journal._manifest.segments
                ]
                self.journal._write_manifest_locked()
            self.journal._index_unindexed_segments()

            self.clock.advance(301)
            for index in (2, 3):
                self.journal.append(self._event(index))
                self.journal.rotate_if_needed(force=True)
                self.clock.advance(301)

            result = self.journal.maintain()
            status = self.journal.status()

        remaining = {
            item.id
            for item in self.journal._manifest.segments
        }
        self.assertIn(protected.id, remaining)
        self.assertGreaterEqual(len(result["deleted"]), 1)
        self.assertGreaterEqual(
            status["protectedSegments"],
            1,
        )
        self.assertGreaterEqual(len(status["lastCleanup"]), 1)
        self.assertIn(
            status["lastCleanup"][-1]["reason"],
            {"age", "count", "bytes"},
        )

    async def test_slow_compression_storage_does_not_block_event_loop(self) -> None:
        with patch.dict(
            os.environ,
            {
                "CODEX_WEB_BOT_EVENT_COMPRESS": "1",
                "CODEX_WEB_BOT_EVENT_HOT_SEGMENTS": "1",
            },
            clear=False,
        ):
            self.journal.append(self._event(1))
            self.journal.rotate_if_needed(force=True)
            self.journal.append(self._event(2))
            self.journal.rotate_if_needed(force=True)

            original = self.journal._compress_segment

            def slow(segment, **kwargs):
                time.sleep(0.15)
                return original(segment, **kwargs)

            with patch.object(
                self.journal,
                "_compress_segment",
                side_effect=slow,
            ):
                task = asyncio.create_task(
                    asyncio.to_thread(self.journal.maintain)
                )
                started = time.perf_counter()
                await asyncio.sleep(0.01)
                elapsed = time.perf_counter() - started
                await task

        self.assertLess(elapsed, 0.08)

    def test_recent_has_total_byte_budget_across_malformed_segments(self) -> None:
        malformed = b"not-json\n" * 700_000
        self.path.write_bytes(malformed)
        self.journal.rotate_if_needed(force=True)
        self.path.write_bytes(malformed)

        values = self.journal.recent(
            300,
            chunk_size=64 * 1024,
            max_bytes=2 * 1024 * 1024,
        )
        metrics = self.journal.recent_metrics()

        self.assertEqual(values, [])
        self.assertLessEqual(
            metrics["bytesRead"],
            2 * 1024 * 1024,
        )

    def test_status_is_metadata_only_for_indexed_segments(self) -> None:
        self.journal.append(self._event(1))
        self.journal.rotate_if_needed(force=True)

        segment = self.journal._manifest.segments[0]
        path = self.root / "bot_events.segments" / segment.path
        with patch(
            "builtins.open",
            side_effect=AssertionError("status must not open event payloads"),
        ):
            status = self.journal.status()

        self.assertEqual(status["segmentCount"], 1)
        self.assertGreaterEqual(status["totalRetainedBytes"], path.stat().st_size)


if __name__ == "__main__":
    unittest.main()
