"""Real canonical Work/SQLite + actual notification callbacks; no app/native IO."""
import hashlib
import importlib.util
import json
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException
from pydantic import ValidationError
from codex_web.models import WorkItemProgressUpdate, WorkItemHandoff, WorkItemState, TaskSourceIdentity
from codex_web.services.work_items import WorkItemService
from codex_web.services.work_item_continuity import WorkItemContinuityService
from codex_web.services.autonomy import AutonomyService
from codex_web.services.work_item_watchdog_candidates import WorkItemWatchdogCandidatePolicy
from codex_web.services.control_plane_broker import ControlPlaneBrokerService
from codex_web.storage.runtime_state import ModelMapRepository
from codex_web.storage.sqlite_state import SQLiteStateStore
from tests import test_work_item_progress_evidence as existing
from tests import test_work_continuity_recovery as continuity_fixture
from tests import test_autonomy_service as autonomy_fixture


def basis(state):
    # Baseline-compatible producer for OLD: the old DTO ignores the additive
    # request, so the real progress/continuity assertion fails behaviorally.
    if importlib.util.find_spec('codex_web.work_item_wait'):
        from codex_web.work_item_wait import wait_input_sha256
        return wait_input_sha256(state)
    values=state.model_dump(mode='json',exclude={'wait_checkpoint','notes','updated_at',
        'last_meaningful_update_at','last_owner_activity_at','last_gitlab_event_at','source_projection'})
    return hashlib.sha256(json.dumps(values,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()


class UnchangedWaitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.f=existing.ProgressEvidenceTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.machine=self.f.machine
        self.sqlite=SQLiteStateStore(self.f.fixture.root/'wait.sqlite')
        close=getattr(self.sqlite,"close",None)
        if close is not None: self.addCleanup(close)
        self.repo=ModelMapRepository(self.sqlite,namespace='work_item_states',
            legacy_path=self.f.fixture.root/'states.json',model=WorkItemState)
        initial=self.machine._work_item_state(self.f.ref).model_copy(update={"project_id":"project-a"})
        self.repo.put(self.f.ref,initial)
        self.machine.dependencies=replace(self.machine.dependencies,get_state=self.repo.get,
            load_states=self.repo.load,save_state=lambda s:self.repo.put(s.ref,s),update_state=self.repo.update)
        self.service=object.__new__(WorkItemService);self.service.state_machine=self.machine
        self.service.task_source_writeback=SimpleNamespace(sync=AsyncMock(side_effect=lambda s:s))
        self.callbacks=SimpleNamespace(schedule_actionable_owner_dispatch=Mock(),
            schedule_actionable_owner_continuity_check=Mock())
        self.publish=AsyncMock()

    def state(self): return self.repo.get(self.f.ref)

    def request(self, *, note='No new prerequisite supplied; retaining current blocker', **changes):
        values={'actor':'sally','note':note,'wait_checkpoint':{'schema_version':'1.0',
            'expected_input_sha256':basis(self.state())},**changes}
        return WorkItemProgressUpdate.model_validate(values)

    async def progress(self,payload):
        return await self.service._progress_with(self.f.ref,payload,continuity=self.callbacks,
            recovery=SimpleNamespace(schedule=Mock()),structured_progress=self.machine._structured_progress,
            public_state=self.machine._work_item_state_public,split_brain_findings=lambda s:[],
            publish_event=self.publish)

    async def test_unchanged_wait_note_preserves_receipt_without_self_redispatch(self):
        # Canonical action owner in this fixture is Carl; sender Sally cannot
        # declare Carl's wait. Establish Sally as both current/action owner.
        state=self.state();state.next_owner='sally';self.repo.put(state.ref,state)
        before=self.state()
        with patch('codex_web.services.work_item_state.time.time',return_value=200.0):
            result=await self.progress(self.request())
        self.assertTrue(result['canonical_committed'])
        self.callbacks.schedule_actionable_owner_dispatch.assert_not_called()
        self.callbacks.schedule_actionable_owner_continuity_check.assert_not_called()
        self.assertEqual(self.state().notes[-1],'No new prerequisite supplied; retaining current blocker')
        self.assertEqual(self.state().last_meaningful_update_at,before.last_meaningful_update_at)
        self.assertEqual(self.state().last_owner_activity_at,200.0)
        records=[json.loads(x) for x in self.f.fixture.host.WORK_ITEM_EVENTS_FILE.read_text().splitlines()]
        self.assertFalse(records[-1]['payload']['meaningful_change'])
        self.assertTrue(records[-1]['payload']['unchanged_wait_checkpoint'])

    async def wait(self):
        state=self.state();state.next_owner='sally';self.repo.put(state.ref,state)
        await self.progress(self.request())
        return self.state()

    async def test_material_note_only_update_still_wakes_and_clears_witness(self):
        await self.wait()
        await self.progress(WorkItemProgressUpdate(actor='sally',note='New concrete source artifact now available'))
        self.callbacks.schedule_actionable_owner_dispatch.assert_called_once()
        self.callbacks.schedule_actionable_owner_continuity_check.assert_called_once()
        self.assertIsNone(self.state().wait_checkpoint)

    async def test_repeated_receipt_notes_are_retained_without_new_material_clock(self):
        await self.wait();before=self.state().last_meaningful_update_at
        for number in range(3): await self.progress(self.request(note=f'Waiting receipt {number}'))
        self.assertEqual(self.state().last_meaningful_update_at,before)
        self.assertEqual(self.state().notes[-3:],['Waiting receipt 0','Waiting receipt 1','Waiting receipt 2'])
        self.callbacks.schedule_actionable_owner_dispatch.assert_not_called()

    async def test_stale_expected_input_refuses_atomically_before_audit(self):
        await self.wait();request=self.request();before_events=self.f.fixture.host.WORK_ITEM_EVENTS_FILE.read_bytes()
        original_update=self.machine.dependencies.update_state
        def changed(ref,updater):
            state=self.repo.get(ref);state.next_action='New exact prerequisite';self.repo.put(ref,state)
            return original_update(ref,updater)
        self.machine.dependencies=replace(self.machine.dependencies,update_state=changed)
        with self.assertRaises(HTTPException) as caught: await self.progress(request)
        self.assertEqual(caught.exception.detail['code'],'work_item_wait_checkpoint_changed')
        self.assertEqual(self.state().next_action,'New exact prerequisite')
        self.assertEqual(self.f.fixture.host.WORK_ITEM_EVENTS_FILE.read_bytes(),before_events)

    async def test_changed_lane_foreign_actor_and_pending_handoff_cannot_claim_wait(self):
        await self.wait()
        for values in ({'next_action':'Different action'},{'actor':'operator'},{'current_owner':'carl'}):
            before=self.state()
            with self.assertRaises(HTTPException): await self.progress(self.request(**values))
            self.assertEqual(self.state(),before)
        state=self.state();state.handoff=WorkItemHandoff(from_agent='sally',to_agent='carl',requested_at=3)
        self.repo.put(state.ref,state)
        with self.assertRaises(HTTPException): await self.progress(self.request())

    async def test_current_canonical_changes_expire_wait_without_remote_inference(self):
        from codex_web.work_item_wait import current_unchanged_wait
        original=await self.wait()
        changes=[{'blocker':'New blocker'},{'next_owner':'carl'},{'resource_ids':['new-resource']},
            {'next_action':'Changed access result'},{'artifact_state':'merge_request'},
            {'source_identity':TaskSourceIdentity(source_type='gitlab',source_instance='private',external_id='1',revision='new')},
            {'handoff':WorkItemHandoff(from_agent='sally',to_agent='carl',requested_at=123)},
            {'execution':original.execution.model_copy(update={'evidence_requirements':[],'writable_repository_resource_ids':('new',)})}]
        self.assertTrue(current_unchanged_wait(original))
        for change in changes:
            with self.subTest(change=tuple(change)):
                self.assertFalse(current_unchanged_wait(original.model_copy(update=change)))
        self.assertTrue(current_unchanged_wait(original.model_copy(update={'updated_at':999,'notes':['Audit only']})))

    async def test_deferred_continuity_rechecks_wait_and_material_change(self):
        await self.wait();deliveries=[]
        c=continuity_fixture.WorkItemContinuityTests()._service(states=self.repo.load(),events=[],deliveries=deliveries)
        c.get_state=self.repo.get
        await c.run_actionable_owner_continuity_check(self.f.ref,expected_owner='sally',expected_stage='failed_with_action_owner',source='work-item-progress-continuity')
        self.assertEqual(deliveries,[])
        state=self.state();state.next_action='Exact prerequisite changed';self.repo.put(state.ref,state)
        await c.run_actionable_owner_continuity_check(self.f.ref,expected_owner='sally',expected_stage='failed_with_action_owner',source='work-item-progress-continuity')
        self.assertEqual(len(deliveries),1)
        await c.stop()

    async def test_explicit_human_dispatch_is_not_suppressed(self):
        state=await self.wait();deliveries=[]
        c=continuity_fixture.WorkItemContinuityTests()._service(states=self.repo.load(),events=[],deliveries=deliveries)
        c.get_state=self.repo.get
        result=await c.dispatch_actionable_owner(state,source='operator-owner-steer',actor='operator')
        self.assertTrue(result['dispatched']);self.assertEqual(len(deliveries),1)
        await c.stop()

    async def test_background_wait_does_not_admit_model_and_changed_input_does(self):
        state=await self.wait();dispatch=AsyncMock(return_value={'ok':True})
        service=AutonomyService(runtime=SimpleNamespace(dispatch_event=dispatch))
        binding=SimpleNamespace(thread_id='sally-thread')
        result=await service._bounded_reasoning_dispatch(binding,'checkpoint','work-item-sla',
            cycle_key='same-work',payload={'project_id':state.project_id},work_item=state)
        self.assertEqual(result['reason'],'work_item_unchanged_wait');dispatch.assert_not_awaited()
        state.next_action='Actual changed canonical input'
        await service._bounded_reasoning_dispatch(binding,'changed','work-item-sla',
            cycle_key='same-work',payload={'project_id':state.project_id},work_item=state)
        dispatch.assert_awaited_once()

    async def test_orchestrator_unchanged_wait_suppression_preserves_split_brain_and_new_input(self):
        state=await self.wait();findings=[]
        policy=WorkItemWatchdogCandidatePolicy(load_states=self.repo.load,split_brain_findings=lambda s:findings,
            handoff_timeout_seconds=lambda:900,coerce_owner=lambda v:v,sla_threshold_seconds=lambda s:100)
        self.assertIsNone(policy.orchestrator_reason(state,now=10000))
        findings.append('Current owner drift')
        self.assertEqual(policy.orchestrator_reason(state,now=10000)[0],'split_brain')
        findings.clear();state.blocker='Actual new prerequisite result'
        self.assertEqual(policy.orchestrator_reason(state,now=10000)[0],'blocked_lane')

    async def test_descriptor_public_context_guidance_and_legacy_serialization(self):
        from codex_web.services.work_item_dispatch_prompt import WorkItemDispatchPromptPolicy
        descriptor=ControlPlaneBrokerService.public_operation(next(o for o in ControlPlaneBrokerService.operations() if o.id=='work_item.progress'))
        self.assertIn('wait_checkpoint',descriptor['request_body']['properties'])
        self.assertNotIn('wait_checkpoint',self.state().model_dump())
        state=await self.wait();public=self.machine._work_item_state_public(state)
        self.assertTrue(public['wait_checkpoint_context']['unchanged_wait_active'])
        self.assertFalse(public['wait_checkpoint_context']['remote_prerequisites_verified'])
        text=WorkItemDispatchPromptPolicy(coerce_owner=lambda v:v,coordination_channel='private').render(state)
        self.assertIn('expected_input_sha256',text)
        self.assertEqual(WorkItemState.model_validate(state.model_dump()).wait_checkpoint,state.wait_checkpoint)
        with self.assertRaises(ValidationError): self.request(wait_checkpoint={'schema_version':'1.0','expected_input_sha256':'a'*64,'force':True})


    async def test_owner_cycle_waiting_first_work_does_not_starve_runnable_second(self):
        waiting=await self.wait()
        runnable=waiting.model_copy(update={'ref':'same-owner-second','wait_checkpoint':None,
            'last_meaningful_update_at':waiting.last_meaningful_update_at+1})
        states={waiting.ref:waiting,runnable.ref:runnable}
        runtime=autonomy_fixture.AutonomyOwnerWorkTests._runtime(states)
        runtime.owner_queue_agents=('sally',)
        runtime.gitlab_token_for_project=lambda _:None
        await AutonomyService(runtime=runtime).run_owner_work_cycle()
        runtime.dispatch_event.assert_awaited_once()
        self.assertEqual(runtime.dispatch_event.await_args.kwargs['work_item_ref'],runnable.ref)
        self.assertEqual(runtime.dispatch_event.await_args.args[1],'dispatch:'+runnable.ref)
        runtime.gitlab_group_issues.assert_not_awaited()
        runtime.dispatch_event.reset_mock();states.pop(runnable.ref)
        await AutonomyService(runtime=runtime).run_owner_work_cycle()
        runtime.dispatch_event.assert_not_awaited()

    async def test_release_cycle_omits_only_exact_wait_and_wakes_changed_release_inputs(self):
        state=self.state();state.current_owner=state.next_owner='release manager'
        state.current_stage='validation_running';state.artifact_state='merged_main'
        self.repo.put(state.ref,self.machine._sync_work_item_status_label(state))
        await self.progress(self.request(actor='release manager'))
        waiting=self.state();states={waiting.ref:waiting}
        runtime=autonomy_fixture.AutonomyOwnerWorkTests._runtime(states)
        runtime.release_validation_sla_seconds=lambda:0
        service=AutonomyService(runtime=runtime)
        await service.run_release_gate_cycle()
        runtime.dispatch_event.assert_not_awaited()
        changed=waiting.model_copy(update={'next_action':'Genuine new release prerequisite result'})
        states[changed.ref]=changed
        await service.run_release_gate_cycle()
        runtime.dispatch_event.assert_awaited_once()
        self.assertIn(changed.ref,runtime.dispatch_event.await_args.args[1])


    async def test_orchestrator_recovery_wait_does_not_suppress_other_candidate(self):
        waiting=await self.wait()
        other=waiting.model_copy(update={'ref':'other-orchestration-candidate','wait_checkpoint':None})
        states={waiting.ref:waiting,other.ref:other}
        runtime=autonomy_fixture.AutonomyOwnerWorkTests._runtime(states)
        binding=SimpleNamespace(thread_id='orchestrator-thread')
        runtime.orchestrator_binding=lambda _:binding
        runtime.orchestrator_watchdog_candidates=lambda _:[('blocked_lane',other,500)]
        runtime.format_orchestrator_watchdog_prompt=lambda _,items:'dispatch:'+items[0][1].ref
        selected=[]
        def select(binding,items):
            selected.append([s.ref for s in items])
            return items[0] if items else None
        runtime.owner_lane_recovery=SimpleNamespace(owns=lambda b,s:True,select_current_work=select,
            candidate=lambda b,s:True,resolve_binding=lambda b:b,terminal=lambda b:None)
        await AutonomyService(runtime=runtime).run_orchestrator_cycle()
        self.assertEqual(selected,[[other.ref]])
        runtime.dispatch_event.assert_awaited_once()
        self.assertEqual(runtime.dispatch_event.await_args.kwargs['work_item_ref'],other.ref)
