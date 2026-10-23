"""Cancellation joins the original canonical owner; observations never authorize."""
import asyncio
import unittest
from types import SimpleNamespace

from codex_web.services.native_model_provider import NativeProcessOwnership
from codex_web.services.native_reasoning_bridge import CanonicalNativeReasoningBridge
from codex_web.services.codex_reasoning_policy import NativeReasoningPolicyError


class NativeReasoningOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_waiter_joins_preparation_before_releasing_scope(self):
        entered, release, joined = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls=[]
        class Held(CanonicalNativeReasoningBridge):
            async def _invoke_owned(self,*args,cancelled,**kwargs):
                calls.append('original')
                entered.set()
                await release.wait()
                assert cancelled.is_set()
                joined.set()
                raise asyncio.CancelledError
        bridge=object.__new__(Held)
        task=asyncio.create_task(bridge.invoke(None,None,None,actor=None,context=None))
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(joined.is_set())
        self.assertEqual(calls,['original'])

    async def test_cancel_interrupts_wait_for_missing_terminal_measurement(self):
        cancelled=asyncio.Event()
        result=asyncio.get_running_loop().create_future()
        waiter=asyncio.create_task(CanonicalNativeReasoningBridge._wait_or_cancel(result,cancelled))
        cancelled.set()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertFalse(result.done())  # Cancellation is not a fabricated terminal result.

    async def test_one_physical_owner_cannot_be_rebound_to_another_assignment(self):
        owner=NativeProcessOwnership()
        stop=lambda:None
        owner.attach('original',stop)
        with self.assertRaises(NativeReasoningPolicyError):
            owner.attach('replacement',lambda:None)
        self.assertEqual(owner.assignment_id,'original')
        self.assertIs(owner.stop,stop)

    async def test_usage_notification_waits_for_real_terminal_call_measurements(self):
        bridge=object.__new__(CanonicalNativeReasoningBridge)
        future=asyncio.get_running_loop().create_future()
        bridge._usage_waiters={'session':(asyncio.get_running_loop(),future)}
        record=SimpleNamespace(agent_session_id='session',runtime_id='codex-reasoning',
            terminal_outcome='unknown',model_call_count=None,input_tokens=1,output_tokens=2,
            provider_native_turn_id='original')
        bridge.observe_usage(record)
        await asyncio.sleep(0)
        self.assertFalse(future.done())
        record.terminal_outcome='succeeded';record.model_call_count=1
        bridge.observe_usage(record)
        await asyncio.sleep(0)
        self.assertEqual(future.result(),'original')
