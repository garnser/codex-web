"""Actual blocked reconciliation thread remains owned through cancellation/timeout."""
import asyncio
import threading
import time
import unittest
from unittest.mock import patch
from tests import test_recovery_operator_api as api


class RecoveryProviderCustodyTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = api.RecoveryOperatorApiTests.asyncSetUp
    asyncTearDown = api.RecoveryOperatorApiTests.asyncTearDown
    client = api.RecoveryOperatorApiTests.client
    url = api.RecoveryOperatorApiTests.url
    body = api.RecoveryOperatorApiTests.body

    async def blocked_writer(self, timeout):
        async with self.client() as client:
            entered, release = threading.Event(), threading.Event()
            self.addCleanup(release.set)
            original = self.operator.provider.reconcile.reconcile
            def blocked(*args, **kwargs):
                entered.set()
                if not release.wait(5):
                    raise AssertionError('actual reconciliation thread not released')
                return original(*args, **kwargs)
            definition = self.operator.provider.actions()[0]
            if timeout:
                self.operator.provider.actions = lambda: (definition.model_copy(update={'timeout_seconds': 1}),)
            with patch.object(self.operator.provider.reconcile, 'reconcile', side_effect=blocked) as engine:
                work = asyncio.create_task(client.post(self.url(), json=self.body()))
                stopping = None
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    if timeout:
                        await asyncio.sleep(1.1)
                    else:
                        work.cancel()
                        await asyncio.sleep(0)
                    self.assertEqual(self.recovery._scheduled_executor.status()['inflight_work'], 1)
                    stopping = asyncio.create_task(self.recovery.stop())
                    await asyncio.sleep(0)
                    self.assertFalse(stopping.done())
                    self.assertFalse(work.done())
                finally:
                    release.set()
                    replies = await asyncio.gather(work, return_exceptions=True)
                    if stopping is not None:
                        await stopping
                self.assertEqual(engine.call_count, 1)
            self.assertEqual(self.recovery._scheduled_executor.status()['inflight_work'], 0)
            record = self.recovery.store.load().reconciliations[self.payload.event_id]
            self.assertEqual(record.outcome, 'unknown')
            intents = self.service.list(self.actor)
            intent = next(i for i in intents if i.action_id == 'recovery.scheduled-firing.reconcile')
            # Actual caller cancellation may leave its lease EXECUTING. Only
            # the ordinary expired-lease reconciler transitions that custody;
            # it does not execute the provider or recreate the domain receipt.
            self.service.recover_stale_claims(now=time.time()+500,
                organization_id=self.actor.organization_id, workspace_id=self.actor.workspace_id)
            with patch.object(self.operator.provider.reconcile, 'reconcile', side_effect=AssertionError('domain replay')):
                response = await client.post(self.url('/reconciliation-intents/' + intent.id + '/observe'), json={})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()['item']['status'], 'succeeded', response.text)
            self.assertEqual(self.recovery.store.load().reconciliations[self.payload.event_id], record)

    async def test_cancelled_http_observer_cannot_abandon_domain_writer(self):
        await self.blocked_writer(timeout=False)

    async def test_canonical_timeout_cannot_abandon_domain_writer(self):
        await self.blocked_writer(timeout=True)
