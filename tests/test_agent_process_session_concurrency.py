import asyncio
import unittest
from types import SimpleNamespace
from codex_web.services.agent_process_session import AssignmentBoundAgentProcessSessionManager


class SessionStartupConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_independent_assignments_start_while_same_assignment_deduplicates(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        created = []
        class Session:
            def __init__(self, worker, host, assignment_id, **kwargs):
                self.assignment_id = assignment_id
                created.append(assignment_id)
            async def start(self):
                if self.assignment_id == 'slow':
                    entered.set()
                    await release.wait()
            def status(self):
                return SimpleNamespace(running=True, ready=True)
            async def stop(self):
                pass
        manager = AssignmentBoundAgentProcessSessionManager(
            None, None, runtime_factory=None, credential_provider=None, session_factory=Session)
        first = asyncio.create_task(manager.start('slow'))
        await entered.wait()
        duplicate = asyncio.create_task(manager.start('slow'))
        try:
            other = await asyncio.wait_for(manager.start('independent'), 0.5)
            self.assertEqual(other.assignment_id, 'independent')
            self.assertFalse(first.done())
            self.assertFalse(duplicate.done())
            release.set()
            self.assertIs(await first, await duplicate)
            self.assertEqual(created.count('slow'), 1)
        finally:
            release.set()
            await asyncio.gather(first, duplicate, return_exceptions=True)
            await manager.stop_all()
