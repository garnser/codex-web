"""Upgrade checkpoint writers leave the API loop and retain cancellation ownership."""
import asyncio
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import Request
from codex_web.api.upgrades import build_upgrades_router
from codex_web.upgrades import UpgradeServiceDrainRequest


class UpgradeOwnedRouteTests(unittest.IsolatedAsyncioTestCase):
    def route(self, service, suffix):
        router = build_upgrades_router(service)
        return next(route.endpoint for route in router.routes if route.path.endswith(suffix))

    async def test_drain_and_verification_run_outside_the_api_loop(self):
        loop_thread = threading.get_ident()
        observed = []
        actor = object()
        def operation(plan_id, **kwargs):
            observed.append((threading.get_ident(), plan_id, kwargs['actor']))
            return SimpleNamespace(model_dump=lambda **_: {'id': plan_id})
        service = SimpleNamespace(start_service_drain=operation, verify_post_upgrade=operation)
        request = Request({'type': 'http', 'headers': []})
        with patch('codex_web.api.upgrades.request_actor', return_value=actor), \
                patch('codex_web.api.upgrades.UpgradeService.require_operator'):
            await self.route(service, '/service-drain')('plan', UpgradeServiceDrainRequest(
                service_instance_id='instance', source_revision='a' * 40, target_revision='b' * 40), request)
            await self.route(service, '/verify')('plan', request)
        self.assertEqual(len(observed), 2)
        self.assertTrue(all(thread != loop_thread and plan == 'plan' and actual is actor
            for thread, plan, actual in observed))

    async def test_cancelled_verification_waits_for_the_actual_writer_to_join(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def operation(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('private writer test release missing')
            finished.set()
            return SimpleNamespace(model_dump=lambda **_: {'id': 'plan'})
        service = SimpleNamespace(verify_post_upgrade=operation)
        request = Request({'type': 'http', 'headers': []})
        with patch('codex_web.api.upgrades.request_actor', return_value=object()), \
                patch('codex_web.api.upgrades.UpgradeService.require_operator'):
            task = asyncio.create_task(self.route(service, '/verify')('plan', request))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                self.assertFalse(finished.is_set())
            finally:
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertTrue(finished.is_set())
