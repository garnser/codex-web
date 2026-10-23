"""Successful bounded inference uses the existing fenced completion owner."""
import asyncio
import threading
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from codex_web.services.agent_process_session import AssignmentBoundAgentProcessSessionManager


class NativeReasoningCompletionTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_completion_storage_is_offloop_and_original_fence_is_preserved(self):
        loop_thread=threading.get_ident();seen=[]
        assignment=NS(id='original',lease=NS(lease_token='private-test-lease-token-12345'),execution_workspace_id='scratch')
        def validate():
            self.assertNotEqual(threading.get_ident(),loop_thread);seen.append('current')
            return assignment
        def checkpoint(assignment_id):
            self.assertNotEqual(threading.get_ident(),loop_thread)
            self.assertEqual(assignment_id,'original');seen.append('checkpoint')
            return ()
        def complete(worker,assignment_id,request,*,actor):
            self.assertNotEqual(threading.get_ident(),loop_thread)
            self.assertEqual((worker,assignment_id,request.fence,request.lease_token),
                             ('worker','original',3,'private-test-lease-token-12345'))
            self.assertTrue(request.succeeded);seen.append('completed')
            return assignment
        def release(*args,**kwargs):
            self.assertNotEqual(threading.get_ident(),loop_thread);seen.append('released')
        owner=object.__new__(AssignmentBoundAgentProcessSessionManager)
        owner.sessions={'original':NS(validate_current=validate,worker_id='worker',fence=3)}
        owner.local_worker=NS(checkpoint_assignment=checkpoint,worker_service=NS(complete=complete),
            worker_actor=NS(),workspace_service=NS(release=release),control_actor=NS())
        owner.stop=AsyncMock()
        self.assertIs(await owner.complete('original',succeeded=True),assignment)
        self.assertEqual(seen,['current','current','checkpoint','completed','released'])
        owner.stop.assert_awaited_once_with('original')
