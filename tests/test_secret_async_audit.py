from __future__ import annotations

import asyncio
import threading
import unittest
from unittest.mock import patch

from tests import test_secrets_broker as fixtures
from codex_web.identity import TenantScope
from codex_web.secrets import SecretCreate
from codex_web.services.secrets import SecretUseDeniedError


class AsyncSecretAuditTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = fixtures.SecretBrokerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.broker = self.fixture.broker
        self.admin = self.fixture.admin
        self.reference = self.broker.create(SecretCreate(name='test', value='private-fixture-token'), actor=self.admin)

    async def exercise(self, outcome):
        loop = asyncio.get_running_loop()
        responsive = threading.Event()
        loop.call_later(0.01, responsive.set)
        original = self.broker._audit
        seen = []
        calls = []
        def audit(*args, **kwargs):
            self.assertTrue(responsive.wait(1), 'secret audit blocked the event loop')
            seen.append(threading.get_ident())
            return original(*args, **kwargs)
        async def consumer(value):
            calls.append(value)
            if outcome == 'failed':
                raise ValueError('provider failed')
            return {'echo': value, 'ok': True}
        actor = self.admin
        if outcome == 'denied':
            actor = self.fixture.identity.bootstrap_service_actor(identity_id='unallowed-service',
                name='Unallowed', scope=TenantScope(), service_scopes=('secret:use',))
        with patch.object(self.broker, '_audit', audit):
            task = self.broker.use_async(self.reference.id, actor=actor, operation='fixture.read', consumer=consumer)
            if outcome == 'succeeded':
                self.assertEqual(await task, {'echo':'[REDACTED]', 'ok':True})
            else:
                with self.assertRaises(ValueError if outcome == 'failed' else SecretUseDeniedError):
                    await task
        self.assertEqual(len(calls), 0 if outcome == 'denied' else 1)
        self.assertEqual(len(seen), 1)
        self.assertNotEqual(seen[0], threading.get_ident())
        event = self.broker.audit(self.admin)[-1]
        self.assertEqual((event.action, event.outcome), ('use', outcome))
        self.assertNotIn('private-fixture-token', event.model_dump_json())

    async def test_success_audit_is_responsive_durable_and_redacted(self):
        await self.exercise('succeeded')

    async def test_failed_consumer_audit_is_responsive_and_preserves_exception(self):
        await self.exercise('failed')

    async def test_denial_audit_is_responsive_without_using_credential(self):
        await self.exercise('denied')

    async def test_repeated_cancellation_joins_exact_audit_writer(self):
        loop = asyncio.get_running_loop()
        entered = loop.create_future()
        release = threading.Event()
        original = self.broker._audit
        calls = []
        def audit(*args, **kwargs):
            loop.call_soon_threadsafe(lambda: entered.set_result(None) if not entered.done() else None)
            self.assertTrue(release.wait(2))
            return original(*args, **kwargs)
        async def consumer(value):
            calls.append(1)
            return True
        with patch.object(self.broker, '_audit', audit):
            task = asyncio.create_task(self.broker.use_async(self.reference.id, actor=self.admin,
                operation='fixture.read', consumer=consumer))
            try:
                await asyncio.wait_for(asyncio.shield(entered), 1)
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done(), 'cancelled observer abandoned audit writer')
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(calls, [1])
        self.assertEqual(self.broker.audit(self.admin)[-1].outcome, 'succeeded')

    async def test_audit_failure_propagates_without_repeating_provider(self):
        calls = []
        async def consumer(value):
            calls.append(1)
            return True
        with patch.object(self.broker, '_audit', side_effect=RuntimeError('audit unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'audit unavailable'):
                await self.broker.use_async(self.reference.id, actor=self.admin,
                    operation='fixture.read', consumer=consumer)
        self.assertEqual(calls, [1])
