from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from fastapi import Request

import server


class _ReadyFlag:
    def __init__(self, ready: bool) -> None:
        self.ready = ready

    def is_set(self) -> bool:
        return self.ready


class _RunningProcess:
    pid = 1234

    def poll(self) -> None:
        return None


class DaemonHealthTests(unittest.TestCase):
    def test_running_unready_codex_process_is_unhealthy(self) -> None:
        codex = SimpleNamespace(proc=_RunningProcess(), ready=_ReadyFlag(False))
        bot_runtime = SimpleNamespace(fingerprints={}, tasks={})

        with patch.object(server, "codex", codex):
            with patch.object(server, "bot_runtime", bot_runtime):
                with patch.object(server, "BOT_RUNTIME_STATUS", {}):
                    health = server._daemon_health()

        self.assertFalse(health["ok"])
        self.assertIn("codex app-server is not ready", health["problems"])
        self.assertFalse(health["codexReady"])


class DevHealthRouteTests(unittest.TestCase):
    def test_devhealth_renders_human_readable_sections(self) -> None:
        payload = {
            "ok": False,
            "problems": ["codex app-server process is not running"],
            "codexReady": False,
            "codexPid": None,
            "runtimeConnections": 2,
            "runtimeStatus": [
                {"connectionId": "slack-main", "provider": "slack", "status": "connected", "updatedAt": "2026-06-19T10:00:00Z"},
                {"connectionId": "telegram-alerts", "provider": "telegram", "status": "error", "updatedAt": "2026-06-19T10:02:00Z"},
            ],
        }
        status_context = {
            "canonical_items": [{"ref": "veridataops/platform#10", "owner": "james", "stage": "implementation_active", "findings": ["canonical next_action missing"]}],
            "split_brain_items": [{"ref": "veridataops/platform#11", "owner": "quinn", "stage": "ready_for_validation", "findings": ["conflicting owner labels"]}],
            "release": {
                "aligned_count": 1,
                "drift_count": 1,
                "items": [
                    {
                        "component": "saas-app",
                        "latest_valid_tag": "app-v2026.06.19-2",
                        "latest_any_tag": "app-v2026.06.19-2",
                        "main_sha": "abc123",
                        "status": {"label": "Aligned", "tone": "ok"},
                    }
                ],
            },
        }
        request = Request({"type": "http", "query_string": b"refresh=1", "headers": []})
        work_item_stats = {
            "open_count": 7,
            "blocked_count": 2,
            "pending_handoff_count": 1,
            "release_gate_count": 1,
            "ready_for_validation_count": 2,
            "implementation_active_count": 3,
        }
        with patch("server._daemon_health", return_value=payload):
            with patch("server._load_active_turns", return_value=[object(), object()]):
                with patch("server._load_turn_queues", return_value={"james": [object()], "quinn": [object(), object()]}):
                    with patch("server.build_devstatus_context", return_value=status_context):
                        with patch("server._devhealth_work_item_stats", return_value=work_item_stats):
                            response = asyncio.run(server.devhealth(request))

        self.assertEqual(response.status_code, 200)
        body = response.body.decode("utf-8")
        self.assertIn("VeridataOps Dev Health", body)
        self.assertIn("Daemon Status", body)
        self.assertIn("Runtime Connections", body)
        self.assertIn("Active Turns", body)
        self.assertIn("Queued Turns", body)
        self.assertIn("Work Item Stats", body)
        self.assertIn("Open Work Items", body)
        self.assertIn("Pending Handoffs", body)
        self.assertIn("Ready for Validation", body)
        self.assertIn("Implementation Active", body)
        self.assertIn("Divergence and Split Brain", body)
        self.assertIn("Current Release Prod Cuts", body)
        self.assertIn("Live refresh", body)
        self.assertIn("slack-main", body)
        self.assertIn("telegram-alerts", body)
        self.assertIn("app-v2026.06.19-2", body)
        self.assertIn(">7<", body)
        self.assertIn(">2<", body)
        self.assertNotIn(json.dumps(payload), body)

    def test_api_healthz_remains_machine_json(self) -> None:
        payload = {
            "ok": False,
            "problems": ["bot runtime task stopped for: slack-main"],
            "codexReady": False,
            "codexPid": 1234,
            "runtimeConnections": 1,
            "runtimeStatus": [{"connectionId": "slack-main", "status": "error"}],
        }
        with patch("server._daemon_health", return_value=payload):
            with self.assertRaises(HTTPException) as exc_info:
                asyncio.run(server.healthz())

        self.assertEqual(exc_info.exception.status_code, 503)
        self.assertEqual(exc_info.exception.detail, payload)
