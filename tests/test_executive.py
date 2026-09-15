from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI

from codex_web.executive import AGENTS, ExecutiveStore, rank_agents, route_agent
from codex_web.executive_integration import MultiProviderExecutiveService, install_executive_integrated


class _Host:
    def __init__(self, data_dir: Path) -> None:
        self.DATA_DIR = data_dir


class ExecutiveRoutingTests(unittest.TestCase):
    def test_routes_architecture_to_cto(self) -> None:
        self.assertEqual(route_agent("Should we migrate our API and database architecture?").id, "cto")

    def test_onboarding_does_not_accidentally_trigger_board_keyword(self) -> None:
        ranked = rank_agents("How can we improve onboarding and activation?")
        self.assertEqual(ranked[0].id, "cpo")

    def test_explicit_handle_wins(self) -> None:
        self.assertEqual(route_agent("@cfo review this architecture investment").id, "cfo")

    def test_catalog_contains_saas_functions(self) -> None:
        expected = {"cto", "vp-engineering", "cpo", "cro", "cfo", "customer-success", "security"}
        self.assertTrue(expected.issubset(AGENTS))


class ExecutiveStoreTests(unittest.TestCase):
    def test_history_is_bounded_to_configured_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {"CODEX_WEB_EXECUTIVE_MAX_HISTORY": "4"},
            clear=False,
        ):
            store = ExecutiveStore(_Host(Path(temp_dir)))
            for index in range(6):
                store.append_history("session", "user", f"message-{index}")

            history = store.history("session")

        self.assertEqual(len(history), 4)
        self.assertEqual(history[0]["content"], "message-2")
        self.assertEqual(history[-1]["content"], "message-5")


class ExecutiveIntegrationTests(unittest.TestCase):
    def test_install_registers_routes_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app = FastAPI()
            host = _Host(Path(temp_dir))

            first = install_executive_integrated(app, host)
            second = install_executive_integrated(app, host)
            executive_paths = [
                path
                for path in app.openapi().get("paths", {})
                if path.startswith("/api/executive/")
            ]

        self.assertIs(first, second)
        self.assertEqual(len(executive_paths), 5)
        self.assertEqual(len(executive_paths), len(set(executive_paths)))

    def test_ollama_defaults_are_local_and_do_not_require_key_at_construction(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {"CODEX_WEB_EXECUTIVE_PROVIDER": "ollama"},
            clear=False,
        ):
            os.environ.pop("CODEX_WEB_EXECUTIVE_BASE_URL", None)
            os.environ.pop("CODEX_WEB_EXECUTIVE_MODEL", None)
            service = MultiProviderExecutiveService(_Host(Path(temp_dir)))

        self.assertEqual(service.provider, "ollama")
        self.assertEqual(service.base_url, "http://127.0.0.1:11434/v1")
        self.assertEqual(service.model, "gpt-oss:20b")


if __name__ == "__main__":
    unittest.main()
