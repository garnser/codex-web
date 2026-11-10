"""Private routing/preparation owners; provider and supervisor edges controlled."""
from __future__ import annotations
import time
import unittest
from types import SimpleNamespace
from codex_web.agent_profiles import AgentProfileLifecycle, AgentProfileLifecycleChange
from codex_web.execution_subjects import ExecutionSubject
from codex_web.execution_workers import AssignmentStatus
from codex_web.owner_routing import OwnerRoutingError
from codex_web.services.procedure_automation import ProcedureAutomationError, ProcedureAutomationService
from tests.test_procedure_automation import REF, automation_payload
from tests.test_procedure_control_plane import content
from tests import test_owner_native_session_recipient as native
from tests import test_accepted_prepared_owner_routing as accepted
from tests import test_owner_preworker_reservation as preworker


class ProcedureRecipientSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def prepared(self):
        n = native.NativeRecipientScopeTests(); n.setUp(); self.addCleanup(n.doCleanups)
        f = n.fixture
        # Platform57 anchor and original native custody agree. The genuine
        # Work/nonce instead requests Connector33 (fixture Resource 'platform').
        source = f.assignments['assignment-existing-thread']
        source.resource_ids = ('platform57-anchor',)
        source.repository_scope = source.repository_scope.model_copy(update={
            'writable_repository_ids': ('platform57-anchor',)})
        n.original.resource_ids = source.resource_ids
        n.original.repository_scope = source.repository_scope
        n.store.upsert(n.session.model_copy(update={'resource_ids': source.resource_ids}))
        n.prepare_fixture()
        create = n.create
        async def create_with_subject(**kwargs):
            result = await create(**kwargs)
            n.reserved.subject = ExecutionSubject(kind='thread_bootstrap', ref='bootstrap-'+kwargs['bootstrap_token'])
            return result
        n.threads.create.side_effect = create_with_subject
        self.pin = await n.prepare()  # Real preparer, exact nonce, replicated fence.
        self.created = n.threads.create.await_count
        self.binding_before = n.binding.model_dump(mode='json')
        self.source_before = source.model_dump(mode='json')
        return n

    def observe(self, n, state=None):
        state = state or n.state
        return n.routing.recovery_recipient(state, n.binding, state.handoff.to_agent,
            writable_repository_ids=('platform',), lookup_actor=lambda:n.fixture.f.actor)

    def bridge(self, n):
        """Actual bridge and recipient owner; unrelated supervisor edges controlled."""
        from codex_web.automation_definitions import AutomationDefinition
        routing, actor = n.routing, n.fixture.f.actor
        source = routing.assignments(routing.bootstraps.get_by_thread(n.binding.thread_id, actor).assignment_id)
        supervisor = source.model_copy(deep=True, update={'id':'private-supervisor',
            'execution_id':'private-supervisor-execution', 'execution_workspace_id':'private-supervisor-workspace',
            'resource_ids':('platform',)})
        sb = SimpleNamespace(thread_id='private-supervisor-thread')
        bootstrap = SimpleNamespace(assignment_id=supervisor.id, execution_id=supervisor.execution_id,
            execution_workspace_id=supervisor.execution_workspace_id)
        class RoutingView:
            def snapshot(self, state):
                s = routing.snapshot(state)
                return SimpleNamespace(owner=s.owner, coordination_owners=(*s.coordination_owners,'custody-supervisor'))
            def existing_binding(self, state, role):
                return sb if role=='custody-supervisor' else routing.existing_binding(state,role)
            def executor(self, state, binding, role, **kwargs):
                if role=='custody-supervisor':
                    return SimpleNamespace(profile_id=supervisor.agent_profile.profile_id),actor
                return routing.executor(state,binding,role,**kwargs)
            def __getattr__(self,key): return getattr(routing,key)
        view=RoutingView()
        view.bootstraps=SimpleNamespace(get_by_thread=lambda thread,who:bootstrap if thread==sb.thread_id
            else routing.bootstraps.get_by_thread(thread,who))
        self.observed_threads=[]
        async def runner(thread):
            self.observed_threads.append(thread)
            a=supervisor if thread==sb.thread_id else routing.assignments(routing.bootstraps.get_by_thread(thread,actor).assignment_id)
            return {'runner':{'assignmentId':a.id,'fence':a.fence,'live':True,'processRunning':True,'pendingExecutorWork':0},
                'activity':{'state':'idle','turnId':None}}
        skill={'name':'Private selection fixture','instructions':'No authority.',
            'owner_identity_id':actor.identity_id,'operational_procedure':content()}
        engine=SimpleNamespace(workers=SimpleNamespace(store=SimpleNamespace(assignment=lambda key:
            supervisor if key==supervisor.id else routing.assignments(key))),_live=lambda _:None,_targets=lambda *args:None,
            skills=SimpleNamespace(validate_required_procedure=lambda *args,**kw:None,
                definitions=SimpleNamespace(get_record=lambda _:SimpleNamespace(payload=skill))))
        self.service=ProcedureAutomationService(runs=SimpleNamespace(clock=time.time),engine=engine,identity=None,
            routing=view,threads=SimpleNamespace(runner=runner),continuity=SimpleNamespace(thread_is_active=lambda _:False),
            get_work=routing.get_state,events=None,event_triggers=None,controller=None,coordinator=None,automation_execution=None)
        p=automation_payload(REF);p['target']['supervisor_role']='custody-supervisor'
        return AutomationDefinition.model_validate(p),actor,SimpleNamespace(payload={
            'project_id':n.state.project_id,'work_item_ref':n.state.ref})

    async def test_actual_bridge_selects_nonce_recipient_not_platform_anchor(self):
        n=await self.prepared();args=self.bridge(n);refusal=None;selection=None
        try: selection=await self.service._select(*args)
        except (ProcedureAutomationError,OwnerRoutingError) as exc: refusal=str(exc)
        # OLD reaches its actual typed scope guard, never a missing-method error.
        self.assertIsNone(refusal,refusal)
        self.assertEqual(selection.values['target_assignment_id'],n.reserved.id)
        self.assertIn(n.bound.thread_id,self.observed_threads)
        self.assertNotIn(n.binding.thread_id,self.observed_threads)
        self.assertEqual(n.binding.model_dump(mode='json'),self.binding_before)
        self.assertEqual(n.fixture.assignments['assignment-existing-thread'].model_dump(mode='json'),self.source_before)
        self.assertEqual(n.threads.create.await_count,self.created)

    async def test_initial_expired_and_lost_metadata_do_not_grant_live_send(self):
        n=await self.prepared();old=n.reserved.model_copy(deep=True)
        for lost in (False,True):
            with self.subTest(lost=lost):
                target=old.model_copy(deep=True)
                if lost: target.status=AssignmentStatus.LOST;target.lease=None
                else: target.lease=target.lease.model_copy(update={'expires_at':time.time()-1})
                n.fixture.assignments[target.id]=target
                bound,observed=self.observe(n)
                self.assertEqual((bound.thread_id,observed.id,observed.fence),(n.bound.thread_id,target.id,old.fence))
                with self.assertRaises(OwnerRoutingError):n.routing._live_recipient(n.state,n.binding,bound,'orchestrator')
                args=self.bridge(n)
                selection=await self.service._select(*args)
                self.assertEqual(selection.values['target_assignment_id'],target.id)
        self.assertEqual(n.threads.create.await_count,self.created)

    async def test_resume_preserves_original_target_fence_nonce(self):
        n=await self.prepared();args=self.bridge(n);first=await self.service._select(*args)
        n.reserved.status=AssignmentStatus.LOST;n.reserved.lease=None
        self.assertEqual((await self.service._select(*args,original_values=first.values)).values,first.values)
        n.reserved.fence+=1
        with self.assertRaisesRegex(ProcedureAutomationError,'original_target_changed'):
            await self.service._select(*args,original_values=first.values)
        self.assertEqual(n.threads.create.await_count,self.created)

    async def test_scope_subject_tenant_profile_and_native_drift_refuse(self):
        n=await self.prepared();old=n.reserved.model_copy(deep=True)
        changes=({'project_id':'foreign'},{'created_by':'foreign'},{'execution_workspace_id':'foreign'},
            {'execution_id':'foreign'},{'resource_ids':('platform57-anchor',)},
            {'subject':ExecutionSubject(kind='thread_bootstrap',ref='bootstrap-foreign')},
            {'agent_profile':old.agent_profile.model_copy(update={'role_id':'foreign'})},
            {'repository_scope':old.repository_scope.model_copy(update={'read_only_repository_ids':('foreign',)})})
        for change in changes:
            with self.subTest(fields=tuple(change)):
                n.fixture.assignments[old.id]=old.model_copy(deep=True,update=change)
                with self.assertRaises(OwnerRoutingError):self.observe(n)
        n.fixture.assignments[old.id]=old
        n.store.upsert(n.session.model_copy(update={'id':'target-native',
            'provider_native_session_id':n.bound.thread_id,'assignment_id':n.original.id,'resource_ids':n.original.resource_ids}))
        with self.assertRaisesRegex(OwnerRoutingError,'native_scope_incompatible'):self.observe(n)
        self.assertEqual(n.threads.create.await_count,self.created)

    async def test_unknown_original_preserved_without_side_effect(self):
        n=await self.prepared();f=n.fixture;unknown=f.f.uncertain(item_id='retained-unknown')
        f.f.preflight.put(unknown)
        with self.assertRaisesRegex(OwnerRoutingError,'native_outcome_unresolved'):self.observe(n)
        self.assertEqual(f.f.preflight.get(unknown.id),unknown)
        self.assertEqual(n.threads.create.await_count,self.created)

    async def test_current_profile_revocation_refuses_metadata(self):
        n=await self.prepared();f=n.fixture.f
        f.profiles.lifecycle(f.profile.profile_id,AgentProfileLifecycle.DISABLED,
            AgentProfileLifecycleChange(reason='Private authority negative'),actor=f.actor)
        with self.assertRaises(OwnerRoutingError):self.observe(n)
        self.assertEqual(n.threads.create.await_count,self.created)

    async def test_missing_nonce_reservation_and_ambiguous_binding_do_not_adopt_other_lane(self):
        from codex_web.services.thread_bootstrap_bindings import ThreadBootstrapBindingNotFoundError
        n=await self.prepared()
        n.routing.bootstraps.get_by_bootstrap=lambda *_:(_ for _ in ()).throw(ThreadBootstrapBindingNotFoundError())
        n.routing.recipient_preparer.existing_assignment=lambda **_:None
        # Other LIVE same-scope lane exists; exact reservation absent. No scan.
        with self.assertRaises(OwnerRoutingError):self.observe(n)
        n.state.handoff.requested_at+=1;n.fixture.f.env.states.put(n.state.ref,n.state)
        with self.assertRaises(OwnerRoutingError):self.observe(n)
        n.fixture.f.bindings.append(n.binding.model_copy(update={'id':'other-binding','thread_id':'other-anchor'}))
        with self.assertRaisesRegex(OwnerRoutingError,'binding_ambiguous'):
            n.routing.existing_binding(n.state,'orchestrator')
        self.assertEqual(n.threads.create.await_count,self.created)

    async def test_only_verified_same_scope_anchor_can_fallback_without_reservation(self):
        from codex_web.services.thread_bootstrap_bindings import ThreadBootstrapBindingNotFoundError
        n=await self.prepared();source=n.fixture.assignments['assignment-existing-thread']
        source.resource_ids=n.reserved.resource_ids;source.repository_scope=n.reserved.repository_scope
        n.original.resource_ids=source.resource_ids;n.original.repository_scope=source.repository_scope
        n.store.upsert(n.session.model_copy(update={'resource_ids':source.resource_ids}))
        bound=n.fixture.bootstraps[n.binding.thread_id];bound.bootstrap_id=bound.id
        source.subject=ExecutionSubject(kind='thread_bootstrap',ref=bound.bootstrap_id)
        n.routing.bootstraps.get_by_bootstrap=lambda *_:(_ for _ in ()).throw(ThreadBootstrapBindingNotFoundError())
        n.routing.recipient_preparer.existing_assignment=lambda **_:None
        chosen,target=self.observe(n)
        self.assertEqual((chosen.thread_id,target.id),(n.binding.thread_id,source.id))
        self.assertEqual(n.threads.create.await_count,self.created)

    async def test_actual_accepted_start_ack_lineage_allows_expired_metadata_without_replay(self):
        f=accepted.AcceptedPreparedOwnerRoutingTests();f.setUp();self.addCleanup(f.doCleanups)
        state=await f.accepted();n=f.fixture.n;before=f.snapshot()
        bound,target=n.routing.recovery_recipient(state,n.binding,state.handoff.to_agent,writable_repository_ids=('platform',))
        self.assertEqual((bound.thread_id,target.id),(state.handoff.delivery.thread_id,'fresh-recipient-assignment'))
        target.lease=target.lease.model_copy(update={'expires_at':time.time()-1})
        self.assertEqual(n.routing.recovery_recipient(state,n.binding,state.handoff.to_agent,
            writable_repository_ids=('platform',))[1].id,target.id)
        self.assertEqual(n.threads.create.await_count,before[-1])
        self.assertEqual(n.binding.model_dump(mode='json'),before[2])
        # Metadata-only mode still requires the real retained START, not merely
        # an accepted Work row or a matching Role/Resource.
        state.handoff.delivery.turn_id='foreign-turn'
        f.fixture.f.f.env.states.put(state.ref,state)
        with self.assertRaisesRegex(OwnerRoutingError,'accepted_lineage_unverified'):
            n.routing.recovery_recipient(state,n.binding,state.handoff.to_agent,writable_repository_ids=('platform',))

    async def test_generation_one_requires_actual_preworker_witness(self):
        f=preworker.RecipientRecoveryTests();f.setUp();self.addCleanup(f.doCleanups)
        from codex_web.services.thread_bootstrap_bindings import ThreadBootstrapBindingNotFoundError
        def exact_bootstrap(key, actor):
            if f.n.bound is None or key != f.n.bound.bootstrap_id:
                raise ThreadBootstrapBindingNotFoundError('exact private reservation absent')
            return f.n.bound
        f.n.routing.bootstraps.get_by_bootstrap=exact_bootstrap
        await f.refuse_once();await f.n.prepare()
        target=f.n.routing.recovery_recipient(f.n.state,f.n.binding,'orchestrator',writable_repository_ids=('platform',))[1]
        self.assertEqual(target.execution_id,f.n.bound.execution_id);self.assertNotEqual(target.id,f.original.id)
        before=f.n.threads.create.await_count
        def remove_witness(state):
            next(a for a in state.assignments if a.id==f.original.id).failure=None
            return state
        f.workers.store.update(remove_witness)
        with self.assertRaisesRegex(OwnerRoutingError,'bootstrap_pending'):
            f.n.routing.recovery_recipient(f.n.state,f.n.binding,'orchestrator',writable_repository_ids=('platform',))
        self.assertEqual(f.n.threads.create.await_count,before)
