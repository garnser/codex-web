"""Actual canonical Upgrade/Artifact/queue/custody; no deploy or provider calls."""
import time
import unittest
from codex_web.artifact_evidence import EvidenceCreate
from codex_web.releases import ReleaseRecord, ReleaseBuild, ReleasePolicy
from codex_web.upgrades import UpgradePlan, UpgradeCompatibilityProfile, UpgradeStatus
from codex_web.services.releases import ReleaseService
from codex_web.services.upgrades import UpgradeService
from codex_web.services.extensions import ExtensionService
from codex_web.storage.extensions import ExtensionStateStore
from codex_web.storage.releases import ReleaseStore
from codex_web.storage.upgrades import UpgradeStore
from codex_web.runtime.execution import ExecutionPrestartRefusal
from codex_web.services.home_upgrade_checkpoint import HomeUpgradeCheckpointService
from tests.test_home_restart_recovery import HomeRestartRecoveryTests


class HomeUpgradeCheckpointTests(HomeRestartRecoveryTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.revision='source-build'
        # This is an existing idle provider conversation, not a fresh thread
        # whose initial native process was lost before its first input.
        self.bindings.mark_initial_turn_started('operator-thread',self.actor)
        current=self.projects.get('home',self.actor.tenant).home_operator
        self.projects.repository.update_home_operator(organization_id=self.actor.organization_id,
            workspace_id=self.actor.workspace_id,expected_revision=current.revision,
            binding=current.model_copy(update={'agent_profile_id':self.assignment.agent_profile.profile_id,
                'agent_profile_revision':self.assignment.agent_profile.profile_revision,'revision':current.revision+1}))
        self.operator=self.f.identity.bootstrap_service_actor(identity_id='fixture-upgrade-supervisor',
            name='Existing Upgrade supervisor fixture',scope=self.actor.tenant,service_scopes=('upgrade:admin','execution-worker:admin','artifact-evidence:admin',))
        self.releases=ReleaseService(ReleaseStore(self.f.sqlite),artifacts=self.artifacts,
            resources=self.f.resources,approvals=None,action_intents=self.intents)
        policy=ReleasePolicy()
        release=ReleaseRecord(organization_id=self.actor.organization_id,workspace_id=self.actor.workspace_id,
            project_id='home',name='Fixture deployment',version='2.0',build=ReleaseBuild(
                artifact_id='fixture-build',digest='sha256:'+'a'*64,source_revision='target-build',build_id='build'),
            policy=policy,policy_fingerprint=policy.fingerprint(),created_by=self.actor.identity_id)
        self.releases.store.create(release)
        self.upgrades=UpgradeService(UpgradeStore(self.f.sqlite),state_store=self.f.sqlite,
            definitions=self.registry,workers=self.f.workers,extensions=ExtensionService(ExtensionStateStore(self.f.sqlite)),recovery=None,
            releases=self.releases,action_intents=self.intents,approvals=None,evidence=self.artifacts,identity=self.f.identity)
        compatibility=UpgradeCompatibilityProfile(source_app_version='1.0',target_app_version='2.0',
            supported_control_plane_versions_during_rollout=('1.0','2.0'),source_state_schema_version=1,
            target_state_schema_version=1,target_definition_engine_version='1.0',target_extension_host_version='1.0')
        self.plan=UpgradePlan(organization_id=self.actor.organization_id,workspace_id=self.actor.workspace_id,
            project_id='home',release_id=release.id,current_app_version='1.0',target_app_version='2.0',
            compatibility=compatibility,observed_control_plane_versions=('1.0',),steps=(),created_by=self.operator.identity_id)
        self.put_plan(self.plan)
        self.upgrades.service_instance_id='fixture-service'
        self.planned=HomeUpgradeCheckpointService(restart_recovery=self.restart,upgrades=self.upgrades,
            artifacts=self.artifacts,source_revision=lambda:self.revision)
        self.restart.planned_restart=self.planned
        # Planned idle restart is not an invented active native turn.
        self.active_store.delete('operator-thread')

    def put_plan(self, plan):
        def update(state): state.plans[plan.id]=plan;return state
        self.upgrades.store.update(update)

    def checkpoint(self):
        return self.planned.checkpoint(self.plan.id,snapshot=self.assignment,
            worker_actor=self.worker_actor,operator_actor=self.operator)

    def completed(self):
        evidence=self.artifacts.create_evidence(EvidenceCreate(project_id='home',evidence_type='policy_evaluation',
            provider='codex-web',source='upgrade-post-verification',result='pass',
            metadata={'upgrade_plan_id':self.plan.id,'target_app_version':'2.0'}),actor=self.actor)
        self.plan=self.plan.model_copy(update={'status':UpgradeStatus.COMPLETED,'maintenance_mode':False,
            'completed_at':time.time(),'deployment_source_revision':'source-build',
            'deployment_target_revision':'target-build','post_upgrade_evidence_id':evidence.id})
        self.put_plan(self.plan);self.revision='target-build'
        return evidence

    async def test_same_idle_home_resumes_once_after_real_stored_plan_and_pass_evidence(self):
        artifact=self.checkpoint()
        self.assertNotIn('lease_token',artifact.metadata['checkpoint'])
        self.assertEqual(self.active_store.load(),{})
        self.completed()
        self.assignment=self.heal_same_thread()
        queued=self.planned.resume(artifact.id,operator_actor=self.operator)
        self.assertEqual(queued.thread_id,'operator-thread')
        self.assertEqual(queued.project_coordination_dispatch.schema_version,'1.4')
        attempt,claim=self.claim(queued)
        from types import SimpleNamespace
        from pathlib import Path
        from tests.test_turn_execution import TurnExecutionStartTests
        host,binding,sessions,runtime=TurnExecutionStartTests._service(self)
        binding.prepare=binding.prepare_bootstrap=lambda **kw:SimpleNamespace(
            assignment_id=self.assignment.id,workspace_id=self.assignment.execution_workspace_id,
            execution_id=self.assignment.execution_id)
        sessions.session.assignment=self.assignment
        sessions.session.validate_current=self.current
        sessions.session.status=lambda:SimpleNamespace(worker_id=self.f.worker.id,fence=self.assignment.fence)
        sessions.session.workspace_path=Path(self.f.workspaces.store.workspace(self.assignment.execution_workspace_id).path)
        runtime.bootstrap_bindings=self.bindings
        runtime.control_actor=self.actor
        sessions.get=lambda assignment_id:sessions.session if assignment_id==self.assignment.id else None
        runtime.native_input_guard_factory=self.service.home_operator_host.prepare_native_input_guard
        async def gate(**kwargs):
            self.service.home_operator_host.authorize_native_input(assignment=self.assignment,
                actor=kwargs['input_actor'],project=kwargs['project'],source=kwargs['source'])
            return None,''
        runtime.procedure_admission=gate
        runtime.queued_preflight=self.preflight
        before=self.preflight.store.get(attempt).queued_turn_snapshot
        with self.restart.input_context(queued,attempt_id=attempt,claim_id=claim):
            result=await runtime.start_thread_turn_now('operator-thread',project=self.host._project('home'),
                message=queued.message,sandbox='workspace-write',approval_policy='on-request',
                source='queued:upgrade-recovery:home',execution_id=queued.execution_id,
                execution_profile_id=self.assignment.execution_profile_id,actor=self.actor,
                queued_admission_attempt_id=attempt,queued_admission_claim_id=claim)
        self.assertEqual(result['turn']['id'],'turn-1')
        self.assertEqual([method for method,_ in sessions.session.requests],['thread/resume','turn/start'])
        started=self.preflight.store.get(attempt)
        self.assertEqual(started.status,'started');self.assertEqual(started.queued_turn_snapshot,before)
        self.assertEqual(started.queued_start_receipt.provider_native_turn_id,'turn-1')
        actual_active=host.active['operator-thread']
        checkpoint,requester=self.planned.validate_started_origin(actual_active,queued,started)
        self.assertEqual(checkpoint.thread_id,'operator-thread');self.assertEqual(requester.identity_id,self.actor.identity_id)
        with self.assertRaises(ExecutionPrestartRefusal):
            self.planned.validate_started_origin(actual_active.model_copy(update={'turn_id':'different-turn'}),queued,started)
        with self.assertRaises(ExecutionPrestartRefusal):self.planned.resume(artifact.id,operator_actor=self.operator)
        self.assertEqual(len(self.queue.get('operator-thread')),1)
        self.assertEqual(self.effects,0)
        # A further server restart must retain the actual planned continuation,
        # rather than trusting its source string or replaying the Upgrade input.
        self.queue.update('operator-thread', lambda rows: [row for row in rows if row.id != queued.id])
        recovered = self.restart.retain_startup(actual_active,
            project=self.host._project('home'), actor=self.actor)
        self.assertEqual(recovered.home_restart_input.schema_version, '1.3')
        self.assertEqual(recovered.thread_id, 'operator-thread')
        next_attempt, next_claim = self.claim(recovered)
        with self.restart.input_context(recovered, attempt_id=next_attempt, claim_id=next_claim):
            self.service.home_operator_host.prepare_native_input_guard(project=self.host._project('home'), actor=self.actor,
                execution_id=self.assignment.execution_id, assignment_id=self.assignment.id,
                source='queued:restart-recovery:home')()
        self.assertEqual(self.queue.get('operator-thread'), [recovered])
        self.assertEqual(self.effects, 0)

    async def test_no_completed_or_old_plan_or_wrong_running_source_can_resume(self):
        artifact=self.checkpoint()
        with self.assertRaises(ExecutionPrestartRefusal):self.planned.resume(artifact.id,operator_actor=self.operator)
        self.completed();self.revision='unrelated-build'
        with self.assertRaises(ExecutionPrestartRefusal):self.planned.resume(artifact.id,operator_actor=self.operator)
        self.revision='target-build'
        self.plan=self.plan.model_copy(update={'completed_at':artifact.created_at-1})
        self.put_plan(self.plan)
        with self.assertRaises(ExecutionPrestartRefusal):self.planned.resume(artifact.id,operator_actor=self.operator)
        self.assertEqual(self.queue.get('operator-thread'),[])

    async def test_completion_invalidation_and_home_reselection_revoke_final_send(self):
        artifact=self.checkpoint();completion=self.completed()
        queued=self.planned.resume(artifact.id,operator_actor=self.operator)
        attempt,claim=self.claim(queued)
        with self.restart.input_context(queued,attempt_id=attempt,claim_id=claim):
            guard=self.service.home_operator_host.prepare_native_input_guard(project=self.host._project('home'),actor=self.actor,
                execution_id=self.assignment.execution_id,assignment_id=self.assignment.id,source='upgrade-recovery:home')
        self.select('operator-thread',2)
        with self.assertRaises(Exception):guard()
        self.assertEqual(self.effects,0)

    async def test_completion_evidence_revocation_is_checked_again_at_final_send(self):
        artifact=self.checkpoint();completion=self.completed()
        queued=self.planned.resume(artifact.id,operator_actor=self.operator)
        attempt,claim=self.claim(queued)
        with self.restart.input_context(queued,attempt_id=attempt,claim_id=claim):
            guard=self.service.home_operator_host.prepare_native_input_guard(project=self.host._project('home'),actor=self.actor,
                execution_id=self.assignment.execution_id,assignment_id=self.assignment.id,source='upgrade-recovery:home')
        self.artifacts.invalidate_evidence(completion.id,'Independent supervisor evidence revoked',actor=self.actor)
        with self.assertRaises(Exception):guard()
        self.assertEqual(self.effects,0)

    async def test_old_completed_plan_and_nonoperator_cannot_create_checkpoint(self):
        with self.assertRaises(Exception):
            self.planned.checkpoint(self.plan.id,snapshot=self.assignment,
                worker_actor=self.worker_actor,operator_actor=self.worker_actor)
        self.completed()
        with self.assertRaises(ExecutionPrestartRefusal):self.checkpoint()
        self.assertEqual(self.queue.get('operator-thread'),[])

    def drain(self):
        return self.upgrades.start_service_drain(self.plan.id,actor=self.operator,
            service_instance_id='fixture-service',source_revision='source-build',target_revision='target-build')

    async def test_existing_upgrade_hooks_checkpoint_before_drain_and_enqueue_after_pass(self):
        self.upgrades.home_checkpoint_before_drain=lambda plan_id,actor:self.planned.checkpoint_current(
            plan_id,worker_runtime=self.runtime,operator_actor=actor)
        self.upgrades.home_resume_after_upgrade=lambda plan_id,actor:self.planned.resume_completed(plan_id,operator_actor=actor)
        drained=self.drain()
        self.assertTrue(drained.maintenance_mode)
        checkpoint=self.artifacts.find_idempotent_artifact(actor=self.actor,source='home-upgrade-checkpoint',idempotency_key=self.plan.id)
        self.assertIsNotNone(checkpoint)
        # Current service instance rebind must preserve the ORIGINAL checkpoint,
        # not create one from canceled/changed post-stop custody.
        self.upgrades.home_checkpoint_before_drain=lambda *_:self.fail('checkpoint reran during drain rebind')
        self.drain()
        self.revision='target-build'
        completed=self.upgrades.verify_post_upgrade(self.plan.id,actor=self.operator)
        self.assertEqual(completed.status,'completed');self.assertFalse(completed.maintenance_mode)
        self.assertEqual(len(self.queue.get('operator-thread')),1)
        self.upgrades.verify_post_upgrade(self.plan.id,actor=self.operator)
        self.assertEqual(len(self.queue.get('operator-thread')),1)

    async def test_checkpoint_failure_prevents_maintenance_and_after_failure_preserves_completed(self):
        def refused(*_):raise RuntimeError('Fixture checkpoint unavailable')
        self.upgrades.home_checkpoint_before_drain=refused
        with self.assertRaises(RuntimeError):self.drain()
        self.assertFalse(self.upgrades.get(self.plan.id,actor=self.operator).maintenance_mode)
        self.upgrades.home_checkpoint_before_drain=None
        self.upgrades.home_resume_after_upgrade=refused
        self.drain()
        completed=self.upgrades.verify_post_upgrade(self.plan.id,actor=self.operator)
        self.assertEqual(completed.status,'completed');self.assertFalse(completed.maintenance_mode)
        warnings=[e for e in self.artifacts.store.load().evidence if e.source=='home-upgrade-resume-blocked']
        self.assertEqual(len(warnings),1);self.assertEqual(warnings[0].result,'warn')
        self.assertEqual(warnings[0].metadata['upgrade_plan_id'],self.plan.id)

    async def test_unconfigured_home_noop_and_configured_worker_mismatch_explicit(self):
        from types import SimpleNamespace
        with self.assertRaises(ExecutionPrestartRefusal):
            self.planned.checkpoint_current(self.plan.id,worker_runtime=SimpleNamespace(
                worker=SimpleNamespace(id='foreign-worker'),worker_actor=self.worker_actor),operator_actor=self.operator)
        self.select(None,2)
        self.assertIsNone(self.planned.checkpoint_current(self.plan.id,worker_runtime=None,operator_actor=self.operator))
        self.assertIsNone(self.planned.resume_completed(self.plan.id,operator_actor=self.operator))

    async def test_navigation_only_without_bootstrap_never_blocks_upgrade(self):
        from unittest.mock import patch
        current=self.projects.get('home',self.actor.tenant).home_operator
        self.projects.repository.update_home_operator(organization_id=self.actor.organization_id,
            workspace_id=self.actor.workspace_id,expected_revision=current.revision,
            binding=current.model_copy(update={'agent_profile_id':None,'agent_profile_revision':None,'revision':current.revision+1}))
        with patch.object(self.bindings,'get_by_thread',side_effect=AssertionError('navigation must not resolve bootstrap')):
            self.assertIsNone(self.planned.checkpoint_current(self.plan.id,worker_runtime=None,operator_actor=self.operator))
            self.assertIsNone(self.planned.resume_completed(self.plan.id,operator_actor=self.operator))

    async def test_changed_message_with_recomputed_proof_cannot_replace_supervisor_request(self):
        from codex_web.services.home_upgrade_checkpoint import sha
        artifact=self.checkpoint();self.completed()
        queued=self.planned.resume(artifact.id,operator_actor=self.operator)
        proof=queued.home_restart_input.model_copy(update={'message_sha256':sha('different')})
        changed=queued.model_copy(update={'message':'different','project_coordination_dispatch':proof})
        attempt,claim=self.claim(changed)
        with self.assertRaises(ExecutionPrestartRefusal):
            self.restart.input_context(changed,attempt_id=attempt,claim_id=claim)
        self.assertEqual(self.effects,0)


def load_tests(loader,tests,pattern):
    return unittest.TestSuite(HomeUpgradeCheckpointTests(n) for n in HomeUpgradeCheckpointTests.__dict__ if n.startswith('test_'))
