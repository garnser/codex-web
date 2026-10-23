"""Same canonical Home recovery chain; only native response/control projection are fake."""
import hashlib
import time
import unittest
from types import SimpleNamespace as NS
from codex_web.models import ActiveThreadTurn
from codex_web.services.queued_native_start import QueuedNativeStartCapture
from codex_web.runtime.execution import ExecutionPrestartRefusal
from tests.test_home_restart_recovery import HomeRestartRecoveryTests


class RepeatedHomeRestartTests(HomeRestartRecoveryTests):
    def start_input(self, queued, turn_id):
        attempt, claim = self.claim(queued)
        capture = QueuedNativeStartCapture(self.preflight, attempt, claim, queued.execution_id,
            queued.thread_id,self.actor.identity_id).bind_assignment(self.assignment,
                worker_id=self.assignment.assigned_worker_id,fence=self.assignment.fence,validator=self.current)
        correlation = capture.prepare(generation='private-generation',rpc_request_id=turn_id,
            native_session_id=queued.thread_id)
        capture.validate_before_send(correlation)
        self.assertTrue(capture.observe(correlation,generation='private-generation',
            message={'id':turn_id,'result':{'turn':{'id':turn_id,'status':'inProgress'}}}))
        self.queue.update(queued.thread_id,lambda rows:[row for row in rows if row.id != queued.id])
        active = self.interrupted.model_copy(update={'turn_id':turn_id,'source':'queued:'+queued.source,
            'started_at':time.time(),'updated_at':time.time()})
        self.active_store.put(active.thread_id,active)
        return active

    async def test_second_and_third_restart_keep_all_originals_and_pending_human(self):
        human=self.execution.enqueue_turn(thread_id='operator-thread',project_id='home',message='New priority human request',source='web')
        original=human.model_dump(mode='json')
        first=self.restart.retain_startup(self.interrupted,project=self.host._project('home'),actor=self.actor)
        active=self.start_input(first,'recovery-native-one')
        second=self.restart.retain_startup(active,project=self.host._project('home'),actor=self.actor)
        active=self.start_input(second,'recovery-native-two')
        third=self.restart.retain_startup(active,project=self.host._project('home'),actor=self.actor)
        saved=self.records.get('operator-thread')
        self.assertEqual(len(saved.prior),2)
        self.assertEqual(saved.prior[0]['original'],self.interrupted.model_dump(mode='json'))
        self.assertEqual(saved.prior[1]['original']['turn_id'],'recovery-native-one')
        self.assertNotEqual(first.home_restart_input.recovery_id,third.home_restart_input.recovery_id)
        self.assertEqual(self.queue.get('operator-thread')[0].model_dump(mode='json'),original)
        attempt,claim=self.claim(third)
        with self.restart.input_context(third,attempt_id=attempt,claim_id=claim):
            self.service.home_operator_host.prepare_native_input_guard(project=self.host._project('home'),actor=self.actor,
                execution_id=self.assignment.execution_id,assignment_id=self.assignment.id,source='queued:restart-recovery:home')()
        self.assertEqual(self.effects,0)

    async def test_recovery_source_or_unknown_start_cannot_create_another_generation(self):
        first=self.restart.retain_startup(self.interrupted,project=self.host._project('home'),actor=self.actor)
        self.claim(first)  # committed possible input, no positive native START
        forged=self.interrupted.model_copy(update={'source':'queued:restart-recovery:home','turn_id':'unknown-native'})
        before=self.records.get('operator-thread')
        with self.assertRaises(ExecutionPrestartRefusal):
            self.restart.retain_startup(forged,project=self.host._project('home'),actor=self.actor)
        self.assertEqual(self.records.get('operator-thread'),before)
        self.assertEqual(len(self.queue.get('operator-thread')),1)
        self.assertEqual(self.effects,0)

    async def test_unknown_original_action_stays_held_across_recovery_generations(self):
        from codex_web.action_intents import ActionIntentStatus
        intent=self.create()
        def unknown(state):
            state.intents=[row.model_copy(update={'status':ActionIntentStatus.UNCERTAIN,'execution_started_at':time.time(),
                'attempt':1,'lease':None}) if row.id==intent.id else row for row in state.intents]
            return state
        self.intents.store.update(unknown,intent_ids=(intent.id,))
        original=self.intents.store.get(intent.id).model_dump(mode='json')
        first=self.restart.retain_startup(self.interrupted,project=self.host._project('home'),actor=self.actor)
        active=self.start_input(first,'recovery-native-one')
        second=self.restart.retain_startup(active,project=self.host._project('home'),actor=self.actor)
        self.assertIn(intent.id,second.message)
        self.assertEqual(self.intents.store.get(intent.id).model_dump(mode='json'),original)
        self.assertEqual(self.effects,0)

    async def test_same_native_turn_cannot_generate_more_recoveries_by_timestamp_change(self):
        first=self.restart.retain_startup(self.interrupted,project=self.host._project('home'),actor=self.actor)
        active=self.start_input(first,'recovery-native-one')
        self.restart.retain_startup(active,project=self.host._project('home'),actor=self.actor)
        with self.assertRaises(ExecutionPrestartRefusal):
            self.restart.retain_startup(active.model_copy(update={'updated_at':time.time()+1}),
                project=self.host._project('home'),actor=self.actor)
        self.assertEqual(len(self.records.get('operator-thread').prior),1)

    def event(self):
        self.actor=self.f.identity.actor_for_identity(self.actor.identity_id,scope=self.actor.tenant)
        from codex_web.services.automation_definitions import install_automation_definitions,AutomationEventTriggerService
        from codex_web.services.automation_runs import AutomationRunService,AutomationTriggerAdmissionBridge,CanonicalEventBus
        from codex_web.storage.automation_runs import AutomationRunStore
        from codex_web.storage.canonical_events import CanonicalEventStore
        from codex_web.services.home_operator_followup import HomeOperatorFollowup,SOURCE
        from codex_web.automation_runs import AutomationRunTrigger,HomeEventCustody,AutomationRunStatus
        from codex_web.compatibility import CanonicalEventEnvelope
        from codex_web.definitions import DefinitionDraftCreate,DefinitionPublishRequest
        definitions=install_automation_definitions(self.registry)
        self.runs=AutomationRunService(AutomationRunStore(self.f.sqlite),definitions)
        events=CanonicalEventStore(self.f.sqlite);triggers=AutomationEventTriggerService(self.registry,CanonicalEventBus(events))
        binding=self.projects.get('home',self.actor.tenant).home_operator
        self.projects.repository.update_home_operator(organization_id=self.actor.organization_id,workspace_id=self.actor.workspace_id,
            expected_revision=binding.revision,binding=binding.model_copy(update={'revision':binding.revision+1,
                'agent_profile_id':self.assignment.agent_profile.profile_id,'agent_profile_revision':self.assignment.agent_profile.profile_revision}))
        binding=self.projects.get('home',self.actor.tenant).home_operator
        selected=lambda actor:{'status':'configured','binding':self.projects.get('home',actor.tenant).home_operator.model_dump(),
            'thread':{'id':'operator-thread'}}
        controller=NS(store=NS(control=lambda:NS(mode='active',dry_run=False,simulation=False)),
            _scoped_pause_reason=lambda *a,**k:None,_exclusive_goal_scope_reason=lambda *a:None)
        self.followup=HomeOperatorFollowup(runs=self.runs,operator=NS(status=selected),identity=self.f.identity,
            events=events,turns=NS(execution=self.execution),controller=controller,authority=self.authority,
            event_triggers=triggers,workers=self.f.workers,bootstrap_bindings=self.bindings,sessions=self.sessions.store)
        self.restart.event_followup=self.followup;self.followup.restart_recovery=self.restart
        payload={'name':'Canonical Home restart event','lifecycle':'enabled',
            'trigger':{'type':'canonical_event','event_type':'ci.pipeline'},
            'target':{'kind':'home_operator','id':self.assignment.agent_profile.profile_id,
                'home_thread_id':'operator-thread','home_binding_revision':binding.revision,
                'agent_profile_revision':self.assignment.agent_profile.profile_revision},
            'instructions':'Inspect canonical completed work without replaying uncertain effects.',
            'owner_identity_id':self.actor.identity_id,'work_item_policy':'reuse_only',
            'budget':{'max_input_tokens':1000,'max_output_tokens':500,'max_duration_seconds':300},'retry':{'max_attempts':1}}
        draft=self.registry.create_draft(DefinitionDraftCreate(definition_id='restart-event',kind='automation',
            definition_schema_version='1.1',scope_type='workspace',scope_id=self.actor.workspace_id,
            payload=payload,actor=self.actor.identity_id))
        self.registry.publish(draft.record_id,DefinitionPublishRequest(actor=self.actor.identity_id))
        event=CanonicalEventEnvelope(event_id='actual-private-event',event_type='ci.pipeline',occurred_at=time.time(),
            source='private-provider',tenant_id=self.actor.organization_id,workspace_id=self.actor.workspace_id,payload={})
        events.record_if_new(event,idempotency_key=event.event_id)
        run=self.runs.admit('restart-event',AutomationRunTrigger(kind='canonical_event',source_id=event.event_id,event_id=event.event_id),
            organization_id=self.actor.organization_id,workspace_id=self.actor.workspace_id,project_id='home').run
        custody=HomeEventCustody(assignment_id=self.assignment.id,execution_id=self.assignment.execution_id,
            worker_id=self.assignment.assigned_worker_id,fence=self.assignment.fence,
            execution_workspace_id=self.assignment.execution_workspace_id)
        def start(state):
            state.runs=[row.model_copy(update={'status':AutomationRunStatus.RUNNING,'started_at':time.time(),'home_custody':custody,
                'execution_ids':('home-followup-'+run.id,)}) if row.id==run.id else row for row in state.runs];return state
        self.runs.store.update(start)
        run=self.runs.store.get(run.id,organization_id=self.actor.organization_id,workspace_id=self.actor.workspace_id)
        queued=self.execution.enqueue_turn(thread_id='operator-thread',project_id='home',source=SOURCE+run.id,
            message=self.followup.message(run,definitions.resolve('restart-event',organization_id=self.actor.organization_id,
                workspace_id=self.actor.workspace_id,project_id='home')[0]),execution_id='home-followup-'+run.id,
            execution_profile_id=self.assignment.execution_profile_id,agent_profile_id=self.assignment.agent_profile.profile_id,
            agent_profile_revision=self.assignment.agent_profile.profile_revision,agent_profile_actor_id=self.actor.identity_id)
        return run,self.start_input(queued,'event-native-one')

    async def test_active_event_restarts_twice_from_real_grant_and_positive_start(self):
        run,active=self.event()
        queued=self.restart.retain_startup(active,project=self.host._project('home'),actor=self.actor)
        recovered=self.start_input(queued,'event-recovery-native')
        again=self.restart.retain_startup(recovered,project=self.host._project('home'),actor=self.actor)
        self.assertEqual(self.records.get('operator-thread').evidence['home_restart']['event_origin']['active'],active.model_dump(mode='json'))
        attempt,claim=self.claim(again)
        with self.restart.input_context(again,attempt_id=attempt,claim_id=claim):
            final=self.service.home_operator_host.prepare_native_input_guard(project=self.host._project('home'),actor=self.actor,
                execution_id=self.assignment.execution_id,assignment_id=self.assignment.id,source='queued:restart-recovery:home')
        final()
        self.select('operator-thread',self.projects.get('home',self.actor.tenant).home_operator.revision)
        with self.assertRaises(Exception):final()
        self.assertEqual(self.effects,0)

    async def test_event_prefix_without_canonical_run_start_never_grants_recovery(self):
        active=self.interrupted.model_copy(update={'source':'queued:home-operator-automation:invented'})
        with self.assertRaises(ExecutionPrestartRefusal):
            self.restart.retain_startup(active,project=self.host._project('home'),actor=self.actor)
        self.assertIsNone(self.records.get('operator-thread'))

    async def test_restarted_event_terminal_requires_original_and_current_actual_usage(self):
        from codex_web.agent_runtime_usage import AgentRuntimeUsage
        from codex_web.storage.agent_runtime_usage import AgentRuntimeUsageStore
        from codex_web.services.automation_outcomes import AutomationOutcomeReconciliationService
        run,active=self.event()
        queued=self.restart.retain_startup(active,project=self.host._project('home'),actor=self.actor)
        recovered=self.start_input(queued,'event-recovery-native')
        usage=AgentRuntimeUsageStore(self.f.sqlite)
        self.followup.runtime_usage=usage
        self.followup.outcomes=AutomationOutcomeReconciliationService(self.runs,None,runtime_usage=usage)
        session=self.sessions.store.find_by_native_id('operator-thread',organization_id=self.actor.organization_id,
            workspace_id=self.actor.workspace_id,provider_id='openai',runtime_id='codex')
        def record(turn):
            return usage.upsert(AgentRuntimeUsage(id='runtime-usage-'+hashlib.sha256(f'{session.id}\n{turn}'.encode()).hexdigest()[:32],
                organization_id=self.actor.organization_id,workspace_id=self.actor.workspace_id,assignment_id=self.assignment.id,
                execution_id=self.assignment.execution_id,provider_id='openai',runtime_id='codex',runtime_type='native',
                provider_native_session_id='operator-thread',provider_native_turn_id=turn,input_tokens=10,output_tokens=5,
                terminal_outcome='succeeded',started_at=time.time()-2,completed_at=time.time()))
        record(recovered.turn_id)
        await self.restart.record_terminal(recovered,succeeded=True)
        current=self.runs.store.get(run.id,organization_id=self.actor.organization_id,workspace_id=self.actor.workspace_id)
        self.assertEqual(current.status,'running');self.assertIsNotNone(current.home_terminal)
        record(active.turn_id)
        await self.followup.recover_pending()
        current=self.runs.store.get(run.id,organization_id=self.actor.organization_id,workspace_id=self.actor.workspace_id)
        self.assertEqual(current.status,'succeeded');self.assertEqual(current.home_terminal.native_turn_id,recovered.turn_id)
        self.assertEqual(self.effects,0)


def load_tests(loader,tests,pattern):
    return unittest.TestSuite(RepeatedHomeRestartTests(name) for name in RepeatedHomeRestartTests.__dict__ if name.startswith('test_'))
