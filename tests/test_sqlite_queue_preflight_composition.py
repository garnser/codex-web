from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

from codex_web.runtime.execution import TurnExecutionService
from codex_web.services.execution_preflight import ExecutionPreflightService
from codex_web.storage.execution_preflight import ExecutionPreflightStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.turn_queue import TurnQueueRepository
from tests import test_turn_execution as fixtures


class SQLiteQueuePreflightCompositionTests(unittest.TestCase):
    def runtime(self, root):
        state = SQLiteStateStore(root / 'state.sqlite')
        queues = TurnQueueRepository(state, root / 'queues.json')
        host = fixtures._Host()
        host._update_thread_queue_record = queues.update
        host._thread_queue_record = queues.get
        runtime = TurnExecutionService(host)
        runtime.queued_preflight = ExecutionPreflightService(ExecutionPreflightStore(state))
        return runtime, queues

    def test_fresh_enqueue_restart_and_retained_duplicate_use_same_store(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime, queues = self.runtime(root)
            original = runtime.enqueue_turn(thread_id='t', project_id='p', message='original')
            self.assertEqual(queues.get('t'), [original])
            duplicate = runtime.enqueue_turn(thread_id='t', project_id='p', message='original')
            self.assertEqual(duplicate, original)

            restarted, reopened = self.runtime(root)
            self.assertEqual(reopened.get('t'), [original])
            retained = restarted.queued_preflight.retain_queued(original,
                organization_id='local', workspace_id='default')
            reopened.put('t', [])
            before = retained.model_dump(mode='json')
            with self.assertRaises(HTTPException) as refused:
                restarted.enqueue_turn(thread_id='t', project_id='p', message='original')
            self.assertEqual(refused.exception.detail['attemptId'], retained.id)
            self.assertEqual(reopened.get('t'), [])
            self.assertEqual(restarted.queued_preflight.store.get(retained.id).model_dump(mode='json'), before)
            other = restarted.enqueue_turn(thread_id='t', project_id='p', message='different')
            self.assertEqual(reopened.get('t'), [other])
