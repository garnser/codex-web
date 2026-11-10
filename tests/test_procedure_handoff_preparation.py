"""#1989 private Run/interpreter owners; final native preparation I/O controlled.

The existing routing.prepare_compatible_pin eligibility wrapper is real. Its
native bootstrap owner edge is controlled, not a claim of provider execution.
"""
import copy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
from types import MethodType, SimpleNamespace
import unittest
import uuid
from unittest.mock import AsyncMock

from codex_web.automation_definitions import AutomationDefinition
from codex_web.automation_runs import AutomationRunStatus
from codex_web.definitions import DefinitionScope
from codex_web.owner_routing import OwnerRoutingError
from codex_web.services.keyed_background_tasks import KeyedTaskCoordinator
from codex_web.services.owner_routing import OwnerRoutingService
from codex_web.storage.automation_runs import AutomationRunStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from tests import test_procedure_automation as bridge_fixture


class PendingHandoffPreparationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.b = bridge_fixture.ProcedureAutomationIntegrationTests()
        await self.b.build()
        for function,args,kwargs in self.b._cleanups:
            self.addCleanup(function,*args,**kwargs)
        self.b._cleanups.clear()
        self.prepared = False
        self.blocked = False
        self.refusal = 'owner_recipient_native_scope_incompatible'
        self.prepare_calls = 0
        self.after_prepare = None
        self.observed_runs = []
        self.existing_recipient = self.b.routing.recovery_recipient
        def missing(*args,**kwargs):
            if not self.prepared:
                raise OwnerRoutingError(self.refusal)
            return self.existing_recipient(*args,**kwargs)
        self.b.routing.recovery_recipient = missing
        def compatible(*args,**kwargs):
            if not self.prepared:
                raise OwnerRoutingError(self.refusal)
            return SimpleNamespace(recipient=SimpleNamespace(thread_id='recipient'))
        self.b.routing.compatible_pin = compatible
        self.b.routing.prepare_compatible_pin = MethodType(OwnerRoutingService.prepare_compatible_pin,self.b.routing)
        self.b.routing.recipient_preparer = SimpleNamespace(prepare=self.prepare)

    def effects(self):
        return (self.b.env.sqlite.record_get('fixture_native_preparation','exact-handoff') or {}).get('count',0)

    async def prepare(self,state,binding,owner,*,writable_repository_ids):
        self.prepare_calls += 1
        rows = self.b.rows()
        self.assertEqual(len(rows),1,'the canonical Run must exist BEFORE preparation')
        run = rows[0]
        self.assertEqual(run.status,'admitted')
        self.assertIsNone(run.procedure_custody)
        self.assertIsNotNone(run.started_at,'first preparation clock must precede its effect')
        self.assertEqual(run.work_item_ref,state.ref)
        self.assertEqual(state.handoff.requested_at,10.0)
        self.assertEqual(owner,state.handoff.to_agent)
        self.assertEqual(tuple(writable_repository_ids),tuple(state.resource_ids))
        self.observed_runs.append(run.model_copy(deep=True))
        if self.blocked:
            raise OwnerRoutingError('owner_recipient_bootstrap_pending')
        self.b.env.sqlite.record_update('fixture_native_preparation','exact-handoff',
            lambda old:{'count':(old or {}).get('count',0)+1},default={})
        self.prepared = True
        if self.after_prepare is not None:
            await self.after_prepare()
        return SimpleNamespace(recipient=SimpleNamespace(thread_id='recipient'))

    async def event(self,key):
        await self.b.event(key)
        await self.b.join()

    async def restart(self):
        await self.b.coordinator.stop()
        coordinator = KeyedTaskCoordinator(max_concurrency=1,per_scope_concurrency=1)
        self.addAsyncCleanup(coordinator.stop)
        service = copy.copy(self.b.service)
        service.coordinator = coordinator
        self.b.service,self.b.coordinator = service,coordinator
        self.b.admission.procedure_handler = service.handle_match
        await service.recover_pending()
        await self.b.join()

    async def test_missing_recipient_admits_before_preparation_and_completes_without_model(self):
        await self.event('missing-recipient')
        self.assertEqual(len(self.b.rows()),1,'matching pending handoff must retain a Run before preparation')
        run = self.b.rows()[0]
        self.assertEqual(run.status,'succeeded',run.result_code)
        self.assertEqual(self.prepare_calls,1)
        self.assertEqual(self.effects(),1)
        self.assertEqual(run.id,self.observed_runs[0].id)
        self.assertEqual(run.started_at,self.observed_runs[0].started_at)
        self.assertEqual(run.procedure_custody.handoff_requested_at,10.0)
        self.assertEqual(self.b.service._original_values(run,self.b.env.actor),
            {'work_item_ref':self.b.work.ref,'target_assignment_id':self.b.target.id,
             'expected_target_fence':self.b.target.fence,'expected_handoff_requested_at':10.0})
        self.assertTrue(run.evidence_ids)
        self.b.env.runner.models.invoke.assert_not_awaited()
        self.assertEqual(self.b.env.provider.calls,[])

    async def test_failed_before_worker_then_restart_reuses_run_nonce_and_first_clock(self):
        self.blocked = True
        await self.event('known-before-worker-refusal')
        before = self.b.rows()[0]
        self.assertEqual(before.status,'admitted')
        self.assertEqual(before.result_code,'owner_recipient_bootstrap_pending')
        self.assertEqual(self.effects(),0)
        self.blocked = False
        await self.restart()
        after = self.b.rows()[0]
        self.assertEqual(after.status,'succeeded',after.result_code)
        self.assertEqual((after.id,after.dedupe_key,after.trigger,after.started_at),
            (before.id,before.dedupe_key,before.trigger,before.started_at))
        self.assertEqual(self.effects(),1)
        await self.event('another-genuine-watchdog-window')
        self.assertEqual(len(self.b.rows()),1)
        self.assertEqual(self.effects(),1)

    async def test_unknown_and_ambiguous_originals_never_reach_preparer(self):
        for code in ('owner_routing_native_outcome_unresolved','owner_recipient_ambiguous',
                     'owner_executor_bootstrap_unverified'):
            self.refusal = code
            await self.event(code)
            self.assertEqual(len(self.b.rows()),1)
            self.assertEqual(self.b.rows()[0].result_code,code)
            self.assertEqual(self.prepare_calls,0)
            self.assertEqual(self.effects(),0)

    async def test_pause_and_cancel_before_or_after_preparation_prevent_interpreter_start(self):
        self.b.controller.pause(actor_id=self.b.env.actor.identity_id)
        await self.event('paused')
        self.assertEqual(self.prepare_calls,0)
        self.assertIsNone(self.b.rows()[0].procedure_custody)
        self.b.controller.resume(actor_id=self.b.env.actor.identity_id)
        async def cancel():
            run = self.b.rows()[0]
            self.b.run_store.replace(run.model_copy(update={'status':AutomationRunStatus.CANCELLED}))
        self.after_prepare = cancel
        await self.event('resume-and-cancel')
        run = self.b.rows()[0]
        self.assertEqual(run.status,'cancelled')
        self.assertIsNone(run.procedure_custody)
        self.assertEqual(self.b.owner.calls,0)
        self.assertEqual(self.effects(),1)

    async def test_current_scope_and_external_source_refuse_without_preparation(self):
        original = self.b.work.model_copy(deep=True)
        self.b.work.resource_ids = ['foreign-resource']
        self.b.save_work()
        await self.event('foreign-scope')
        self.assertEqual(self.b.rows(),[])
        self.b.work = original
        self.b.save_work()
        await self.b.events.ingest(event_type='work.transition',source='external:untrusted',
            idempotency_key='external',payload={'project_id':'home','work_item_ref':'fixture!1'},
            tenant_id=self.b.env.actor.organization_id,workspace_id=self.b.env.actor.workspace_id)
        await self.b.join()
        self.assertEqual(self.b.rows(),[])
        self.assertEqual(self.prepare_calls,0)

    async def test_supervisor_fence_drift_retains_original_run_without_new_suffix(self):
        self.blocked = True
        await self.event('first-refusal')
        before = self.b.rows()[0]
        def change(state):
            for row in state.assignments:
                if row.id == self.b.env.assignment.id:
                    row.fence += 1
                    row.lease = row.lease.model_copy(update={'fence':row.fence})
            return state
        self.b.env.workers.store.update(change)
        self.blocked = False
        await self.event('new-fence')
        self.assertEqual(len(self.b.rows()),1)
        self.assertEqual(self.b.rows()[0].id,before.id)
        self.assertEqual(self.b.rows()[0].dedupe_key,before.dedupe_key)
        self.assertEqual(self.b.rows()[0].result_code,'procedure_automation_preparation_custody_changed')
        self.assertEqual(self.effects(),0)
        self.assertEqual(self.prepare_calls,1)

    async def test_archive_preserves_original_no_recreation_on_restart(self):
        self.blocked = True
        await self.event('before-expiry')
        before = self.b.rows()[0]
        self.b.work.handoff_history.append(self.b.work.handoff.model_copy(update={
            'status':'superseded','reason_code':'handoff_delivery_expired'}))
        self.b.work.handoff = None
        self.b.save_work()
        self.blocked = False
        await self.restart()
        after = self.b.rows()[0]
        self.assertEqual((after.id,after.dedupe_key,after.started_at),
            (before.id,before.dedupe_key,before.started_at))
        self.assertEqual(self.effects(),0)
        self.assertIsNone(self.b.get_work(self.b.work.ref).handoff)

    async def test_current_definition_revision_change_never_mints_competing_run(self):
        self.blocked = True
        await self.event('old-definition')
        before = self.b.rows()[0]
        automation,reference = self.b.definitions.resolve('fixture-automation',
            organization_id=self.b.env.actor.organization_id,workspace_id=self.b.env.actor.workspace_id,
            project_id=self.b.env.project.id)
        draft = self.b.definitions.create_draft('fixture-automation',automation.model_copy(update={
            'description':'A real changed published Definition must not repin an original invocation.'}),
            actor_id=self.b.env.actor.identity_id,scope_type=DefinitionScope.PROJECT,scope_id=self.b.env.project.id)
        self.b.definitions.publish_draft(draft.record_id,actor_id=self.b.env.actor.identity_id,
            expected_active_revision=reference.revision)
        self.blocked = False
        await self.event('new-definition')
        self.assertEqual(len(self.b.rows()),1)
        self.assertEqual(self.b.rows()[0].definition_ref,before.definition_ref)
        self.assertEqual(self.b.rows()[0].result_code,'procedure_automation_definition_changed')
        self.assertEqual(self.effects(),0)

    async def test_required_approval_and_disabled_definition_never_prepare(self):
        automation,reference = self.b.definitions.resolve('fixture-automation',
            organization_id=self.b.env.actor.organization_id,workspace_id=self.b.env.actor.workspace_id,
            project_id=self.b.env.project.id)
        draft = self.b.definitions.create_draft('fixture-automation',automation.model_copy(update={'approval_required':True}),
            actor_id=self.b.env.actor.identity_id,scope_type=DefinitionScope.PROJECT,scope_id=self.b.env.project.id)
        self.b.definitions.publish_draft(draft.record_id,actor_id=self.b.env.actor.identity_id,
            expected_active_revision=reference.revision)
        self.b.service.automation_execution._wait_for_approval = AsyncMock()
        await self.event('requires-approval')
        self.b.service.automation_execution._wait_for_approval.assert_awaited_once()
        self.assertEqual(self.prepare_calls,0)
        current,reference = self.b.definitions.resolve('fixture-automation',
            organization_id=self.b.env.actor.organization_id,workspace_id=self.b.env.actor.workspace_id,
            project_id=self.b.env.project.id)
        payload = current.model_dump(mode='json');payload['lifecycle']='paused'
        draft = self.b.definitions.create_draft('fixture-automation',AutomationDefinition.model_validate(payload),
            actor_id=self.b.env.actor.identity_id,scope_type=DefinitionScope.PROJECT,scope_id=self.b.env.project.id)
        self.b.definitions.publish_draft(draft.record_id,actor_id=self.b.env.actor.identity_id,
            expected_active_revision=reference.revision)
        await self.event('disabled')
        self.assertEqual(len(self.b.rows()),1)
        self.assertEqual(self.prepare_calls,0)

    async def test_real_consumed_exact_approval_precedes_preparer(self):
        from codex_web.services.automation_execution import AutomationExecutionService
        from codex_web.approval_requests import ApprovalDecisionSubmit, ApprovalConsumeRequest
        from codex_web.identity import HumanIdentity, Membership, MembershipRole, PrincipalKind, AuthenticationAssurance
        automation,reference = self.b.definitions.resolve('fixture-automation',
            organization_id=self.b.env.actor.organization_id,workspace_id=self.b.env.actor.workspace_id,
            project_id=self.b.env.project.id)
        draft = self.b.definitions.create_draft('fixture-automation',automation.model_copy(update={'approval_required':True}),
            actor_id=self.b.env.actor.identity_id,scope_type=DefinitionScope.PROJECT,scope_id=self.b.env.project.id)
        self.b.definitions.publish_draft(draft.record_id,actor_id=self.b.env.actor.identity_id,
            expected_active_revision=reference.revision)
        execution = SimpleNamespace(approvals=self.b.env.approvals,runs=self.b.runs,
            _approval_target=AutomationExecutionService._approval_target)
        execution._wait_for_approval = MethodType(AutomationExecutionService._wait_for_approval,execution)
        self.b.service.automation_execution = execution
        await self.event('pending-canonical-approval')
        run = self.b.rows()[0]
        self.assertEqual(run.status,'waiting_for_approval')
        self.assertEqual(self.prepare_calls,0)
        def add_human(state):
            state.humans.append(HumanIdentity(id='handoff-approver',display_name='Private handoff approver'))
            return state
        identity = self.b.env.identity
        identity.store.update(add_human)
        identity.add_membership(Membership(identity_id='handoff-approver',principal_kind=PrincipalKind.HUMAN,
            organization_id=self.b.env.actor.organization_id,workspace_id=self.b.env.actor.workspace_id,
            roles=[MembershipRole.APPROVER]))
        credentials = identity.create_session(identity_id='handoff-approver',scope=self.b.env.actor.tenant,
            assurance=AuthenticationAssurance.MFA)
        approver = identity.authenticate_session(credentials.session_token,touch=False).actor
        await execution.approvals.decide(run.approval_request_id,
            ApprovalDecisionSubmit(outcome='approve',idempotency_key='private-handoff-review'),actor=approver)
        # Merely approved is deliberately insufficient for native preparation.
        self.b.runs.resume_after_approval(run.id,organization_id=run.organization_id,workspace_id=run.workspace_id)
        await self.event('approved-not-consumed')
        self.assertEqual(self.prepare_calls,0)
        await execution.approvals.consume(run.approval_request_id,ApprovalConsumeRequest(
            target=execution._approval_target(run),idempotency_key='private-handoff-consume',
            resulting_operation_reference='automation-run:'+run.id),actor=self.b.env.actor)
        async def verify_consumed():
            request = execution.approvals.get(run.approval_request_id,actor=self.b.env.actor)
            self.assertEqual(request.status,'consumed')
            self.assertEqual(request.target,execution._approval_target(run))
        self.after_prepare = verify_consumed
        await self.event('consumed-exact-approval')
        self.assertEqual(self.b.rows()[0].status,'succeeded',self.b.rows()[0].result_code)
        self.assertEqual(self.prepare_calls,1)
        self.assertEqual(self.effects(),1)

    async def test_nonce_drift_while_preparing_cannot_claim_or_start(self):
        async def drift():
            self.b.work.handoff.requested_at = 11.0
            self.b.save_work()
        self.after_prepare = drift
        await self.event('nonce-changes-during-preparation')
        run = self.b.rows()[0]
        self.assertEqual(run.status,'admitted')
        self.assertIsNone(run.procedure_custody)
        self.assertEqual(run.result_code,'procedure_automation_preparation_custody_changed')
        self.assertEqual(self.b.owner.calls,0)
        self.assertEqual(self.effects(),1)
        self.assertEqual(self.observed_runs[0].dedupe_key,run.dedupe_key)

    async def test_first_preparation_duration_is_not_reset_by_restart(self):
        self.blocked = True
        await self.event('first-bounded-preparation')
        before = self.b.rows()[0]
        self.assertIsNotNone(before.started_at)
        self.b.runs.clock = lambda:before.started_at+121
        self.blocked = False
        await self.restart()
        after = self.b.rows()[0]
        self.assertEqual(after.id,before.id)
        self.assertEqual(after.started_at,before.started_at)
        self.assertEqual(after.result_code,'procedure_automation_duration_exhausted')
        self.assertEqual(self.prepare_calls,1)
        self.assertEqual(self.effects(),0)

    async def test_atomic_prefix_race_and_ordinary_exact_key_scope(self):
        self.blocked = True
        await self.event('canonical-model')
        template = self.b.rows()[0]
        prefix = template.dedupe_key.rsplit(':',1)[0]+':'
        with tempfile.TemporaryDirectory() as directory:
            store = AutomationRunStore(SQLiteStateStore(Path(directory)/'race.sqlite'))
            first = template.model_copy(update={'id':'automation-run-'+uuid.uuid4().hex,'dedupe_key':prefix+'1'*64})
            second = template.model_copy(update={'id':'automation-run-'+uuid.uuid4().hex,'dedupe_key':prefix+'2'*64})
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda row:store.append(row,canonical_invocation_prefix=prefix),
                    (first,second)))
            self.assertEqual(results[0].id,results[1].id)
            self.assertEqual(len(store.load().runs),1)
            for index,changes in enumerate(({'organization_id':'foreign-org'},{'workspace_id':'foreign-workspace'},
                    {'project_id':'foreign-project'},{'automation_id':'foreign-automation'},
                    {'work_item_ref':'foreign!2'},{'target_id':'foreign-procedure'}),start=4):
                foreign = second.model_copy(update={'id':'automation-run-'+uuid.uuid4().hex,
                    'dedupe_key':prefix+format(index,'064x'),**changes})
                self.assertEqual(store.append(foreign,canonical_invocation_prefix=prefix).id,foreign.id)
            ordinary = second.model_copy(update={'id':'automation-run-'+uuid.uuid4().hex,'dedupe_key':prefix+'3'*64})
            self.assertEqual(store.append(ordinary).id,ordinary.id)
            # Legacy exact-key idempotency is tenant/workspace-wide, even
            # across Project/Work/target changes. Prefix scoping must not alter it.
            same_exact_key = results[0].model_copy(update={'id':'automation-run-'+uuid.uuid4().hex,
                'project_id':'legacy-other-project','work_item_ref':'legacy!9','target_id':'legacy-target'})
            self.assertEqual(store.append(same_exact_key).id,results[0].id)
            self.assertEqual(len(store.load().runs),8)
