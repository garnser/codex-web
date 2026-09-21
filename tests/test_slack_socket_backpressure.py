from __future__ import annotations

import asyncio
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from codex_web.models import BotConnection
from codex_web.runtime.bots import BotRuntime, SlackSocketCleanClose


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

    def snapshot(self):
        return dict(self.status)


class _Connections:
    def load_connections(self):
        return []

    def runtime_actor(self, _project_id):
        return None


class _Presentation:
    @staticmethod
    def strip_slack_mentions(value):
        return value

    @staticmethod
    def truncate_text(value, limit):
        return str(value)[:limit]

    @staticmethod
    def ambiguous_route_message(_prefixes):
        return "ambiguous"

    @staticmethod
    def slack_reply_username(_binding):
        return None

    @staticmethod
    def slack_reply_icon(_binding):
        return None


class _Routing:
    async def handle_inbound(self, _message):
        return {"ok": True}


class _Delivery:
    async def handle_slack_interaction(self, _connection, _payload):
        return None


class _Bindings:
    def first_for_connection(self, _provider, _channel):
        return None


class _Slack:
    async def socket_url(self, _token):
        return "wss://socket.test"


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
        bindings=_Bindings(),
        presentation=_Presentation(),
        telemetry=_Telemetry(),
        routing=_Routing(),
        delivery=_Delivery(),
        publish_event=_publish,
        slack_client=_Slack(),
    )


def _payload(channel: str, event_id: str) -> dict:
    return {
        "type": "events_api",
        "event_id": event_id,
        "event": {
            "type": "message",
            "channel": channel,
            "ts": event_id,
            "text": "hello",
        },
    }


class _Socket:
    def __init__(self, envelopes: list[dict]) -> None:
        self._rows = [json.dumps(item) for item in envelopes]
        self.sent: list[dict] = []

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


class SlackSocketBackpressureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        # Any test that fails before explicit cleanup should not leak workers
        # into the following asyncio test loop.
        for task in list(asyncio.all_tasks()):
            if task is asyncio.current_task():
                continue
            if task.get_name().startswith("slack-payload-worker-"):
                task.cancel()
        await asyncio.sleep(0)

    async def test_10k_burst_creates_only_fixed_workers_and_bounded_queue(self) -> None:
        runtime = _runtime()
        connection = _connection()
        gate = asyncio.Event()

        async def slow_handler(_connection, _payload):
            await gate.wait()

        runtime._handle_slack_payload = slow_handler
        with patch.dict(
            os.environ,
            {
                "CODEX_WEB_SLACK_PAYLOAD_WORKERS": "4",
                "CODEX_WEB_SLACK_PAYLOAD_QUEUE_PER_WORKER": "8",
                "CODEX_WEB_SLACK_PAYLOAD_DRAIN_TIMEOUT_SECONDS": "0",
            },
            clear=False,
        ):
            results = [
                runtime._admit_slack_payload(
                    connection,
                    _payload("C-HOT", f"E{index}"),
                    envelope_id=f"ENV{index}",
                )
                for index in range(10_000)
            ]

            self.assertEqual(
                len(runtime.slack_payload_workers[connection.id]),
                4,
            )
            self.assertLessEqual(
                sum(
                    queue.qsize()
                    for queue in runtime.slack_payload_queues[
                        connection.id
                    ]
                ),
                32,
            )
            self.assertGreater(results.count("rejected"), 0)
            self.assertEqual(
                sum(
                    1
                    for task in asyncio.all_tasks()
                    if task.get_name().startswith(
                        "slack-payload-worker-slack-1-"
                    )
                ),
                4,
            )
            await asyncio.wait_for(asyncio.sleep(0), timeout=0.05)
            await runtime._stop_slack_payload_workers(connection)

    async def test_hot_channel_does_not_starve_channel_on_other_shard(self) -> None:
        runtime = _runtime()
        connection = _connection()
        hot_channel = "C-HOT"
        hot_shard = runtime._slack_payload_shard(
            f"{connection.id}:{hot_channel}",
            4,
        )
        cool_channel = None
        for index in range(100):
            candidate = f"C-COOL-{index}"
            if (
                runtime._slack_payload_shard(
                    f"{connection.id}:{candidate}",
                    4,
                )
                != hot_shard
            ):
                cool_channel = candidate
                break
        assert cool_channel is not None

        hot_gate = asyncio.Event()
        cool_done = asyncio.Event()

        async def handler(_connection, payload):
            channel = payload["event"]["channel"]
            if channel == hot_channel:
                await hot_gate.wait()
            if channel == cool_channel:
                cool_done.set()

        runtime._handle_slack_payload = handler
        with patch.dict(
            os.environ,
            {
                "CODEX_WEB_SLACK_PAYLOAD_WORKERS": "4",
                "CODEX_WEB_SLACK_PAYLOAD_QUEUE_PER_WORKER": "16",
                "CODEX_WEB_SLACK_PAYLOAD_DRAIN_TIMEOUT_SECONDS": "0",
            },
            clear=False,
        ):
            for index in range(12):
                runtime._admit_slack_payload(
                    connection,
                    _payload(hot_channel, f"H{index}"),
                )
            runtime._admit_slack_payload(
                connection,
                _payload(cool_channel, "COOL"),
            )

            await asyncio.wait_for(cool_done.wait(), timeout=0.2)
            hot_gate.set()
            await asyncio.sleep(0)
            await runtime._stop_slack_payload_workers(connection)

    async def test_duplicate_event_replay_is_deduped_before_processing(self) -> None:
        runtime = _runtime()
        connection = _connection()
        processed: list[str] = []

        async def handler(_connection, payload):
            processed.append(payload["event_id"])

        runtime._handle_slack_payload = handler
        with patch.dict(
            os.environ,
            {
                "CODEX_WEB_SLACK_PAYLOAD_WORKERS": "2",
                "CODEX_WEB_SLACK_PAYLOAD_QUEUE_PER_WORKER": "8",
            },
            clear=False,
        ):
            first = runtime._admit_slack_payload(
                connection,
                _payload("C1", "EVENT-1"),
                envelope_id="ENV-1",
            )
            second = runtime._admit_slack_payload(
                connection,
                _payload("C1", "EVENT-1"),
                envelope_id="ENV-REPLAY",
            )
            self.assertEqual(first, "accepted")
            self.assertEqual(second, "deduped")

            await asyncio.gather(
                *(
                    queue.join()
                    for queue in runtime.slack_payload_queues[
                        connection.id
                    ]
                )
            )
            self.assertEqual(processed, ["EVENT-1"])
            self.assertEqual(
                runtime.slack_payload_status(connection.id)[
                    "deduped"
                ],
                1,
            )
            await runtime._stop_slack_payload_workers(connection)

    async def test_shutdown_cancels_and_accounts_for_known_pending_work(self) -> None:
        runtime = _runtime()
        connection = _connection()
        gate = asyncio.Event()

        async def handler(_connection, _payload):
            await gate.wait()

        runtime._handle_slack_payload = handler
        with patch.dict(
            os.environ,
            {
                "CODEX_WEB_SLACK_PAYLOAD_WORKERS": "2",
                "CODEX_WEB_SLACK_PAYLOAD_QUEUE_PER_WORKER": "5",
                "CODEX_WEB_SLACK_PAYLOAD_DRAIN_TIMEOUT_SECONDS": "0",
            },
            clear=False,
        ):
            accepted = 0
            for index in range(5):
                accepted += (
                    runtime._admit_slack_payload(
                        connection,
                        _payload("C1", f"E{index}"),
                    )
                    == "accepted"
                )
            await asyncio.sleep(0)

            await runtime._stop_slack_payload_workers(connection)

            self.assertNotIn(
                connection.id,
                runtime.slack_payload_queues,
            )
            self.assertNotIn(
                connection.id,
                runtime.slack_payload_workers,
            )
            self.assertGreaterEqual(
                runtime.slack_payload_status(connection.id)[
                    "cancelled"
                ],
                accepted,
            )
            self.assertTrue(
                any(
                    event.get("type")
                    == "slack_payload_shutdown_cancelled"
                    for event in runtime.telemetry.events
                )
            )

    async def test_socket_ack_is_sent_only_after_accept_or_safe_dedupe(self) -> None:
        runtime = _runtime()
        connection = _connection()
        socket = _Socket(
            [
                {
                    "envelope_id": "ENV-1",
                    "payload": _payload("C1", "E1"),
                },
                {
                    "envelope_id": "ENV-2",
                    "payload": _payload("C1", "E2"),
                },
                {
                    "envelope_id": "ENV-3",
                    "payload": _payload("C1", "E3"),
                },
            ]
        )
        admissions = iter(["accepted", "rejected", "deduped"])
        runtime._admit_slack_payload = (
            lambda *_args, **_kwargs: next(admissions)
        )

        with patch(
            "codex_web.runtime.bots.websockets.connect",
            return_value=socket,
        ):
            with self.assertRaises(SlackSocketCleanClose):
                await runtime._run_slack(connection)

        self.assertEqual(
            socket.sent,
            [
                {"envelope_id": "ENV-1"},
                {"envelope_id": "ENV-3"},
            ],
        )

    async def test_overload_is_explicit_and_observable(self) -> None:
        runtime = _runtime()
        connection = _connection()
        gate = asyncio.Event()

        async def handler(_connection, _payload):
            await gate.wait()

        runtime._handle_slack_payload = handler
        with patch.dict(
            os.environ,
            {
                "CODEX_WEB_SLACK_PAYLOAD_WORKERS": "1",
                "CODEX_WEB_SLACK_PAYLOAD_QUEUE_PER_WORKER": "1",
                "CODEX_WEB_SLACK_PAYLOAD_DRAIN_TIMEOUT_SECONDS": "0",
            },
            clear=False,
        ):
            self.assertEqual(
                runtime._admit_slack_payload(
                    connection,
                    _payload("C1", "E1"),
                ),
                "accepted",
            )
            self.assertEqual(
                runtime._admit_slack_payload(
                    connection,
                    _payload("C1", "E2"),
                ),
                "rejected",
            )
            status = runtime.slack_payload_status(connection.id)
            self.assertEqual(status["queueCapacity"], 1)
            self.assertEqual(status["rejected"], 1)
            self.assertTrue(status["overloaded"])
            self.assertTrue(
                any(
                    event.get("type") == "slack_payload_overload"
                    for event in runtime.telemetry.events
                )
            )
            await runtime._stop_slack_payload_workers(connection)


if __name__ == "__main__":
    unittest.main()
