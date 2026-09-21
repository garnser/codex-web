from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from codex_web.services.bot_runtime_telemetry import BotRuntimeTelemetry


class BotRuntimeTelemetryTailTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "bot_events.jsonl"
        self.telemetry = BotRuntimeTelemetry(events_file=self.path)

    @staticmethod
    def _event(index: int, *, payload: str | None = None) -> dict:
        return {
            "created_at": float(index),
            "type": "fixture",
            "index": index,
            "payload": payload or f"value-{index}",
        }

    def _write_lines(self, lines: list[str], *, final_newline: bool = True) -> None:
        text = "\n".join(lines)
        if final_newline:
            text += "\n"
        self.path.write_text(text, encoding="utf-8")

    def test_returns_final_valid_events_in_chronological_order(self) -> None:
        lines = [
            json.dumps(self._event(index), separators=(",", ":"))
            for index in range(20)
        ]
        self._write_lines(lines)

        values = self.telemetry.recent(5, chunk_size=128)

        self.assertEqual(
            [item["index"] for item in values],
            [15, 16, 17, 18, 19],
        )

    def test_file_without_final_newline_is_supported(self) -> None:
        lines = [
            json.dumps(self._event(index), separators=(",", ":"))
            for index in range(8)
        ]
        self._write_lines(lines, final_newline=False)

        values = self.telemetry.recent(3, chunk_size=64)

        self.assertEqual(
            [item["index"] for item in values],
            [5, 6, 7],
        )

    def test_malformed_tail_record_does_not_reduce_valid_event_limit(self) -> None:
        lines = [
            json.dumps(self._event(index), separators=(",", ":"))
            for index in range(7)
        ]
        lines.insert(6, "{not-json")
        self._write_lines(lines)

        values = self.telemetry.recent(3, chunk_size=64)

        self.assertEqual(
            [item["index"] for item in values],
            [4, 5, 6],
        )
        metrics = self.telemetry.recent_metrics()
        self.assertGreater(metrics["linesConsidered"], 3)
        self.assertEqual(metrics["validEvents"], 3)

    def test_single_record_larger_than_chunk_is_supported(self) -> None:
        huge = "x" * 20_000
        lines = [
            json.dumps(self._event(1), separators=(",", ":")),
            json.dumps(
                self._event(2, payload=huge),
                separators=(",", ":"),
            ),
            json.dumps(self._event(3), separators=(",", ":")),
        ]
        self._write_lines(lines)

        values = self.telemetry.recent(2, chunk_size=1024)

        self.assertEqual([item["index"] for item in values], [2, 3])
        self.assertEqual(values[0]["payload"], huge)
        self.assertGreater(
            self.telemetry.recent_metrics()["chunksRead"],
            1,
        )

    def test_limit_is_clamped_to_existing_one_to_three_hundred_contract(self) -> None:
        lines = [
            json.dumps(self._event(index), separators=(",", ":"))
            for index in range(350)
        ]
        self._write_lines(lines)

        low = self.telemetry.recent(0)
        high = self.telemetry.recent(999)

        self.assertEqual(len(low), 1)
        self.assertEqual(low[0]["index"], 349)
        self.assertEqual(len(high), 300)
        self.assertEqual(high[0]["index"], 50)
        self.assertEqual(high[-1]["index"], 349)

    def test_small_tail_request_does_not_read_large_journal(self) -> None:
        # Roughly 8 MiB: large enough that a forward full-file scan is
        # observable while remaining cheap for CI.
        padding = "x" * 800
        lines = [
            json.dumps(
                self._event(index, payload=padding),
                separators=(",", ":"),
            )
            for index in range(10_000)
        ]
        self._write_lines(lines)
        file_size = self.path.stat().st_size
        self.assertGreater(file_size, 5 * 1024 * 1024)

        values = self.telemetry.recent(10, chunk_size=64 * 1024)
        metrics = self.telemetry.recent_metrics()

        self.assertEqual(
            [item["index"] for item in values],
            list(range(9990, 10_000)),
        )
        self.assertEqual(metrics["fileSize"], file_size)
        self.assertLess(
            metrics["bytesRead"],
            256 * 1024,
            (
                "small recent() request read too much of the journal: "
                f"{metrics}"
            ),
        )
        self.assertLess(
            metrics["bytesRead"],
            file_size // 10,
        )

    def test_missing_file_returns_empty_metrics(self) -> None:
        self.assertEqual(self.telemetry.recent(10), [])
        self.assertEqual(
            self.telemetry.recent_metrics(),
            {
                "bytesRead": 0,
                "chunksRead": 0,
                "linesConsidered": 0,
                "validEvents": 0,
                "fileSize": 0,
            },
        )


if __name__ == "__main__":
    unittest.main()
