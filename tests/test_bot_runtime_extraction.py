from __future__ import annotations

import asyncio
import time
import unittest
from types import SimpleNamespace

from fastapi import FastAPI

from codex_web.models import BotConnection
from codex_web.runtime.bots import BotRuntime, install_bot_runtime


class _Host:
    def __init__(self) -> None:
        self.bot_runtime = object()
        self.BOT_RUNTIME_STATUS = {}
        self.connections: list[BotConnection] = []

    def _load_bot_connections(self):
        return self.connections


async def _publish(_event) -> None:
    return None


def _runtime_kwargs(host):
    return {
        "connections": SimpleNamespace(
            load_connections=host._load_bot_connections,
        ),
        "bindings": SimpleNamespace(),
        "presentation": SimpleNamespace(),
        "telemetry": SimpleNamespace(status={}),
        "routing": SimpleNamespace(),
        "delivery": SimpleNamespace(),
        "publish_event": _publish,
    }


class _ProbeRuntime(BotRuntime):
    def __init__(self, host) -> None:
        super().__init__(**_runtime_kwargs(host))
        self.started: list[str] = []

    async def _run_connection(self, connection: BotConnection) -> None:
        self.started.append(connection.id)
        await asyncio.Event().wait()


class _FakeSlackClient:
    pass


class _FakeTelegramClient:
    pass


class BotRuntimeExtractionTests(unittest.IsolatedAsyncioTestCase):
    async def test_sync_owns_connection_tasks_and_stops_removed_connections(self) -> None:
        host = _Host()
        host.connections = [
            BotConnection(
                id="slack-1",
                provider="slack",
                name="Slack",
                bot_token="xoxb-test",
                slack_app_token="xapp-test",
                created_at=time.time(),
                updated_at=time.time(),
            )
        ]
        runtime = _ProbeRuntime(host)

        await runtime.sync()
        await asyncio.sleep(0)

        self.assertEqual(runtime.started, ["slack-1"])
        self.assertIn("slack-1", runtime.tasks)
        self.assertEqual(runtime.tasks["slack-1"].get_name(), "bot-runtime-slack-slack-1")

        host.connections = []
        await runtime.sync()
        self.assertNotIn("slack-1", runtime.tasks)

    async def test_installer_replaces_legacy_runtime_and_is_idempotent(self) -> None:
        host = _Host()
        app = FastAPI()
        slack = _FakeSlackClient()
        telegram = _FakeTelegramClient()

        first = install_bot_runtime(
            app,
            host,
            **_runtime_kwargs(host),
            slack_client=slack,
            telegram_client=telegram,
        )
        second = install_bot_runtime(app, host)

        self.assertIs(first, second)
        self.assertIs(host.bot_runtime, first)
        self.assertIs(app.state.bot_runtime, first)
        self.assertEqual(type(first).__module__, "codex_web.runtime.bots")
        self.assertIs(first.slack, slack)
        self.assertIs(first.telegram, telegram)


if __name__ == "__main__":
    unittest.main()
