from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from codex_web.models import BotConnection, BotReplyTarget, QueuedTurn
from codex_web.storage.operational_state import OperationalStateRepositories, install_operational_state
from codex_web.storage.sqlite_state import SQLiteStateStore


class OperationalStateTests(unittest.TestCase):
    def _repositories(self, root: Path) -> OperationalStateRepositories:
        return OperationalStateRepositories(
            SQLiteStateStore(root / "codex-web.db"),
            turn_queue_file=root / "queued_turns.json",
            bot_connections_file=root / "bot_connections.json",
            bot_bindings_file=root / "bot_bindings.json",
            bot_reply_targets_file=root / "bot_reply_targets.json",
            bot_delivery_targets_file=root / "bot_delivery_targets.json",
        )

    def test_turn_queues_import_once_and_mirror_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "queued_turns.json"
            legacy.write_text(
                json.dumps(
                    {
                        "thread-1": [
                            {
                                "id": "q1",
                                "thread_id": "thread-1",
                                "project_id": "home",
                                "message": "first",
                                "created_at": 1.0,
                            }
                        ]
                    }
                )
            )
            repositories = self._repositories(root)

            loaded = repositories.turn_queues.load()
            self.assertEqual(loaded["thread-1"][0].message, "first")

            legacy.write_text("{}")
            self.assertEqual(repositories.turn_queues.load()["thread-1"][0].message, "first")

            loaded["thread-1"].append(
                QueuedTurn(
                    id="q2",
                    thread_id="thread-1",
                    project_id="home",
                    message="second",
                    created_at=2.0,
                )
            )
            repositories.turn_queues.save(loaded)
            mirrored = json.loads(legacy.read_text())
            self.assertEqual([item["id"] for item in mirrored["thread-1"]], ["q1", "q2"])
            self.assertEqual(legacy.stat().st_mode & 0o777, 0o600)

    def test_turn_queue_keyed_write_defers_full_json_mirror(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repositories = self._repositories(root)
            legacy = root / "queued_turns.json"
            legacy.write_text("{}")

            repositories.turn_queues.put(
                "thread-1",
                [
                    QueuedTurn(
                        id="q1",
                        thread_id="thread-1",
                        project_id="home",
                        message="first",
                        created_at=1.0,
                    )
                ],
            )

            self.assertEqual(json.loads(legacy.read_text()), {})
            self.assertEqual(
                repositories.turn_queues.get("thread-1")[0].id,
                "q1",
            )
            self.assertTrue(
                repositories.turn_queues.store.record_collection_exists(
                    "turn_queues"
                )
            )

            repositories.turn_queues.flush_legacy_mirror()
            self.assertEqual(
                json.loads(legacy.read_text())["thread-1"][0]["id"],
                "q1",
            )

    def test_bot_credentials_are_sqlite_primary_and_private_when_mirrored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repositories = self._repositories(root)
            connection = BotConnection(
                id="conn-1",
                provider="slack",
                name="Slack",
                bot_token="secret-token",
                created_at=1.0,
                updated_at=1.0,
            )

            repositories.bot_connections.save([connection])

            legacy = root / "bot_connections.json"
            self.assertEqual(repositories.bot_connections.load()[0].bot_token, "secret-token")
            self.assertEqual(json.loads(legacy.read_text())[0]["bot_token"], "secret-token")
            self.assertEqual(legacy.stat().st_mode & 0o777, 0o600)

    def test_reply_targets_round_trip_through_map_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repositories = self._repositories(root)
            target = BotReplyTarget(
                thread_id="thread-1",
                provider="slack",
                external_conversation_id="C123",
                external_thread_id="123.45",
                updated_at=1.0,
            )

            repositories.bot_reply_targets.save({"key": target})

            self.assertEqual(
                repositories.bot_reply_targets.load()["key"].external_thread_id,
                "123.45",
            )

    def test_installer_replaces_runtime_load_save_functions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = SimpleNamespace(sqlite_state_store=SQLiteStateStore(root / "codex-web.db"))
            app = SimpleNamespace(state=state)
            host = SimpleNamespace()

            import codex_web.paths as paths

            originals = {
                name: getattr(paths, name)
                for name in (
                    "TURN_QUEUE_FILE",
                    "BOTS_CONNECTIONS_FILE",
                    "BOTS_BINDINGS_FILE",
                    "BOT_REPLY_TARGETS_FILE",
                    "BOT_DELIVERY_TARGETS_FILE",
                )
            }
            try:
                paths.TURN_QUEUE_FILE = root / "queued_turns.json"
                paths.BOTS_CONNECTIONS_FILE = root / "bot_connections.json"
                paths.BOTS_BINDINGS_FILE = root / "bot_bindings.json"
                paths.BOT_REPLY_TARGETS_FILE = root / "bot_reply_targets.json"
                paths.BOT_DELIVERY_TARGETS_FILE = root / "bot_delivery_targets.json"

                repositories = install_operational_state(app, host)

                self.assertIs(getattr(host._load_turn_queues, "__self__", None), repositories.turn_queues)
                self.assertIs(getattr(host._thread_queue_record, "__self__", None), repositories.turn_queues)
                self.assertIs(getattr(host._put_thread_queue_record, "__self__", None), repositories.turn_queues)
                self.assertIs(getattr(host._save_bot_connections, "__self__", None), repositories.bot_connections)
                self.assertIs(getattr(host._load_bot_reply_targets, "__self__", None), repositories.bot_reply_targets)
            finally:
                for name, value in originals.items():
                    setattr(paths, name, value)


if __name__ == "__main__":
    unittest.main()
