"""Canonical replacement traversal includes every composed runtime manager."""
import asyncio
import unittest
from unittest.mock import AsyncMock

from fastapi import HTTPException

from codex_web.services.cli_worker_session import (
    AssignmentBoundCliSessionManager, AssignmentBoundCliSessionStaleError,
)
from codex_web.services.mammouth_worker_session import (
    AssignmentBoundMammouthSessionManager, MammouthWorkerHomeRegistry,
)
from tests import test_replaced_bootstrap_retirement as retirement



class CliTerminalRetirementTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = retirement.ReplacedBootstrapRetirementTests.asyncSetUp
    legacy_context = retirement.ReplacedBootstrapRetirementTests.legacy_context
    replace = retirement.ReplacedBootstrapRetirementTests.replace
    cancel_canonically = retirement.ReplacedBootstrapRetirementTests.cancel_canonically

    def manager(self, mammouth=False):
        if mammouth:
            registry = MammouthWorkerHomeRegistry()
            manager = AssignmentBoundMammouthSessionManager(self.env.local_worker,
                runtime_binding=self.old.runtime_binding, worker_homes=registry)
            self.addCleanup(registry.discard, self.old.id)
            return manager
        return AssignmentBoundCliSessionManager(self.env.local_worker,
            runtime_binding=self.old.runtime_binding)

    async def test_terminal_replacement_visits_actual_mixed_manager_composition(self):
        terminal = self.cancel_canonically()
        cli, mammouth = self.manager(), self.manager(True)
        self.env.actual.session_managers = {
            ('openai', 'codex'): self.env.real_manager,
            ('cli', 'plain'): cli, ('mammouth-ai', 'mammouth-cli'): mammouth,
        }
        error = None
        try:
            result = await self.replace()
        except TypeError as exc:
            error = str(exc)
        self.assertIsNone(error, 'every composed manager must implement terminal cleanup: ' + str(error))
        self.assertEqual(result, 'fresh-native')
        self.created.assert_awaited_once()
        self.assertEqual(self.env.f.workers.store.assignment(self.old.id), terminal)
        self.assertTrue(self.session.stopped)
        self.assertEqual(self.env.native_inputs, [])
        self.assertEqual(self.env.host._thread_queue(self.env.native_thread_id), [self.queued])
        self.assertEqual(self.env.turn_service.preflight.store.get(self.unknown.id), self.unknown)

    async def test_registered_cli_owner_remains_visible_until_concurrent_stop_joins(self):
        terminal = self.cancel_canonically()
        manager = self.manager(True)
        manager.sessions[self.old.id] = self.session
        home = manager.worker_homes.home_for(self.old.id)
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.session.stop
        async def stop():
            entered.set()
            await release.wait()
            await original()
        self.session.stop = stop
        first = asyncio.create_task(manager.stop(self.old.id, expected_terminal=terminal))
        await entered.wait()
        second = asyncio.create_task(manager.stop(self.old.id, expected_terminal=terminal))
        try:
            await asyncio.sleep(0)
            self.assertIs(manager.get(self.old.id), self.session)
            self.assertTrue(home.exists())
            self.assertFalse(second.done())
        finally:
            release.set()
        await first
        await second
        self.assertIsNone(manager.get(self.old.id))
        self.assertFalse(home.exists())

    async def test_failed_and_cancelled_cli_cleanup_retains_owner_and_home(self):
        terminal = self.cancel_canonically()
        manager = self.manager(True)
        manager.sessions[self.old.id] = self.session
        home = manager.worker_homes.home_for(self.old.id)
        original = self.session.stop
        for error in (RuntimeError('cleanup unavailable'), asyncio.CancelledError()):
            self.session.stop = AsyncMock(side_effect=error)
            with self.assertRaises(type(error)):
                await manager.stop(self.old.id, expected_terminal=terminal)
            self.assertIs(manager.get(self.old.id), self.session)
            self.assertTrue(home.exists())
        self.session.stop = original
        await manager.stop(self.old.id, expected_terminal=terminal)
        self.assertIsNone(manager.get(self.old.id))
        self.assertFalse(home.exists())

    async def test_terminal_fence_and_lease_drift_refuse_even_for_empty_cli_manager(self):
        terminal = self.cancel_canonically()
        for mammouth in (False, True):
            manager = self.manager(mammouth)
            if mammouth:
                home = manager.worker_homes.home_for(self.old.id)
            for expected in (terminal.model_copy(update={'fence':terminal.fence + 1}), self.old):
                with self.assertRaises(AssignmentBoundCliSessionStaleError):
                    await manager.stop(self.old.id, expected_terminal=expected)
            if mammouth:
                self.assertTrue(home.exists())
            await manager.stop(self.old.id, expected_terminal=terminal)

    async def test_cli_guard_refusal_prevents_successor_and_preserves_registered_owner(self):
        terminal = self.cancel_canonically()
        manager = self.manager()
        manager.sessions[self.old.id] = self.session
        self.env.actual.session_managers = {('cli', 'plain'): manager}
        self.env.actual.session_manager = None
        def change(state):
            state.assignments[0] = terminal.model_copy(update={'fence':terminal.fence + 1})
            return state
        self.env.f.workers.store.update_assignment(self.env.f.worker.id, terminal.id, change)
        # The pre-lock read sees the new exact terminal; force the expected old
        # witness at the manager boundary to exercise its own fence check.
        original = manager.stop
        async def old_witness(assignment_id, **kwargs):
            await original(assignment_id, expected_terminal=terminal)
        manager.stop = old_witness
        with self.assertRaises(HTTPException) as caught:
            await self.replace()
        self.assertEqual(caught.exception.status_code, 409)
        self.created.assert_not_awaited()
        self.assertIs(manager.get(self.old.id), self.session)
        self.assertFalse(self.session.stopped)

    async def test_stop_all_keeps_failed_cli_owner_and_mammouth_home(self):
        manager = self.manager(True)
        manager.sessions[self.old.id] = self.session
        home = manager.worker_homes.home_for(self.old.id)
        original = self.session.stop
        self.session.stop = AsyncMock(side_effect=RuntimeError('cleanup unavailable'))
        await manager.stop_all()
        self.assertIs(manager.get(self.old.id), self.session)
        self.assertTrue(home.exists())
        self.session.stop = original
        await manager.stop_all()
        self.assertIsNone(manager.get(self.old.id))
        self.assertFalse(home.exists())

    async def test_canonical_bootstrap_complete_failure_retains_cli_owner_and_home(self):
        manager = self.manager(True)
        manager.sessions[self.old.id] = self.session
        home = manager.worker_homes.home_for(self.old.id)
        original = self.session.stop
        self.session.stop = AsyncMock(side_effect=RuntimeError('cleanup unavailable'))
        with self.assertRaisesRegex(RuntimeError, 'cleanup unavailable'):
            await manager.complete(self.old.id, succeeded=False,
                failure_message='normal bootstrap cancellation')
        terminal = self.env.f.workers.store.assignment(self.old.id)
        self.assertEqual(terminal.status, 'cancelled')
        self.assertEqual(terminal.fence, self.old.fence + 1)
        self.assertIsNone(terminal.lease)
        self.assertIs(manager.get(self.old.id), self.session)
        self.assertTrue(home.exists())
        self.session.stop = original
        await manager.stop(self.old.id, expected_terminal=terminal)
        self.assertIsNone(manager.get(self.old.id))
        self.assertFalse(home.exists())
