"""Real Project pause decisions with isolated stores and counting transports."""
import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from fastapi import FastAPI
import httpx

from codex_web.api.reconciliation_gates import build_reconciliation_gates_router
from codex_web.conversation_channels import ConversationProjectionOutcome
from codex_web.models import ApprovalSlackMessage, BotBinding
from codex_web.reconciliation_gates import ReconcilerDeclaration, ReconcilerStartupClass
from codex_web.runtime.bots import BotRuntime
from codex_web.services.bot_delivery import BotDeliveryService, install_bot_delivery_service
from codex_web.services.bot_runtime_gate import BotRuntimeGateClosed, project_bot_runtime_gate
from codex_web.services.bot_runtime_telemetry import BotRuntimeTelemetry
from codex_web.services.reconciliation_gates import ReconciliationGateService
from codex_web.storage.sqlite_state import SQLiteStateStore
from tests.test_bot_routing_delivery import _DeliveryHost, _FakeSlackClient, _FakeTelegramClient
from tests import test_conversation_channels as conversation_fixtures
from tests import test_slack_provider as slack_fixtures
from codex_web.api.slack import build_slack_router
from tests.test_reconciliation_gates import _actor


class _ListenerProbe(BotRuntime):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.started = []
        self.stopped = []

    async def _run_connection(self, connection):
        self.started.append(connection.id)
        self.telemetry.set_status(connection, "connected")
        try:
            await asyncio.Event().wait()
        finally:
            self.stopped.append(connection.id)


class BotRuntimePauseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.actor = _actor()
        self.gates = ReconciliationGateService(SQLiteStateStore(self.root / "state.sqlite3"),
            readiness=lambda *_: {"execution_ready": False, "checks": []}, identity=object())
        self.gates.register(ReconcilerDeclaration(service_id="bot-runtime",
            startup_class=ReconcilerStartupClass.PRE_READINESS_BOUNDED))
        self.host = _DeliveryHost()
        self.host.connections["slack-conn"].slack_app_token = "fixture-app-token"
        self.host.connections["slack-other"] = self.host.connections["slack-conn"].model_copy(
            update={"id": "slack-other", "project_id": "other"})
        self.host._thread_project_id = lambda thread: "other" if thread == "other-thread" else "home"
        self.rows = list(self.host.connections.values())
        self.connections = SimpleNamespace(load_connections=lambda: self.rows,
            get=lambda key: self.host.connections[key], runtime_actor=lambda _: self.actor,
            projects=SimpleNamespace(get=lambda _: SimpleNamespace(
                organization_id=self.actor.organization_id, workspace_id=self.actor.workspace_id)))
        self.gate = project_bot_runtime_gate(self.gates, self.connections)
        self.slack, self.telegram = _FakeSlackClient(), _FakeTelegramClient()
        self.telemetry = BotRuntimeTelemetry(events_file=self.root / "events.jsonl")
        self.binding = BotBinding(id="binding", connection_id="slack-conn", provider="slack",
            external_conversation_id="fixture-channel", thread_id="thread", project_id="home",
            created_at=1, updated_at=1)
        self.delivery = BotDeliveryService(self.host, gate_reason=self.gate,
            slack_client=self.slack, telegram_client=self.telegram, telemetry=self.telemetry)
        self.runtime = None

    async def asyncTearDown(self):
        if self.runtime is not None:
            await self.runtime.stop()
            for row in self.rows:
                await self.runtime._stop_slack_payload_workers(row)

    def pause(self, project="home"):
        return self.gates.pause("bot-runtime", project, actor=self.actor, reason="Isolated operator pause")

    def resume(self, project="home"):
        return self.gates.resume("bot-runtime", project, actor=self.actor)

    def listener(self):
        self.runtime = _ListenerProbe(connections=self.connections, bindings=SimpleNamespace(),
            presentation=SimpleNamespace(), telemetry=self.telemetry, routing=SimpleNamespace(),
            delivery=self.delivery, publish_event=self.publish, gate_reason=self.gate)
        return self.runtime

    @staticmethod
    async def publish(_event):
        pass

    def approvals(self):
        self.messages = {}
        self.pending = {"approval": {"id": "approval", "threadId": "thread"}}
        def remember(request_id, **fields):
            self.messages.setdefault(str(request_id), []).append(ApprovalSlackMessage(
                request_id=str(request_id), **fields, created_at=1))
        self.delivery.approvals = SimpleNamespace(thread_id=lambda request: request["threadId"],
            remember_message=remember, load_approval_messages=lambda: self.messages,
            forget_messages=lambda key: self.messages.pop(str(key), None),
            pending=lambda: self.pending, request_id_value=lambda value: value)
        self.delivery.bindings = SimpleNamespace(for_thread=lambda _: [self.binding])
        self.delivery.targets.outbound_bindings_for_thread = lambda _, bindings: bindings
        self.delivery.targets.active_reply_target_for_binding = lambda _: None
        self.delivery.targets.reply_target_for_binding = lambda _: None
        self.delivery.targets.thread_target_for_outbound = (
            lambda binding, reply=None: self.host._thread_target_for_outbound(binding, reply))
        self.delivery.presentation.binding_prefix = lambda _: "Fixture"
        self.delivery.presentation.approval_blocks = lambda *_: []
        self.delivery.presentation.approval_resolved_blocks = lambda *_: []

    async def test_pause_blocks_direct_explicit_and_telegram_sends_without_credential_use(self):
        self.pause()
        used = []
        async def use(*args, **kwargs):
            used.append(args)
            raise AssertionError("Paused credential use")
        self.delivery.secret_broker = SimpleNamespace(use_async=use)
        self.host.connections["slack-conn"].bot_token_secret_id = "fixture-reference"
        for credential in (None, "fixture-explicit-credential"):
            result = await self.delivery.send_outbound(self.binding, "fixture", credential=credential)
            self.assertEqual(result, {"sent": False, "reason": "operator_paused"})
        binding = self.binding.model_copy(update={"provider": "telegram", "connection_id": "telegram-conn"})
        self.assertFalse((await self.delivery.send_outbound(binding, "fixture"))["sent"])
        self.assertEqual((self.slack.posts, self.telegram.messages, used), ([], [], []))

    async def test_other_project_and_pre_readiness_resume_remain_independent(self):
        self.pause()
        other = self.binding.model_copy(update={"project_id": "other", "thread_id": "other-thread",
                                               "connection_id": "slack-other"})
        self.assertTrue((await self.delivery.send_outbound(other, "other Project"))["sent"])
        self.assertFalse((await self.delivery.send_outbound(self.binding, "paused Project"))["sent"])
        self.assertTrue(self.resume().eligible)  # PRE_READINESS remains bounded, not readiness-gated.
        self.assertTrue((await self.delivery.send_outbound(self.binding, "resumed Project"))["sent"])
        self.assertEqual(len(self.slack.posts), 2)

    async def test_pause_during_credential_acquisition_prevents_transport(self):
        used = []
        async def acquire(*args, consumer, **kwargs):
            used.append(True)
            self.pause()
            return await consumer("fixture-token")
        self.delivery.connections = self.connections
        self.delivery.secret_broker = SimpleNamespace(use_async=acquire)
        self.host.connections["slack-conn"].bot_token_secret_id = "fixture-reference"
        result = await self.delivery.send_outbound(self.binding, "fixture")
        self.assertEqual(result, {"sent": False, "reason": "operator_paused"})
        self.assertEqual(used, [True])
        self.assertEqual(self.slack.posts, [])

    async def test_pause_does_not_relabel_a_provider_send_already_performed(self):
        async def post(*args, **kwargs):
            self.slack.posts.append((args, kwargs))
            self.pause()
            return {"sent": True, "providerResponse": {"ok": True, "ts": "fixture-receipt"}}
        self.slack.post_message = post
        result = await self.delivery.send_outbound(self.binding, "admitted before pause")
        self.assertTrue(result["sent"])
        self.assertEqual(result["providerResponse"]["ts"], "fixture-receipt")
        self.assertFalse((await self.delivery.send_outbound(self.binding, "after pause"))["sent"])
        self.assertEqual(len(self.slack.posts), 1)

    async def test_unavailable_or_foreign_gate_refuses_without_transport(self):
        self.connections.runtime_actor = lambda _: (_ for _ in ()).throw(ValueError("private detail"))
        result = await self.delivery.send_outbound(self.binding, "fixture")
        self.assertEqual(result, {"sent": False, "reason": "bot_runtime_gate_unavailable"})
        foreign = self.actor.model_copy(update={"organization_id": "foreign"})
        self.assertEqual(self.gate("home", foreign), "bot_runtime_project_scope_denied")
        self.assertEqual(self.slack.posts, [])

    async def test_pause_stops_only_its_listener_and_resume_restarts_it(self):
        self.rows = [self.host.connections["slack-conn"], self.host.connections["slack-other"]]
        runtime = self.listener()
        await runtime.sync()
        await asyncio.sleep(0)
        self.assertCountEqual(runtime.started, ["slack-conn", "slack-other"])
        self.pause()
        await runtime.sync()
        self.assertEqual(runtime.stopped, ["slack-conn"])
        self.assertEqual(set(runtime.tasks), {"slack-other"})
        self.assertEqual(self.telemetry.status["slack-conn"]["gateReason"], "operator_paused")
        self.resume()
        await runtime.sync()
        await asyncio.sleep(0)
        self.assertEqual(runtime.started.count("slack-conn"), 2)
        self.assertEqual(runtime.started.count("slack-other"), 1)

    async def test_gate_paused_before_start_never_opens_listener_or_payload_workers(self):
        self.rows = [self.host.connections["slack-conn"]]
        self.pause()
        runtime = self.listener()
        await runtime.sync()
        self.assertEqual(runtime.tasks, {})
        self.assertEqual(runtime.started, [])
        self.assertEqual(runtime._admit_slack_payload(self.rows[0], {"event_id": "fixture"}), "paused")
        self.assertEqual(runtime.slack_payload_workers, {})

    async def test_remote_approval_and_expired_interaction_preserve_local_pending_state(self):
        self.approvals()
        before = deepcopy(self.pending)
        self.pause()
        for request_id in ("approval", "expired"):
            with self.assertRaises(BotRuntimeGateClosed):
                await self.delivery.handle_slack_interaction(self.host.connections["slack-conn"], {
                    "actions": [{"action_id": "codex_approval_accept",
                                 "value": '{"request_id":"'+request_id+'","decision":"accept"}'}]})
        await self.delivery.record_approval_request(before["approval"])
        self.assertEqual(self.pending, before)
        self.assertEqual((self.slack.posts, self.slack.updates, self.messages), ([], [], {}))

    async def test_paused_approval_update_keeps_original_message_reference(self):
        self.approvals()
        request = self.pending["approval"]
        await self.delivery.record_approval_request(request)
        before = deepcopy(self.messages)
        self.assertEqual(len(self.slack.posts), 1)
        self.pause()
        await self.delivery.update_approval_messages("approval", request, decision="accept", actor="Local UI")
        self.assertEqual(self.messages, before)
        self.assertEqual(self.slack.updates, [])
        self.resume()
        await self.delivery.update_approval_messages("approval", request, decision="accept", actor="Local UI")
        self.assertEqual(len(self.slack.updates), 1)
        self.assertEqual(self.messages, {})

    async def test_approval_update_paused_during_credentials_keeps_the_original_reference(self):
        self.approvals()
        request = self.pending["approval"]
        await self.delivery.record_approval_request(request)
        before = deepcopy(self.messages)
        self.delivery.connections = self.connections
        self.host.connections["slack-conn"].bot_token_secret_id = "fixture-reference"
        async def acquire(*args, consumer, **kwargs):
            self.pause()
            return await consumer("fixture-token")
        self.delivery.secret_broker = SimpleNamespace(use_async=acquire)
        await self.delivery.update_approval_messages("approval", request, decision="accept", actor="Local UI")
        self.assertEqual(self.messages, before)
        self.assertEqual(self.slack.updates, [])

    async def test_local_native_detail_recording_remains_available_while_paused(self):
        self.pause()
        recorded = []
        self.delivery.presentation.format_detail_item = lambda _: {"title": "Fixture", "text": "Local result"}
        self.delivery.details = SimpleNamespace(record=lambda *args: recorded.append(args))
        await self.delivery.record_outbound({"method": "item/completed", "params": {
            "threadId": "thread", "item": {"type": "commandExecution"}}})
        self.assertEqual(recorded, [("thread", "commandExecution", "Fixture", "Local result")])
        self.assertEqual(self.slack.posts, [])

    async def test_compatibility_sender_keeps_the_same_gate(self):
        app = FastAPI()
        install_bot_delivery_service(app, self.host, slack_client=self.slack,
            telegram_client=self.telegram, gate_reason=self.gate)
        self.pause()
        result = await self.host._send_bot_outbound(self.binding, "fixture")
        self.assertEqual(result, {"sent": False, "reason": "operator_paused"})
        self.assertEqual(self.slack.posts, [])

    async def test_api_pause_and_resume_refresh_the_existing_runtime(self):
        self.rows = [self.host.connections["slack-conn"]]
        runtime = self.listener()
        app = FastAPI()
        @app.middleware("http")
        async def authenticated(request, call_next):
            request.state.identity_actor = self.actor
            return await call_next(request)
        async def refresh(service_id, project_id):
            self.assertEqual((service_id, project_id), ("bot-runtime", "home"))
            await runtime.sync()
        app.include_router(build_reconciliation_gates_router(self.gates, on_control_change=refresh))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://fixture") as client:
            await runtime.sync()
            await asyncio.sleep(0)
            response = await client.post("/api/projects/home/reconciliation-gates/bot-runtime/pause",
                                         json={"reason": "Fixture operator"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()["paused"])
            self.assertEqual(runtime.tasks, {})
            response = await client.post("/api/projects/home/reconciliation-gates/bot-runtime/resume")
            self.assertEqual(response.status_code, 200, response.text)
            await asyncio.sleep(0)
            self.assertIn("slack-conn", runtime.tasks)

    async def test_refresh_failure_reports_committed_control_without_private_error(self):
        app = FastAPI()
        @app.middleware("http")
        async def authenticated(request, call_next):
            request.state.identity_actor = self.actor
            return await call_next(request)
        async def fail(*_):
            raise RuntimeError("private-transport-detail")
        app.include_router(build_reconciliation_gates_router(self.gates, on_control_change=fail))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://fixture") as client:
            response = await client.post("/api/projects/home/reconciliation-gates/bot-runtime/pause",
                                         json={"reason": "Fixture operator"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "reconciliation_runtime_refresh_failed")
        self.assertTrue(response.json()["detail"]["control"]["paused"])
        self.assertNotIn("private-transport-detail", response.text)
        self.assertFalse(self.gates.decision("bot-runtime", "home", actor=self.actor).eligible)

    async def test_public_slack_notice_rechecks_pause_after_credentials(self):
        host = slack_fixtures._Host(self.root)
        host.connection.bot_token_secret_id = "fixture-reference"
        slack, routing = slack_fixtures._Slack(), slack_fixtures._Routing()
        routing.result = {"ok": True, "ambiguous": True, "availablePrefixes": ["Alice", "Bob"]}
        service = slack_fixtures._service(host, slack, routing)
        service.gate_reason = self.gate
        service.bindings.first_for_connection = lambda *_: SimpleNamespace(connection_id=host.connection.id)
        service.connections.get = lambda _: host.connection
        credential_uses = []
        async def use(secret_id, **kwargs):
            credential_uses.append(secret_id)
            self.pause()
            return await kwargs["consumer"]("fixture-token")
        service.secret_broker = SimpleNamespace(use_async=use)
        app = FastAPI()
        app.include_router(build_slack_router(service))
        payload = {"type": "event_callback", "event_id": "notice-race", "event": {
            "type": "message", "channel": "C1", "user": "U1", "text": "hello", "ts": "100.0"}}
        original = deepcopy(payload)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://fixture") as client:
            response = await client.post("/bots/slack/events", json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["notice"], {"sent": False, "reason": "operator_paused"})
        self.assertEqual(credential_uses, ["fixture-reference"])
        self.assertEqual(slack.posts, [])
        self.assertEqual(payload, original)
        self.assertEqual(len(routing.messages), 1)  # Already admitted input is not replayed or relabelled.
        self.resume()
        await service._with_outbound_credential(host.connection, "fixture-notice", lambda token:
            slack.post_message(token, "C1", "resumed"))
        self.assertEqual(slack.posts, [])  # This credential acquisition pauses again.
        # Historical reads are governed separately and remain usable during this pause.
        result = await service._with_credential(host.connection, "bot_token", "slack.history",
            lambda token: slack.history(token, "C1", oldest="0"))
        self.assertTrue(result["ok"])

    async def test_public_slack_pause_preserves_canonical_pending_input(self):
        f = self.conversation_fixture()
        host = slack_fixtures._Host(self.root)
        host.connection.project_id = "project-a"
        service = slack_fixtures._service(host, slack_fixtures._Slack(), slack_fixtures._Routing())
        service.connections.runtime_actor = lambda _: f.actor_a
        service.conversation_channels = f.service
        service.gate_reason = self.gate
        app = FastAPI()
        app.include_router(build_slack_router(service))
        payload = {"type": "event_callback", **f.slack_payload(event_id="public-pending")}
        self.pause("project-a")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://fixture") as client:
            response = await client.post("/bots/slack/events", json=payload)
            self.assertEqual(response.status_code, 200)
            first = f.store.load().model_dump(mode="json")
            receipt = next(iter(f.store.load().receipts.values()))
            self.assertEqual(receipt.outcome, ConversationProjectionOutcome.PENDING)
            self.assertIsNotNone(receipt.retained_event)
            response = await client.post("/bots/slack/events", json=payload)
            self.assertEqual(response.status_code, 200)
        self.assertEqual(f.store.load().model_dump(mode="json"), first)
        self.assertEqual(f.router.messages, [])
        self.assertEqual(service.slack.posts, [])

    def conversation_fixture(self):
        fixture = conversation_fixtures.ConversationChannelServiceTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.service.routing_gate = self.gate
        return fixture

    async def test_retained_ingress_is_pending_while_paused_then_routes_once(self):
        f = self.conversation_fixture()
        self.pause("project-a")
        payload = f.slack_payload(event_id="original")
        receipts = await f.service.ingest_raw("slack", "slack-1", payload,
            actor=f.actor_a, connection_id="slack-1", project_id="project-a")
        original = receipts[0]
        self.assertEqual(original.outcome, ConversationProjectionOutcome.PENDING)
        stored = f.store.load().receipts[original.canonical_event_id].model_dump(mode="json")
        self.assertEqual(f.router.messages, [])
        self.assertFalse(f.service.legacy_routing_result(original)["routed"])
        await f.service.ingest_raw("slack", "slack-1", payload,
            actor=f.actor_a, connection_id="slack-1", project_id="project-a")
        self.assertEqual(f.store.load().receipts[original.canonical_event_id].model_dump(mode="json"), stored)
        self.resume("project-a")
        event = (await f.registry.resolve("slack", "slack-1").normalize_events(payload,
            connection_id="slack-1", project_id="project-a"))[0]
        result = await f.service.ingest(event, actor=f.actor_a)
        self.assertEqual(result.outcome, ConversationProjectionOutcome.ROUTED)
        await f.service.ingest(event, actor=f.actor_a)
        self.assertEqual(len(f.router.messages), 1)

    async def test_pause_during_retention_keeps_original_input_without_routing_claim(self):
        f = self.conversation_fixture()
        retain = f.service.retain
        async def retain_then_pause(*args, **kwargs):
            result = await retain(*args, **kwargs)
            self.pause("project-a")
            return result
        f.service.retain = retain_then_pause
        receipts = await f.service.ingest_raw("slack", "slack-1", f.slack_payload(event_id="race"),
            actor=f.actor_a, connection_id="slack-1", project_id="project-a")
        self.assertEqual(receipts[0].outcome, ConversationProjectionOutcome.PENDING)
        self.assertEqual(f.router.messages, [])
        self.assertEqual(f.store.load().receipts[receipts[0].canonical_event_id].outcome,
                         ConversationProjectionOutcome.PENDING)

    async def test_runtime_retained_recovery_routes_the_same_input_once_after_resume(self):
        f = self.conversation_fixture()
        self.connections.runtime_actor = lambda _: f.actor_a
        connection = self.host.connections["slack-conn"].model_copy(update={"project_id": "project-a"})
        self.rows = [connection]
        self.pause("project-a")
        receipts = await f.service.ingest_raw("slack", connection.id, f.slack_payload(event_id="retained"),
            actor=f.actor_a, connection_id=connection.id, project_id="project-a")
        original = f.store.load().receipts[receipts[0].canonical_event_id].model_dump(mode="json")
        runtime = self.listener()
        runtime.conversation_channels = f.service
        await runtime._recover_retained_slack(connection)
        self.assertEqual(runtime.slack_payload_workers, {})
        self.assertEqual(f.store.load().receipts[receipts[0].canonical_event_id].model_dump(mode="json"), original)
        self.resume("project-a")
        await runtime._recover_retained_slack(connection)
        for queue in runtime.slack_payload_queues[connection.id]:
            await asyncio.wait_for(queue.join(), 2)
        self.assertEqual(len(f.router.messages), 1)
        await runtime._recover_retained_slack(connection)
        for queue in runtime.slack_payload_queues[connection.id]:
            await asyncio.wait_for(queue.join(), 2)
        self.assertEqual(len(f.router.messages), 1)
        self.assertEqual(runtime.slack_payload_stats[connection.id]["processed"], 1)

    async def test_pause_after_memory_admission_defers_without_claim_or_false_processing(self):
        f = self.conversation_fixture()
        self.connections.runtime_actor = lambda _: f.actor_a
        connection = self.host.connections["slack-conn"].model_copy(update={"project_id": "project-a"})
        self.rows = [connection]
        payload = f.slack_payload(event_id="admitted-before-pause")
        event = (await f.registry.resolve("slack", connection.id).normalize_events(payload,
            connection_id=connection.id, project_id="project-a"))[0]
        retained = await f.service.retain(event, actor=f.actor_a)
        original = f.store.load().receipts[retained.canonical_event_id].model_dump(mode="json")
        runtime = self.listener()
        runtime.conversation_channels = f.service
        self.assertEqual(runtime._admit_slack_payload(connection, payload, retained_event=event), "accepted")
        self.pause("project-a")
        for queue in runtime.slack_payload_queues[connection.id]:
            await asyncio.wait_for(queue.join(), 2)
        self.assertEqual(runtime.slack_payload_stats[connection.id]["processed"], 0)
        self.assertEqual(runtime.slack_payload_stats[connection.id]["deferred"], 1)
        self.assertEqual(f.store.load().receipts[retained.canonical_event_id].model_dump(mode="json"), original)
        self.resume("project-a")
        await runtime._recover_retained_slack(connection)
        for queue in runtime.slack_payload_queues[connection.id]:
            await asyncio.wait_for(queue.join(), 2)
        self.assertEqual(len(f.router.messages), 1)

    async def test_telegram_pause_during_retention_does_not_advance_durable_offset(self):
        f = self.conversation_fixture()
        self.connections.runtime_actor = lambda _: f.actor_a
        updates = []
        self.connections.update = lambda *args, **kwargs: updates.append((args, kwargs))
        retain = f.service.retain
        async def retain_then_pause(*args, **kwargs):
            result = await retain(*args, **kwargs)
            self.pause()
            return result
        f.service.retain = retain_then_pause
        async def get_updates(*args, **kwargs):
            return {"ok": True, "result": [{"update_id": 7, "message": {
                "message_id": 9, "chat": {"id": 1}, "from": {"id": 2}, "text": "fixture"}}]}
        self.telegram.get_updates = get_updates
        runtime = self.listener()
        runtime.telegram = self.telegram
        runtime.conversation_channels = f.service
        with self.assertRaises(BotRuntimeGateClosed):
            await runtime._run_telegram(self.host.connections["telegram-conn"])
        self.assertEqual(updates, [])
        self.assertEqual(f.router.messages, [])
        receipts = list(f.store.load().receipts.values())
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0].outcome, ConversationProjectionOutcome.PENDING)

    async def test_pause_does_not_turn_an_existing_uncertain_claim_back_into_pending(self):
        f = self.conversation_fixture()
        event = (await f.registry.resolve("slack", "slack-1").normalize_events(
            f.slack_payload(event_id="prior-claim"), connection_id="slack-1", project_id="project-a"))[0]
        retained = await f.service.retain(event, actor=f.actor_a)
        _, claimed = f.service._claim(canonical_event_id=retained.canonical_event_id, event=event, actor=f.actor_a)
        self.assertTrue(claimed)
        before = f.store.load().receipts[retained.canonical_event_id].model_dump(mode="json")
        self.pause("project-a")
        result = await f.service.ingest(event, actor=f.actor_a)
        self.assertEqual(result.outcome, ConversationProjectionOutcome.REQUIRES_RECONCILIATION)
        self.assertEqual(f.store.load().receipts[retained.canonical_event_id].model_dump(mode="json"), before)
        self.assertEqual(f.router.messages, [])
