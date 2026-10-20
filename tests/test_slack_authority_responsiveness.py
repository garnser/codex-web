import asyncio
from contextvars import ContextVar
import threading
import unittest

from codex_web.conversation_channels import ConversationProjectionOutcome
from codex_web.services.conversation_channels import ConversationChannelConflictError
from tests import test_slack_ingress_retention_identity as fixtures


class SlackAuthorityResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.SlackRetentionIdentityTests.setUp
    tearDown = fixtures.SlackRetentionIdentityTests.tearDown
    service = fixtures.SlackRetentionIdentityTests.service
    payload = staticmethod(fixtures.SlackRetentionIdentityTests.payload)
    retain_and_ack = fixtures.SlackRetentionIdentityTests.retain_and_ack

    def blocked_snapshot(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        original = self.first.authority_snapshot
        observed = []
        context = ContextVar('slack_snapshot_request')
        marker = object()
        token = context.set(marker)
        loop_thread = threading.get_ident()

        def snapshot(event, actor):
            observed.append((threading.get_ident(), context.get(None), actor))
            entered.set()
            try:
                if not release.wait(2):
                    observed.append('barrier_expired')
                return original(event, actor)
            finally:
                finished.set()

        self.first.authority_snapshot = snapshot
        # A cheap source pin remains captured before the awaited routing read.
        self.first.source_authority_snapshot = original
        return entered, release, finished, observed, context, token, marker, loop_thread

    async def test_retention_keeps_loop_available_and_preserves_request_context(self):
        entered, release, finished, observed, context, token, marker, loop_thread = self.blocked_snapshot()
        task = asyncio.create_task(self.retain_and_ack(self.first, self.payload()))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 3))
            await asyncio.sleep(0)
            self.assertNotIn('barrier_expired', observed)
            self.assertFalse(task.done())
            self.assertFalse(self.acks)
            self.assertFalse(self.first.store.load().receipts)
        finally:
            release.set()
            await task
            context.reset(token)
        self.assertNotEqual(observed[0][0], loop_thread)
        self.assertIs(observed[0][1], marker)
        self.assertEqual(observed[0][2], self.actor)
        self.assertEqual(len(self.acks), 1)

    async def test_source_rotation_during_capture_never_commits_or_acknowledges(self):
        entered, release, finished, observed, context, token, marker, loop_thread = self.blocked_snapshot()
        task = asyncio.create_task(self.retain_and_ack(self.first, self.payload()))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 3))
            self.assertNotIn('barrier_expired', observed)
            self.authority_revision = 2
        finally:
            release.set()
            results = await asyncio.gather(task, return_exceptions=True)
            context.reset(token)
        self.assertIsInstance(results[0], ConversationChannelConflictError)
        self.assertFalse(self.acks)
        self.assertFalse(self.first.store.load().receipts)

    async def test_cancellation_before_capture_finishes_cannot_commit_later(self):
        entered, release, finished, observed, context, token, marker, loop_thread = self.blocked_snapshot()
        task = asyncio.create_task(self.retain_and_ack(self.first, self.payload()))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 3))
            self.assertNotIn('barrier_expired', observed)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            release.set()
            self.assertTrue(await asyncio.to_thread(finished.wait, 3))
            await asyncio.gather(task, return_exceptions=True)
            context.reset(token)
        self.assertFalse(self.acks)
        self.assertFalse(self.first.store.load().receipts)

    async def test_routing_revalidation_is_responsive_and_uses_current_authority(self):
        receipt = await self.retain_and_ack(self.first, self.payload())
        adapter = self.first.registry.resolve('slack', 'same-provider-instance')
        event = (await adapter.normalize_events(self.payload(), connection_id='connection-a', project_id='project-a'))[0]
        protected = receipt.protected_input.model_dump(mode='json')
        entered, release, finished, observed, context, token, marker, loop_thread = self.blocked_snapshot()
        task = asyncio.create_task(self.first.ingest(event, actor=self.actor))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 3))
            self.assertNotIn('barrier_expired', observed)
            self.assertFalse(task.done())
            self.authority_revision = 2
        finally:
            release.set()
            result = await task
            context.reset(token)
        self.assertNotEqual(observed[0][0], loop_thread)
        self.assertEqual(result.outcome, ConversationProjectionOutcome.REQUIRES_RECONCILIATION)
        self.first.route_handler.assert_not_called()
        retained = self.first.store.load().receipts[receipt.canonical_event_id]
        self.assertEqual(retained.authority_digest, receipt.authority_digest)
        self.assertEqual(retained.protected_input.model_dump(mode='json'), protected)
