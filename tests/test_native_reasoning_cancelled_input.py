"""Held queue admission cannot outlive the original inference cancellation."""
import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from codex_web.agent_runtime import AgentSession
from codex_web.events import EventHub
from codex_web.model_gateway import ModelProviderRecord,ModelDefinitionRecord,ModelInvocationRequest,ModelMessage
from codex_web.models import QueuedTurn
from codex_web.runtime.codex import CodexRuntime
from codex_web.services.codex_reasoning_policy import ReasoningOnlyCodexRuntime,NativeReasoningPolicyError
from codex_web.services.native_model_provider import (NativeCodexModelProviderAdapter,
    NativeModelInvocationContext,current_native_model_invocation)
from codex_web.services.native_reasoning_bridge import CanonicalNativeReasoningBridge,NativeReasoningBinding
from codex_web.services.execution_preflight import ExecutionPreflightService
from codex_web.services.identity import IdentityService
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.agent_sessions import AgentSessionStore
from codex_web.storage.execution_preflight import ExecutionPreflightStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.turn_queue import TurnQueueRepository


class NativeHeldInputTests(unittest.IsolatedAsyncioTestCase):
    async def check_held(self, timed_out):
        with tempfile.TemporaryDirectory() as private:
            root=Path(private)
            store=SQLiteStateStore(root/'state.sqlite')
            identity=IdentityService(IdentityStateStore(store));identity.bootstrap_local()
            actor=identity.local_trusted_actor()
            sessions=AgentSessionStore(store)
            original=sessions.upsert(AgentSession(organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,provider_id='openai',runtime_id='codex-reasoning',
                runtime_type='codex-app-server',provider_native_session_id='one-thread',
                project_id='project',execution_id='one-bootstrap',assignment_id='one-assignment',
                worker_id='one-worker',model='model'))
            queue=TurnQueueRepository(store,root/'queue.json')
            preflight=ExecutionPreflightService(ExecutionPreflightStore(store))
            stopped=[];entered=asyncio.Event();release=asyncio.Event();refusals=[]
            assignment=NS(id='one-assignment',execution_id='one-bootstrap',assigned_worker_id='one-worker',fence=1)
            native=object.__new__(ReasoningOnlyCodexRuntime)
            native.reasoning_refusal=None
            native.native_runtime_version='0.160.0';native.restart_on_timeout=False
            class Manager:
                async def stop(self,assignment_id):
                    stopped.append(assignment_id)
            manager=Manager()
            class Threads:
                async def create(self,**kwargs):
                    context=current_native_model_invocation()
                    context.process_owner.attach(assignment.id,lambda:manager.stop(assignment.id))
                    return {'thread':{'id':'one-thread'},'agentSessionId':original.id}
            class Turns:
                async def start(self,thread_id,payload,**kwargs):
                    # Same retained canonical queue/preflight owners. Deliberately
                    # do not re-use the caller's advisory admission_guard here:
                    # the actual native writer must independently refuse.
                    item=QueuedTurn(id='original-input',thread_id=thread_id,project_id='project',
                        message=payload.message,execution_id=kwargs['execution_id'],created_at=time.time(),
                        agent_profile_actor_id=actor.identity_id)
                    queue.put(thread_id,[item]);preflight.retain_queued(item,
                        organization_id=actor.organization_id,workspace_id=actor.workspace_id)
                    entered.set();await release.wait()
                    try:
                        await native.request('turn/start',{'threadId':thread_id})
                    except NativeReasoningPolicyError as exc:
                        refusals.append(str(exc));raise
                    return {'queuedId':item.id}
            bridge=CanonicalNativeReasoningBridge(binding=NativeReasoningBinding(provider_id='native',
                model_id='model',concrete_model='model',agent_profile_id='thinker',agent_profile_revision=1,
                execution_profile_id='reasoning',worker_id='one-worker'),threads=Threads(),turns=Turns(),
                sessions=sessions,usage=None,preflight=preflight,
                execution=NS(_session_for_assignment=lambda _: (manager,NS(runtime=native,validate_current=lambda:assignment)),
                             thread_completion_tasks={}),hub=EventHub(),attest_session=lambda *args:None)
            adapter=NativeCodexModelProviderAdapter(bridge)
            provider=ModelProviderRecord(id='native',display_name='Native',adapter_type=adapter.adapter_type,
                runtime_provider='codex',credential_required=False,access_source='chatgpt_subscription',usage_semantics='entitlement',
                organization_id=actor.organization_id,workspace_id=actor.workspace_id,updated_by=actor.identity_id)
            model=ModelDefinitionRecord(id='model',provider_id='native',concrete_model='model',
                organization_id=actor.organization_id,workspace_id=actor.workspace_id,updated_by=actor.identity_id)
            context=NativeModelInvocationContext(organization_id=actor.organization_id,workspace_id=actor.workspace_id,
                actor_id=actor.identity_id,project_id='project',activation_id='a',role_id='cto',
                execution_id='executive:a:cto',max_input_tokens=1000,max_output_tokens=1000,validate_current=lambda:None)
            request=ModelInvocationRequest(model_class='strategic',messages=(ModelMessage(role='user',content='one question'),),
                max_input_tokens=1000,max_output_tokens=1000,allow_fallback=False,
                purpose='executive-role:cto',execution_id='executive:a:cto')
            wire=AsyncMock(return_value={})
            with patch.object(CodexRuntime,'request',wire):
                invocation=asyncio.create_task(adapter.invoke_scoped(provider,model,request,actor=actor,context=context,credential=None))
                await entered.wait()
                if timed_out:
                    async def later_release():
                        await asyncio.sleep(.03);release.set()
                    releasing=asyncio.create_task(later_release())
                    with self.assertRaises(asyncio.TimeoutError):
                        await asyncio.wait_for(invocation,.01)
                    await releasing
                else:
                    invocation.cancel();await asyncio.sleep(0)
                    self.assertFalse(invocation.done())
                    release.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await invocation
                wire.assert_not_awaited()
            self.assertEqual(refusals,['Native invocation cancelled or deadline expired'])
            self.assertEqual(stopped,['one-assignment'])
            retained=queue.get('one-thread')
            self.assertEqual(len(retained),1)
            self.assertEqual(retained[0].id,'original-input')
            attempt=preflight.store.get('preflight-queued-original-input')
            self.assertEqual(attempt.status,'queued')
            self.assertIsNone(attempt.queued_start_receipt)
            self.assertEqual(attempt.queued_turn_snapshot['message'],retained[0].message)

    async def test_cancel_during_held_turn_start_refuses_actual_native_writer(self):
        await self.check_held(False)

    async def test_timeout_during_held_turn_start_refuses_actual_native_writer(self):
        await self.check_held(True)

    async def test_cancel_while_actual_native_write_lock_waits_refuses_bytes(self):
        await self.check_writer(False)

    async def test_deadline_while_actual_native_write_lock_waits_refuses_bytes(self):
        await self.check_writer(True)

    async def check_writer(self, deadline):
        import io
        from codex_web.services.native_model_provider import NativeProcessOwnership
        owner=NativeProcessOwnership(deadline_at=time.monotonic()+10)
        stream=io.StringIO()
        process=NS(stdin=stream,poll=lambda:None)
        runtime=object.__new__(ReasoningOnlyCodexRuntime)
        runtime.proc=process;runtime.generation='original';runtime.write_lock=asyncio.Lock()
        await runtime.write_lock.acquire()
        with patch('codex_web.services.native_model_provider.current_native_model_invocation',
                   return_value=NS(validate_current=owner.validate_current)):
            pending=asyncio.create_task(runtime._send({'method':'turn/start','id':1,'params':{}}))
            await asyncio.sleep(0)
            if deadline:
                owner.deadline_at=time.monotonic()-1
            else:
                owner.cancelled.set()
            runtime.write_lock.release()
            with self.assertRaisesRegex(NativeReasoningPolicyError,'cancelled or deadline expired'):
                await pending
        self.assertEqual(stream.getvalue(),'')


    async def test_valid_reservation_cannot_send_changed_message_session_or_workspace(self):
        import io
        import hashlib
        from codex_web.services.native_model_provider import NativeProcessOwnership
        owner=NativeProcessOwnership(assignment_id='original',deadline_at=time.monotonic()+10)
        owner.bind_workspace('/canonical/scratch');owner.bind_native_session('original-thread')
        context=NS(validate_current=owner.validate_current,process_owner=owner,concrete_model='model',
                   input_message_sha256=hashlib.sha256(b'original').hexdigest())
        stream=io.StringIO();native=object.__new__(ReasoningOnlyCodexRuntime)
        native.proc=NS(stdin=stream,poll=lambda:None);native.generation='original';native.write_lock=asyncio.Lock()
        base={'threadId':'original-thread','cwd':'/canonical/scratch','model':'model',
              'approvalPolicy':'never','sandboxPolicy':{'type':'readOnly'},
              'input':[{'type':'text','text':'original','text_elements':[]}]}
        with patch('codex_web.services.native_model_provider.current_native_model_invocation',return_value=context):
            for changed in ({'threadId':'other-thread'},{'cwd':'/other'},
                            {'input':[{'type':'text','text':'changed','text_elements':[]}]}):
                with self.subTest(changed=changed):
                    with self.assertRaises(NativeReasoningPolicyError):
                        await native._send({'method':'turn/start','id':1,'params':{**base,**changed}},start_guard=lambda:None)
            with self.assertRaisesRegex(NativeReasoningPolicyError,'START capture'):
                await native._send({'method':'turn/start','id':1,'params':base})
        self.assertEqual(stream.getvalue(),'')


class NativeAnswerTests(unittest.TestCase):
    def test_commentary_is_not_concatenated_with_final_json(self):
        from codex_web.services.native_reasoning_bridge import NativeReasoningText
        answer=NativeReasoningText(1024)
        answer.observe('item/completed',{'item':{'type':'agentMessage','phase':'commentary','text':'I will assess.'}})
        answer.observe('item/completed',{'item':{'type':'agentMessage','phase':'final_answer','text':'{"result":"ok"}'}})
        self.assertEqual(answer.result(),'{"result":"ok"}')

    def test_missing_or_ambiguous_final_answer_refuses(self):
        from codex_web.services.native_reasoning_bridge import NativeReasoningText
        answer=NativeReasoningText(1024)
        answer.observe('item/completed',{'item':{'type':'agentMessage','phase':'commentary','text':'Thinking'}})
        with self.assertRaises(ValueError):answer.result()
        for text in ('one','two'):
            answer.observe('item/completed',{'item':{'type':'agentMessage','phase':'final_answer','text':text}})
        with self.assertRaises(ValueError):answer.result()

    def test_observed_bytes_are_bounded_before_result(self):
        from codex_web.services.native_reasoning_bridge import NativeReasoningText
        answer=NativeReasoningText(3)
        with self.assertRaises(ValueError):
            answer.observe('item/agentMessage/delta',{'delta':'four'})
