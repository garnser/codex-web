from __future__ import annotations

import unittest
from unittest.mock import patch
from pathlib import Path

from codex_web.conversation_channels import ConversationProjectionOutcome
from codex_web.services.conversation_channels import ConversationChannelService, ConversationChannelConflictError
from codex_web.storage.conversation_channels import ConversationChannelStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from tests import test_conversation_channels as _fixtures


class ConversationRetainedDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _fixtures.ConversationChannelServiceTests.setUp(self)

    def tearDown(self):
        _fixtures.ConversationChannelServiceTests.tearDown(self)

    async def retain(self, event_id="retained-1"):
        return await self.service.retain_raw(
            "slack", "connection-1",
            _fixtures.ConversationChannelServiceTests.slack_payload(event_id=event_id),
            actor=self.actor_a, connection_id="connection-1", project_id="project-a",
        )

    def pending(self, service=None, actor=None, **kwargs):
        return (service or self.service).pending_events(
            actor=actor or self.actor_a, connection_id="connection-1",
            project_id="project-a", **kwargs,
        )

    async def test_retention_before_routing_survives_service_reconstruction(self):
        receipts = await self.retain()
        self.assertEqual(receipts[0].outcome, ConversationProjectionOutcome.PENDING)
        self.assertEqual(self.router.messages, [])
        recovered = ConversationChannelService(
            ConversationChannelStore(SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")), self.registry,
            self.ingestion, self.router,
        )
        pending = self.pending(recovered)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].text, "hello")
        receipt = await recovered.ingest(pending[0], actor=self.actor_a)
        self.assertEqual(receipt.outcome, ConversationProjectionOutcome.ROUTED)
        self.assertEqual(len(self.router.messages), 1)
        self.assertEqual(self.pending(recovered), ())
        self.assertIsNone(receipt.retained_event)
        await recovered.ingest(pending[0], actor=self.actor_a)
        self.assertEqual(len(self.router.messages), 1)

    async def test_duplicate_retention_does_not_route_or_lose_pending_input(self):
        first = await self.retain()
        second = await self.retain()
        self.assertEqual(first, second)
        self.assertEqual(len(self.pending()), 1)
        self.assertEqual(self.router.messages, [])

    async def test_pending_recovery_pins_actor_tenant_connection_and_project(self):
        await self.retain()
        self.assertEqual(self.pending(actor=self.actor_b), ())
        other_actor = self.actor_a.model_copy(update={"identity_id": "other-runtime"})
        self.assertEqual(self.pending(actor=other_actor), ())
        self.assertEqual(self.service.pending_events(
            actor=self.actor_a, connection_id="other-connection", project_id="project-a",
        ), ())
        with self.assertRaises(ConversationChannelConflictError):
            await self.service.ingest(self.pending()[0], actor=other_actor)
        self.assertEqual(self.router.messages, [])
        self.assertEqual(len(self.pending()), 1)

    async def test_storage_failure_prevents_successful_retention(self):
        with patch.object(self.store.store, "update_many", side_effect=OSError("storage unavailable")):
            with self.assertRaisesRegex(OSError, "storage unavailable"):
                await self.retain()
        self.assertEqual(self.pending(), ())
        self.assertEqual(self.router.messages, [])
        await self.retain()
        self.assertEqual(len(self.pending()), 1)

    async def test_unknown_routing_outcome_retains_input_without_blind_replay(self):
        await self.retain()
        event = self.pending()[0]
        self.router.fail = RuntimeError("unobserved downstream outcome")
        receipt = await self.service.ingest(event, actor=self.actor_a)
        self.assertEqual(receipt.outcome, ConversationProjectionOutcome.REQUIRES_RECONCILIATION)
        self.assertIsNotNone(receipt.retained_event)
        self.assertEqual(self.pending(), ())
        await self.service.ingest(event, actor=self.actor_a)
        self.assertEqual(len(self.router.messages), 1)

    async def test_retention_pruning_preserves_pending_and_unknown_inputs(self):
        await self.retain()
        self.service.MAX_RECEIPTS = 1
        await self.service.ingest_raw(
            "slack", "connection-1",
            _fixtures.ConversationChannelServiceTests.slack_payload(event_id="terminal-1", text="second"),
            actor=self.actor_a, connection_id="connection-1", project_id="project-a",
        )
        self.assertEqual(len(self.pending()), 1)
        self.assertEqual(self.pending()[0].delivery_id, "retained-1")

    def test_legacy_state_migrates_without_changing_event_contract(self):
        self.store.store.put(self.store.namespace, {
            "schema_version": "1.0", "messages": {}, "receipts": {},
        })
        self.assertEqual(self.store.load().schema_version, "3.0")

    async def test_public_receipts_expose_pending_provenance_without_message_body(self):
        receipt = (await self.retain())[0]
        public = self.service.public_receipt(receipt)
        self.assertTrue(public["input_retained"])
        self.assertEqual(public["project_id"], "project-a")
        self.assertEqual(public["ingest_actor_id"], self.actor_a.identity_id)
        self.assertNotIn("retained_event", public)
        self.assertNotIn("text", public)



    async def test_pending_sampling_reserves_capacity_for_each_worker_partition(self):
        for index in range(4):
            await self.retain(f"hot-{index}")
        await self.retain("cold-1")
        events = self.pending(
            limit=2, partition_count=2,
            partition_index=lambda event: 1 if event.delivery_id.startswith("cold") else 0,
        )
        self.assertEqual([event.delivery_id for event in events], ["hot-0", "hot-1", "cold-1"])
