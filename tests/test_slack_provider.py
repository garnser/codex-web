from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi import Request

from codex_web.integrations.slack_client import SlackClient
from codex_web.models import BotConnection
from codex_web.services.slack_provider import SlackProviderService


class _Routing:
    def __init__(self) -> None:
        self.messages = []
        self.result = {"ok": True, "threadId": "thread-1"}

    async def handle_inbound(self, message):
        self.messages.append(message)
        return dict(self.result)


class _Slack:
    def __init__(self) -> None:
        self.history_response = {"ok": True, "messages": []}
        self.replies_response = {"ok": True, "messages": []}
        self.posts = []

    async def history(self, token, channel, *, oldest, limit=50):
        return dict(self.history_response)

    async def replies(self, token, channel, thread_ts, *, oldest, limit=50):
        return dict(self.replies_response)

    async def post_message(self, token, channel, text, **kwargs):
        self.posts.append((token, channel, text, kwargs))
        return {"sent": True}


class _Host:
    def __init__(self, root: Path) -> None:
        self.BOTS_EVENTS_FILE = root / "bot-events.jsonl"
        self.events = []
        self.connection = BotConnection(
            id="slack-1",
            name="Test Slack",
            provider="slack",
            project_id="home",
            bot_token="token",
            default_external_conversation_id="C1",
            default_external_name="#general",
            created_at=1.0,
            updated_at=1.0,
        )

    def _append_bot_event(self, event):
        self.events.append(event)

    def _load_bot_connections(self):
        return [self.connection]

    def _load_bot_bindings(self):
        return []

    def _load_bot_reply_targets(self):
        return {}

    def _load_bot_delivery_targets(self):
        return {}

    def _load_active_turns(self):
        return {}

    def _strip_slack_mentions(self, text):
        return text.strip()

    def _verify_slack_signature(self, request, body):
        return None

    def _bot_connection_for_conversation(self, provider, channel):
        return self.connection if provider == "slack" and channel == "C1" else None

    def _first_binding_for_connection(self, provider, channel):
        return None

    def _bot_connection(self, connection_id):
        return self.connection if connection_id == self.connection.id else None

    def _ambiguous_route_message(self, prefixes):
        return "ambiguous"

    def _slack_reply_username(self, binding):
        return None

    def _slack_reply_icon(self, binding):
        return None


class _WebhookSecurity:
    async def verify_slack(self, request, body) -> None:
        return None


def _service(host, slack, routing, reconciliation_gates=None):
    return SlackProviderService(
        slack_client=slack,
        routing_service=routing,
        connections=SimpleNamespace(
            load_connections=host._load_bot_connections,
            for_conversation=host._bot_connection_for_conversation,
            runtime_actor=lambda project_id: SimpleNamespace(
                organization_id="local",
                workspace_id="default",
            ),
        ),
        bindings=SimpleNamespace(
            load_bindings=host._load_bot_bindings,
            first_for_connection=host._first_binding_for_connection,
        ),
        targets=SimpleNamespace(
            load_reply_targets=host._load_bot_reply_targets,
            load_delivery_targets=host._load_bot_delivery_targets,
            load_active_turns=host._load_active_turns,
        ),
        presentation=SimpleNamespace(
            strip_slack_mentions=host._strip_slack_mentions,
            ambiguous_route_message=host._ambiguous_route_message,
            slack_reply_username=host._slack_reply_username,
            slack_reply_icon=host._slack_reply_icon,
        ),
        telemetry=SimpleNamespace(
            events_file=host.BOTS_EVENTS_FILE,
            append=host._append_bot_event,
        ),
        webhook_security=_WebhookSecurity(),
        reconciliation_gates=reconciliation_gates,
    )


def _request(payload: dict) -> Request:
    body = json.dumps(payload).encode()
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {"type": "http", "method": "POST", "path": "/bots/slack/events", "headers": []},
        receive,
    )


class _GateDecision:
    def __init__(self, eligible, reason_code="eligible"):
        self.eligible = eligible
        self.reason_code = reason_code
        self.state = SimpleNamespace(value="eligible" if eligible else "blocked")

    def model_dump(self, mode="json"):
        return {
            "service_id": "slack-backfill",
            "project_id": "home",
            "state": self.state.value,
            "eligible": self.eligible,
            "reason_code": self.reason_code,
            "reason": self.reason_code,
        }


class _Gate:
    def __init__(self, eligible=False):
        self.eligible = eligible
        self.starts = []
        self.completions = []

    def decision(self, service_id, project_id, *, actor):
        return _GateDecision(self.eligible, "eligible" if self.eligible else "bootstrap_incomplete")

    def record_start(self, service_id, project_id, *, actor):
        self.starts.append((service_id, project_id))

    def record_completion(self, service_id, project_id, *, actor):
        self.completions.append((service_id, project_id))


class SlackProviderServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.host = _Host(Path(self.temp.name))
        self.routing = _Routing()
        self.slack = _Slack()
        self.service = _service(
            self.host,
            self.slack,
            self.routing,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    async def test_webhook_routes_message_through_extracted_routing_service(self) -> None:
        result = await self.service.handle_webhook(
            _request(
                {
                    "type": "event_callback",
                    "event": {"type": "message", "channel": "C1", "user": "U1", "text": "hello", "ts": "1.2"},
                }
            )
        )
        self.assertTrue(result["accepted"])
        self.assertEqual(result["threadId"], "thread-1")
        self.assertEqual(len(self.routing.messages), 1)
        self.assertEqual(self.routing.messages[0].message_id, "1.2")

    async def test_interval_zero_and_negative_disable_polling_without_disabling_webhook(self) -> None:
        with patch.dict(
            os.environ,
            {"CODEX_WEB_SLACK_BACKFILL_INTERVAL_SECONDS": "0"},
            clear=False,
        ):
            self.assertEqual(self.service.interval_seconds(), 0.0)
            await self.service.start()
            self.assertIsNone(self.service.task)
            result = await self.service.handle_webhook(
                _request(
                    {
                        "type": "event_callback",
                        "event": {
                            "type": "message",
                            "channel": "C1",
                            "user": "U1",
                            "text": "webhook still works",
                            "ts": "1.3",
                        },
                    }
                )
            )
            self.assertTrue(result["accepted"])

        with patch.dict(
            os.environ,
            {"CODEX_WEB_SLACK_BACKFILL_INTERVAL_SECONDS": "-12"},
            clear=False,
        ):
            self.assertEqual(self.service.interval_seconds(), 0.0)
        with patch.dict(
            os.environ,
            {"CODEX_WEB_SLACK_BACKFILL_INTERVAL_SECONDS": "2"},
            clear=False,
        ):
            self.assertEqual(self.service.interval_seconds(), 5.0)
        with patch.dict(
            os.environ,
            {"CODEX_WEB_SLACK_BACKFILL_INTERVAL_SECONDS": "invalid"},
            clear=False,
        ):
            self.assertEqual(self.service.interval_seconds(), 15.0)

    async def test_blocked_project_skips_historical_polling_but_webhook_still_routes(self) -> None:
        gate = _Gate(eligible=False)
        service = _service(
            self.host,
            self.slack,
            self.routing,
            reconciliation_gates=gate,
        )
        self.slack.history_response = {
            "ok": True,
            "messages": [{"ts": "2.0", "user": "U1", "text": "missed"}],
        }

        await service.run_backfill_cycle()
        self.assertEqual(self.routing.messages, [])
        self.assertEqual(gate.starts, [])

        result = await service.handle_webhook(
            _request(
                {
                    "type": "event_callback",
                    "event": {
                        "type": "message",
                        "channel": "C1",
                        "user": "U1",
                        "text": "live",
                        "ts": "2.1",
                    },
                }
            )
        )
        self.assertTrue(result["accepted"])
        self.assertEqual(len(self.routing.messages), 1)

    async def test_approved_eligible_project_runs_and_checkpoints_gate(self) -> None:
        gate = _Gate(eligible=True)
        service = _service(
            self.host,
            self.slack,
            self.routing,
            reconciliation_gates=gate,
        )
        self.slack.history_response = {
            "ok": True,
            "messages": [{"ts": "3.0", "user": "U1", "text": "recovered"}],
        }

        await service.run_backfill_cycle()

        self.assertEqual(len(self.routing.messages), 1)
        self.assertEqual(gate.starts, [("slack-backfill", "home")])
        self.assertEqual(gate.completions, [("slack-backfill", "home")])

    async def test_url_verification_never_enters_routing(self) -> None:
        result = await self.service.handle_webhook(_request({"type": "url_verification", "challenge": "abc"}))
        self.assertEqual(result, {"challenge": "abc"})
        self.assertEqual(self.routing.messages, [])

    async def test_rate_limit_sets_service_owned_cooldown(self) -> None:
        self.slack.history_response = {"ok": False, "error": "ratelimited", "_http_status": 429, "_retry_after": 120.0}
        await self.service.run_backfill_cycle()
        self.assertEqual(self.service.rate_limit_failures, 1)
        self.assertGreater(self.service.cooldown_remaining_seconds(), 100)
        self.assertEqual(self.host.events[-1]["type"], "slack_backfill_cooldown_set")

    async def test_backfill_dispatches_unseen_message(self) -> None:
        self.slack.history_response = {
            "ok": True,
            "messages": [{"ts": "2.0", "user": "U1", "text": "missed"}],
        }
        await self.service.run_backfill_cycle()
        self.assertEqual(len(self.routing.messages), 1)
        self.assertEqual(self.routing.messages[0].message_id, "2.0")
        self.assertEqual(self.host.events[-1]["type"], "slack_backfill_dispatched")


class SlackClientBackfillTests(unittest.IsolatedAsyncioTestCase):
    async def test_history_preserves_rate_limit_metadata(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/api/conversations.history")
            return httpx.Response(429, headers={"Retry-After": "45"}, json={"ok": False, "error": "ratelimited"})

        client = SlackClient(transport=httpx.MockTransport(handler))
        response = await client.history("token", "C1", oldest="1.0")
        self.assertEqual(response["_http_status"], 429)
        self.assertEqual(response["_retry_after"], 45.0)
        self.assertEqual(response["error"], "ratelimited")

    async def test_history_and_replies_forward_pagination_cursor(self) -> None:
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "messages": [],
                    "response_metadata": {"next_cursor": ""},
                },
            )

        client = SlackClient(transport=httpx.MockTransport(handler))
        await client.history(
            "token",
            "C1",
            oldest="1.0",
            cursor="history-cursor",
        )
        await client.replies(
            "token",
            "C1",
            "111.22",
            oldest="1.0",
            cursor="reply-cursor",
        )

        self.assertEqual(
            requests[0].url.params["cursor"],
            "history-cursor",
        )
        self.assertEqual(
            requests[1].url.params["cursor"],
            "reply-cursor",
        )


if __name__ == "__main__":
    unittest.main()
