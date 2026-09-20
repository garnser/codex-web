from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from codex_web.services.operator_ui import OperatorUiService
from codex_web.services.runtime_diagnostics import (
    RuntimeDiagnosticsService,
    RuntimeHealthService,
    StaticAssetVersionService,
)


class _Ready:
    @staticmethod
    def is_set() -> bool:
        return True


class _Proc:
    pid = 1234

    @staticmethod
    def poll():
        return None


class _Telemetry:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def snapshot(self):
        return {}

    def recent(self, _limit=80):
        return list(self.events)


class RuntimeDiagnosticsExtractionTests(unittest.TestCase):
    def test_static_asset_version_falls_back_to_mtime_outside_git(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            static = root / "static"
            static.mkdir()
            (static / "index.html").write_text("index")
            service = StaticAssetVersionService(static, root)

            version = service.version()

            self.assertTrue(version)
            self.assertTrue(version.split("-")[-1].isdigit())

    def test_runtime_health_preserves_core_health_shape(self) -> None:
        telemetry = _Telemetry()
        codex = SimpleNamespace(ready=_Ready(), proc=_Proc())
        bot_runtime = SimpleNamespace(
            fingerprints={},
            tasks={},
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
                "last_success_at": 1.0,
            },
        )

        health = service.health()

        self.assertTrue(health["ok"])
        self.assertEqual(health["codexPid"], 1234)
        self.assertEqual(health["runtimeConnections"], 0)
        self.assertEqual(health["runtimeStatus"], [])
        self.assertEqual(health["staleQueueThreads"], {})
        self.assertEqual(health["gitlabSyncLastSuccessAt"], 1.0)

    def test_diagnostic_snapshot_uses_public_connection_projection(self) -> None:
        telemetry = _Telemetry()
        connection = SimpleNamespace(
            id="conn-1",
            project_id="home",
        )
        bot_runtime = SimpleNamespace(
            tasks={},
            telemetry=telemetry,
        )
        policy = SimpleNamespace(
            owner_work_watchdog_interval=lambda: 600.0,
            release_gate_watchdog_interval=lambda: 300.0,
            work_item_sla_watchdog_interval=lambda: 120.0,
            orchestrator_watchdog_interval=lambda: 180.0,
            split_brain_watchdog_interval=lambda: 60.0,
        )
        supervisor = SimpleNamespace(task_status=lambda: {})
        service = RuntimeDiagnosticsService(
            version=lambda: "test",
            health=lambda: {"ok": True, "problems": []},
            codex=SimpleNamespace(
                ready=_Ready(),
                proc=_Proc(),
                last_error=None,
                pending_approvals={},
            ),
            bot_runtime=bot_runtime,
            runtime_policy=policy,
            supervisor=supervisor,
            thread_message_limit=lambda: 100,
            slack_provider_health=lambda: {},
            project_lookup=lambda _project_id: object(),
            load_projects=lambda: [],
            load_thread_index=lambda: [],
            load_active_turns=lambda: {},
            load_queues=lambda: {},
            queue_tasks={},
            load_connections=lambda: [connection],
            connection_public=lambda _connection: {
                "id": "conn-1",
                "bot_token": "supe...oken",
            },
            load_bindings=lambda: [],
            load_agent_presence=lambda: SimpleNamespace(
                model_dump=lambda: {"projects": {}}
            ),
            load_reply_targets=lambda: {},
            load_delivery_targets=lambda: {},
            load_work_item_states=lambda: {},
            work_item_public=lambda state: state,
            recent_events=lambda _limit: [],
        )

        snapshot = service.snapshot()

        self.assertEqual(
            snapshot["connections"][0]["bot_token"],
            "supe...oken",
        )
        self.assertNotIn("supersecret-token", repr(snapshot))

    def test_operator_ui_work_item_stats_are_repository_driven(self) -> None:
        states = {
            "open": SimpleNamespace(
                current_stage="implementation_active",
                handoff=None,
                release_gate=False,
            ),
            "handoff": SimpleNamespace(
                current_stage="ready_for_validation",
                handoff=SimpleNamespace(status="pending"),
                release_gate=True,
            ),
            "closed": SimpleNamespace(
                current_stage="closed",
                handoff=None,
                release_gate=False,
            ),
        }
        service = OperatorUiService(
            static_dir=Path("."),
            version=lambda: "test",
            health=lambda: {"ok": True, "problems": []},
            load_turn_queues=lambda: {},
            load_active_turns=lambda: {},
            load_work_item_states=lambda: states,
            event_hub=SimpleNamespace(),
        )

        stats = service.work_item_stats()

        self.assertEqual(stats["open_count"], 2)
        self.assertEqual(stats["pending_handoff_count"], 1)
        self.assertEqual(stats["release_gate_count"], 1)
        self.assertEqual(stats["implementation_active_count"], 1)
        self.assertEqual(stats["ready_for_validation_count"], 1)


if __name__ == "__main__":
    unittest.main()
