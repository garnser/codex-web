from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codex_web.services.bot_runtime_telemetry import BotRuntimeTelemetry
from codex_web.services.runtime_diagnostics import (
    RuntimeHealthService,
    StaticAssetVersionService,
)


class StaticAssetVersionServiceTests(unittest.TestCase):
    def test_version_combines_git_revision_and_latest_asset_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            static = root / "static"
            static.mkdir()
            index = static / "index.html"
            app = static / "app.js"
            index.write_text("<html></html>")
            app.write_text("console.log('ok')")
            with patch(
                "codex_web.services.runtime_diagnostics.subprocess.check_output",
                return_value="abc123\n",
            ):
                version = StaticAssetVersionService(
                    static,
                    root,
                ).version()

            self.assertTrue(version.startswith("abc123-"))
            self.assertEqual(
                version.split("-", 1)[1],
                str(int(max(index.stat().st_mtime, app.stat().st_mtime))),
            )

    def test_version_hot_path_is_cached_after_construction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            static = root / "static"
            static.mkdir()
            (static / "index.html").write_text("<html></html>")
            with patch(
                "codex_web.services.runtime_diagnostics.subprocess.check_output",
                return_value="abc123\n",
            ) as git:
                service = StaticAssetVersionService(static, root)
                first = service.version()
                second = service.version()
                third = service.version()

            self.assertEqual(first, second)
            self.assertEqual(second, third)
            self.assertEqual(git.call_count, 1)


class BotRuntimeTelemetryDiagnosticsTests(unittest.TestCase):
    def test_recent_activity_and_event_counts_use_event_journal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            events_file = Path(directory) / "events.jsonl"
            events_file.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "created_at": 80.0,
                                "type": "turn_started",
                                "thread_id": "thread-1",
                            }
                        ),
                        json.dumps(
                            {
                                "created_at": 90.0,
                                "type": "owner_work_watchdog_dispatched",
                                "thread_id": "thread-1",
                            }
                        ),
                    ]
                )
                + "\n"
            )
            telemetry = BotRuntimeTelemetry(events_file=events_file)

            self.assertEqual(
                telemetry.thread_recent_activity_age_seconds(
                    "thread-1",
                    now=100.0,
                ),
                10.0,
            )
            self.assertEqual(
                telemetry.thread_recent_event_count(
                    "thread-1",
                    {"owner_work_watchdog_dispatched"},
                    within_seconds=30.0,
                    now=100.0,
                ),
                1,
            )


class RuntimeHealthServiceTests(unittest.TestCase):
    def test_healthy_snapshot_preserves_public_schema(self) -> None:
        ready = asyncio.Event()
        ready.set()
        proc = SimpleNamespace(pid=42, poll=lambda: None)
        codex = SimpleNamespace(proc=proc, ready=ready)
        task = SimpleNamespace(done=lambda: False)
        bot_runtime = SimpleNamespace(
            fingerprints={"connection-1": "fingerprint"},
            tasks={"connection-1": task},
        )

        with tempfile.TemporaryDirectory() as directory:
            telemetry = BotRuntimeTelemetry(
                events_file=Path(directory) / "events.jsonl",
                status={
                    "connection-1": {
                        "connectionId": "connection-1",
                        "status": "connected",
                    }
                },
            )
            service = RuntimeHealthService(
                codex=codex,
                bot_runtime=bot_runtime,
                telemetry=telemetry,
                load_bindings=lambda: [],
                terminal_failures={},
                terminal_recovery_tasks={},
                terminal_failure_window_seconds=lambda: 300.0,
                load_queues=lambda: {},
                slack_provider_health=lambda: {
                    "cooldownRemainingSeconds": 0.0,
                    "rateLimitFailures": 0,
                },
                gitlab_sync_status=lambda: {
                    "consecutive_failures": 0,
                    "last_error": None,
                    "last_error_at": 0.0,
                    "last_success_at": 10.0,
                },
            )

            snapshot = service.refresh_sync()

        self.assertTrue(snapshot["ok"])
        self.assertTrue(snapshot["codexReady"])
        self.assertEqual(snapshot["codexPid"], 42)
        self.assertEqual(snapshot["runtimeConnections"], 1)
        self.assertEqual(snapshot["recentDeliveryFailures"], 0)
        self.assertEqual(snapshot["gitlabSyncConsecutiveFailures"], 0)


if __name__ == "__main__":
    unittest.main()
