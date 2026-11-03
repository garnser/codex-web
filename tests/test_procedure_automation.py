"""Private canonical stores/interpreter; native/profile/routing observations are controlled."""
from __future__ import annotations

import json
import time
import unittest
from types import SimpleNamespace
from pydantic import ValidationError

from codex_web.automation_definitions import AutomationDefinition
from codex_web.automation_runs import AutomationRunState
from codex_web.definitions import DefinitionReference, DefinitionScope
from codex_web.models import WorkItemHandoff, WorkItemState
from codex_web.procedure_control_plane import CanonicalOperationContract, CanonicalOperationRegistry, CanonicalOperationResult
from codex_web.services.automation_definitions import install_automation_definitions, AutomationEventTriggerService
from codex_web.services.automation_runs import AutomationRunService, AutomationTriggerAdmissionBridge
from codex_web.services.canonical_events import CanonicalEventBus, CanonicalEventIngestionService
from codex_web.services.keyed_background_tasks import KeyedTaskCoordinator
from codex_web.services.procedure_automation import ProcedureAutomationService
from codex_web.services.procedure_custody_recovery import CustodyInput
from codex_web.storage.automation_runs import AutomationRunStore
from codex_web.storage.canonical_events import CanonicalEventStore
from tests import test_procedure_execution as fixture
from tests.test_procedure_control_plane import content


REF = dict(definition_id='fixture-skill',kind='agent.skill',revision=1,
    record_id='fixture-record',checksum='a'*64,definition_schema_version='1.0')


def automation_payload(reference=None, *, advances=8):
    return {'name':'Canonical custody fixture', 'lifecycle':'enabled',
        'trigger':{'type':'canonical_event','event_type':'work.transition','event_filter':{'project_id':'home'}},
        'target':{'kind':'canonical_procedure','id':(reference or REF)['definition_id'],
            'procedure_ref':reference or REF,'supervisor_role':'supervisor','max_advances':advances},
        'instructions':'Use the exact deterministic canonical procedure; no model call.',
        'owner_identity_id':'local-admin','work_item_policy':'reuse_only',
        'budget':{'max_input_tokens':0,'max_output_tokens':0,'max_cost_usd':0,
            'max_duration_seconds':120,'max_concurrency':1},'retry':{'max_attempts':1}}


class ProcedureAutomationContractTests(unittest.TestCase):
    def test_explicit_target_contract_is_supported(self):
        refusal = None
        try:
            parsed = AutomationDefinition.model_validate(automation_payload())
        except ValidationError as exc:
            refusal = str(exc)
        self.assertIsNone(refusal,refusal)
        self.assertEqual(parsed.target.procedure_ref,DefinitionReference.model_validate(REF))

    def test_legacy_payload_has_no_new_target_or_run_fields(self):
        payload=automation_payload();payload['target']={'kind':'agent_profile','id':'profile'}
        target=AutomationDefinition.model_validate(payload).model_dump(mode='json')['target']
        self.assertEqual(target,{'kind':'agent_profile','id':'profile'})
        self.assertEqual(AutomationRunState().model_dump(mode='json'),{'schema_version':'1.4','runs':[]})

    def test_old_schema_validators_reject_new_fields_including_null(self):
        import tempfile
        from pathlib import Path
        from codex_web.services.definitions import DefinitionRegistryService
        from codex_web.storage.definition_registry import DefinitionRegistryStore
        from codex_web.storage.sqlite_state import SQLiteStateStore
        with tempfile.TemporaryDirectory() as folder:
            registry=DefinitionRegistryService(DefinitionRegistryStore(SQLiteStateStore(Path(folder)/'state.sqlite')))
            install_automation_definitions(registry)
            for version in ('1.0','1.1'):
                validate=registry.schemas.get('automation',version).validate
                with self.subTest(version=version):
                    with self.assertRaises(ValueError):validate(automation_payload())
                    for key in ('procedure_ref','supervisor_role','max_advances'):
                        value=automation_payload();value['target']={'kind':'agent_profile','id':'profile',key:None}
                        with self.assertRaises(ValueError):validate(value)

    def test_contract_cannot_choose_actor_transport_scope_or_unbounded_calls(self):
        for variant in ('actor','url','repository_ids','max_advances','model_budget','trigger','manual'):
            with self.subTest(variant=variant):
                value=automation_payload()
                if variant in {'actor','url','repository_ids'}:value['target'][variant]='forbidden'
                elif variant=='max_advances':value['target']['max_advances']=65
                elif variant=='model_budget':value['budget']['max_input_tokens']=1
                elif variant=='trigger':value['trigger']['event_type']='ci.pipeline'
                else:value['trigger']={'type':'manual'}
                with self.assertRaises(ValidationError):AutomationDefinition.model_validate(value)


class Owner:
    """Controlled external canonical-owner edge, not a provider/native result."""
    def __init__(self, sqlite):
        self.sqlite=sqlite
        self.ready=False
        self.interrupt=False
        self.calls=0
        self.reconciliations=0
        self.before_effect=None

    def effects(self):
        return (self.sqlite.record_get('fixture_canonical_owner','effect') or {}).get('count',0)

    def effect(self):
        if self.before_effect is not None:self.before_effect()
        def update(current):return {'count':(current or {}).get('count',0)+1}
        self.sqlite.record_update('fixture_canonical_owner','effect',update,default={})

    async def execute(self,context,value):
        context.validate_current();self.calls+=1
        if context.step.kind in {'mutation','recovery'}:
            return CanonicalOperationResult(disposition='waiting',code='fixture_before_effect:busy')
        return CanonicalOperationResult(disposition='succeeded',outputs={'observed':True})

    async def reconcile(self,context,value):
        import asyncio
        context.validate_current();self.reconciliations+=1
        previous=context.previous_step
        if (previous.status=='waiting' and previous.operation_code=='fixture_before_effect:busy'
                and previous.diagnostic_evidence_ids):
            if not self.ready:return CanonicalOperationResult(disposition='waiting',code='fixture_before_effect:busy')
            self.effect()
            if self.interrupt:raise asyncio.CancelledError()
            return CanonicalOperationResult(disposition='succeeded',outputs={'observed':True})
        return CanonicalOperationResult(disposition='reconciliation_required',code='fixture_unknown_owner')


def procedure_content(effect):
    from tests.test_procedure_control_plane import OUTPUT
    value=content(effect)
    inputs=[{'name':name,'type':kind,'required':required,'description':'Exact governed canonical input'}
        for name,kind,required in [('work_item_ref','string',True),('target_assignment_id','string',True),
            ('expected_target_fence','integer',True),('expected_handoff_requested_at','number',True),
            ('original_preflight_id','string',False)]]
    value['inputs']=inputs
    for step in value['steps']:
        step['inputs']=inputs
        step['failure'].pop('escalation_owner_identity_id')
        step['failure']['escalation_owner_policy']='current_work_action_owner'
    return value,CanonicalOperationContract(operation_id='fixture.canonical.v1',contract_version='1.0',
        resource_types=('service',),required_capability='service.read' if effect=='read' else 'service.change',
        authority_level='read' if effect=='read' else 'execute',input_model=CustodyInput,
        output_contracts=(OUTPUT,),effect=effect,timeout_seconds=10)


class ProcedureAutomationIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def build(self,effect='read',*,advances=8,claim_target=False,event_filter=None):
        from codex_web.agent_profiles import AgentProfileExecutionBinding
        from codex_web.execution_workers import ExecutionAssignmentCreate
        from codex_web.services.autonomy_controller import AutonomyController
        from codex_web.storage.autonomy import AutonomyStateStore
        f=self.env=fixture.ProcedureExecutionTests()
        self.owner=Owner(None)
        registry=CanonicalOperationRegistry()
        value,contract=procedure_content(effect)
        registry.register(contract,self.owner)
        if effect=='mutation':
            from dataclasses import replace
            registry.register(replace(contract,operation_id='fixture.verify.v1',effect='read',
                required_capability='service.read',authority_level='read'),self.owner)
        f.assignment_options=lambda:{'agent_profile':AgentProfileExecutionBinding(
            profile_id='fixture-supervisor',profile_revision=1,profile_record_id='fixture-profile-record',role_id='supervisor')}
        def configure(_):
            self.owner.sqlite=f.sqlite
            f.skills.procedure_control_plane=registry
            self.target=f.workers.create_assignment(ExecutionAssignmentCreate(subject={'kind':'thread','ref':'recipient'},
                execution_id='recipient-execution',project_id=f.project.id,
                resource_ids=(f.resource.id,),required_capabilities=('command_execution',),
                execution_contract_version='1.0',sandbox='workspace-write',
                agent_profile=AgentProfileExecutionBinding(profile_id='fixture-recipient',profile_revision=1,
                    profile_record_id='fixture-recipient-record',role_id='recipient')),actor=f.actor)
            if claim_target:
                from codex_web.execution_workers import AssignmentClaimRequest, AssignmentStartRequest, ExecutionWorkerRegister
                recipient_worker=f.workers.register(ExecutionWorkerRegister(
                    service_identity_id=f.worker_actor.identity_id,pool='recipient',version='fixture',
                    capabilities=('command_execution',)),actor=f.actor)
                claim=f.workers.claim(recipient_worker.id,AssignmentClaimRequest(lease_seconds=120),
                    actor=f.worker_actor,assignment_id=self.target.id)
                self.target=f.workers.start(recipient_worker.id,self.target.id,AssignmentStartRequest(
                    lease_token=claim.lease.lease_token,fence=claim.fence),actor=f.worker_actor)
            self.values={'work_item_ref':'fixture!1','target_assignment_id':self.target.id,
                'expected_target_fence':self.target.fence,'expected_handoff_requested_at':10.0}
            f.snapshot=f.artifact(json.dumps(self.values).encode(),'canonical-existing-input')
            return value
        f.configure=configure
        def rebuild():
            fixture.ProcedureExecutionTests.rebuild(f)
            f.runner.control_plane=registry
        f.rebuild=rebuild
        await f.asyncSetUp();self.addCleanup(f.tearDown)
        self.initial_checkpoint=f.workers.store.assignment(f.assignment.id).procedure_checkpoint.id
        self.definitions=install_automation_definitions(f.definition_registry)
        self.run_store=AutomationRunStore(f.sqlite)
        self.runs=AutomationRunService(self.run_store,self.definitions)
        self.bus=CanonicalEventBus(CanonicalEventStore(f.sqlite))
        self.events=CanonicalEventIngestionService(self.bus)
        self.triggers=AutomationEventTriggerService(f.definition_registry,self.bus)
        self.coordinator=KeyedTaskCoordinator(max_concurrency=1,per_scope_concurrency=1)
        self.addAsyncCleanup(self.coordinator.stop)
        self.controller=AutonomyController(AutonomyStateStore(f.sqlite))
        self.controller.resume(actor_id=f.actor.identity_id)
        self.work=WorkItemState(ref='fixture!1',organization_id=f.actor.organization_id,
            workspace_id=f.actor.workspace_id,project_id=f.project.id,resource_ids=[f.resource.id],
            current_owner='sender',next_owner='recipient',created_at=time.time(),updated_at=time.time(),
            last_meaningful_update_at=time.time(),
            handoff=WorkItemHandoff(from_agent='sender',to_agent='recipient',requested_at=10.0))
        self.save_work()
        self.target_busy=False
        self.binding_threads={'supervisor':'supervisor','recipient':'recipient'}
        def assignment(thread):return f.workers.store.assignment(f.assignment.id if thread=='supervisor' else self.target.id)
        def bootstrap(thread,actor):
            a=assignment(thread)
            return SimpleNamespace(thread_id=thread,assignment_id=a.id,execution_id=a.execution_id,
                execution_workspace_id=a.execution_workspace_id)
        # Only native/Profile/routing read edges are controlled. Authority,
        # assignment/store, governed input, checkpoint and Evidence are real.
        self.routing=SimpleNamespace(snapshot=lambda _:SimpleNamespace(
            owner=lambda v:v,coordination_owners=('supervisor',)),
            existing_binding=lambda _,role:SimpleNamespace(thread_id=self.binding_threads[role]),
            bootstraps=SimpleNamespace(get_by_thread=bootstrap),
            recovery_recipient=lambda _,binding,role,**kw:(bootstrap(binding.thread_id,f.actor),assignment(binding.thread_id)),
            executor=lambda _,binding,role,**kw:(SimpleNamespace(profile_id=assignment(binding.thread_id).agent_profile.profile_id),f.actor))
        async def runner(thread):
            a=assignment(thread);busy=thread=='recipient' and self.target_busy
            return {'runner':{'assignmentId':a.id,'fence':a.fence,'live':True,'processRunning':True,
                'pendingExecutorWork':0},'activity':{'state':'active' if busy else 'idle',
                'turnId':'controlled-busy' if busy else None}}
        self.threads=SimpleNamespace(runner=runner)
        self.service=ProcedureAutomationService(runs=self.runs,engine=f.runner,identity=f.identity,
            routing=self.routing,threads=self.threads,continuity=SimpleNamespace(thread_is_active=lambda _:False),
            get_work=self.get_work,events=self.bus.store,event_triggers=self.triggers,controller=self.controller,
            coordinator=self.coordinator,automation_execution=SimpleNamespace())
        payload=automation_payload(f.pin.model_dump(mode='json'),advances=advances)
        if event_filter is not None:payload['trigger']['event_filter']=event_filter
        self.draft=self.definitions.create_draft('fixture-automation',AutomationDefinition.model_validate(payload),
            actor_id=f.actor.identity_id,scope_type=DefinitionScope.PROJECT,scope_id=f.project.id)
        self.published=self.definitions.publish_draft(self.draft.record_id,actor_id=f.actor.identity_id)
        self.admission=AutomationTriggerAdmissionBridge(self.runs,self.triggers,self.bus)
        self.admission.procedure_handler=self.service.handle_match
        self.unsubscribe=self.admission.install();self.addCleanup(self.unsubscribe)
        return f

    def save_work(self):
        self.env.sqlite.record_apply('fixture_work_items',upserts={self.work.ref:self.work.model_dump(mode='json')})

    def get_work(self,ref):
        return WorkItemState.model_validate(self.env.sqlite.record_get('fixture_work_items',ref))

    async def event(self,key):
        delivery=await self.events.ingest(event_type='work.transition',source='autonomy-watchdog:fixture',
            idempotency_key=key,payload={'project_id':'home','work_item_ref':'fixture!1'},
            tenant_id=self.env.actor.organization_id,workspace_id=self.env.actor.workspace_id)
        return delivery.event

    async def join(self):
        import asyncio
        # Join the original coordinator tasks; no elapsed-time success inference.
        while self.coordinator._tasks:
            await asyncio.gather(*tuple(self.coordinator._tasks.values()),return_exceptions=True)

    def rows(self):return self.runs.store.list(organization_id=self.env.actor.organization_id,
        workspace_id=self.env.actor.workspace_id,automation_id='fixture-automation')

    async def test_actual_event_admission_checkpoint_and_evidence_complete_without_model(self):
        f=await self.build()
        await self.event('initial');await self.join()
        rows=self.rows();self.assertEqual(len(rows),1)
        run=rows[0];self.assertEqual(run.status,'succeeded',run)
        self.assertEqual(run.procedure_custody.checkpoint_id,self.initial_checkpoint)
        self.assertEqual(run.procedure_custody.advances_started,2)
        self.assertTrue(run.evidence_ids)
        self.assertEqual(f.workers.store.assignment(f.assignment.id).status,'running')
        self.assertEqual(self.owner.calls,1)
        f.runner.models.invoke.assert_not_awaited();self.assertEqual(f.provider.calls,[])
        self.assertEqual(f.sqlite.get('automation_runs')['schema_version'],'1.5')

    async def test_duplicate_windows_and_unchanged_waiting_do_not_advance(self):
        await self.build('mutation');self.target_busy=True
        event=await self.event('first');await self.join()
        first=self.rows()[0]
        await self.service.handle_event(event);await self.event('second-window');await self.join()
        current=self.rows()[0]
        self.assertEqual(len(self.rows()),1)
        self.assertEqual(current.procedure_custody.advances_started,first.procedure_custody.advances_started)
        self.assertEqual(self.owner.effects(),0)
        self.assertEqual(self.owner.calls,1)

    async def test_material_remediation_joins_same_original_and_makes_one_effect(self):
        await self.build('mutation');self.target_busy=True
        await self.event('first');await self.join();first=self.rows()[0]
        self.owner.ready=True;self.target_busy=False
        await self.event('remediated');await self.join();settled=self.rows()[0]
        self.assertEqual(settled.id,first.id)
        self.assertEqual(settled.procedure_custody.checkpoint_id,first.procedure_custody.checkpoint_id)
        self.assertEqual(settled.status,'succeeded',settled)
        self.assertEqual(self.owner.effects(),1)
        self.assertEqual(self.owner.calls,2)  # one mutation observation plus verification read
        await self.event('later-window');await self.join()
        self.assertEqual(self.owner.effects(),1)

    async def test_interrupted_effect_is_reconciled_without_replay_on_startup(self):
        await self.build('mutation');self.target_busy=True
        await self.event('first');await self.join()
        self.owner.ready=True;self.owner.interrupt=True;self.target_busy=False
        await self.event('actual-change');await self.join()
        self.assertEqual(self.owner.effects(),1)
        before=self.rows()[0]
        self.coordinator=KeyedTaskCoordinator(max_concurrency=1)
        self.addAsyncCleanup(self.coordinator.stop);self.service.coordinator=self.coordinator
        proof=await self.service.recover_pending();self.assertTrue(proof['complete']);await self.join()
        after=self.rows()[0]
        self.assertEqual(after.id,before.id)
        self.assertEqual(after.status,'running')
        self.assertEqual(self.owner.effects(),1)
        self.assertEqual(self.env.workers.store.assignment(self.env.assignment.id).procedure_checkpoint.status,'reconciliation_required')

    async def test_archived_handoff_is_not_recreated_or_replayed(self):
        await self.build('mutation');self.target_busy=True
        await self.event('first');await self.join();before=self.rows()[0]
        self.work.handoff_history.append(self.work.handoff.model_copy(update={'status':'superseded','reason_code':'handoff_delivery_expired'}))
        self.work.handoff=None;self.save_work()
        await self.event('archived');await self.join();after=self.rows()[0]
        self.assertEqual(len(self.rows()),1)
        self.assertEqual(after.id,before.id)
        self.assertEqual(after.procedure_custody,before.procedure_custody)
        self.assertEqual(self.owner.effects(),0)
        self.assertIsNone(self.get_work('fixture!1').handoff)

    async def test_counter_exhaustion_retains_original_and_never_resets(self):
        await self.build(advances=1)
        await self.event('first');await self.join();before=self.rows()[0]
        self.assertEqual(before.status,'running')
        self.assertEqual(before.result_code,'procedure_automation_advance_budget_exhausted')
        await self.event('later');await self.join();after=self.rows()[0]
        self.assertEqual(after.procedure_custody.advances_started,1)
        self.assertEqual(after.id,before.id);self.assertEqual(self.owner.calls,1)

    async def test_current_pause_prevents_effect_without_dropping_admission(self):
        await self.build()
        self.controller.pause(actor_id=self.env.actor.identity_id)
        await self.event('first');await self.join();before=self.rows()[0]
        self.assertEqual(before.status,'admitted')
        self.assertIsNone(before.procedure_custody)
        self.assertEqual(self.owner.calls,0)
        self.controller.resume(actor_id=self.env.actor.identity_id)
        await self.event('resume');await self.join()
        self.assertEqual(self.rows()[0].status,'succeeded')

    async def test_missing_supervisor_scope_and_foreign_work_fail_before_admission(self):
        await self.build()
        original=self.work.model_copy(deep=True)
        for change in ({'resource_ids':['unassigned-resource']},{'project_id':'foreign-project'}):
            self.work=original.model_copy(update=change);self.save_work()
            await self.event(str(change));await self.join()
            self.assertEqual(self.rows(),[])
            self.assertEqual(self.owner.calls,0)

    async def test_expected_fence_guard_prevents_start_and_advance(self):
        from codex_web.services.procedure_execution import ProcedureExecutionError
        f=await self.build()
        with self.assertRaises(ProcedureExecutionError):
            f.runner.start(f.assignment.id,definition=f.pin,input_snapshot_id=f.snapshot.id,
                actor=f.actor,expected_fence=f.assignment.fence+1)
        with self.assertRaises(ProcedureExecutionError):
            await f.runner.advance(f.assignment.id,actor=f.actor,expected_fence=f.assignment.fence+1)
        self.assertEqual(self.owner.calls,0)

    async def test_stop_refuses_new_schedule_and_keeps_durable_admission(self):
        await self.build();await self.coordinator.stop()
        await self.event('stopping')
        self.assertEqual(self.rows()[0].status,'admitted')
        self.assertIsNone(self.rows()[0].procedure_custody)
        self.assertEqual(self.owner.calls,0)

    async def test_autonomy_event_is_handled_before_any_native_reasoner(self):
        from unittest.mock import AsyncMock
        from codex_web.services.autonomy import AutonomyService
        f=await self.build()
        runtime=SimpleNamespace(project_scope=lambda _: (f.actor.organization_id,f.actor.workspace_id),
            dispatch_event=AsyncMock())
        service=AutonomyService(runtime=runtime,controller=self.controller,canonical_events=self.events)
        service.procedure_event_handler=self.service.handle_event
        result=await service._bounded_reasoning_dispatch(SimpleNamespace(thread_id='supervisor'),
            'not sent to a model','fixture',cycle_key='fixture-cycle',
            payload={'project_id':'home','work_item_ref':'fixture!1'})
        await self.join()
        self.assertEqual(result['reason'],'canonical_procedure_owned')
        self.assertEqual(self.rows()[0].status,'succeeded')
        runtime.dispatch_event.assert_not_awaited()
        f.runner.models.invoke.assert_not_awaited()

    async def watchdog_work_event(self, key, **payload_changes):
        from unittest.mock import AsyncMock
        from codex_web.services.autonomy import AutonomyService
        f=self.env
        runtime=SimpleNamespace(project_scope=lambda _: (f.actor.organization_id,f.actor.workspace_id),
            dispatch_event=AsyncMock(return_value={'ok':True}))
        service=AutonomyService(runtime=runtime,controller=self.controller,canonical_events=self.events)
        service.procedure_event_handler=self.service.handle_event
        # Exact pending-handoff SLA producer shape: legacy ref, not work_item_ref.
        payload={'project_id':self.work.project_id,'ref':self.work.ref,'agent':'recipient',
            'stage':self.work.current_stage,'handoff_status':'pending',**payload_changes}
        result=await service._bounded_reasoning_dispatch(SimpleNamespace(thread_id='supervisor'),
            'ordinary owner guidance','work-item-sla',cycle_key=key,payload=payload,work_item=self.work)
        await self.join()
        return result,runtime

    async def test_sla_legacy_ref_reaches_exact_pending_work_procedure_without_native_fallback(self):
        f=await self.build(event_filter={'project_id':'home','work_item_ref':'fixture!1','handoff_status':'pending'})
        result,runtime=await self.watchdog_work_event('actual-handoff-sla')
        self.assertEqual(result.get('reason'),'canonical_procedure_owned')
        run=self.rows()[0]
        event=self.bus.store.event(run.trigger.event_id)
        self.assertEqual(event.payload['ref'],self.work.ref)
        self.assertEqual(event.payload['work_item_ref'],self.work.ref)
        self.assertEqual(event.payload['handoff_status'],'pending')
        self.assertEqual(event.payload['resource_ids'],[f.resource.id])
        self.assertEqual(run.work_item_ref,self.work.ref)
        self.assertEqual(run.procedure_custody.checkpoint_id,self.initial_checkpoint)
        self.assertEqual(run.status,'succeeded')
        self.assertEqual(self.owner.calls,1)
        runtime.dispatch_event.assert_not_awaited()
        f.runner.models.invoke.assert_not_awaited()

    async def test_missing_handoff_does_not_match_pending_policy_or_swallow_normal_owner_event(self):
        await self.build(event_filter={'project_id':'home','work_item_ref':'fixture!1','handoff_status':'pending'})
        self.work.handoff=None;self.save_work()
        observed=[]
        self.bus.subscribe(lambda event:observed.append(event))
        result,runtime=await self.watchdog_work_event('ordinary-owner-no-handoff')
        self.assertTrue(result['ok'])
        self.assertEqual(observed[-1].payload['handoff_status'],'none')
        self.assertEqual(observed[-1].payload['work_item_ref'],self.work.ref)
        self.assertEqual(self.rows(),[])
        self.assertEqual(self.owner.calls,0)
        runtime.dispatch_event.assert_awaited_once()
        self.assertEqual(runtime.dispatch_event.await_args.kwargs['work_item_ref'],self.work.ref)

    async def test_canonical_accepted_status_and_work_override_caller_matching_fields(self):
        await self.build(event_filter={'project_id':'home','work_item_ref':'fixture!1','handoff_status':'pending'})
        self.work.handoff.status='accepted';self.save_work()
        observed=[]
        self.bus.subscribe(lambda event:observed.append(event))
        result,runtime=await self.watchdog_work_event('accepted-owner',work_item_ref='foreign!2')
        self.assertTrue(result['ok'])
        self.assertEqual(observed[-1].payload['handoff_status'],'accepted')
        self.assertEqual(observed[-1].payload['work_item_ref'],self.work.ref)
        self.assertEqual(observed[-1].payload['ref'],self.work.ref)
        self.assertEqual(self.rows(),[])
        runtime.dispatch_event.assert_awaited_once()

    async def test_run_reservation_cas_refuses_stale_counter_before_an_effect(self):
        from codex_web.services.procedure_automation import ProcedureAutomationError
        await self.build('mutation');self.target_busy=True
        await self.event('initial');await self.join()
        run=self.rows()[0];before=run.procedure_custody
        reserved=before.model_copy(update={'advances_started':before.advances_started+1})
        self.service._cas_custody(run,before,reserved)
        with self.assertRaises(ProcedureAutomationError):self.service._cas_custody(run,before,reserved)
        self.assertEqual(self.rows()[0].procedure_custody.advances_started,reserved.advances_started)
        self.assertEqual(self.owner.effects(),0)

    async def test_settlement_checks_original_full_custody_and_governed_proof(self):
        from codex_web.services.procedure_automation import ProcedureAutomationError
        f=await self.build(advances=1)
        await self.event('initial');await self.join();run=self.rows()[0]
        self.assertEqual(f.workers.store.assignment(f.assignment.id).status,'running')
        # A live/idle supervisor is not completed procedure evidence.
        self.assertEqual(self.service.reconcile(run.id,actor=f.actor).status,'running')
        row=f.workers.store.assignment(f.assignment.id)
        def drift(state):
            state.assignments=[a.model_copy(update={'execution_workspace_id':'foreign-workspace'})
                if a.id==row.id else a for a in state.assignments]
            return state
        f.workers.store.update(drift)
        with self.assertRaises(ProcedureAutomationError):self.service.reconcile(run.id,actor=f.actor)
        self.assertEqual(self.rows()[0].status,'running')

    async def test_actual_expiry_and_recipient_successor_keep_original_pin_and_checkpoint(self):
        from codex_web.execution_workers import ExecutionAssignmentCreate
        f=await self.build('mutation',claim_target=True);self.target_busy=True
        await self.event('busy');await self.join();before=self.rows()[0]
        original=f.workers.store.assignment(self.target.id)
        def expire(state):
            state.assignments=[a.model_copy(update={'lease':a.lease.model_copy(update={'expires_at':time.time()-1})})
                if a.id==original.id else a for a in state.assignments]
            return state
        f.workers.store.update(expire)
        def recover_and_prepare():
            self.assertEqual(f.workers.recover_expired(actor=f.actor,assignment_id=original.id,
                expected_fence=original.fence),[original.id])
            retired=f.workers.store.assignment(original.id)
            self.assertEqual(retired.fence,original.fence);self.assertEqual(retired.status,'lost')
            # Controlled recipient-preparation edge creates separate canonical
            # custody. It does not edit the original procedure's governed input.
            self.target=f.workers.create_assignment(ExecutionAssignmentCreate(
                subject={'kind':'thread','ref':'recipient-successor'},
                execution_id='recipient-successor',project_id=f.project.id,resource_ids=(f.resource.id,),
                required_capabilities=('command_execution',),execution_contract_version='1.0',
                sandbox='workspace-write',agent_profile=original.agent_profile),actor=f.actor)
        self.owner.before_effect=recover_and_prepare
        self.owner.ready=True;self.target_busy=False
        await self.event('real-expired-classification');await self.join();after=self.rows()[0]
        self.assertEqual(after.status,'succeeded',after)
        self.assertEqual(after.id,before.id)
        self.assertEqual(after.procedure_custody.checkpoint_id,before.procedure_custody.checkpoint_id)
        self.assertEqual(after.procedure_custody.input_snapshot_id,before.procedure_custody.input_snapshot_id)
        values=self.service._original_values(after,f.actor)
        self.assertEqual((values['target_assignment_id'],values['expected_target_fence']),
            (original.id,original.fence))
        self.assertNotEqual(self.target.id,original.id)
        self.assertEqual(self.owner.effects(),1)
