from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import HTTPException

from codex_web.agent_runtime import AgentRuntimeEvent
from codex_web.models import IndexedThread
from codex_web.services.codex_agent_runtime import CodexAgentRuntimeAdapter
from codex_web.services.thread_transcript import ThreadTranscriptService
from codex_web.services.threads import ThreadService
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.thread_history import ThreadHistoryRepository


class UnsupportedNativeHistoryTests(unittest.IsolatedAsyncioTestCase):
    def service(self, *, error=None, history=None, transcript=None, replaced=False):
        error = error or RuntimeError({'code': -32601, 'message': 'list_turns is not supported yet'})
        request = AsyncMock(side_effect=error)
        recovery = SimpleNamespace(raise_if_thread_replaced=lambda _: None)
        if replaced:
            def refuse(_):
                raise HTTPException(409, 'thread_replaced')
            recovery.raise_if_thread_replaced = refuse
        index = SimpleNamespace(get=lambda _: IndexedThread(id='home-new', name='Home operator'))
        service = ThreadService(runtime_transport=SimpleNamespace(request=AsyncMock()),
            event_sink=lambda _: None, runtime_request_for_thread=request, recovery=recovery,
            resume_runtime=SimpleNamespace(active_task=lambda _: None, is_timeout_error=lambda _: False),
            thread_index=index, thread_history=history, transcript=transcript)
        # Current custody projection is independent of provider history. This
        # fixture does not start a session, input, or authorization grant.
        runner = {'assignmentId': 'assignment-new', 'fence': 1, 'state': 'running', 'live': True}
        service._runner_projection = lambda _: runner
        service._runner_activity = lambda *_: {'state': 'idle', 'turnId': None, 'source': 'current_turn'}
        return service, request

    async def test_recovered_thread_without_retained_history_is_explicitly_incomplete(self):
        service, request = self.service()
        try:
            result = await service.read('home-new')
        except RuntimeError as exc:
            self.fail(f'Explicit unsupported native history escaped the ordinary read: {exc}')
        self.assertEqual(result['thread']['id'], 'home-new')
        self.assertEqual(result['thread']['name'], 'Home operator')
        self.assertEqual(result['thread']['turns'], [])
        self.assertEqual(result['thread']['historyRead'], {'state': 'unsupported',
            'code': 'native_history_unsupported', 'source': 'none', 'providerHistoryComplete': False})
        self.assertTrue(result['runtimeReadDegraded'])
        self.assertEqual(result['thread']['status'], {'type': 'unknown'})
        self.assertEqual(result['thread']['activity']['state'], 'idle')
        self.assertEqual(result['thread']['runner']['assignmentId'], 'assignment-new')
        self.assertNotIn('timedOut', result)
        self.assertNotIn('readTimedOut', result['thread'])
        request.assert_awaited_once_with('home-new', 'thread/read', {'threadId': 'home-new', 'includeTurns': True})

    async def test_real_canonical_history_remains_visible_and_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            history = ThreadHistoryRepository(SQLiteStateStore(Path(directory) / 'state.sqlite'))
            history.start_turn('home-new', turn_id='retained', message='Retained original',
                               provider_id='openai', runtime_id='codex', created_at=1)
            before = history.get('home-new')
            service, request = self.service(history=history)
            result = await service.read('home-new', message_limit=1)
            self.assertEqual(result['thread']['turns'][0]['items'][0]['content'][0]['text'], 'Retained original')
            self.assertEqual(result['thread']['historyRead']['source'], 'canonical_history')
            self.assertFalse(result['thread']['historyRead']['providerHistoryComplete'])
            self.assertEqual(history.get('home-new'), before)
            request.assert_awaited_once()

    async def test_real_durable_transcript_preserves_retained_content(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = ThreadTranscriptService(SQLiteStateStore(Path(directory) / 'state.sqlite'))
            transcript.record_user('home-new', 'retained', 'Original')
            transcript.record_event('home-new', AgentRuntimeEvent(event_type='item/agentMessage/delta',
                provider_native_turn_id='turn-1', payload={'itemId': 'reply', 'delta': 'Retained reply'}))
            before = transcript.read('home-new')
            service, _ = self.service(transcript=transcript)
            result = await service.read('home-new')
            self.assertEqual(result['thread']['historyRead']['source'], 'retained_transcript')
            self.assertEqual(result['thread']['turns'], before['turns'])
            self.assertEqual(transcript.read('home-new'), before)

    async def test_unrelated_rpc_transport_and_authority_errors_propagate_unchanged(self):
        for error in (RuntimeError({'code': -32601, 'message': 'permission denied'}),
            RuntimeError({'code': -32000, 'message': 'list_turns is not supported yet'}),
            RuntimeError('list_turns is not supported yet'),
            RuntimeError({'code': -32601, 'message': ['list_turns is not supported yet']}),
            HTTPException(403, 'forbidden')):
            with self.subTest(error=repr(error)):
                service, request = self.service(error=error)
                with self.assertRaises(type(error)) as raised:
                    await service.read('home-new')
                self.assertIs(raised.exception, error)
                request.assert_awaited_once()

    async def test_replaced_thread_guard_runs_before_provider_read(self):
        service, request = self.service(replaced=True)
        with self.assertRaises(HTTPException) as raised:
            await service.read('home-new')
        self.assertEqual(raised.exception.status_code, 409)
        request.assert_not_awaited()

    async def test_native_read_success_is_not_reclassified(self):
        payload = {'thread': {'id': 'home-new', 'turns': [{'id': 'native', 'items': []}]}}
        request = AsyncMock(return_value=payload)
        result = await CodexAgentRuntimeAdapter(SimpleNamespace(request=request)).read_session('home-new')
        self.assertEqual(result.payload, payload)
        self.assertEqual(result.provider_native_session_id, 'home-new')
        request.assert_awaited_once_with('thread/read', {'threadId': 'home-new', 'includeTurns': True})
