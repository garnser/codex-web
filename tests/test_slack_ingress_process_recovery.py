from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock, Mock
from fastapi.testclient import TestClient

from codex_web.conversation_channels import ConversationProjectionOutcome
from codex_web.services.identity import TenantIsolationError
from codex_web.runtime.bots import SlackSocketCleanClose
from codex_web.runtime.execution import ExecutionPrestartRefusal
from codex_web.data_governance import GovernanceActionRequest, GovernanceAction
from codex_web.services.crypto_keys import CryptoKeyConflictError
from codex_web.services.conversation_channels import ConversationChannelUnavailableError
from tests.test_slack_socket_backpressure import _Socket
from tests.slack_ingress_recovery_fixture import compose, payload


class SlackIngressProcessRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="slack-process-recovery-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def process(self, stage):
        return subprocess.run([sys.executable, "-m", "tests.slack_ingress_recovery_fixture", str(self.root), stage],
            env={**os.environ, "CODEX_WEB_STATE_BACKEND": "sqlite"}, capture_output=True, text=True, timeout=20)

    async def test_kill_after_real_transport_ack_restart_route_and_api_projection(self):
        crashed = await asyncio.to_thread(self.process, "ack-crash")
        self.assertEqual(crashed.returncode, 77, crashed.stderr)
        self.assertEqual(json.loads(crashed.stdout), {"envelope_id": "controlled-envelope"})
        recovered = compose(self.root)
        receipt = next(iter(recovered.service.store.load().receipts.values()))
        self.assertEqual(receipt.outcome, ConversationProjectionOutcome.PENDING)
        self.assertEqual(recovered.queue.get("fixture-thread"), [])
        actor = recovered.connections.runtime_actor("fixture")
        self.assertEqual(recovered.service.retained_input(receipt, actor)["original_payload"], payload())
        restarted = await asyncio.to_thread(self.process, "normal")
        self.assertEqual(restarted.returncode, 0, restarted.stderr)
        self.assertEqual(len(recovered.queue.get("fixture-thread")), 1)
        queued = recovered.queue.get("fixture-thread")[0]
        self.assertEqual(queued.agent_profile_revision, 1)
        self.assertEqual(queued.execution_profile_id, "orchestration-only")
        self.assertEqual(queued.writable_repository_resource_ids, ())
        with TestClient(recovered.app) as client:
            response = client.get("/api/conversation-channels/receipts")
        self.assertEqual(response.status_code, 200)
        row = response.json()["items"][0]
        self.assertEqual(row["delivery_state"], "projected")
        self.assertTrue(row["input_protected"])
        self.assertEqual(row["connection_id"], "fixture-slack")
        self.assertNotIn("original <@U2>", response.text)
        self.assertNotIn("ciphertext", response.text)
        self.assertNotIn("controlled-bot-token", response.text)

    async def test_kill_after_atomic_queue_admission_restart_never_readmits_even_if_queue_removed(self):
        self.assertEqual((await asyncio.to_thread(self.process, "ack-crash")).returncode, 77)
        self.assertEqual((await asyncio.to_thread(self.process, "queue-crash")).returncode, 78)
        fixture = compose(self.root)
        receipt = next(iter(fixture.service.store.load().receipts.values()))
        self.assertEqual(receipt.outcome, ConversationProjectionOutcome.QUEUE_ADMITTED)
        original = fixture.queue.get("fixture-thread")[0]
        attempt = fixture.preflight.retain_queued(original, organization_id="local", workspace_id="default")
        _, claim = fixture.preflight.claim_queued_admission(attempt.id)
        self.assertTrue(fixture.preflight.begin_queued_start(attempt.id, claim_id=claim))
        fixture.queue.delete("fixture-thread")
        restarted = await asyncio.to_thread(self.process, "normal")
        self.assertEqual(restarted.returncode, 0, restarted.stderr)
        self.assertEqual(fixture.queue.get("fixture-thread"), [])
        self.assertEqual(fixture.preflight.store.get(attempt.id).status, "reconciliation_required")
        self.assertIsNone(fixture.preflight.claim_queued_admission(attempt.id)[1])
        updated = fixture.service.store.load().receipts[receipt.canonical_event_id]
        self.assertEqual(updated.outcome, ConversationProjectionOutcome.ROUTED)
        self.assertEqual(updated.queued_id, original.id)

    async def test_real_connection_revocation_changed_scope_and_membership_stop_recovery(self):
        for change in ("credential", "connection", "membership"):
            with self.subTest(change=change), tempfile.TemporaryDirectory(dir=self.root) as directory:
                fixture = compose(Path(directory))
                actor = fixture.connections.runtime_actor("fixture")
                receipt = (await fixture.service.retain_raw("slack", fixture.connection.id, payload(),
                    actor=actor, connection_id=fixture.connection.id, project_id="fixture"))[0]
                if change == "credential":
                    fixture.connections.secret_broker.revoke(fixture.connection.bot_token_secret_id, actor=fixture.admin)
                elif change == "connection":
                    connections = fixture.connections.load_connections()
                    connections[0].project_id = "another-project"
                    fixture.connections.save_connections(connections)
                else:
                    membership = next(row for row in fixture.identity.state().memberships if row.identity_id == actor.identity_id)
                    fixture.identity.revoke_membership(membership.id, actor=fixture.admin)
                    with self.assertRaises(TenantIsolationError):
                        fixture.connections.runtime_actor("fixture")
                event = fixture.service.pending_events(actor=actor, connection_id=fixture.connection.id, project_id="fixture")[0]
                result = await fixture.service.ingest(event, actor=actor)
                self.assertEqual(result.outcome, ConversationProjectionOutcome.REQUIRES_RECONCILIATION)
                self.assertEqual(fixture.queue.get("fixture-thread"), [])
                self.assertEqual(result.protected_input, receipt.protected_input)

    async def test_storage_failure_before_retention_no_transport_ack_or_orphan(self):
        fixture = compose(self.root)
        socket = _Socket([{"envelope_id": "failure", "payload": payload()}])
        original = fixture.state.update_many
        def fail(defaults, updater):
            if "conversation_channels" in defaults:
                raise OSError("controlled failure")
            return original(defaults, updater)
        with patch.object(fixture.state, "update_many", side_effect=fail), patch("codex_web.runtime.bots.websockets.connect", return_value=socket):
            with self.assertRaises(SlackSocketCleanClose):
                await fixture.runtime._run_slack(fixture.connection)
        self.assertEqual(socket.sent, [])
        self.assertEqual(fixture.service.store.load().receipts, {})
        self.assertEqual(fixture.governance.store.load().records, [])

    async def test_actual_canonical_queue_drain_keeps_failed_and_unknown_native_start_nonreplayable(self):
        for error, expected in ((ExecutionPrestartRefusal(status_code=503, detail={"code": "execution_preflight_blocked"}), "blocked"),
            (RuntimeError("controlled native outcome unknown"), "reconciliation_required")):
            with self.subTest(outcome=expected), tempfile.TemporaryDirectory(dir=self.root) as directory:
                fixture = compose(Path(directory))
                actor = fixture.connections.runtime_actor("fixture")
                receipt = (await fixture.service.retain_raw("slack", fixture.connection.id, payload(),
                    actor=actor, connection_id=fixture.connection.id, project_id="fixture"))[0]
                event = fixture.service.pending_events(actor=actor, connection_id=fixture.connection.id, project_id="fixture")[0]
                await fixture.service.ingest(event, actor=actor)
                original = fixture.queue.get("fixture-thread")[0]
                fixture.execution.host._project = fixture.connections._project
                fixture.execution.actor_resolver = lambda identity, project: fixture.connections.runtime_actor(project.id)
                native = AsyncMock(side_effect=error)
                fixture.execution.start_thread_turn_now = native
                fixture.execution.schedule_queue_drain = Mock()
                await fixture.execution.drain_thread_queue("fixture-thread")
                native.assert_awaited_once()
                stored = fixture.preflight.store.for_thread("fixture-thread")[0]
                self.assertEqual(stored.status, expected)
                self.assertEqual(stored.queued_turn_snapshot["canonical_ingress_id"], receipt.canonical_event_id)
                self.assertEqual(stored.queued_turn_snapshot["message"], original.message)
                restarted = compose(Path(directory))
                restarted.execution.start_thread_turn_now = AsyncMock()
                restarted.execution.schedule_queue_drain = Mock()
                await restarted.execution.drain_thread_queue("fixture-thread")
                restarted.execution.start_thread_turn_now.assert_not_awaited()
                self.assertEqual(restarted.preflight.store.get(stored.id).status, expected)

    async def test_protected_retrieval_samples_shards_before_decrypting_and_memory_backpressure_is_bounded(self):
        fixture = compose(self.root)
        actor = fixture.connections.runtime_actor("fixture")
        runtime = fixture.runtime
        hot = "hot"
        hot_shard = runtime._slack_payload_shard(f"{fixture.connection.id}:{hot}", 2)
        cold = next(f"cold-{i}" for i in range(100) if runtime._slack_payload_shard(f"{fixture.connection.id}:cold-{i}", 2) != hot_shard)
        for index in range(14):
            raw = payload(index, hot if index < 12 else cold)
            raw["event"]["text"] = f"original-{index}"
            await fixture.service.retain_raw("slack", fixture.connection.id, raw, actor=actor,
                connection_id=fixture.connection.id, project_id="fixture")
        with patch.object(fixture.crypto, "decrypt", wraps=fixture.crypto.decrypt) as decrypt:
            selected = fixture.service.pending_events(actor=actor, connection_id=fixture.connection.id,
                project_id="fixture", limit=2, partition_count=2,
                partition_index=lambda event: runtime._slack_payload_shard(f"{fixture.connection.id}:{event.message.conversation.conversation_id}", 2))
        self.assertEqual(decrypt.call_count, 4)
        self.assertEqual([event.delivery_id for event in selected], ["E0", "E1", "E12", "E13"])
        with patch.dict(os.environ, {"CODEX_WEB_SLACK_PAYLOAD_WORKERS": "2", "CODEX_WEB_SLACK_PAYLOAD_QUEUE_PER_WORKER": "1", "CODEX_WEB_SLACK_PAYLOAD_DRAIN_TIMEOUT_SECONDS": "0"}):
            for event in selected:
                runtime._admit_slack_payload(fixture.connection, {"event_id": event.delivery_id,
                    "event": {"channel": event.message.conversation.conversation_id}}, retained_event=event, retained_retry=True)
            status = runtime.slack_payload_status(fixture.connection.id)
            self.assertEqual(status["workerTasks"], 2)
            self.assertEqual(status["queueCapacity"], 2)
            self.assertLessEqual(status["queueDepth"], 2)
            self.assertGreater(status["rejected"], 0)
            self.assertEqual(fixture.service.status(actor=actor)["retained_pending"], 14)
            await runtime._stop_slack_payload_workers(fixture.connection)

    async def test_retention_capacity_and_governed_release_keep_identity_tombstone(self):
        fixture = compose(self.root)
        actor = fixture.connections.runtime_actor("fixture")
        first = (await fixture.service.retain_raw("slack", fixture.connection.id, payload(), actor=actor,
            connection_id=fixture.connection.id, project_id="fixture"))[0]
        key = first.protected_input.key_id
        with self.assertRaises(CryptoKeyConflictError):
            fixture.crypto.revoke_key(key, "isolated qualification", actor=fixture.admin)
        self.assertEqual(fixture.crypto.usage(key, fixture.admin)["items"][0]["object_type"], "conversation_ingress")
        with patch.object(fixture.service, "MAX_PROTECTED_BYTES", len(first.protected_input.ciphertext_b64)):
            with self.assertRaises(ConversationChannelUnavailableError):
                await fixture.service.retain_raw("slack", fixture.connection.id, payload(1), actor=actor,
                    connection_id=fixture.connection.id, project_id="fixture")
        self.assertEqual(len(fixture.governance.store.load().records), 1)
        request = fixture.governance.request_action(GovernanceActionRequest(record_id=first.governance_record_id,
            action=GovernanceAction.DELETE, reason="isolated qualification"), actor=fixture.admin)
        fixture.governance.execute_request(request.id, actor=fixture.admin)
        tombstone = fixture.service.store.load().receipts[first.canonical_event_id]
        self.assertIsNone(tombstone.protected_input)
        duplicate = (await fixture.service.retain_raw("slack", fixture.connection.id, payload(), actor=actor,
            connection_id=fixture.connection.id, project_id="fixture"))[0]
        self.assertEqual(duplicate, tombstone)
        self.assertEqual(fixture.service.pending_events(actor=actor, connection_id=fixture.connection.id, project_id="fixture"), ())

    async def test_transport_authenticated_credential_version_cannot_follow_in_place_rotation(self):
        from codex_web.secrets import SecretRotate
        from codex_web.runtime.bots import SlackSocketAuthorityChanged
        fixture = compose(self.root)
        class RotatingSocket(_Socket):
            async def __aenter__(self):
                fixture.connections.secret_broker.rotate(fixture.connection.slack_app_token_secret_id,
                    SecretRotate(value="controlled-new-account-token"),actor=fixture.admin)
                return self
        socket = RotatingSocket([{"envelope_id":"old-authenticated-socket","payload":payload()}])
        with patch("codex_web.runtime.bots.websockets.connect",return_value=socket):
            with self.assertRaises(SlackSocketAuthorityChanged):
                await fixture.runtime._run_slack(fixture.connection)
        self.assertEqual(socket.sent,[])
        self.assertEqual(fixture.service.store.load().receipts,{})
        self.assertEqual(fixture.governance.store.load().records,[])
