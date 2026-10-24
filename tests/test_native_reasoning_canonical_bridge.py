"""Real Thread/Turn/worker/queue/START composition, with fake process wire only."""
import asyncio
import hashlib
import inspect
import io
import json
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from codex_web.agent_profiles import AgentProfileCreate
from codex_web.agent_runtime_usage import AgentRuntimeUsage
from codex_web.definitions import DefinitionDraftCreate,DefinitionPublishRequest
from codex_web.events import EventHub
from codex_web.execution_workers import AssignmentClaimRequest,AssignmentStartRequest,ExecutionRuntimeBinding
from codex_web.model_gateway import ModelProviderRecord,ModelDefinitionRecord,ModelInvocationRequest,ModelMessage
from codex_web.services.agent_process_session import AssignmentBoundAgentProcessSession,AssignmentBoundAgentProcessSessionManager
from codex_web.services.codex_reasoning_policy import (ReasoningOnlyCodexRuntime,ReasoningOnlyCodexAdapter,
    DISABLED_FEATURES,REASONING_CATALOG)
from codex_web.services.native_model_provider import NativeCodexModelProviderAdapter,NativeModelInvocationContext,current_native_model_invocation
from codex_web.services.native_reasoning_bridge import CanonicalNativeReasoningBridge,NativeReasoningBinding
from codex_web.services.threads import ThreadService
from codex_web.storage.agent_runtime_usage import AgentRuntimeUsageStore
from tests import test_home_coordination_canonical_send as canonical_fixtures
from tests.test_codex_worker_session import _FakeDelegationService
from tests.test_codex_runtime import _Host as CodexHost


class NativeCanonicalBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_thread_turn_original_start_and_usage_complete_once(self):
        await self._run_private_case()

    async def test_null_gateway_budget_keeps_actual_joint_context_capacity(self):
        await self._run_private_case(token_optout=True)

    async def test_null_gateway_budget_refuses_actual_joint_context_overflow(self):
        await self._run_private_case(token_optout=True, input_usage=11900, expected_refusal=True)

    async def test_null_gateway_budget_retains_declared_model_output_capacity(self):
        await self._run_private_case(token_optout=True, input_usage=1000, output_usage=2049,
                                     expected_refusal=True)

    async def _run_private_case(self, *, token_optout=False, input_usage=11000,
                                output_usage=256, expected_refusal=False):
        f=canonical_fixtures.HomeCanonicalSendTests()
        self.addAsyncCleanup(self.cleanup,f)
        await f.asyncSetUp()
        # The shared fixture simplifies this to the UI sandbox spelling.
        # Exercise the real protocol policy here, not that unrelated stub.
        from codex_web.services.project_runtime import ProjectRuntimeService
        f.host._sandbox_policy = ProjectRuntimeService.sandbox_policy
        payload=f.f.execution_profiles.record(organization_id=f.actor.organization_id,
            workspace_id=f.actor.workspace_id).payload.copy()
        payload['profiles']=[*payload['profiles'],dict(id='reasoning-only',name='Reasoning only',
            description='Private tool-free reasoning',workspace_mode='scratch',repository_access='none',network_enabled=False,
            required_worker_capabilities=['command_execution'],control_plane_operations=[])]
        record=f.registry.create_draft(DefinitionDraftCreate(definition_id='execution.profiles.default',
            kind='execution-profile-catalog',definition_schema_version='1.3',scope_type='global',
            payload=payload,actor=f.actor.identity_id))
        f.registry.publish(record.record_id,DefinitionPublishRequest(actor=f.actor.identity_id))
        profile=f.profiles.create(AgentProfileCreate(profile_id='thinker',name='Isolated thinker',
            execution_profile_id='reasoning-only',sandbox_requirement='read-only'),actor=f.actor)
        f.f.service.local_session_probe=lambda:True
        runtime_binding=ExecutionRuntimeBinding(provider_id='openai',runtime_id='codex-reasoning',
            capability_revision=1,sandbox_profiles=('read-only',),authentication_mode='trusted_local_session')
        hub=EventHub();usage=AgentRuntimeUsageStore(f.f.sqlite)
        native_sessions={};stopped=[];wire=io.StringIO();test=self
        class Manager:
            local_worker=f.native_sessions[f.target.id].local_worker
            sessions=native_sessions
            checkpoint=AssignmentBoundAgentProcessSessionManager.checkpoint
            complete=AssignmentBoundAgentProcessSessionManager.complete
            def get(self,assignment_id):return native_sessions.get(assignment_id)
            async def start(self,assignment_id):
                if assignment_id in native_sessions:return native_sessions[assignment_id]
                context=current_native_model_invocation()
                context.process_owner.attach(assignment_id,lambda:self.stop(assignment_id))
                claim=f.f.workers.claim(f.f.worker.id,AssignmentClaimRequest(),actor=f.worker_actor,assignment_id=assignment_id)
                assignment=f.f.workers.start(f.f.worker.id,assignment_id,
                    AssignmentStartRequest(fence=claim.fence,lease_token=claim.lease.lease_token),actor=f.worker_actor)
                credentials=_FakeDelegationService()
                session=AssignmentBoundAgentProcessSession(f.native_sessions[f.target.id].local_worker,f.host,assignment_id,
                    runtime_factory=ReasoningOnlyCodexRuntime,credential_provider=credentials,runtime_binding=assignment.runtime_binding)
                test.addCleanup(session._supervision_executor.shutdown,wait=True,cancel_futures=True)
                session.fence=assignment.fence
                session.delegation=credentials._delegation(assignment,f.f.worker.id,assignment.fence)
                session.workspace_path=Path(f.f.workspaces.get(assignment.execution_workspace_id,f.actor).path)
                context.process_owner.bind_workspace(session.workspace_path)
                session.started_monotonic=time.monotonic()
                native=ReasoningOnlyCodexRuntime(CodexHost())
                native.native_runtime_version='0.160.0';native.ensure_started=AsyncMock();native.ready.set()
                native.proc=NS(poll=lambda:None,stdin=wire,pid=99999999)
                original_send=native._send
                async def send(message,**options):
                    await original_send(message,**options)
                    method=message['method']
                    if method=='config/read':
                        result={'config':{'features':dict.fromkeys(DISABLED_FEATURES,False),'mcp_servers':{},
                            'web_search':'disabled','forced_login_method':'chatgpt','model_provider':'openai',
                            'model_catalog_json':str(REASONING_CATALOG),'project_doc_max_bytes':0}}
                    elif method=='turn/start':result={'turn':{'id':'actual-one-turn','threadId':'native-reasoning-thread'}}
                    else:result={'thread':{'id':'native-reasoning-thread'}}
                    await native._dispatch_message({'id':message['id'],'result':result})
                native._send=send;session.runtime=native;native_sessions[assignment_id]=session
                return session
            async def stop(self,assignment_id):
                if native_sessions.pop(assignment_id,None) is not None:
                    stopped.append(assignment_id)
        manager=Manager()
        async def route(request,*,actor):
            selected,_=f.profiles.resolve_for_execution(request.agent_profile_id,revision=request.agent_profile_revision,
                actor=actor,project_id=request.project_id)
            return NS(selected_runtime=NS(execution_binding=lambda:runtime_binding),agent_profile=f.profiles.binding_for(selected))
        threads=ThreadService(runtime_transport=None,runtime_request_for_thread=None,event_sink=lambda _:None,
            binding_service=f.f.service,session_manager=manager,bootstrap_bindings=f.bindings,control_actor=f.actor,
            agent_sessions=f.sessions,agent_profiles=f.profiles,routing_service=NS(route=route),
            session_managers={('openai','codex-reasoning'):manager},runtime_adapter_factory=lambda b,s:ReasoningOnlyCodexAdapter(s),
            project_runtime=f.runtime,settings=f.settings,thread_index=f.index,thread_scope=f.scope,
            active_turn_getter=lambda _:None)
        f.execution.session_managers={('openai','codex-reasoning'):manager}
        f.execution.runtime_adapter_factory=lambda b,s:ReasoningOnlyCodexAdapter(s)
        errors=[]
        original_start=f.execution.start_thread_turn_now
        async def traced_start(*args,**kwargs):
            try:return await original_start(*args,**kwargs)
            except Exception as error:
                errors.append((type(error).__name__,str(error)))
                raise
        f.execution.start_thread_turn_now=traced_start
        from codex_web.runtime.execution import TurnExecutionService
        f.execution.schedule_queue_drain=TurnExecutionService.schedule_queue_drain.__get__(f.execution)
        binding=NativeReasoningBinding(provider_id='native',model_id='model',concrete_model='model',
            agent_profile_id=profile.profile_id,agent_profile_revision=profile.revision,
            execution_profile_id='reasoning-only',worker_id=f.f.worker.id)
        bridge=CanonicalNativeReasoningBridge(binding=binding,threads=threads,turns=f.turns,sessions=f.sessions.store,
            usage=usage,preflight=f.execution.queued_preflight,execution=f.execution,hub=hub,
            attest_session=lambda serving,assignment,context,model:serving.validate_current())
        adapter=NativeCodexModelProviderAdapter(bridge)
        fields=dict(organization_id=f.actor.organization_id,workspace_id=f.actor.workspace_id,updated_by=f.actor.identity_id)
        provider=ModelProviderRecord(id='native',display_name='Native',adapter_type=adapter.adapter_type,
            runtime_provider='codex',credential_required=False,access_source='chatgpt_subscription',usage_semantics='entitlement',**fields)
        model=ModelDefinitionRecord(id='model',provider_id='native',concrete_model='model',**fields)
        input_ceiling = None if token_optout else 1000
        output_ceiling = None if token_optout else 1000
        context=NativeModelInvocationContext(organization_id=f.actor.organization_id,workspace_id=f.actor.workspace_id,
            actor_id=f.actor.identity_id,project_id='data',activation_id='one',role_id='cto',execution_id='executive:one:cto',
            max_input_tokens=input_ceiling,max_output_tokens=output_ceiling,validate_current=lambda:None)
        request=ModelInvocationRequest(model_class='strategic',messages=(ModelMessage(role='user',content='Assess once'),),
            max_input_tokens=input_ceiling,max_output_tokens=output_ceiling,allow_fallback=False,purpose='executive-role:cto',execution_id='executive:one:cto')
        if token_optout:
            from codex_web.model_gateway import ModelProviderUpsert, ModelDefinitionUpsert, PromptTemplateUpsert
            from codex_web.services.model_gateway import ModelGatewayService
            from codex_web.storage.model_gateway import ModelGatewayStore
            gateway = ModelGatewayService(ModelGatewayStore(f.f.sqlite))
            gateway.register_adapter(adapter)
            gateway.upsert_provider(ModelProviderUpsert(id='native',display_name='Native',
                adapter_type=adapter.adapter_type,runtime_provider='codex',credential_required=False,
                access_source='chatgpt_subscription',usage_semantics='entitlement'),actor=f.actor)
            gateway.upsert_model(ModelDefinitionUpsert(id='model',provider_id='native',concrete_model='model',
                model_classes=('strategic',),capabilities=('text','reasoning'),
                context_window_tokens=12000,max_output_tokens=2048),actor=f.actor)
            gateway.upsert_template(PromptTemplateUpsert(template_id='generic.system',version='1.0',
                content='{{ instructions }}'),actor=f.actor)
            invocation=asyncio.create_task(gateway.invoke(request,actor=f.actor,native_context=context))
        else:
            invocation=asyncio.create_task(adapter.invoke_scoped(provider,model,request,actor=f.actor,context=context,credential=None))
        async def wait_queue():
            while (f.execution._active_turn('native-reasoning-thread') is None
                       or f.execution._active_turn('native-reasoning-thread').turn_id != 'actual-one-turn'):
                if errors:raise AssertionError(errors)
                if invocation.done():return await invocation
                await asyncio.sleep(.01)
        try:
            await asyncio.wait_for(wait_queue(),5)
        except TimeoutError:
            active=f.execution._active_turn('native-reasoning-thread')
            invocation.cancel();await asyncio.gather(invocation,return_exceptions=True)
            self.fail({'active':active.model_dump() if active else None,'events':f.host.events,
                       'wire_methods':[json.loads(line)['method'] for line in wire.getvalue().splitlines()]})
        active=f.execution._active_turn('native-reasoning-thread')
        self.assertIsNotNone(active,(f.queue_store.get("native-reasoning-thread"), f.host.events))
        session=f.sessions.store.find_unique_by_native_id('native-reasoning-thread',provider_id='openai',runtime_id='codex-reasoning')
        measured=AgentRuntimeUsage(id='runtime-usage-'+hashlib.sha256(f'{session.id}\n{active.turn_id}'.encode()).hexdigest()[:32],
            organization_id=f.actor.organization_id,workspace_id=f.actor.workspace_id,project_id='data',
            agent_session_id=session.id,assignment_id=active.assignment_id,execution_id=active.execution_id,
            execution_workspace_id=active.execution_workspace_id,provider_id='openai',runtime_id='codex-reasoning',
            runtime_type='codex-app-server',provider_native_session_id=active.thread_id,provider_native_turn_id=active.turn_id,
            model_call_count=1,input_tokens=input_usage if token_optout else 17,
            output_tokens=output_usage if token_optout else 9,runtime_duration_seconds=.1,
            terminal_outcome='succeeded',telemetry_completeness='exact')
        usage.upsert(measured);bridge.observe_usage(measured)
        for method,params in [('item/completed',{'threadId':active.thread_id,'item':{'type':'agentMessage','phase':'final_answer','text':'{"answer":"ok"}'}}),
                              ('turn/completed',{'threadId':active.thread_id,'turn':{'id':active.turn_id,'status':'completed'}})]:
            await hub.publish({'type':'codex.event','message':{'method':method,'params':params}})
        if expected_refusal:
            from codex_web.services.model_gateway import ModelProviderUnavailableError
            with self.assertRaises(ModelProviderUnavailableError):
                await asyncio.wait_for(invocation,5)
            self.assertEqual(sum(json.loads(line)['method']=='turn/start'
                                 for line in wire.getvalue().splitlines()),1)
            self.assertEqual(f.execution.queued_preflight.store.get(active.queued_admission_attempt_id).status,'started')
            self.assertNotEqual(f.f.workers.store.assignment(active.assignment_id).status,'succeeded')
            return
        result=await asyncio.wait_for(invocation,5)
        self.assertEqual(result.text,'{"answer":"ok"}')
        if token_optout:
            self.assertEqual((result.invocation.attempts[0].input_tokens,
                              result.invocation.attempts[0].output_tokens),(11000,256))
            self.assertIsNone(result.invocation.attempts[0].actual_cost_usd)
        else:
            self.assertEqual((result.usage.input_tokens,result.usage.output_tokens),(17,9))
            self.assertIsNone(result.usage.cost)
        self.assertEqual(len(stopped),1)
        self.assertEqual(f.f.workers.store.assignment(active.assignment_id).status,'succeeded')
        writes=[json.loads(line) for line in wire.getvalue().splitlines()]
        self.assertEqual(sum(row['method']=='turn/start' for row in writes),1)
        attempt=f.execution.queued_preflight.store.get(active.queued_admission_attempt_id)
        self.assertEqual(attempt.status,'started')
        self.assertEqual(attempt.execution_id,'executive:one:cto')
        self.assertEqual(attempt.queued_start_receipt.execution_id,active.execution_id)

    async def cleanup(self,fixture):
        while fixture._cleanups:
            callback,args,kwargs=fixture._cleanups.pop()
            result=callback(*args,**kwargs)
            if inspect.isawaitable(result):await result
