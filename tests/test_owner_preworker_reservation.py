"""Controller refusal before worker start is durable evidence, never a reason-string grant."""
import unittest
from types import SimpleNamespace
from tests import test_execution_workers as worker_fixtures
from tests import test_thread_bootstrap_routing as thread_fixtures
from codex_web.execution_subjects import ExecutionSubject
from codex_web.services.threads import ThreadBootstrapPrestartRefusal

class PreworkerReservationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.f = worker_fixtures.ExecutionWorkerServiceTests(); self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.base = thread_fixtures.ThreadBootstrapCreateTests()
        self.host, self.planner, self.manager, self.bindings, self.threads = self.base._service()
        self.threads.control_actor = self.f.admin
        self.planner.workers = self.f.service
        prepare = self.planner.prepare_bootstrap
        def actual_prepare(**kwargs):
            planned = prepare(**kwargs)
            self.assignment = self.f._assignment(work_item_ref=None,
                subject=ExecutionSubject(kind='thread_bootstrap',ref=kwargs['bootstrap_id']),
                execution_id=kwargs['execution_id'],project_id='p1')
            return SimpleNamespace(**{**vars(planned),'assignment_id':self.assignment.id})
        self.planner.prepare_bootstrap = actual_prepare

    async def test_actual_thread_guard_refusal_records_preprovider_failure_without_start(self):
        calls = 0
        def guard():
            nonlocal calls
            calls += 1
            if calls == 2:raise TimeoutError('Post-turn readiness operation deadline exceeded')
        with self.assertRaises(ThreadBootstrapPrestartRefusal):
            await self.threads.create(project_id='p1',bootstrap_token='a'*32,admission_guard=guard)
        current = self.f.service.store.assignment(self.assignment.id)
        self.assertEqual((current.status,current.fence,current.assigned_worker_id,current.started_at),('cancelled',1,None,None))
        self.assertEqual(self.manager.started,[])
        self.assertEqual(self.manager.session.requests,[])
        self.assertIsNotNone(current.failure,'pre-worker refusal lost authoritative phase/error evidence')
        self.assertEqual(current.failure.details['phase'],'before_worker_start')
        self.assertIs(current.failure.details['provider_start_called'],False)
        self.assertEqual(current.failure.details['error_type'],'TimeoutError')

    async def test_external_cancel_reason_cannot_create_or_retrofit_proof(self):
        from codex_web.execution_workers import AssignmentCancelRequest
        from codex_web.thread_bootstrap import BootstrapPreworkerRefusal
        a=self.f._assignment(work_item_ref=None,subject=ExecutionSubject(kind='thread_bootstrap',ref='bootstrap-legacy'),execution_id='legacy')
        result=self.f.service.cancel_bootstrap(a.id,AssignmentCancelRequest(expected_fence=0,
            reason='governor admission changed before isolated worker startup'),actor=self.f.admin)
        self.assertIsNone(result.failure)
        repeated=self.f.service.cancel_bootstrap(a.id,AssignmentCancelRequest(expected_fence=0,reason='same'),actor=self.f.admin,
            preworker_refusal=BootstrapPreworkerRefusal('bootstrap-legacy','legacy','TimeoutError',True))
        self.assertIsNone(repeated.failure)

    async def test_claimed_worker_cannot_receive_preworker_proof(self):
        from codex_web.execution_workers import AssignmentCancelRequest,AssignmentClaimRequest
        from codex_web.thread_bootstrap import BootstrapPreworkerRefusal
        from codex_web.services.execution_workers import WorkerConflictError
        a=self.f._assignment(work_item_ref=None,subject=ExecutionSubject(kind='thread_bootstrap',ref='bootstrap-claimed'),execution_id='claimed')
        claimed=self.f.service.claim(self.f.worker.id,AssignmentClaimRequest(),actor=self.f.worker_actor,assignment_id=a.id)
        with self.assertRaisesRegex(WorkerConflictError,'unclaimed'):
            self.f.service.cancel_bootstrap(a.id,AssignmentCancelRequest(expected_fence=claimed.fence,reason='cannot grant'),actor=self.f.admin,
                preworker_refusal=BootstrapPreworkerRefusal('bootstrap-claimed','claimed','TimeoutError',True))
        self.assertEqual(self.f.service.store.assignment(a.id),claimed)

    async def test_actual_owned_guard_deadline_records_exact_type_and_joins_before_cancel(self):
        import time
        from unittest.mock import patch
        from codex_web.services.post_turn_readiness import PostTurnReadinessService
        run=PostTurnReadinessService._run_owned;calls=0;settled=[]
        async def limited(operation,*,seconds,name):return await run(operation,seconds=min(seconds,.01),name=name)
        def guard():
            nonlocal calls
            calls+=1
            if calls==2:time.sleep(.04);settled.append(True)
        with patch.object(PostTurnReadinessService,'_run_owned',staticmethod(limited)):
            with self.assertRaises(ThreadBootstrapPrestartRefusal):
                await self.threads.create(project_id='p1',bootstrap_token='b'*32,admission_guard=guard)
        failure=self.f.service.store.assignment(self.assignment.id).failure
        self.assertEqual(settled,[True]);self.assertEqual(self.manager.started,[])
        self.assertEqual(failure.reason_code,'execution_timeout')
        self.assertTrue(failure.details['guard_deadline_exceeded'])
        self.assertEqual(failure.details['error_type'],'TimeoutError')

    async def test_http_504_is_typed_timeout_without_falsely_claiming_owned_deadline(self):
        from fastapi import HTTPException
        calls=0
        def guard():
            nonlocal calls
            calls+=1
            if calls==2:raise HTTPException(status_code=504,detail='private transport message never retained')
        with self.assertRaises(ThreadBootstrapPrestartRefusal):
            await self.threads.create(project_id='p1',bootstrap_token='c'*32,admission_guard=guard)
        failure=self.f.service.store.assignment(self.assignment.id).failure
        self.assertEqual(failure.reason_code,'execution_timeout')
        self.assertFalse(failure.details['guard_deadline_exceeded'])
        self.assertEqual(failure.details['error_http_status'],504)
        self.assertNotIn('private transport',failure.model_dump_json())


class RecipientRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import asyncio
        from tests import test_owner_native_session_recipient as native_fixtures
        from codex_web.services.execution_workers import ExecutionWorkerService
        from codex_web.storage.execution_workers import ExecutionWorkerStore
        from codex_web.execution_workers import ExecutionWorkerRegister,WorkerCapability
        from codex_web.identity import AuthenticationActor,PrincipalKind,AuthenticationAssurance
        self.n=native_fixtures.NativeRecipientScopeTests();self.n.setUp();self.addCleanup(self.n.doCleanups);self.n.prepare_fixture()
        self.actor=self.n.fixture.f.actor
        self.workers=ExecutionWorkerService(ExecutionWorkerStore(self.n.fixture.f.env.env.state))
        self.worker=self.workers.register(ExecutionWorkerRegister(service_identity_id='private-worker',pool='local',version='1',capabilities=(WorkerCapability.GIT,),max_concurrency=1),actor=self.actor)
        self.worker_actor=AuthenticationActor(identity_id='private-worker',principal_kind=PrincipalKind.SERVICE,
            organization_id='org-a',workspace_id='workspace-a',assurance=AuthenticationAssurance.SERVICE_TOKEN,service_scopes=('execution-worker:run',))
        self.n.routing.assignments=lambda key:self.workers.store.assignment(key)or self.n.fixture.assignments.get(key)
        old_worker=self.n.routing.workers
        self.n.routing.workers=lambda key:self.workers.store.worker(key)or old_worker(key)
        self.n.routing.recipient_preparer.existing_assignment=lambda **kw:next((a for a in self.workers.list_assignments(self.actor)if a.execution_id==kw['execution_id']),None)
        self.n.threads.create.side_effect=self.create
        self.failures=1;self.unknown=False;self.original=None;self.failure_generations=[]

    async def create(self,**kwargs):
        from codex_web.execution_workers import ExecutionAssignmentCreate,WorkerResourceLimits,WorkerCapability,AssignmentCancelRequest,AssignmentClaimRequest,AssignmentStartRequest
        from codex_web.thread_bootstrap import BootstrapPreworkerRefusal
        kwargs['admission_guard']()
        token=kwargs['bootstrap_token'];profile=self.n.fixture.f.profile
        a=self.workers.create_assignment(ExecutionAssignmentCreate(subject=ExecutionSubject(kind='thread_bootstrap',ref='bootstrap-'+token),
            execution_id='thread-bootstrap-'+token,project_id='project-a',resource_ids=('platform',),execution_contract_version='1.0',required_capabilities=(WorkerCapability.GIT,),
            sandbox=self.n.binding.sandbox,approval_policy=self.n.binding.approval_policy,
            limits=WorkerResourceLimits(cpu_seconds=10,memory_bytes=128*1024*1024,disk_bytes=64*1024*1024,process_count=4,wall_seconds=60),
            repository_scope=self.n.template.repository_scope,execution_profile_id=profile.execution_profile_id,
            agent_profile=self.n.template.agent_profile),actor=self.actor)
        if self.unknown:raise TimeoutError('provider outcome unavailable')
        if self.failures:
            self.failures-=1
            result=self.workers.cancel_bootstrap(a.id,AssignmentCancelRequest(expected_fence=0,reason='governor admission changed before isolated worker startup'),actor=self.actor,
                preworker_refusal=BootstrapPreworkerRefusal('bootstrap-'+token,a.execution_id,'TimeoutError',True,kwargs['recipient_origin']))
            self.failure_generations.append(result.failure.details['generation'])
            if self.original is None:self.original=result
            raise ThreadBootstrapPrestartRefusal(status_code=503,detail={'code':'bootstrap_prestart_failed'})
        claimed=self.workers.claim(self.worker.id,AssignmentClaimRequest(),actor=self.worker_actor,assignment_id=a.id)
        live=self.workers.start(self.worker.id,a.id,AssignmentStartRequest(lease_token=claimed.lease.lease_token,fence=claimed.fence),actor=self.worker_actor)
        self.n.bound=SimpleNamespace(id='fresh-binding',bootstrap_id='bootstrap-'+token,thread_id='platform-thread',assignment_id=live.id,
            execution_id=live.execution_id,execution_workspace_id=live.execution_workspace_id,created_by=live.created_by,
            organization_id='org-a',workspace_id='workspace-a')
        self.n.fixture.bootstraps['platform-thread']=self.n.bound
        self.n.fixture.assignments['assignment-platform-thread']=live
        return {'thread':{'id':'platform-thread'}}

    async def refuse_once(self):
        with self.assertRaises(ThreadBootstrapPrestartRefusal):await self.n.prepare()
        return self.original.model_dump(mode='json')

    async def test_known_preworker_refusal_recovers_same_handoff_once_and_preserves_original(self):
        before=await self.refuse_once()
        await self.n.fixture.continuity.dispatch_structured_handoff(self.n.state,source='work-item-handoff')
        pin=self.n.fixture.fixture.dispatch.call_args.kwargs['owner_routing_pin'];self.n.fixture.validate(pin)
        self.assertEqual(pin.recipient.thread_id,'platform-thread')
        self.assertEqual(self.n.threads.create.await_count,2)
        self.assertEqual(self.workers.store.assignment(self.original.id).model_dump(mode='json'),before)
        self.assertEqual(self.n.state.handoff.requested_at,1)
        self.assertIsNone(self.n.state.handoff.acknowledged_at)
        await self.n.prepare();self.assertEqual(self.n.threads.create.await_count,2)

    async def test_concurrent_recovery_reserves_one_new_physical_bootstrap(self):
        import asyncio
        await self.refuse_once()
        pins=await asyncio.gather(self.n.prepare(),self.n.prepare())
        self.assertEqual([p.recipient.thread_id for p in pins],['platform-thread','platform-thread'])
        self.assertEqual(self.n.threads.create.await_count,2)

    async def test_second_preworker_failure_is_held_without_third_generation(self):
        from codex_web.owner_routing import OwnerRoutingError
        self.failures=2;await self.refuse_once()
        with self.assertRaises(ThreadBootstrapPrestartRefusal):await self.n.prepare()
        with self.assertRaisesRegex(OwnerRoutingError,'recovery_pending'):await self.n.prepare()
        self.assertEqual(self.failure_generations,[0,1]);self.assertEqual(self.n.threads.create.await_count,2)

    async def test_unknown_recovery_never_creates_again(self):
        from codex_web.owner_routing import OwnerRoutingError
        await self.refuse_once();self.unknown=True
        with self.assertRaises(TimeoutError):await self.n.prepare()
        with self.assertRaisesRegex(OwnerRoutingError,'recovery_pending'):await self.n.prepare()
        self.assertEqual(self.n.threads.create.await_count,2)

    async def test_legacy_reason_string_and_changed_attribution_do_not_grant_recovery(self):
        from codex_web.owner_routing import OwnerRoutingError
        await self.refuse_once()
        self.workers.store.update(lambda state:state.model_copy(update={'assignments':[a.model_copy(update={'failure':None})if a.id==self.original.id else a for a in state.assignments]}))
        with self.assertRaisesRegex(OwnerRoutingError,'bootstrap_pending'):await self.n.prepare()
        self.assertEqual(self.n.threads.create.await_count,1)

    async def test_changed_source_fence_refuses_recovery(self):
        from codex_web.owner_routing import OwnerRoutingError
        await self.refuse_once();self.n.fixture.assignments['assignment-existing-thread'].fence+=1
        with self.assertRaisesRegex(OwnerRoutingError,'bootstrap_pending'):await self.n.prepare()
        self.assertEqual(self.n.threads.create.await_count,1)

    async def test_predecessor_changed_at_actual_recovery_edge_refuses_new_creation(self):
        from codex_web.owner_routing import OwnerRoutingError
        await self.refuse_once()
        async def changed(**kwargs):
            self.workers.store.update(lambda state:state.model_copy(update={'assignments':[a.model_copy(update={'fence':a.fence+1})if a.id==self.original.id else a for a in state.assignments]}))
            kwargs['admission_guard']()
            self.fail('changed predecessor reached provider creation')
        self.n.threads.create.side_effect=changed
        with self.assertRaisesRegex(OwnerRoutingError,'predecessor_changed'):await self.n.prepare()
        self.assertEqual(len(self.workers.list_assignments(self.actor)),1)

    async def test_expired_handoff_never_recovers_original_reservation(self):
        from codex_web.owner_routing import OwnerRoutingError
        await self.refuse_once()
        current=self.n.state.model_copy(deep=True);current.handoff=None
        self.n.fixture.f.env.states.put(current.ref,current)
        with self.assertRaisesRegex(OwnerRoutingError,'preparation_changed'):await self.n.prepare()
        self.assertEqual(self.n.threads.create.await_count,1)

    async def test_provider_edges_recheck_exact_authority_without_rescanning_candidates(self):
        from unittest.mock import Mock
        await self.refuse_once()
        scan=Mock(wraps=self.n.routing.bootstraps.list);self.n.routing.bootstraps.list=scan
        create=self.create
        async def edges(**kwargs):
            before=scan.call_count
            kwargs['admission_guard']();kwargs['admission_guard']()
            self.assertEqual(scan.call_count,before,'provider edge rescanned unrelated candidate history')
            return await create(**kwargs)
        self.n.threads.create.side_effect=edges
        pin=await self.n.prepare();self.n.fixture.validate(pin)
        self.assertGreaterEqual(scan.call_count,3,'initial/owned/final discovery must remain')

    async def test_new_compatible_racer_is_refused_by_final_unique_selection(self):
        from codex_web.owner_routing import OwnerRoutingError
        self.failures=0;create=self.create
        async def raced(**kwargs):
            result=await create(**kwargs)
            other=self.n.template.model_copy(deep=True,update={'id':'assignment-racer'})
            self.n.fixture.assignments[other.id]=other
            self.n.fixture.bootstraps['racer']=SimpleNamespace(id='racer',thread_id='racer',assignment_id=other.id,
                execution_id=other.execution_id,execution_workspace_id=other.execution_workspace_id,
                organization_id='org-a',workspace_id='workspace-a',created_by=other.created_by)
            return result
        self.n.threads.create.side_effect=raced
        with self.assertRaisesRegex(OwnerRoutingError,'ambiguous'):await self.n.prepare()
        self.n.fixture.fixture.dispatch.assert_not_awaited()
        self.assertEqual(self.n.threads.create.await_count,1)

    async def test_profile_revision_changed_at_recovery_edge_cannot_borrow_old_proof(self):
        from codex_web.agent_profiles import AgentProfileUpdate
        from codex_web.owner_routing import OwnerRoutingError
        await self.refuse_once()
        async def changed(**kwargs):
            self.n.fixture.f.profiles.update(self.n.fixture.f.profile.profile_id,
                AgentProfileUpdate(name='Fresh canonical profile revision',reason='Private stale-profile boundary test'),actor=self.actor)
            kwargs['admission_guard']()
            self.fail('stale Profile reached provider creation')
        self.n.threads.create.side_effect=changed
        with self.assertRaisesRegex(OwnerRoutingError,'profile_changed'):await self.n.prepare()
        self.assertEqual(len(self.workers.list_assignments(self.actor)),1)

    async def test_nonce_changed_after_reservation_is_freshly_refused(self):
        from codex_web.owner_routing import OwnerRoutingError
        await self.refuse_once()
        async def changed(**kwargs):
            current=self.n.state.model_copy(deep=True);current.handoff.requested_at=2
            self.n.fixture.f.env.states.put(current.ref,current)
            kwargs['admission_guard']()
            self.fail('changed nonce reached provider creation')
        self.n.threads.create.side_effect=changed
        with self.assertRaisesRegex(OwnerRoutingError,'preparation_changed'):await self.n.prepare()
        self.assertEqual(len(self.workers.list_assignments(self.actor)),1)

    async def test_actor_changed_at_recovery_edge_is_not_replaced_by_control_actor(self):
        from codex_web.owner_routing import OwnerRoutingError
        await self.refuse_once()
        async def changed(**kwargs):
            self.n.fixture.assignments['assignment-existing-thread'].created_by='foreign-actor'
            kwargs['admission_guard']()
            self.fail('changed actor reached provider creation')
        self.n.threads.create.side_effect=changed
        with self.assertRaises(OwnerRoutingError):await self.n.prepare()
        self.assertEqual(len(self.workers.list_assignments(self.actor)),1)

    async def test_failure_annotations_cannot_mint_another_recovery_generation(self):
        from codex_web.owner_routing import OwnerRoutingError
        await self.refuse_once();self.unknown=True
        with self.assertRaises(TimeoutError):await self.n.prepare()
        # Failure observability is not a new logical/physical invocation.
        self.workers.store.update(lambda state:state.model_copy(update={'assignments':[
            a.model_copy(update={'failure':a.failure.model_copy(update={'last_occurred_at':a.failure.last_occurred_at+1})})
            if a.id==self.original.id else a for a in state.assignments]}))
        with self.assertRaisesRegex(OwnerRoutingError,'recovery_pending'):await self.n.prepare()
        self.assertEqual(self.n.threads.create.await_count,2)

class RecipientGuardReadCostTests(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_provider_edges_do_not_repeat_global_candidate_discovery(self):
        from tests import test_owner_native_session_recipient as native_fixtures
        from unittest.mock import Mock
        n=native_fixtures.NativeRecipientScopeTests();n.setUp();self.addCleanup(n.doCleanups);n.prepare_fixture()
        scan=Mock(wraps=n.routing.bootstraps.list);n.routing.bootstraps.list=scan
        async def provider(**kwargs):
            before=scan.call_count
            for _ in range(3):kwargs['admission_guard']()
            self.assertEqual(scan.call_count,before,'three fresh authority checks reloaded unrelated recipient history')
            return await n.create(**kwargs)
        n.threads.create.side_effect=provider
        pin=await n.prepare();n.fixture.validate(pin)
        self.assertEqual(pin.recipient.thread_id,'platform-thread')
        self.assertGreaterEqual(scan.call_count,3)
