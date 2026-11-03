"""Real private Work ACK, publisher and interpreter; controlled operation edge."""
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from codex_web.events import EventHub
from codex_web.models import WorkItemAckCreate
from codex_web.services.work_item_dependencies import WorkItemRuntimeDependencies
from codex_web.services.work_item_state import WorkItemStateMachine
from codex_web.services.work_items import WorkItemService
from tests import test_procedure_automation as fixtures


class ProcedureWorkContinuationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.b = fixtures.ProcedureAutomationIntegrationTests()
        self.b.addCleanup = self.addCleanup
        self.b.addAsyncCleanup = self.addAsyncCleanup
        await self.b.build('mutation', event_filter={'project_id':'home','handoff_status':'pending'})
        self.b.work.current_owner='carl'
        self.b.work.next_owner='quinn'
        self.b.work.implementation_owner='carl'
        self.b.work.validation_owner='quinn'
        self.b.work.current_stage='ready_for_validation'
        self.b.work.artifact_state='merge_request'
        self.b.work.handoff=self.b.work.handoff.model_copy(update={
            'from_agent':'carl','to_agent':'quinn','artifact_state':'merge_request'})
        self.b.binding_threads['quinn']='recipient'
        self.b.save_work()
        root=Path(self.b.env.temp.name)
        def save(values):
            self.b.env.sqlite.record_apply('fixture_work_items',upserts={
                ref:state.model_dump(mode='json') for ref,state in values.items()})
        dependencies=WorkItemRuntimeDependencies(data_dir=root,events_file=root/'work-events.jsonl',
            load_states=lambda:{self.b.work.ref:self.b.get_work(self.b.work.ref)},save_states=save,
            load_projects=lambda:[self.b.env.project],resource_ids_for_project=lambda *a,**k:[],
            leading_owner_cue_in_action=lambda _:None,default_validation_owner='quinn',
            default_release_owner='release manager',
            non_implementation_owners=frozenset({'quinn','release manager','orchestrator'}),
            get_state=self.b.get_work,save_state=lambda state:save({state.ref:state}))
        self.machine=WorkItemStateMachine(store=self.b.env.sqlite,dependencies=dependencies)
        self.hub=EventHub()
        self.published=[]
        async def publisher(event):
            await self.hub.publish(event)
            self.published.append((event['type'],event.get('ref')))
        wrap=getattr(self.b.service,'wrap_work_publisher',None)
        self.publisher=wrap(publisher) if wrap is not None else publisher
        self.continuity=SimpleNamespace(schedule_actionable_owner_dispatch=Mock(),
            schedule_actionable_owner_continuity_check=Mock())
        self.api=SimpleNamespace(task_source_writeback=SimpleNamespace(
            sync=AsyncMock(side_effect=lambda state:state)))
        # The registered controlled operation observes real canonical Work;
        # there is no fabricated provider/native START or QA result in this test.
        reconcile=self.b.owner.reconcile
        async def observe_ack(context,value):
            current=self.b.get_work(self.b.work.ref)
            self.b.owner.ready=current.handoff.status=='accepted'
            return await reconcile(context,value)
        self.b.owner.reconcile=observe_ack
        await self.event('initial','pending')
        await self.b.join()
        self.original=self.b.rows()[0].model_copy(deep=True)
        self.assertEqual(self.original.status,'running')
        self.assertEqual(self.checkpoint().status,'waiting')
        self.assertEqual(self.b.owner.effects(),0)

    def checkpoint(self):
        return self.b.env.workers.store.assignment(self.b.env.assignment.id).procedure_checkpoint

    async def event(self,key,status,*,tenant=None):
        return await self.b.events.ingest(event_type='work.transition',
            source='autonomy-watchdog:fixture',idempotency_key=key,
            payload={'project_id':'home','work_item_ref':self.b.work.ref,'handoff_status':status},
            tenant_id=tenant or self.b.env.actor.organization_id,
            workspace_id=self.b.env.actor.workspace_id)

    async def ack(self,nonce=10.0):
        return await WorkItemService._acknowledge_with(self.api,self.b.work.ref,
            WorkItemAckCreate(actor='quinn',expected_handoff_requested_at=nonce,
                artifact_state='merge_request'),continuity=self.continuity,
            recovery=SimpleNamespace(schedule=Mock()),structured_ack=self.machine._structured_ack,
            public_state=self.machine._work_item_state_public,
            split_brain_findings=self.machine._work_item_split_brain_findings,
            publish_event=self.publisher)

    def assert_identity(self):
        rows=self.b.rows()
        self.assertEqual(len(rows),1)
        current=rows[0]
        self.assertEqual((current.id,current.trigger,current.dedupe_key,current.definition_ref,
            current.started_at,current.procedure_custody.input_snapshot_id,
            current.procedure_custody.handoff_requested_at),
            (self.original.id,self.original.trigger,self.original.dedupe_key,self.original.definition_ref,
             self.original.started_at,self.original.procedure_custody.input_snapshot_id,10.0))
        self.b.env.runner.models.invoke.assert_not_awaited()
        self.assertEqual(self.b.env.provider.calls,[])

    async def test_real_ack_publication_resumes_same_run_after_initial_filter_stops_matching(self):
        result=await self.ack()
        self.assertTrue(result['ok'])
        self.assertEqual(self.b.get_work(self.b.work.ref).handoff.status,'accepted')
        self.assertEqual(self.published,[('work-item.ack',self.b.work.ref)])
        self.continuity.schedule_actionable_owner_dispatch.assert_called_once()
        await self.b.join()
        self.assertEqual(self.b.rows()[0].status,'succeeded',self.b.rows()[0].result_code)
        self.assertEqual(self.checkpoint().status,'completed')
        self.assertEqual(self.b.owner.effects(),1)
        self.assert_identity()

    async def test_accepted_watchdog_wakes_existing_run_without_admitting_or_claiming_owner_event(self):
        # Commit a normal ACK with the original publisher only, then deliver
        # the existing accepted-owner watchdog observation as restart-safe wake.
        publisher=self.publisher
        self.publisher=self.hub.publish
        await self.ack()
        self.publisher=publisher
        delivery=await self.event('accepted-owner','accepted')
        handled=await self.b.service.handle_event(delivery.event)
        self.assertIsNone(handled,'existing-run wake must not suppress accepted Quinn owner work')
        await self.b.join()
        self.assertEqual(self.b.rows()[0].status,'succeeded')
        self.assert_identity()

    async def test_duplicate_work_notifications_do_not_repeat_effect_or_create_run(self):
        await self.test_real_ack_publication_resumes_same_run_after_initial_filter_stops_matching()
        for _ in range(2):
            await self.publisher({'type':'work-item.ack','ref':self.b.work.ref})
        await self.event('duplicate-accepted-owner','accepted')
        await self.b.join()
        self.assertEqual(self.b.owner.effects(),1)
        self.assert_identity()

    async def test_observer_failure_does_not_turn_committed_ack_into_failure_or_block_owner_dispatch(self):
        self.b.service.get_work=Mock(side_effect=RuntimeError('controlled read unavailable'))
        result=await self.ack()
        self.assertTrue(result['ok'])
        self.assertEqual(self.b.get_work(self.b.work.ref).handoff.status,'accepted')
        self.continuity.schedule_actionable_owner_dispatch.assert_called_once()
        self.continuity.schedule_actionable_owner_continuity_check.assert_called_once()
        self.assertEqual(self.b.owner.effects(),0)

    async def test_other_nonce_cannot_resume_original(self):
        changed=self.b.get_work(self.b.work.ref)
        changed.handoff=changed.handoff.model_copy(update={'requested_at':20.0})
        self.b.work=changed
        self.b.save_work()
        await self.ack(20.0)
        await self.b.join()
        self.assertEqual(self.b.owner.effects(),0)
        self.assertEqual(self.b.rows()[0].id,self.original.id)

    async def test_foreign_watchdog_cannot_resume_matching_accepted_original(self):
        self.publisher=self.hub.publish
        await self.ack()
        delivery=await self.event('foreign-owner','accepted',tenant='foreign-tenant')
        await self.b.service.handle_event(delivery.event)
        await self.b.join()
        self.assertEqual(self.b.owner.effects(),0)
        self.assertEqual(self.b.rows()[0].id,self.original.id)

    async def test_pause_refuses_effect_then_normal_material_wake_rechecks_after_resume(self):
        self.b.controller.pause(actor_id=self.b.env.actor.identity_id)
        await self.ack()
        await self.b.join()
        self.assertEqual(self.b.owner.effects(),0)
        self.assertEqual(self.b.rows()[0].result_code,'procedure_automation_controls_blocked')
        self.b.controller.resume(actor_id=self.b.env.actor.identity_id)
        delivery=await self.event('accepted-after-normal-resume','accepted')
        await self.b.service.handle_event(delivery.event)
        await self.b.join()
        self.assertEqual(self.b.rows()[0].status,'succeeded')
        self.assert_identity()

    async def test_retired_original_custody_is_not_transferred_or_executed(self):
        from codex_web.execution_workers import AssignmentCompleteRequest
        current=self.b.env.workers.store.assignment(self.b.env.assignment.id)
        retired=self.b.env.workers.complete(self.b.env.worker.id,current.id,
            AssignmentCompleteRequest(lease_token=current.lease.lease_token,
                fence=current.fence,succeeded=True),actor=self.b.env.worker_actor)
        self.assertEqual(retired.status,'succeeded')
        self.assertIsNone(retired.lease)
        await self.ack()
        await self.b.join()
        current=self.b.rows()[0]
        self.assertEqual(current.id,self.original.id)
        self.assertEqual(current.procedure_custody,self.original.procedure_custody)
        self.assertNotEqual(current.status,'succeeded')
        self.assertEqual(self.b.owner.effects(),0)

    async def test_changed_published_definition_refuses_existing_owner_before_effect(self):
        from codex_web.definitions import DefinitionScope
        definition,reference=self.b.definitions.resolve('fixture-automation',
            organization_id=self.b.env.actor.organization_id,workspace_id=self.b.env.actor.workspace_id,
            project_id=self.b.env.project.id)
        draft=self.b.definitions.create_draft('fixture-automation',definition.model_copy(update={
            'description':'Private changed current publication'}),actor_id=self.b.env.actor.identity_id,
            scope_type=DefinitionScope.PROJECT,scope_id=self.b.env.project.id)
        self.b.definitions.publish_draft(draft.record_id,actor_id=self.b.env.actor.identity_id,
            expected_active_revision=reference.revision)
        await self.ack()
        await self.b.join()
        self.assertEqual(self.b.rows()[0].result_code,'procedure_automation_definition_changed')
        self.assertEqual(self.b.rows()[0].definition_ref,self.original.definition_ref)
        self.assertEqual(self.b.owner.effects(),0)

    async def test_material_wakes_do_not_replay_interrupted_unknown_effect(self):
        self.b.owner.interrupt=True
        await self.ack()
        await self.b.join()
        self.assertEqual(self.b.owner.effects(),1)
        self.b.owner.interrupt=False
        await self.publisher({'type':'work-item.progress','ref':self.b.work.ref})
        await self.b.join()
        self.assertEqual(self.b.owner.effects(),1)
        self.assertEqual(self.checkpoint().status,'reconciliation_required')
        self.assertEqual(self.b.rows()[0].id,self.original.id)
