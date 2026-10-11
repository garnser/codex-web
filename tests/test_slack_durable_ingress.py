from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from codex_web.runtime.bots import SlackSocketCleanClose
from tests import test_slack_socket_backpressure as _socket
from tests import test_conversation_channels as _conversation


class SlackDurableIngressTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        await _socket.SlackSocketBackpressureTests.asyncTearDown(self)

    def runtime(self):
        runtime = _socket._runtime()
        runtime.conversation_channels = SimpleNamespace(
            retain_raw=AsyncMock(return_value=(SimpleNamespace(canonical_event_id="retained-1"),)),
            ingest_raw=AsyncMock(return_value=()),
            pending_events=Mock(return_value=()),
        )
        return runtime

    async def run_socket(self, runtime, socket):
        with patch("codex_web.runtime.bots.websockets.connect", return_value=socket):
            with self.assertRaises(SlackSocketCleanClose):
                await runtime._run_slack(_socket._connection())

    def socket(self, payload=None):
        return _socket._Socket([{
            "envelope_id": "ENV-1", "payload": payload or _socket._payload("C1", "E1"),
        }])

    async def test_ack_waits_for_durable_commit_not_memory_admission(self):
        runtime = self.runtime(); socket = self.socket()
        entered, release = asyncio.Event(), asyncio.Event()

        async def retain(*args, **kwargs):
            entered.set()
            await release.wait()
            return (SimpleNamespace(canonical_event_id="retained-1"),)

        runtime.conversation_channels.retain_raw = retain
        task = asyncio.create_task(self.run_socket(runtime, socket))
        observed = asyncio.create_task(entered.wait())
        try:
            await asyncio.wait_for(asyncio.wait({task, observed}, return_when=asyncio.FIRST_COMPLETED), 2)
            self.assertEqual(socket.sent, [])
        finally:
            release.set()
            await task
            observed.cancel()
            await asyncio.gather(observed, return_exceptions=True)
        self.assertEqual(socket.sent, [{"envelope_id": "ENV-1"}])

    async def test_failed_commit_is_never_acknowledged_even_with_memory_dedupe(self):
        runtime = self.runtime(); socket = self.socket()
        runtime.conversation_channels.retain_raw.side_effect = OSError("storage unavailable")
        runtime._admit_slack_payload = Mock(return_value="deduped")
        await self.run_socket(runtime, socket)
        self.assertEqual(socket.sent, [])
        runtime._admit_slack_payload.assert_not_called()

    async def test_missing_canonical_retention_cannot_ack_message(self):
        runtime = _socket._runtime(); socket = self.socket()
        await self.run_socket(runtime, socket)
        self.assertEqual(socket.sent, [])

    async def test_full_memory_shard_retains_and_recovers_after_runtime_reconstruction(self):
        fixture = _conversation.ConversationChannelServiceTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        runtime = _socket._runtime(); socket = self.socket()
        runtime.conversation_channels = fixture.service
        runtime.connections.runtime_actor = lambda _project: fixture.actor_a
        runtime._admit_slack_payload = Mock(return_value="rejected")
        await self.run_socket(runtime, socket)
        self.assertEqual(socket.sent, [{"envelope_id": "ENV-1"}])
        self.assertEqual(fixture.router.messages, [])
        self.assertEqual(len(fixture.service.pending_events(
            actor=fixture.actor_a, connection_id="slack-1", project_id="home",
        )), 1)
        recovered = _socket._runtime()
        recovered.conversation_channels = fixture.service
        recovered.connections.runtime_actor = lambda _project: fixture.actor_a
        await recovered._recover_retained_slack(_socket._connection())
        await asyncio.gather(*(q.join() for q in recovered.slack_payload_queues["slack-1"]))
        self.assertEqual(len(fixture.router.messages), 1)
        await recovered._recover_retained_slack(_socket._connection())
        self.assertEqual(len(fixture.router.messages), 1)
        await recovered._stop_slack_payload_workers(_socket._connection())

    async def test_approval_interaction_ack_waits_for_existing_resolution_handler(self):
        runtime = self.runtime(); socket = self.socket({"type": "block_actions", "actions": []})
        entered, release = asyncio.Event(), asyncio.Event()

        async def handle(*args):
            entered.set()
            await release.wait()

        runtime.delivery.handle_slack_interaction = handle
        task = asyncio.create_task(self.run_socket(runtime, socket))
        observed = asyncio.create_task(entered.wait())
        try:
            await asyncio.wait_for(asyncio.wait({task, observed}, return_when=asyncio.FIRST_COMPLETED), 2)
            self.assertEqual(socket.sent, [])
        finally:
            release.set()
            await task
            observed.cancel()
            await asyncio.gather(observed, return_exceptions=True)
        self.assertEqual(socket.sent, [{"envelope_id": "ENV-1"}])
        runtime.conversation_channels.retain_raw.assert_not_called()

    async def test_pending_input_retries_after_preclaim_failure_despite_memory_seen_key(self):
        fixture = _conversation.ConversationChannelServiceTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        runtime = _socket._runtime(); socket = self.socket()
        runtime.conversation_channels = fixture.service
        runtime.connections.runtime_actor = lambda _project: fixture.actor_a
        original_ingest = fixture.service.ingest
        fixture.service.ingest = AsyncMock(side_effect=RuntimeError("before routing claim"))
        await self.run_socket(runtime, socket)
        await asyncio.gather(*(q.join() for q in runtime.slack_payload_queues["slack-1"]))
        self.assertEqual(fixture.router.messages, [])
        self.assertIn("event:E1", runtime.slack_payload_seen["slack-1"])
        fixture.service.ingest = original_ingest
        await runtime._recover_retained_slack(_socket._connection())
        await asyncio.gather(*(q.join() for q in runtime.slack_payload_queues["slack-1"]))
        self.assertEqual(len(fixture.router.messages), 1)
        await runtime._stop_slack_payload_workers(_socket._connection())


    async def test_new_live_message_cannot_overtake_older_durable_pending_message(self):
        fixture = _conversation.ConversationChannelServiceTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        runtime = _socket._runtime()
        runtime.conversation_channels = fixture.service
        runtime.connections.runtime_actor = lambda _project: fixture.actor_a
        await fixture.service.retain_raw(
            "slack", "slack-1", _socket._payload("C1", "OLDER"),
            actor=fixture.actor_a, connection_id="slack-1", project_id="home",
        )
        await self.run_socket(runtime, self.socket(_socket._payload("C1", "NEWER")))
        await asyncio.gather(*(q.join() for q in runtime.slack_payload_queues["slack-1"]))
        self.assertEqual([message.message_id for message in fixture.router.messages], ["OLDER", "NEWER"])
        await runtime._stop_slack_payload_workers(_socket._connection())

    async def test_recovery_store_failure_keeps_committed_input_acknowledged(self):
        runtime = self.runtime()
        runtime.conversation_channels.pending_events.side_effect = OSError("recovery unavailable")
        socket = self.socket()
        await self.run_socket(runtime, socket)
        self.assertEqual(socket.sent, [{"envelope_id": "ENV-1"}])
        self.assertTrue(any(event["type"] == "slack_payload_recovery_failed" for event in runtime.telemetry.events))
