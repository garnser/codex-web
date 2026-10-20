"""Canonical ambiguity survives durable projection without replaying routed input."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

from fastapi import FastAPI
import httpx

from codex_web.api.slack import build_slack_router
from codex_web.models import BotInboundMessage
from codex_web.services.bot_routing import BotRoutingService
from codex_web.services.conversation_channels import ConversationChannelService
from codex_web.storage.conversation_channels import ConversationChannelStore
from tests import test_conversation_channels as conversation_fixtures
from tests import test_slack_provider as slack_fixtures
from tests import test_bot_runtime_pause as pause_fixtures


class ConversationRoutingChoicesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.f = conversation_fixtures.ConversationChannelServiceTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.calls = []
        self.choices = ["Alice", "Bob", "Åsa"]
        async def route(message):
            self.calls.append(message)
            return {"ok": True, "ambiguous": True, "availablePrefixes": self.choices,
                    "availablePrefixesJson": '["untrusted"]'}
        self.f.service.route_handler = route

    async def receipt(self):
        return (await self.f.service.ingest_raw("slack", "slack-1",
            self.f.slack_payload(event_id="ambiguous"), actor=self.f.actor_a,
            connection_id="slack-1", project_id="project-a"))[0]

    async def test_round_trip_and_duplicate_preserve_choices_without_rerouting(self):
        original = await self.receipt()
        reopened = ConversationChannelStore(self.f.store.store).load()
        stored = reopened.receipts[original.canonical_event_id]
        self.assertEqual(stored.routing_result["availablePrefixesJson"], '["Alice","Bob","\\u00c5sa"]')
        result = self.f.service.legacy_routing_result(stored)
        self.assertEqual(result["availablePrefixes"], self.choices)
        self.assertNotIn("availablePrefixesJson", result)
        duplicate = await self.receipt()
        self.assertEqual(self.f.service.legacy_routing_result(duplicate)["availablePrefixes"], self.choices)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.f.store.load().model_dump(mode="json"), reopened.model_dump(mode="json"))
        self.assertIsNotNone(stored.retained_event)

    async def test_actual_frozen_v3_reader_preserves_additive_scalar_bytes(self):
        await self.receipt()
        path = Path(__file__).parent / "fixtures/compatibility/conversation_channels_v3.py"
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),
                         "bb50602667127c83deccec803b05014285f74d20810dc115b828583f522fba32")
        name = "tests._frozen_conversation_channels_v3"
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        self.addCleanup(sys.modules.pop, name, None)
        spec.loader.exec_module(module)
        payload = self.f.store.load().model_dump(mode="json")
        old = module.ConversationChannelState.model_validate(payload)
        self.assertEqual(old.schema_version, "3.0")
        self.assertEqual(old.model_dump(mode="json"), payload)
        key = next(iter(payload["receipts"]))
        self.assertEqual(old.receipts[key].routing_result["availablePrefixesJson"].encode(),
                         payload["receipts"][key]["routing_result"]["availablePrefixesJson"].encode())

    def test_invalid_or_unbounded_prefix_metadata_is_not_persisted(self):
        for choices in (None, "Alice", {"Alice": True}, [1], [""], ["x" * 513],
                        ["a"] * 257, ["😀" * 512] * 20):
            with self.subTest(type=type(choices).__name__, size=len(choices) if choices else 0):
                result = ConversationChannelService._routing_projection({"ok": True, "ambiguous": True,
                    "availablePrefixes": choices, "availablePrefixesJson": '["injected"]'})
                self.assertEqual(result["availablePrefixesJson"], "[]")
        result = ConversationChannelService._routing_projection({"availablePrefixesJson": '["injected"]'})
        self.assertNotIn("availablePrefixesJson", result)

    async def test_old_or_malformed_receipts_produce_no_fabricated_choices_or_replay(self):
        original = await self.receipt()
        for encoded in (None, "not json", "[1]", '"Alice"', "[" * 2000,
                        json.dumps(["a"] * 257), " " * 65537):
            with self.subTest(encoded_length=len(encoded) if encoded else 0):
                projection = {"ok": True, "ambiguous": True}
                if encoded is not None:
                    projection["availablePrefixesJson"] = encoded
                receipt = original.model_copy(update={"routing_result": projection})
                result = self.f.service.legacy_routing_result(receipt)
                self.assertEqual(result["availablePrefixes"], [])
                self.assertNotIn("availablePrefixesJson", result)
        self.assertEqual(len(self.calls), 1)

    async def test_public_webhook_uses_canonical_choices_after_persistence(self):
        host = slack_fixtures._Host(Path(self.f.temp.name))
        host.connection.project_id = "project-a"
        slack = slack_fixtures._Slack()
        service = slack_fixtures._service(host, slack, slack_fixtures._Routing())
        service.connections.runtime_actor = lambda _: self.f.actor_a
        service.connections.get = lambda _: host.connection
        service.bindings.first_for_connection = lambda *_: SimpleNamespace(connection_id=host.connection.id)
        service.conversation_channels = self.f.service
        displayed = []
        service.presentation.ambiguous_route_message = lambda choices: displayed.append(choices) or "Select a recipient"
        app = FastAPI()
        app.include_router(build_slack_router(service))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://fixture") as client:
            response = await client.post("/bots/slack/events", json={"type": "event_callback",
                **self.f.slack_payload(event_id="http-ambiguous")})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["availablePrefixes"], self.choices)
        self.assertEqual(displayed, [self.choices])
        self.assertEqual(len(slack.posts), 1)
        self.assertEqual(len(self.calls), 1)
        stored = next(iter(ConversationChannelStore(self.f.store.store).load().receipts.values()))
        self.assertEqual(self.f.service.legacy_routing_result(stored)["availablePrefixes"], self.choices)

    async def test_canonical_notice_pause_during_credentials_keeps_retained_receipt(self):
        control = pause_fixtures.BotRuntimePauseTests()
        control.setUp()
        self.addCleanup(control.doCleanups)
        self.f.service.routing_gate = control.gate
        host = slack_fixtures._Host(Path(self.f.temp.name))
        host.connection.project_id = "project-a"
        host.connection.bot_token_secret_id = "fixture-reference"
        slack = slack_fixtures._Slack()
        service = slack_fixtures._service(host, slack, slack_fixtures._Routing())
        service.connections.runtime_actor = lambda _: self.f.actor_a
        service.connections.get = lambda _: host.connection
        service.bindings.first_for_connection = lambda *_: SimpleNamespace(connection_id=host.connection.id)
        service.conversation_channels = self.f.service
        service.gate_reason = control.gate
        captured = []
        async def use(secret_id, **kwargs):
            captured.append(self.f.store.load().model_dump(mode="json"))
            control.pause("project-a")
            return await kwargs["consumer"]("fixture-token")
        service.secret_broker = SimpleNamespace(use_async=use)
        app = FastAPI()
        app.include_router(build_slack_router(service))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://fixture") as client:
            response = await client.post("/bots/slack/events", json={"type": "event_callback",
                **self.f.slack_payload(event_id="canonical-notice-race")})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["availablePrefixes"], self.choices)
        self.assertEqual(response.json()["notice"], {"sent": False, "reason": "operator_paused"})
        self.assertEqual(slack.posts, [])
        self.assertEqual(self.f.store.load().model_dump(mode="json"), captured[0])
        self.assertEqual(len(self.calls), 1)
        self.assertIsNotNone(next(iter(self.f.store.load().receipts.values())).retained_event)

    async def test_normalized_bot_ingress_uses_same_canonical_pending_projection(self):
        self.f.service.routing_gate = lambda *_: "operator_paused"
        service = BotRoutingService(delivery=SimpleNamespace(),
            connections=SimpleNamespace(runtime_actor=lambda _: self.f.actor_a),
            bindings=SimpleNamespace(for_connection=lambda *_: []))
        service.conversation_channels = self.f.service
        result = await service.handle_inbound(BotInboundMessage(provider="slack", connection_id="slack-1",
            project_id="project-a", external_conversation_id="C1", text="hello", message_id="100.0"))
        self.assertTrue(result["ok"])
        self.assertFalse(result["routed"])
        self.assertTrue(result["pending"])
        self.assertEqual(result["reason"], "operator_paused")
        self.assertEqual(self.calls, [])
        host = slack_fixtures._Host(Path(self.f.temp.name))
        provider = slack_fixtures._service(host, slack_fixtures._Slack(), service)
        host.connection.project_id = "project-a"
        self.assertTrue(await provider._dispatch_backfill_message(host.connection, "C1",
            {"ts": "101.0", "user": "U1", "text": "hello"}, recent_message_ids=set()))
        self.assertEqual(host.events[-1]["type"], "slack_backfill_deferred")
        self.assertTrue(host.events[-1]["pending"])
        self.assertEqual(host.events[-1]["reason"], "operator_paused")
        self.assertEqual(self.calls, [])
