"""Independent actual-store negative; no application import or native process."""
import unittest
from unittest.mock import patch

from tests.test_home_child_terminal_event import HomeChildTerminalTests


class HomeChildRealAliasTests(HomeChildTerminalTests):
    async def test_real_cross_tenant_alias_ambiguity_emits_unknown_without_usage_read(self):
        active = await self.start_child()
        original_usage = self.final_usage(active)
        original = self.sessions.store.find_unique_by_native_id(
            active.thread_id, provider_id='openai', runtime_id='codex')
        self.assertIsNotNone(original)
        foreign = original.model_copy(update={
            'id': 'agent-session-independent-foreign-tenant',
            'organization_id': 'foreign-organization',
            'workspace_id': 'foreign-workspace',
            'project_id': 'foreign-private-project',
            'execution_id': 'foreign-private-execution',
            'assignment_id': 'foreign-private-assignment',
            'execution_workspace_id': 'foreign-private-workspace',
        })
        self.sessions.store.upsert(foreign)
        self.assertIsNone(self.sessions.store.find_unique_by_native_id(
            active.thread_id, provider_id='openai', runtime_id='codex'))
        with patch.object(self.usage, 'get', side_effect=AssertionError('ambiguous session must not read usage')):
            await self.complete_child(active)
        self.assertEqual(len(self.received), 1)
        event = self.received[0]
        self.assertEqual((event.tenant_id, event.workspace_id),
                         (self.actor.organization_id, self.actor.workspace_id))
        self.assertEqual(event.payload['outcome'], 'unknown')
        self.assertFalse(event.payload['usage_available'])
        for field in ('usage_id', 'input_tokens', 'output_tokens', 'runtime_duration_seconds'):
            self.assertNotIn(field, event.payload)
        serialized = str(event.model_dump())
        self.assertNotIn('foreign-private', serialized)
        self.assertNotIn(original_usage.id, serialized)
        self.assertEqual(len(self.starts()), 1)
        self.assertEqual(len(self.intents.store.load().intents), 1)
        self.assertEqual(len(self.intents.history(self.original['item']['id'], self.actor)['receipts']), 1)
        self.assertEqual(self.queue_store.get(self.thread), [])


def load_tests(loader, tests, pattern):
    return unittest.TestSuite([HomeChildRealAliasTests(
        'test_real_cross_tenant_alias_ambiguity_emits_unknown_without_usage_read')])
