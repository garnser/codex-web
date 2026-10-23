"""Actual canonical child start and owned terminal observer; no application import."""
import ast
import hashlib
import time
from pathlib import Path
from unittest.mock import AsyncMock
from codex_web.agent_runtime_usage import AgentRuntimeUsage
from codex_web.services.canonical_events import CanonicalEventBus, CanonicalEventIngestionService
from codex_web.services.home_child_terminal import HomeChildTerminalService
from codex_web.storage.agent_runtime_usage import AgentRuntimeUsageStore
from codex_web.storage.canonical_events import CanonicalEventStore
from tests.test_home_coordination_canonical_send import HomeCanonicalSendTests


class HomeChildTerminalTests(HomeCanonicalSendTests):
    def publish(self, payload):
        profile = next(row for row in payload['profiles'] if row['id'] == 'orchestration-only')
        profile['control_plane_operations'].append('operator.event.followup')
        return super().publish(payload)

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.bus = CanonicalEventBus(CanonicalEventStore(self.f.sqlite))
        self.received = []
        self.bus.subscribe(self.received.append)
        self.usage = AgentRuntimeUsageStore(self.f.sqlite)
        self.observer = HomeChildTerminalService(preflight=self.execution.queued_preflight,
            events=CanonicalEventIngestionService(self.bus), intents=self.intents.store,
            sessions=self.sessions.store, usage=self.usage)
        self.restart = AsyncMock()
        self.followup = AsyncMock()
        namespace = {'home_restart_recovery_service':type('Observer', (), {'record_terminal':self.restart})(),
                     'home_operator_followup':type('Observer', (), {'record_terminal':self.followup})(),
                     'home_child_terminal_service':self.observer}
        tree = ast.parse(Path('codex_web/application.py').read_text())
        function = next(node for node in tree.body if isinstance(node,ast.AsyncFunctionDef) and node.name=='_record_home_retained_terminal')
        exec(compile(ast.Module(body=[function],type_ignores=[]),'<actual application observer>','exec'),namespace)
        self.execution.retained_turn_outcome_recorder = namespace['_record_home_retained_terminal']

    async def start_child(self):
        self.original = await self.message()
        await self.execution.drain_thread_queue(self.thread)
        self.assertEqual(len(self.starts()),1)
        active = self.execution._active_turn(self.thread)
        self.assertIsNotNone(active)
        return active

    async def complete_child(self, active, method='turn/completed'):
        self.execution.record_thread_activity({'method':method,'params':{'threadId':self.thread,'turn':{'id':active.turn_id}}})
        task = self.execution.assignment_completion_tasks.get(self.target.id)
        self.assertIsNotNone(task)
        await task

    def final_usage(self, active, outcome='succeeded'):
        session = self.sessions.store.find_by_native_id(active.thread_id,organization_id=self.actor.organization_id,
            workspace_id=self.actor.workspace_id,provider_id='openai',runtime_id='codex')
        usage = AgentRuntimeUsage(id='runtime-usage-'+hashlib.sha256(f'{session.id}\n{active.turn_id}'.encode()).hexdigest()[:32],
            organization_id=self.actor.organization_id,workspace_id=self.actor.workspace_id,project_id=active.project_id,
            execution_id=active.execution_id,assignment_id=active.assignment_id,execution_workspace_id=active.execution_workspace_id,
            agent_session_id=session.id,provider_id='openai',runtime_id='codex',runtime_type='app-server',
            provider_native_session_id=active.thread_id,provider_native_turn_id=active.turn_id,
            input_tokens=17,output_tokens=9,runtime_duration_seconds=2.5,terminal_outcome=outcome,
            telemetry_completeness='exact',started_at=time.time()-2.5,completed_at=time.time())
        self.usage.upsert(usage)
        return usage

    async def test_owned_child_completion_publishes_canonical_event(self):
        active = await self.start_child()
        await self.complete_child(active)
        self.assertEqual(len(self.received),1)
        payload = self.received[0].payload
        self.assertEqual(payload['project_id'],'data')
        self.assertEqual(payload['parent_intent_id'],self.original['item']['id'])
        self.assertEqual(payload['outcome'],'unknown')
        self.assertFalse(payload['usage_available'])
        self.assertNotIn('input_tokens',payload)
        self.assertNotIn('message',payload)
        self.restart.assert_awaited_once()
        self.followup.assert_awaited_once()

    async def test_actual_usage_duplicate_and_reopened_bus_keep_one_original(self):
        active = await self.start_child()
        usage = self.final_usage(active)
        from unittest.mock import patch
        with patch.object(self.sessions.store,'find_by_native_id',side_effect=AssertionError('unbounded native alias lookup')):
            await self.complete_child(active)
        payload = self.received[0].payload
        self.assertEqual((payload['input_tokens'],payload['output_tokens'],payload['runtime_duration_seconds']),(17,9,2.5))
        self.assertEqual(payload['usage_id'],usage.id)
        self.assertEqual(payload['outcome'],'succeeded')
        await self.observer.record_terminal(active,succeeded=True)
        reopened = HomeChildTerminalService(preflight=self.execution.queued_preflight,
            events=CanonicalEventIngestionService(CanonicalEventBus(CanonicalEventStore(self.f.sqlite))),
            intents=self.intents.store,sessions=self.sessions.store,usage=self.usage)
        await reopened.record_terminal(active,succeeded=True)
        self.assertEqual(len(self.received),1)
        self.assertEqual(len(self.intents.history(self.original['item']['id'],self.actor)['receipts']),1)
        self.assertEqual(len(self.starts()),1)

    async def test_positive_child_start_can_finish_before_parent_provider_returns(self):
        from unittest.mock import patch
        active = await self.start_child()
        original = self.intents.store.get(self.original['item']['id'])
        with patch.object(self.intents.store,'get',return_value=original.model_copy(update={'status':'executing'})):
            await self.complete_child(active)
        self.assertEqual(len(self.received),1)
        self.assertEqual(self.received[0].payload['parent_intent_status'],'executing')
        self.assertEqual(self.received[0].payload['outcome'],'unknown')
        self.assertEqual(len(self.starts()),1)

    async def test_wrong_turn_fence_or_original_intent_refuses(self):
        active = await self.start_child()
        for changes in ({'turn_id':'different-turn'},{'fence':active.fence+1}):
            with self.assertRaises(ValueError):
                await self.observer.record_terminal(active.model_copy(update=changes),succeeded=True)
        from unittest.mock import patch
        original_intent = self.intents.store.get(self.original['item']['id'])
        for candidate in (None, original_intent.model_copy(update={'status':'uncertain'})):
            with patch.object(self.intents.store,'get',return_value=candidate):
                with self.assertRaises(ValueError):
                    await self.observer.record_terminal(active,succeeded=True)
        self.execution.queued_preflight.store.update(active.queued_admission_attempt_id,
            lambda row:row.model_copy(update={'status':'reconciliation_required'}))
        with self.assertRaises(ValueError):
            await self.observer.record_terminal(active,succeeded=True)
        self.assertEqual(self.received,[])

    async def test_unrelated_or_legacy_marker_never_claims_start(self):
        active = await self.start_child()
        for changes in ({'source':'web'},{'queued_admission_attempt_id':None}):
            await self.observer.record_terminal(active.model_copy(update=changes),succeeded=True)
        legacy = active.model_copy(update={'queued_admission_attempt_id':None}).model_dump()
        self.assertNotIn('queued_admission_attempt_id',legacy)
        self.assertIsNone(type(active).model_validate(legacy).queued_admission_attempt_id)
        self.assertEqual(self.received,[])

    async def test_interrupted_usage_never_becomes_success_and_no_text_leaks(self):
        active = await self.start_child()
        self.final_usage(active,'interrupted')
        await self.complete_child(active)
        self.assertEqual(self.received[0].payload['outcome'],'interrupted')
        self.assertNotIn('One bounded current Data review',str(self.received[0].model_dump()))

    async def test_observer_refusal_does_not_skip_existing_terminal_observers(self):
        active = await self.start_child()
        self.observer.record_terminal = AsyncMock(side_effect=ValueError('Synthetic observer refusal'))
        await self.complete_child(active)
        self.restart.assert_awaited_once()
        self.followup.assert_awaited_once()
        self.assertEqual(self.received,[])

    async def test_failed_terminal_never_certifies_success(self):
        active = await self.start_child()
        self.final_usage(active,'failed')
        await self.complete_child(active,'turn/failed')
        self.assertEqual(self.received[0].payload['outcome'],'failed')
        self.assertFalse(self.received[0].payload['completion_succeeded'])

    async def test_foreign_usage_refuses_instead_of_disclosing_or_certifying(self):
        active = await self.start_child()
        usage = self.final_usage(active)
        self.usage.upsert(usage.model_copy(update={'project_id':'foreign'}))
        with self.assertRaises(ValueError):
            await self.observer.record_terminal(active,succeeded=True)
        self.assertEqual(self.received,[])
        from unittest.mock import patch
        # Canonical telemetry's bounded ambiguity result never selects a different
        # session/usage record or leaks the mismatched record above.
        with patch.object(self.sessions.store,'find_unique_by_native_id',return_value=None):
            await self.observer.record_terminal(active,succeeded=True)
        self.assertEqual(self.received[0].payload['outcome'],'unknown')
        self.assertFalse(self.received[0].payload['usage_available'])
        self.assertNotIn('input_tokens',self.received[0].payload)

    async def test_cancelled_waiter_retains_terminal_owner_until_writer_joins(self):
        import asyncio
        active = await self.start_child()
        entered, release = asyncio.Event(), asyncio.Event()
        ingest = self.observer.events.ingest
        async def blocked(**kwargs):
            entered.set()
            await release.wait()
            return await ingest(**kwargs)
        self.observer.events.ingest = blocked
        self.execution.record_thread_activity({'method':'turn/completed','params':{
            'threadId':self.thread,'turn':{'id':active.turn_id}}})
        task = self.execution.assignment_completion_tasks[self.target.id]
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.assertIs(self.execution.assignment_completion_tasks[self.target.id],task)
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertNotIn(self.target.id,self.execution.assignment_completion_tasks)
        self.restart.assert_awaited_once()
        self.followup.assert_awaited_once()

    async def test_child_event_actual_automation_same_home_queue_native_and_usage(self):
        import time
        from types import SimpleNamespace as NS
        from codex_web.models import HomeOperatorUpdate
        from codex_web.definitions import DefinitionDraftCreate, DefinitionPublishRequest
        from codex_web.services.home_operator import HomeOperatorService
        from codex_web.services.home_operator_followup import HomeOperatorFollowup
        from codex_web.services.automation_definitions import install_automation_definitions, AutomationEventTriggerService
        from codex_web.services.automation_runs import AutomationRunService, AutomationTriggerAdmissionBridge
        from codex_web.services.automation_execution import AutomationExecutionService
        from codex_web.services.automation_outcomes import AutomationOutcomeReconciliationService
        from codex_web.storage.automation_runs import AutomationRunStore
        self.settings.remember('operator-thread',execution_profile_id='orchestration-only',sandbox='workspace-write')
        operator = HomeOperatorService(projects=self.projects,thread_scope=self.scope,thread_index=self.index,
            settings=self.settings,agent_profiles=self.profiles)
        selected = operator.configure(HomeOperatorUpdate(thread_id='operator-thread',expected_revision=1,
            agent_profile_id='home-operator'),self.actor)
        definitions = install_automation_definitions(self.registry)
        runs = AutomationRunService(AutomationRunStore(self.f.sqlite),definitions)
        triggers = AutomationEventTriggerService(self.registry,self.bus)
        control = NS(mode='active',dry_run=False,simulation=False,cooldown_seconds=0)
        async def process(event, observation, *, reasoner, **kwargs):
            await reasoner()
            return NS(reason='explicit private fixture admission')
        controller = NS(store=NS(control=lambda:control,recent_cycles=lambda limit:[]),process=process,
            _scoped_pause_reason=lambda *a,**k:None,_exclusive_goal_scope_reason=lambda *a:None)
        followup = HomeOperatorFollowup(runs=runs,operator=operator,identity=self.f.identity,
            events=self.bus.store,turns=self.turns,controller=controller,authority=self.authority,event_triggers=triggers,
            workers=self.f.workers,bootstrap_bindings=self.bindings,sessions=self.sessions.store,runtime_usage=self.usage,
            outcomes=AutomationOutcomeReconciliationService(runs,None,runtime_usage=self.usage))
        execution = AutomationExecutionService(runs,identity=self.f.identity,threads=None,turns=self.turns,teams=None,events=self.bus.store)
        execution.home_operator_followup = followup
        async def launch(run):
            return await execution.launch(run.id,organization_id=self.actor.organization_id,workspace_id=self.actor.workspace_id)
        followup.launch_handler = launch
        bridge = AutomationTriggerAdmissionBridge(runs,triggers,self.bus,launch_handler=launch)
        self.addCleanup(bridge.install())
        payload = dict(name='Private delegated child completion',lifecycle='enabled',
            trigger=dict(type='canonical_event',event_type='work.transition',event_filter={
                'project_id':'data','transition':'home_child_native_terminal'}),
            target=dict(kind='home_operator',id='home-operator',home_thread_id='operator-thread',
                home_binding_revision=selected['binding']['revision'],agent_profile_revision=1),
            instructions='Inspect the genuine completed child references once.',owner_identity_id=self.actor.identity_id,
            work_item_policy='reuse_only',budget=dict(max_input_tokens=2000,max_output_tokens=1000,max_duration_seconds=60),
            retry=dict(max_attempts=1))
        draft = self.registry.create_draft(DefinitionDraftCreate(definition_id='child-completion-followup',kind='automation',
            definition_schema_version='1.1',scope_type='workspace',scope_id=self.actor.workspace_id,
            payload=payload,actor=self.actor.identity_id))
        self.registry.publish(draft.record_id,DefinitionPublishRequest(actor=self.actor.identity_id))
        active = await self.start_child()
        self.final_usage(active)
        await self.complete_child(active)
        queued = self.queue_store.get('operator-thread')
        self.assertEqual(len(queued),1,[(row.status,row.result_code,row.result_reason) for row in runs.store.load().runs])
        self.assertEqual(queued[0].source.split(':')[0],'home-operator-automation')
        self.assertEqual(len(self.starts()),1)
        # Same existing Home bootstrap, real retained queue and canonical START;
        # only the external native wire/process and deterministic control seam are fake.
        previous_target,previous_thread = self.target,self.thread
        self.target,self.thread = self.assignment,'operator-thread'
        try:
            self.bindings.mark_initial_turn_started(self.thread,self.actor)
            await self.attach_native()
            self.service.home_operator_host.event_input_authorizer = followup.authorize_input
            self.execution.native_input_guard_factory = self.service.home_operator_host.prepare_native_input_guard
            await self.execution.drain_thread_queue(self.thread)
            self.assertEqual(len(self.starts()),1,(self.host.events,self.queue_store.get(self.thread)))
            home_active = self.execution._active_turn(self.thread)
            self.assertEqual(home_active.assignment_id,self.assignment.id)
            self.final_usage(home_active)
            self.followup.side_effect = followup.record_terminal
            await self.complete_child(home_active)
        finally:
            self.target,self.thread = previous_target,previous_thread
        done = runs.store.load().runs[0]
        self.assertEqual(done.status,'succeeded',done.result_reason)
        self.assertIn(home_active.turn_id,done.result_reason)
        self.assertEqual(self.queue_store.get('operator-thread'),[])
        self.assertEqual(self.current().status,'running')
        await self.observer.record_terminal(active,succeeded=True)
        self.assertEqual(len(runs.store.load().runs),1)
        self.assertEqual(self.queue_store.get('operator-thread'),[])


def load_tests(loader, tests, pattern):
    import unittest
    names = [name for name in HomeChildTerminalTests.__dict__ if name.startswith('test_')]
    return unittest.TestSuite(HomeChildTerminalTests(name) for name in names)
