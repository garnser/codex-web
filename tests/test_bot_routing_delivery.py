from __future__ import annotations

import time
import unittest
from types import SimpleNamespace

from fastapi import FastAPI

from codex_web.models import BotBinding, BotConnection, BotInboundMessage, BotReplyTarget
from codex_web.services.bot_delivery import BotDeliveryService, install_bot_delivery_service
from codex_web.services.bot_routing import BotRoutingService, install_bot_routing_service


class _FakeSlackClient:
    def __init__(self) -> None:
        self.posts: list[tuple[tuple, dict]] = []
        self.updates: list[tuple[tuple, dict]] = []

    async def post_message(self, *args, **kwargs):
        self.posts.append((args, kwargs))
        return {"sent": True, "providerResponse": {"ok": True, "ts": "123.45"}}

    async def update_message(self, *args, **kwargs):
        self.updates.append((args, kwargs))
        return {"sent": True, "providerResponse": {"ok": True}}


class _FakeTelegramClient:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, str]] = []

    async def send_message(self, token: str, chat_id: str, text: str):
        self.messages.append((token, chat_id, text))
        return {"sent": True, "providerResponse": {"ok": True}}


class _DeliveryHost:
    def __init__(self) -> None:
        self.connections = {
            "slack-conn": BotConnection(
                id="slack-conn",
                provider="slack",
                name="Slack",
                project_id="home",
                bot_token="xoxb-test",
                created_at=time.time(),
                updated_at=time.time(),
            ),
            "telegram-conn": BotConnection(
                id="telegram-conn",
                provider="telegram",
                name="Telegram",
                project_id="home",
                bot_token="tg-test",
                created_at=time.time(),
                updated_at=time.time(),
            ),
        }

    def _bot_connection(self, connection_id: str):
        return self.connections[connection_id]

    @staticmethod
    def _thread_target_for_outbound(binding, reply_in_thread):
        return (
            BotReplyTarget(
                thread_id=binding.thread_id,
                provider=binding.provider,
                external_conversation_id=binding.external_conversation_id,
                external_thread_id="111.22",
                message_id="111.22",
                updated_at=time.time(),
            ),
            True,
        )

    @staticmethod
    def _slack_reply_username(binding):
        return "Codex · Agent"

    @staticmethod
    def _slack_reply_icon(binding):
        return ":robot_face:"


class BotDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_slack_delivery_uses_async_client_and_thread_target(self) -> None:
        host = _DeliveryHost()
        slack = _FakeSlackClient()
        service = BotDeliveryService(host, slack_client=slack, telegram_client=_FakeTelegramClient())
        binding = BotBinding(
            id="binding-1",
            connection_id="slack-conn",
            provider="slack",
            external_conversation_id="C1",
            thread_id="thread-1",
            project_id="home",
            created_at=time.time(),
            updated_at=time.time(),
        )

        result = await service.send_outbound(binding, "hello")

        self.assertTrue(result["sent"])
        self.assertEqual(len(slack.posts), 1)
        args, kwargs = slack.posts[0]
        self.assertEqual(args[:3], ("xoxb-test", "C1", "hello"))
        self.assertEqual(kwargs["thread_ts"], "111.22")
        self.assertEqual(kwargs["username"], "Codex · Agent")
        self.assertEqual(kwargs["icon_emoji"], ":robot_face:")

    async def test_telegram_delivery_uses_async_client(self) -> None:
        host = _DeliveryHost()
        telegram = _FakeTelegramClient()
        service = BotDeliveryService(host, slack_client=_FakeSlackClient(), telegram_client=telegram)
        binding = BotBinding(
            id="binding-2",
            connection_id="telegram-conn",
            provider="telegram",
            external_conversation_id="42",
            thread_id="thread-2",
            project_id="home",
            created_at=time.time(),
            updated_at=time.time(),
        )

        result = await service.send_outbound(binding, "hello telegram")

        self.assertTrue(result["sent"])
        self.assertEqual(telegram.messages, [("tg-test", "42", "hello telegram")])


class _RoutingHost:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.binding = BotBinding(
            id="binding-1",
            provider="slack",
            external_conversation_id="C1",
            thread_id="thread-1",
            project_id="home",
            route_prefix="Agent One",
            created_at=time.time(),
            updated_at=time.time(),
        )

    def _bindings_for_connection(self, provider, conversation):
        return [self.binding]

    @staticmethod
    def _steer_route_message(message):
        return False, message

    @staticmethod
    def _has_single_master_binding(bindings):
        return False

    @staticmethod
    def _is_top_level_external_message(message):
        return True

    @staticmethod
    def _resolve_bot_binding(bindings, message, **kwargs):
        return None, message.text, True

    @staticmethod
    def _cross_channel_binding_for_message(provider, project_id, bindings, message, **kwargs):
        return None, message.text, False

    @staticmethod
    def _binding_prefix(binding):
        return binding.route_prefix or binding.thread_id

    @staticmethod
    def _bindings_for_project(provider, project_id):
        return []

    def _append_bot_event(self, event):
        self.events.append(event)


class BotRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_ambiguous_inbound_is_resolved_outside_core(self) -> None:
        host = _RoutingHost()
        service = BotRoutingService(host, delivery=SimpleNamespace())
        message = BotInboundMessage(
            provider="slack",
            external_conversation_id="C1",
            text="which agent?",
            project_id="home",
        )

        result = await service.handle_inbound(message)

        self.assertFalse(result["ok"])
        self.assertTrue(result["ambiguous"])
        self.assertEqual(result["availablePrefixes"], ["Agent One"])
        self.assertEqual(host.events[-1]["type"], "inbound_ambiguous")


class BotCompositionTests(unittest.TestCase):
    def test_installers_rebind_compatibility_entrypoints(self) -> None:
        app = FastAPI()
        host = SimpleNamespace()
        slack = _FakeSlackClient()
        telegram = _FakeTelegramClient()

        delivery = install_bot_delivery_service(
            app,
            host,
            slack_client=slack,
            telegram_client=telegram,
        )
        routing = install_bot_routing_service(app, host, delivery)

        self.assertIs(host._send_bot_outbound.__self__, delivery)
        self.assertIs(host._record_bot_outbound.__self__, delivery)
        self.assertIs(host._record_bot_approval_request.__self__, delivery)
        self.assertIs(host._resolve_approval_request.__self__, delivery)
        self.assertIs(host._handle_slack_interaction.__self__, delivery)
        self.assertIs(host._handle_bot_inbound.__self__, routing)
        self.assertIs(app.state.bot_delivery_service, delivery)
        self.assertIs(app.state.bot_routing_service, routing)


if __name__ == "__main__":
    unittest.main()
