from __future__ import annotations

import asyncio
import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from codex_web.crypto import KeyPurpose, ManagedKeyCreate
from codex_web.identity import AuthenticationActor, AuthenticationAssurance, PrincipalKind
from codex_web.key_backends import LocalFileKeyBackend
from codex_web.services.canonical_events import CanonicalEventBus, CanonicalEventIngestionService
from codex_web.services.conversation_channel_adapters import SlackConversationChannel
from codex_web.services.conversation_channels import (
    ConversationChannelConflictError, ConversationChannelRegistry, ConversationChannelService,
)
from codex_web.services.crypto_keys import CryptoKeyService
from codex_web.services.data_governance import DataGovernanceService
from codex_web.storage.canonical_events import CanonicalEventStore, CanonicalEventConflictError
from codex_web.storage.conversation_channels import ConversationChannelStore
from codex_web.storage.crypto_keys import CryptoKeyStore
from codex_web.storage.data_governance import DataGovernanceStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from tests.test_slack_socket_backpressure import _runtime, _connection, _Socket
from codex_web.runtime.bots import SlackSocketCleanClose


class _CommitGate(SQLiteStateStore):
    """Pause inside a real SQLite transaction, after mutation and before COMMIT."""

    def __init__(self, path):
        super().__init__(path)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.hold = False
        self.fail = False

    def update_many(self, defaults, updater):
        def guarded(documents):
            result = updater(documents)
            if self.hold and ConversationChannelStore.namespace in defaults:
                self.hold = False
                self.entered.set()
                if not self.release.wait(4):
                    raise TimeoutError("test commit gate was not released")
                if self.fail:
                    raise OSError("controlled storage failure before commit")
            return result
        return super().update_many(defaults, guarded)


class SlackRetentionIdentityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="slack-retention-identity-")
        self.root = Path(self.temp.name)
        self.database = self.root / "state.sqlite3"
        self.actor = AuthenticationActor(identity_id="authenticated-runtime", principal_kind=PrincipalKind.SERVICE,
            organization_id="org", workspace_id="workspace", assurance=AuthenticationAssurance.SERVICE_TOKEN,
            service_scopes=("crypto:admin",))
        self.store = _CommitGate(self.database)
        crypto = CryptoKeyService(CryptoKeyStore(self.store), {"local": LocalFileKeyBackend(self.root / "keys")})
        for project in ("project-a", "project-b", "home"):
            crypto.create_key(ManagedKeyCreate(project_id=project, purpose=KeyPurpose.INTEGRATION_PAYLOAD), actor=self.actor)
        self.authority_revision = 1
        self.first = self.service(self.store)
        self.second = self.service(SQLiteStateStore(self.database))
        self.acks = []

    def tearDown(self):
        self.store.release.set()
        self.temp.cleanup()

    def service(self, store):
        registry = ConversationChannelRegistry()
        registry.register_provider("slack", SlackConversationChannel)
        return ConversationChannelService(ConversationChannelStore(store), registry,
            CanonicalEventIngestionService(CanonicalEventBus(CanonicalEventStore(store))),
            AsyncMock(side_effect=AssertionError("routing is not authorized by this fixture")),
            crypto=CryptoKeyService(CryptoKeyStore(store), {"local": LocalFileKeyBackend(self.root / "keys")}),
            governance=DataGovernanceService(DataGovernanceStore(store)),
            authority_snapshot=lambda event, actor: {
                "connection": event.connection_id, "project": event.project_id,
                "account": "canonical-account", "revision": self.authority_revision,
            })

    @staticmethod
    def payload(text="  original <@U2> message\n", *, conversation="C1"):
        return {"type": "event_callback", "event_id": "shared-delivery-id",
            "team_id": "untrusted-team-claim", "event": {"type": "message", "user": "U1",
                "channel": conversation, "ts": "100.1", "thread_ts": "99.9", "text": text}}

    async def retain_and_ack(self, service, payload, connection="connection-a", project="project-a"):
        receipts = await service.retain_raw("slack", "same-provider-instance", payload,
            actor=self.actor, connection_id=connection, project_id=project)
        # Controlled transport boundary: ACK is observable only after the real
        # canonical service has returned a committed, compatible receipt.
        self.acks.append((connection, project, receipts[0].canonical_event_id))
        return receipts[0]

    async def test_concurrent_same_delivery_two_connections_and_projects_retain_both_originals(self):
        one = self.payload("first original")
        two = self.payload("second original")
        first, second = await asyncio.gather(self.retain_and_ack(self.first, one),
            self.retain_and_ack(self.second, two, "connection-b", "project-b"))
        self.assertNotEqual(first.canonical_event_id, second.canonical_event_id)
        self.assertEqual(len(self.acks), 2)
        reconstructed = self.service(SQLiteStateStore(self.database))
        receipts = reconstructed.store.load().receipts
        self.assertEqual(len(receipts), 2)
        for receipt, original in ((first, one), (second, two)):
            content = reconstructed.retained_input(receipts[receipt.canonical_event_id], self.actor)
            self.assertEqual(content["original_payload"], original)
            self.assertIsNotNone(receipts[receipt.canonical_event_id].protected_input)
        # Only metadata is retained in canonical events. No plaintext fallback.
        persisted = json.dumps(self.store.get("conversation_channels"))
        canonical = json.dumps(CanonicalEventStore(self.store).load().model_dump(mode="json"))
        self.assertNotIn("first original", persisted + canonical)
        self.assertNotIn("second original", persisted + canonical)

    async def test_scoped_message_positions_do_not_suppress_another_retained_original(self):
        self.first.route_handler = AsyncMock(return_value={"ok":True,"threadId":"first-thread"})
        self.second.route_handler = AsyncMock(return_value={"ok":True,"threadId":"second-thread"})
        first = (await self.first.retain_raw("slack","same-instance",self.payload(),actor=self.actor,
            connection_id="connection-a",project_id="project-a"))[0]
        second = (await self.second.retain_raw("slack","same-instance",self.payload(),actor=self.actor,
            connection_id="connection-b",project_id="project-b"))[0]
        for service, receipt in ((self.first,first),(self.second,second)):
            event = service.pending_events(actor=self.actor,connection_id=receipt.connection_id,project_id=receipt.project_id)[0]
            routed = await service.ingest(event,actor=self.actor)
            self.assertEqual(routed.outcome.value,"routed")
            service.route_handler.assert_awaited_once()
        positions = self.first.store.load().messages
        self.assertEqual(len(positions),2)
        self.assertEqual({row.connection_id for row in positions.values()},{"connection-a","connection-b"})
        self.assertEqual({row.project_id for row in positions.values()},{"project-a","project-b"})

    async def test_connection_and_project_are_independent_identity_dimensions(self):
        original = self.payload()
        receipts = await asyncio.gather(self.retain_and_ack(self.first, original),
            self.retain_and_ack(self.second, original, "connection-b", "project-a"),
            self.retain_and_ack(self.second, original, "connection-a", "project-b"))
        self.assertEqual(len({row.canonical_event_id for row in receipts}), 3)
        self.assertEqual(len(self.first.store.load().receipts), 3)

    async def test_changed_substantive_input_does_not_ack_or_replace_original(self):
        original = self.payload()
        receipt = await self.retain_and_ack(self.first, original)
        before = self.first.store.load().receipts[receipt.canonical_event_id].model_dump(mode="json")
        for change in ("text", "user", "ts", "thread_ts"):
            altered = copy.deepcopy(original)
            altered["event"][change] = "substantive-change"
            with self.subTest(field=change):
                with self.assertRaises((CanonicalEventConflictError, ConversationChannelConflictError)):
                    await self.retain_and_ack(self.second, altered)
        # Provenance which is not normalized into text is still substantive.
        altered = copy.deepcopy(original)
        altered["team_id"] = "different-source-claim"
        with self.assertRaises((CanonicalEventConflictError, ConversationChannelConflictError)):
            await self.retain_and_ack(self.second, altered)
        self.assertEqual(len(self.acks), 1)
        self.assertEqual(self.first.store.load().receipts[receipt.canonical_event_id].model_dump(mode="json"), before)
        self.assertEqual(self.first.retained_input(receipt, self.actor)["original_payload"], original)

    async def test_equivalent_retry_metadata_acks_original_receipt_without_replacing_input(self):
        original = self.payload()
        original.update(envelope_id="envelope-original", retry_attempt=0, accepts_response_payload=False)
        first = await self.retain_and_ack(self.first, original)
        retry = copy.deepcopy(original)
        retry.update(envelope_id="envelope-retry", retry_attempt=3,
            retry_reason="timeout", accepts_response_payload=True)
        before = self.store.documents()
        with patch.object(self.second.crypto, "encrypt", side_effect=AssertionError("equivalent duplicate must reuse protection")), patch.object(self.second.governance, "register_domain_record", side_effect=AssertionError("equivalent duplicate must reuse governance")):
            second = await self.retain_and_ack(self.second, retry)
        self.assertEqual(self.store.documents(), before)
        self.assertEqual(len(self.second.governance.store.load().records), 1)
        self.assertEqual(first.canonical_event_id, second.canonical_event_id)
        self.assertEqual(first.model_dump(mode="json"), second.model_dump(mode="json"))
        self.assertEqual(len(self.acks), 2)
        self.assertEqual(self.second.retained_input(second, self.actor)["original_payload"], original)
        # Exclusions apply to the transport wrapper, never message content.
        retry["event"]["retry_reason"] = "new message data"
        with self.assertRaises((CanonicalEventConflictError, ConversationChannelConflictError)):
            await self.retain_and_ack(self.second, retry)
        self.assertEqual(len(self.acks), 2)

    async def test_duplicate_waits_for_original_commit_with_no_uncommitted_ack(self):
        # Let BOTH encrypted candidates reach canonical event admission before
        # holding the first transaction. The duplicate must wait on the actual
        # canonical transaction, rather than only on an earlier crypto audit.
        prepared = threading.Barrier(2)
        duplicate_attempted = threading.Event()
        original_record = self.first.canonical_events.bus.store.record_with_document_mutation
        duplicate_record = self.second.canonical_events.bus.store.record_with_document_mutation
        def first_record(*args, **kwargs):
            prepared.wait(3)
            return original_record(*args, **kwargs)
        def second_record(*args, **kwargs):
            prepared.wait(3)
            if not self.store.entered.wait(3):
                raise TimeoutError("original did not enter canonical transaction")
            duplicate_attempted.set()
            return duplicate_record(*args, **kwargs)
        self.first.canonical_events.bus.store.record_with_document_mutation = first_record
        self.second.canonical_events.bus.store.record_with_document_mutation = second_record
        self.store.hold = True
        original = asyncio.create_task(self.retain_and_ack(self.first, self.payload()))
        duplicate = asyncio.create_task(self.retain_and_ack(self.second, self.payload()))
        try:
            self.assertTrue(await asyncio.to_thread(self.store.entered.wait, 2))
            self.assertTrue(await asyncio.to_thread(duplicate_attempted.wait, 2))
            try:
                # An independent SQLite reader sees neither half of the
                # original transaction while the duplicate is in progress.
                await asyncio.sleep(0.05)
                self.assertEqual(self.acks, [])
                self.assertFalse(duplicate.done())
                self.assertEqual(self.second.store.load().receipts, {})
                self.store.release.set()
                first, second = await asyncio.gather(original, duplicate)
                self.assertEqual(first.canonical_event_id, second.canonical_event_id)
                self.assertEqual(len(self.acks), 2)
                self.assertEqual(len(self.second.store.load().receipts), 1)
                self.assertEqual(len(self.second.governance.store.load().records), 1)
            finally:
                self.store.release.set()
                await asyncio.gather(duplicate, return_exceptions=True)
        finally:
            self.store.release.set()
            await asyncio.gather(original, return_exceptions=True)

    async def test_concurrent_changed_input_conflicts_and_equivalent_retries_commit_once(self):
        original = self.payload()
        changed = self.payload("a different substantive input")
        results = await asyncio.gather(self.retain_and_ack(self.first, original),
            self.retain_and_ack(self.second, changed), return_exceptions=True)
        successes = [item for item in results if not isinstance(item, Exception)]
        conflicts = [item for item in results if isinstance(item, CanonicalEventConflictError)]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(len(self.second.governance.store.load().records), 1)
        self.assertEqual(len(self.acks), 1)
        winner = original if not isinstance(results[0], Exception) else changed
        self.assertEqual(self.first.retained_input(successes[0], self.actor)["original_payload"], winner)
        # Concurrent equivalent retries preserve the winning original as well.
        retries = [copy.deepcopy(winner), copy.deepcopy(winner)]
        retries[0].update(envelope_id="retry-a", retry_attempt=1)
        retries[1].update(envelope_id="retry-b", retry_attempt=2, retry_reason="timeout")
        receipts = await asyncio.gather(self.retain_and_ack(self.first, retries[0]),
            self.retain_and_ack(self.second, retries[1]))
        self.assertEqual(receipts[0].canonical_event_id, receipts[1].canonical_event_id)
        self.assertEqual(len(self.first.store.load().receipts), 1)
        self.assertEqual(self.second.retained_input(receipts[1], self.actor)["original_payload"], winner)

    async def test_duplicate_validates_receipt_provenance_inside_atomic_storage_boundary(self):
        first = await self.retain_and_ack(self.first, self.payload())
        for field in ("connection_id", "project_id", "message_key", "input_digest", "authority_digest", "source_authority_digest"):
            def corrupt(state):
                state.receipts[first.canonical_event_id] = first.model_copy(update={field: "incompatible"})
                return state
            self.first.store.update(corrupt)
            with self.subTest(field=field):
                with self.assertRaises(ConversationChannelConflictError):
                    await self.retain_and_ack(self.second, self.payload())
        self.assertEqual(len(self.acks), 1)

    async def test_legacy_identity_cannot_be_treated_as_a_new_unstarted_delivery(self):
        payload = self.payload()
        event = (await SlackConversationChannel("same-provider-instance").normalize_events(
            payload, connection_id="connection-a", project_id="project-a"))[0]
        source = "conversation-channel:slack:same-provider-instance"
        legacy = await self.first.canonical_events.ingest(
            event_type="conversation_channel.event", source=source,
            idempotency_key="::".join((self.actor.organization_id, self.actor.workspace_id, source, event.delivery_id)),
            payload=event.model_dump(mode="json"), tenant_id=self.actor.organization_id,
            workspace_id=self.actor.workspace_id)
        with self.assertRaises(ConversationChannelConflictError):
            await self.retain_and_ack(self.second, payload)
        self.assertEqual(self.acks, [])
        self.assertEqual(self.first.store.load().receipts, {})
        self.assertEqual(CanonicalEventStore(self.store).load().events[0].event_id, legacy.event.event_id)

    async def test_rollback_before_retention_commit_never_acks(self):
        self.store.hold = True
        self.store.fail = True
        original = asyncio.create_task(self.retain_and_ack(self.first, self.payload()))
        try:
            self.assertTrue(await asyncio.to_thread(self.store.entered.wait, 2))
            self.assertEqual(self.acks, [])
            self.store.release.set()
            with self.assertRaises(OSError):
                await original
            self.assertEqual(self.acks, [])
            self.assertEqual(self.second.store.load().receipts, {})
            self.assertEqual(CanonicalEventStore(self.store).load().events, [])
            self.assertEqual(self.second.governance.store.load().records, [])
        finally:
            self.store.release.set()
            await asyncio.gather(original, return_exceptions=True)

    async def test_changed_authority_never_reauthorizes_old_retained_input(self):
        first = await self.retain_and_ack(self.first, self.payload())
        before = first.model_dump(mode="json")
        self.authority_revision = 2
        with self.assertRaises((CanonicalEventConflictError, ConversationChannelConflictError)):
            await self.retain_and_ack(self.second, self.payload())
        self.assertEqual(len(self.acks), 1)
        self.assertEqual(self.first.store.load().receipts[first.canonical_event_id].model_dump(mode="json"), before)

    async def test_real_socket_ack_is_suppressed_for_changed_duplicate_input(self):
        runtime = _runtime()
        runtime.conversation_channels = self.first
        runtime.connections.runtime_actor = lambda project: self.actor
        runtime._recover_retained_slack = AsyncMock()  # no routing/native work
        first = self.payload("socket original")
        changed = self.payload("socket altered")
        socket = _Socket([{"envelope_id": "env-original", "payload": first},
            {"envelope_id": "env-altered", "payload": changed}])
        with patch("codex_web.runtime.bots.websockets.connect", return_value=socket):
            with self.assertRaises(SlackSocketCleanClose):
                await runtime._run_slack(_connection())
        self.assertEqual(socket.sent, [{"envelope_id": "env-original"}])
        receipts = self.first.store.load().receipts
        self.assertEqual(len(receipts), 1)
        self.assertEqual(self.first.retained_input(next(iter(receipts.values())), self.actor)["original_payload"], first)


if __name__ == "__main__":
    unittest.main()
