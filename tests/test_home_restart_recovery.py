"""Private canonical Home/recovery/queue/native-admission proof; no provider process."""
import time
import unittest
from codex_web.execution_workers import AssignmentCancelRequest, AssignmentClaimRequest, AssignmentStartRequest, AssignmentRenewRequest
from codex_web.action_intents import ActionIntentCreate, ActionIntentClaimRequest, ActionDecisionSnapshot, ActionIntentStatus
from codex_web.action_providers import ActionRequest
from unittest.mock import Mock
from codex_web.models import ActiveThreadTurn
from codex_web.runtime.execution import TurnExecutionService, ExecutionPrestartRefusal
from codex_web.services.home_restart_recovery import HomeRestartRecoveryService
from codex_web.services.stale_active_turns import ActiveTurnRecoveryStore
from codex_web.services.execution_preflight import ExecutionPreflightService
from codex_web.storage.execution_preflight import ExecutionPreflightStore
from codex_web.storage.turn_queue import TurnQueueRepository
from codex_web.storage.runtime_state import ModelMapRepository
from tests.test_home_host_action_intents import HomeHostActionIntentTests
from tests.test_turn_execution import _Host


class HomeRestartRecoveryTests(HomeHostActionIntentTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.host=_Host();self.host._project=lambda key:self.projects.get(key,self.actor.tenant)
        self.queue=TurnQueueRepository(self.f.sqlite,self.root/'restart-queue.json')
        self.active_store=ModelMapRepository(self.f.sqlite,namespace='active_turns',
            legacy_path=self.root/'restart-active.json',model=ActiveThreadTurn)
        self.host._update_thread_queue_with_ingress=self.queue.update_with_ingress
        self.host._thread_queue_record=self.queue.get
        self.host._thread_queue=self.queue.get
        self.host._update_thread_queue_record=self.queue.update
        self.host._load_active_turns=self.active_store.load
        self.host._get_active_turn_record=self.active_store.get
        self.host._put_active_turn_record=self.active_store.put
        self.host._delete_active_turn_record=self.active_store.delete
        self.execution=TurnExecutionService(self.host)
        self.execution.actor_resolver=lambda identity,project:self.f.identity.actor_for_identity(identity,scope=self.actor.tenant)
        self.execution.schedule_queue_drain=Mock()
        self.preflight=ExecutionPreflightService(ExecutionPreflightStore(self.f.sqlite))
        self.execution.queued_preflight=self.preflight
        self.records=ActiveTurnRecoveryStore(self.f.sqlite)
        self.restart=HomeRestartRecoveryService(home_host=self.service.home_operator_host,
            recovery_store=self.records,execution=self.execution,intents=self.intents,preflight=self.preflight)
        self.execution.home_restart_recovery=self.restart
        a=self.assignment
        self.interrupted=ActiveThreadTurn(thread_id='operator-thread',turn_id='original-native-turn',
            project_id='home',source='web',execution_id=a.execution_id,assignment_id=a.id,
            execution_workspace_id=a.execution_workspace_id,worker_id=a.assigned_worker_id,fence=a.fence,
            execution_profile_id=a.execution_profile_id,agent_profile=a.agent_profile,
            agent_profile_actor_id=a.created_by,sandbox=a.sandbox,approval_policy=a.approval_policy,
            started_at=time.time()-5,updated_at=time.time()-4)
        self.active_store.put('operator-thread',self.interrupted)

    def claim(self,queued):
        attempt=self.preflight.retain_queued(queued,organization_id=self.actor.organization_id,workspace_id=self.actor.workspace_id)
        attempt,claim=self.preflight.claim_queued_admission(attempt.id)
        self.assertIsNotNone(claim)
        self.assertTrue(self.preflight.begin_queued_start(attempt.id,claim_id=claim))
        return attempt.id,claim

    async def test_history_budget_exhaustion_never_means_no_original(self):
        from unittest.mock import patch
        from codex_web.services.action_providers import ActionRequirementError
        intent = self.create()
        with patch.object(self.intents.store, 'intent_page', return_value=([intent] * 100, 'more')) as pages:
            with self.assertRaises(ActionRequirementError):
                self.provider._refuse_held_duplicate(self.intents, intent)
            self.assertEqual(pages.call_count, 100)
        with patch.object(self.intents.store, 'intent_page', return_value=([intent] * 100, 'more')) as pages:
            with self.assertRaises(ExecutionPrestartRefusal):
                self.restart._held(set())
            self.assertEqual(pages.call_count, 100)

    async def test_host_admission_history_reads_run_outside_event_loop(self):
        import threading
        from unittest.mock import patch
        caller = threading.get_ident()
        observed = []
        original = self.provider._refuse_held_duplicate
        def inspect(*args):
            observed.append(threading.get_ident())
            return original(*args)
        intent = self.create()
        with patch.object(self.provider, '_refuse_held_duplicate', side_effect=inspect):
            await self.execute(intent)
        self.assertEqual(len(observed), 1)
        self.assertNotEqual(observed[0], caller)
        self.assertEqual(self.effects, 1)

    async def test_legacy_dispatch_reader_refuses_restart_proof_without_dropping_it(self):
        from codex_web.models import ProjectCoordinationDispatch, QueuedTurn
        from pydantic import ValidationError
        queued = self.restart.retain_startup(self.interrupted, project=self.host._project('home'), actor=self.actor)
        raw = queued.model_dump(mode='json')
        self.assertNotIn('home_restart_input', raw)
        self.assertEqual(raw['project_coordination_dispatch']['schema_version'], '1.3')
        # The actual legacy dispatch type rejects both the distinct version and
        # fields; a legacy QueuedTurn therefore cannot read/rewrite this row.
        with self.assertRaises(ValidationError):
            ProjectCoordinationDispatch.model_validate(raw['project_coordination_dispatch'])
        self.assertEqual(QueuedTurn.model_validate(raw).home_restart_input, queued.home_restart_input)
        ordinary = queued.model_copy(update={'project_coordination_dispatch': None, 'source': 'web'})
        self.assertNotIn('project_coordination_dispatch', ordinary.model_dump(mode='json'))

    async def test_startup_retains_same_conversation_and_original_without_direct_native_send(self):
        self.execution.start_thread_turn_now=Mock(side_effect=AssertionError('startup cannot directly send privileged input'))
        await self.execution.resume_active_threads_after_startup({'operator-thread'})
        queued=self.queue.get('operator-thread')
        self.assertEqual(len(queued),1)
        self.assertIsNotNone(queued[0].home_restart_input)
        self.assertEqual(queued[0].thread_id,'operator-thread')
        self.assertEqual(queued[0].agent_profile_id,self.assignment.agent_profile.profile_id)
        self.assertIsNone(self.active_store.get('operator-thread'))
        original=self.records.get('operator-thread').original
        self.assertEqual(original,self.interrupted)
        self.execution.start_thread_turn_now.assert_not_called()
        self.execution.schedule_queue_drain.assert_called_once_with('operator-thread')
        with self.assertRaises(ExecutionPrestartRefusal):
            self.restart.retain_startup(self.interrupted,project=self.host._project('home'),actor=self.actor)
        self.assertEqual(len(self.queue.get('operator-thread')),1)
        self.assertEqual(self.effects,0)

    async def test_final_input_guard_requires_exact_retained_native_claim_and_current_home(self):
        queued=self.restart.retain_startup(self.interrupted,project=self.host._project('home'),actor=self.actor)
        attempt,claim=self.claim(queued)
        host=self.service.home_operator_host
        # This is the meaningful old boundary: source alone is always denied.
        with self.assertRaises(Exception):
            host.authorize_native_input(assignment=self.assignment,actor=self.actor,project=self.host._project('home'),source='restart-recovery:home')
        with self.restart.input_context(queued,attempt_id=attempt,claim_id=claim):
            final=host.prepare_native_input_guard(project=self.host._project('home'),actor=self.actor,
                execution_id=self.assignment.execution_id,assignment_id=self.assignment.id,source='restart-recovery:home')
            final()
        # The callback preserves exact internal authority after its producer scope.
        final()
        self.select('operator-thread',1)
        with self.assertRaises(Exception):final()
        self.assertEqual(self.effects,0)

    async def test_unknown_start_cannot_acquire_another_claim_or_resubmit_original(self):
        queued=self.restart.retain_startup(self.interrupted,project=self.host._project('home'),actor=self.actor)
        attempt,claim=self.claim(queued)
        current,second=self.preflight.claim_queued_admission(attempt)
        self.assertIsNone(second);self.assertEqual(current.status,'reconciliation_required')
        with self.assertRaises(ExecutionPrestartRefusal):
            self.restart.retain_startup(self.interrupted,project=self.host._project('home'),actor=self.actor)
        self.assertEqual(self.queue.get('operator-thread')[0].id,queued.id)
        with self.assertRaises(ExecutionPrestartRefusal):
            with self.restart.input_context(queued,attempt_id=attempt,claim_id='wrong-claim'):pass
        self.assertEqual(self.effects,0)

    async def test_unsettled_action_is_held_but_same_conversation_can_resume_to_inspect(self):
        intent=self.create()
        queued=self.restart.retain_startup(self.interrupted,project=self.host._project('home'),actor=self.actor)
        self.assertIn(intent.id,queued.message)
        self.assertIn(intent.id,self.records.get('operator-thread').evidence['home_restart']['held_action_ids'])
        attempt,claim=self.claim(queued)
        with self.restart.input_context(queued,attempt_id=attempt,claim_id=claim):
            guard=self.service.home_operator_host.prepare_native_input_guard(project=self.host._project('home'),actor=self.actor,
                execution_id=self.assignment.execution_id,assignment_id=self.assignment.id,source='restart-recovery:home')
            guard()
        self.assertEqual(self.intents.get(intent.id,self.actor).attempt,0)
        self.assertEqual(self.effects,0)

    def heal_same_thread(self):
        old=self.assignment
        self.f.workers.recover_expired(actor=self.actor,now=old.lease.expires_at+1)
        lost=self.current()
        self.assertEqual(lost.status,'lost')
        cancelled=self.f.workers.cancel_bootstrap(old.id,AssignmentCancelRequest(expected_fence=lost.fence,reason='Known expired private Home custody'),actor=self.actor)
        bound=self.f.service.prepare_bootstrap(bootstrap_id='recovered-home-bootstrap',execution_id='recovered-home-execution',
            project_id='home',sandbox='workspace-write',approval_policy='on-request',execution_profile_id='orchestration-only',
            agent_profile=old.agent_profile)
        claimed=self.f.workers.claim(self.f.worker.id,AssignmentClaimRequest(),actor=self.worker_actor,assignment_id=bound.assignment_id)
        current=self.f.workers.start(self.f.worker.id,bound.assignment_id,
            AssignmentStartRequest(fence=claimed.fence,lease_token=claimed.lease.lease_token),actor=self.worker_actor)
        self.bindings.assignment_store=self.f.workers.store
        self.bindings.rebind(bootstrap_id='recovered-home-bootstrap',thread_id='operator-thread',execution_id=current.execution_id,
            assignment_id=current.id,execution_workspace_id=current.execution_workspace_id,actor=self.actor,
            expected_assignment_id=old.id,expected_assignment_fence=cancelled.fence)
        return current

    async def test_known_unsent_human_input_survives_expiry_and_same_thread_custody_healing(self):
        queued=self.execution.enqueue_turn(thread_id='operator-thread',project_id='home',message='Inspect existing state after restart',
            source='web',execution_profile_id=self.assignment.execution_profile_id,
            agent_profile_id=self.assignment.agent_profile.profile_id,
            agent_profile_revision=self.assignment.agent_profile.profile_revision,agent_profile_actor_id=self.actor.identity_id)
        before=queued.model_dump(mode='json')
        retained=self.preflight.retain_queued(queued,organization_id=self.actor.organization_id,workspace_id=self.actor.workspace_id)
        self.assertEqual(retained.status,'queued');self.assertIsNone(retained.queued_start_receipt)
        current=self.heal_same_thread()
        original=self.queue.get('operator-thread')[0]
        self.assertEqual(original.model_dump(mode='json'),before)
        guard=self.service.home_operator_host.prepare_native_input_guard(project=self.host._project('home'),actor=self.actor,
            execution_id=current.execution_id,assignment_id=current.id,source='queued:web')
        guard()
        self.assertEqual(self.projects.get('home',self.actor.tenant).home_operator.thread_id,'operator-thread')
        self.assertEqual(len(self.index.load()),1)
        self.assertEqual(self.effects,0)

    async def test_restart_guard_accepts_legitimate_lease_renewal(self):
        queued=self.restart.retain_startup(self.interrupted,project=self.host._project('home'),actor=self.actor)
        attempt,claim=self.claim(queued)
        with self.restart.input_context(queued,attempt_id=attempt,claim_id=claim):
            final=self.service.home_operator_host.prepare_native_input_guard(project=self.host._project('home'),actor=self.actor,
                execution_id=self.assignment.execution_id,assignment_id=self.assignment.id,source='restart-recovery:home')
        self.f.workers.renew(self.f.worker.id,self.assignment.id,AssignmentRenewRequest(
            fence=self.assignment.fence,lease_token=self.assignment.lease.lease_token,lease_seconds=180),actor=self.worker_actor)
        final()
        self.assertEqual(self.effects,0)

    async def test_new_key_cannot_replay_unknown_command_across_custody_but_other_inspection_can_run(self):
        original=self.create()
        def unknown(state):
            state.intents=[i.model_copy(update={'status':ActionIntentStatus.UNCERTAIN,'attempt':1,'lease':None,
                'execution_started_at':time.time()}) if i.id==original.id else i for i in state.intents]
            return state
        self.intents.store.update(unknown,intent_ids=(original.id,))
        self.assignment=self.heal_same_thread()
        def fresh(argv,key):
            request=ActionRequest(action_id='operator.host.command',organization_id=self.actor.organization_id,
                workspace_id=self.actor.workspace_id,project_id='home',resource_ids=(self.host_resource_id,),
                requested_by=self.actor.identity_id,correlation_id=key,
                parameters={'assignment_id':self.assignment.id,'fence':self.assignment.fence,'argv':argv})
            intent=self.intents.create(ActionIntentCreate(binding_id=self.action_binding.id,request=request,
                execution_id=self.assignment.execution_id,policy_decision=ActionDecisionSnapshot(outcome='allow',source='policy:fixture',reason='new current inspection')),
                actor=self.actor,assignment_id=self.assignment.id)
            self.intents.claim(ActionIntentClaimRequest(worker_id='fixture-host-action',lease_seconds=120),actor=self.worker_actor,intent_id=intent.id)
            return intent
        duplicate=fresh(['/usr/bin/true'],'new-key-cannot-replay')
        self.assertNotEqual(duplicate.idempotency_key,original.idempotency_key)
        await self.execute(duplicate)
        self.assertEqual(self.effects,0)
        self.assertNotEqual(self.intents.get(duplicate.id,self.actor).status,'succeeded')
        inspect=fresh(['/usr/bin/false'],'different-authorized-inspection')
        await self.execute(inspect)
        self.assertEqual(self.effects,1)
        self.assertEqual(self.intents.get(inspect.id,self.actor).status,'succeeded')
        self.assertEqual(self.intents.get(original.id,self.actor).status,'uncertain')

    async def test_tampered_original_or_message_is_refused_before_native_input(self):
        queued=self.restart.retain_startup(self.interrupted,project=self.host._project('home'),actor=self.actor)
        attempt,claim=self.claim(queued)
        changed=queued.model_copy(update={'message':'different instructions'})
        with self.assertRaises(ExecutionPrestartRefusal):
            with self.restart.input_context(changed,attempt_id=attempt,claim_id=claim):pass
        self.assertEqual(self.effects,0)


def load_tests(loader,tests,pattern):
    return unittest.TestSuite(HomeRestartRecoveryTests(n) for n in HomeRestartRecoveryTests.__dict__ if n.startswith('test_'))
