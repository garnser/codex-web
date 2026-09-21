from __future__ import annotations

import asyncio
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from codex_web.models import BotConnection
from codex_web.runtime.bots import (
    BotRuntime,
    SlackSocketCleanClose,
)


class _Telemetry:
    def __init__(self) -> None:
        self.status: dict[str, dict] = {}
        self.events: list[dict] = []

    def set_status(self, connection, status, **values) -> None:
        self.status[connection.id] = {
            **self.status.get(connection.id, {}),
            "connectionId": connection.id,
            "status": status,
            **values,
        }

    def append(self, event: dict) -> None:
        self.events.append(dict(event))


class _Connections:
    def load_connections(self):
        return []

    def runtime_actor(self, _project_id):
        return None


class _Slack:
    def __init__(self) -> None:
        self.socket_url_calls = 0

    async def socket_url(self, _token):
        self.socket_url_calls += 1
        return "wss://socket.test"


class _Routing:
    async def handle_inbound(self, _message):
        return {"ok": True}


class _Delivery:
    async def handle_slack_interaction(self, _connection, _payload):
        return None


async def _publish(_event):
    return None


def _connection() -> BotConnection:
    return BotConnection(
        id="slack-1",
        provider="slack",
        name="Slack",
        project_id="home",
        bot_token="xoxb-test",
        slack_app_token="xapp-test",
        created_at=1.0,
        updated_at=1.0,
    )


def _runtime() -> BotRuntime:
    return BotRuntime(
        connections=_Connections(),
        bindings=SimpleNamespace(),
        presentation=SimpleNamespace(),
        telemetry=_Telemetry(),
        routing=_Routing(),
        delivery=_Delivery(),
        publish_event=_publish,
        slack_client=_Slack(),
    )


def _payload(event_id: str = "EVENT-1") -> dict:
    return {
        "type": "events_api",
        "event_id": event_id,
        "event": {
            "type": "message",
            "channel": "C1",
            "ts": event_id,
            "text": "hello",
        },
    }


class _Socket:
    def __init__(self, envelopes=None) -> None:
        self._rows = [
            json.dumps(value)
            for value in (envelopes or [])
        ]
        self.sent: list[dict] = []
        self.ping_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._rows:
            raise StopAsyncIteration
        return self._rows.pop(0)

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def ping(self, _data=None):
        self.ping_calls += 1
        future = asyncio.get_running_loop().create_future()
        future.set_result(0.012)
        return future


class _ReconnectProbeRuntime(BotRuntime):
    def __init__(self, attempts) -> None:
        super().__init__(
            connections=_Connections(),
            bindings=SimpleNamespace(),
            presentation=SimpleNamespace(),
            telemetry=_Telemetry(),
            routing=_Routing(),
            delivery=_Delivery(),
            publish_event=_publish,
            slack_client=_Slack(),
        )
        self.attempts = list(attempts)
        self.attempt_count = 0

    async def _run_slack(self, connection):
        self.attempt_count += 1
        action = self.attempts.pop(0)
        if isinstance(action, BaseException):
            raise action
        return action


class _ReplayReconnectRuntime(BotRuntime):
    def __init__(self) -> None:
        super().__init__(
            connections=_Connections(),
            bindings=SimpleNamespace(),
            presentation=SimpleNamespace(),
            telemetry=_Telemetry(),
            routing=_Routing(),
            delivery=_Delivery(),
            publish_event=_publish,
            slack_client=_Slack(),
        )
        self.admissions: list[str] = []
        self.worker_counts: list[int] = []
        self.attempt = 0

    async def _handle_slack_payload(self, _connection, _payload):
        return None

    async def _run_slack(self, connection):
        self.attempt += 1
        self.admissions.append(
            self._admit_slack_payload(
                connection,
                _payload(),
                envelope_id=f"ENV-{self.attempt}",
            )
        )
        self.worker_counts.append(
            len(self.slack_payload_workers[connection.id])
        )
        if self.attempt == 1:
            raise SlackSocketCleanClose("clean close")
        raise asyncio.CancelledError


class SlackSocketLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        for task in list(asyncio.all_tasks()):
            if task is asyncio.current_task():
                continue
            if (
                task.get_name().startswith("slack-payload-worker-")
                or task.get_name().startswith("slack-pong-observer-")
            ):
                task.cancel()
        await asyncio.sleep(0)

    def test_default_keepalive_and_reconnect_configuration_is_bounded(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                BotRuntime.slack_socket_ping_interval(),
                20.0,
            )
            self.assertEqual(
                BotRuntime.slack_socket_ping_timeout(),
                10.0,
            )
            self.assertEqual(
                BotRuntime.slack_socket_open_timeout(),
                10.0,
            )
            self.assertEqual(
                BotRuntime.slack_reconnect_min_seconds(),
                2.0,
            )
            self.assertEqual(
                BotRuntime.slack_reconnect_max_seconds(),
                60.0,
            )

        with patch.dict(
            os.environ,
            {
                "CODEX_WEB_SLACK_SOCKET_PING_INTERVAL_SECONDS": "1",
                "CODEX_WEB_SLACK_SOCKET_PING_TIMEOUT_SECONDS": "999",
                "CODEX_WEB_SLACK_SOCKET_OPEN_TIMEOUT_SECONDS": "bad",
                "CODEX_WEB_SLACK_RECONNECT_MIN_SECONDS": "0",
                "CODEX_WEB_SLACK_RECONNECT_MAX_SECONDS": "9999",
                "CODEX_WEB_SLACK_RECONNECT_JITTER_RATIO": "9",
            },
            clear=True,
        ):
            self.assertEqual(
                BotRuntime.slack_socket_ping_interval(),
                5.0,
            )
            self.assertEqual(
                BotRuntime.slack_socket_ping_timeout(),
                60.0,
            )
            self.assertEqual(
                BotRuntime.slack_socket_open_timeout(),
                10.0,
            )
            self.assertEqual(
                BotRuntime.slack_reconnect_min_seconds(),
                0.1,
            )
            self.assertEqual(
                BotRuntime.slack_reconnect_max_seconds(),
                600.0,
            )
            self.assertEqual(
                BotRuntime.slack_reconnect_jitter_ratio(),
                0.5,
            )

    def test_transport_failure_classes_are_distinct(self) -> None:
        cases = {
            "timed out during opening handshake": "handshake_timeout",
            "keepalive ping timeout": "keepalive_timeout",
            "no close frame received or sent": "transport_reset",
            "connection reset by peer": "transport_reset",
            "invalid_auth": "authentication_configuration",
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                self.assertEqual(
                    BotRuntime.slack_socket_failure_class(
                        RuntimeError(message)
                    ),
                    expected,
                )
        self.assertEqual(
            BotRuntime.slack_socket_failure_class(
                SlackSocketCleanClose("closed")
            ),
            "clean_close",
        )

    async def test_socket_connect_uses_finite_keepalive_and_open_timeouts(self) -> None:
        runtime = _runtime()
        connection = _connection()
        socket = _Socket()
        call: dict = {}

        def connect(url, **kwargs):
            call["url"] = url
            call.update(kwargs)
            return socket

        with patch(
            "codex_web.runtime.bots.websockets.connect",
            side_effect=connect,
        ):
            with self.assertRaises(SlackSocketCleanClose):
                await runtime._run_slack(connection)

        self.assertEqual(call["url"], "wss://socket.test")
        self.assertEqual(call["ping_interval"], 20.0)
        self.assertEqual(call["ping_timeout"], 10.0)
        self.assertEqual(call["open_timeout"], 10.0)
        self.assertEqual(call["close_timeout"], 5)
        status = runtime.telemetry.status[connection.id]
        self.assertEqual(status["status"], "connected")
        self.assertIsNotNone(status["connectedSince"])

    async def test_ping_instrumentation_records_ping_and_pong(self) -> None:
        runtime = _runtime()
        connection = _connection()
        socket = _Socket()

        runtime._instrument_slack_ping(socket, connection)
        waiter = await socket.ping()
        await waiter
        await asyncio.sleep(0)

        status = runtime.telemetry.status[connection.id]
        self.assertTrue(status["pingTelemetryInstrumented"])
        self.assertIsNotNone(status["lastPingAt"])
        self.assertIsNotNone(status["lastPongAt"])
        self.assertAlmostEqual(
            status["lastPongLatencySeconds"],
            0.012,
            places=3,
        )

    async def test_reconnect_backoff_is_bounded_and_reopens_socket_flow(self) -> None:
        runtime = _ReconnectProbeRuntime(
            [
                RuntimeError("keepalive ping timeout"),
                RuntimeError("no close frame received or sent"),
                asyncio.CancelledError(),
            ]
        )
        connection = _connection()
        sleeps: list[float] = []

        async def fake_sleep(delay):
            sleeps.append(delay)

        with patch.dict(
            os.environ,
            {
                "CODEX_WEB_SLACK_RECONNECT_MIN_SECONDS": "2",
                "CODEX_WEB_SLACK_RECONNECT_MAX_SECONDS": "8",
                "CODEX_WEB_SLACK_RECONNECT_JITTER_RATIO": "0",
                "CODEX_WEB_SLACK_PAYLOAD_DRAIN_TIMEOUT_SECONDS": "0",
            },
            clear=False,
        ), patch(
            "codex_web.runtime.bots.asyncio.sleep",
            side_effect=fake_sleep,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await runtime._run_connection(connection)

        self.assertEqual(runtime.attempt_count, 3)
        self.assertEqual(sleeps[:2], [2.0, 4.0])
        self.assertEqual(
            runtime.slack_reconnect_counts[connection.id],
            2,
        )
        reconnect_events = [
            event
            for event in runtime.telemetry.events
            if event.get("type") == "runtime_reconnect"
        ]
        self.assertEqual(
            [item["failure_class"] for item in reconnect_events],
            ["keepalive_timeout", "transport_reset"],
        )
        self.assertTrue(
            all(
                item["delay_seconds"] <= 8.0
                for item in reconnect_events
            )
        )
        self.assertFalse(
            runtime.telemetry.status[connection.id][
                "tokenRotationRequired"
            ]
        )

    async def test_replayed_envelope_stays_deduped_across_reconnect(self) -> None:
        runtime = _ReplayReconnectRuntime()
        connection = _connection()

        async def fake_sleep(_delay):
            return None

        with patch.dict(
            os.environ,
            {
                "CODEX_WEB_SLACK_PAYLOAD_WORKERS": "1",
                "CODEX_WEB_SLACK_PAYLOAD_QUEUE_PER_WORKER": "8",
                "CODEX_WEB_SLACK_PAYLOAD_DRAIN_TIMEOUT_SECONDS": "0",
                "CODEX_WEB_SLACK_RECONNECT_JITTER_RATIO": "0",
            },
            clear=False,
        ), patch(
            "codex_web.runtime.bots.asyncio.sleep",
            side_effect=fake_sleep,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await runtime._run_connection(connection)

        self.assertEqual(
            runtime.admissions,
            ["accepted", "deduped"],
        )
        self.assertEqual(runtime.worker_counts, [1, 1])

    async def test_cancellation_does_not_enter_reconnect_loop(self) -> None:
        gate = asyncio.Event()

        class Runtime(_ReconnectProbeRuntime):
            async def _run_slack(self, _connection):
                self.attempt_count += 1
                await gate.wait()

        runtime = Runtime([])
        connection = _connection()

        with patch.dict(
            os.environ,
            {
                "CODEX_WEB_SLACK_PAYLOAD_DRAIN_TIMEOUT_SECONDS": "0",
            },
            clear=False,
        ):
            task = asyncio.create_task(
                runtime._run_connection(connection)
            )
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertEqual(runtime.attempt_count, 1)
        self.assertEqual(
            [
                event
                for event in runtime.telemetry.events
                if event.get("type") == "runtime_reconnect"
            ],
            [],
        )


if __name__ == "__main__":
    unittest.main()
