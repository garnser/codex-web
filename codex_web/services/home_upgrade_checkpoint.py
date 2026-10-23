"""Planned Home continuity through the EXISTING external Upgrade supervisor.

Internal composition only: no restart service, signal, model loop or API identity
is introduced. The supervisor must hold its ordinary canonical Upgrade authority.
"""
import hashlib
import json
from pydantic import BaseModel, ConfigDict, Field
from codex_web.agent_profiles import AgentProfileExecutionBinding
from codex_web.artifact_evidence import ArtifactCreate, ArtifactDigest, EvidenceCreate
from codex_web.definitions import DefinitionReference
from codex_web.home_restart import HomeUpgradeInputProof, home_restart_context
from codex_web.identity import TenantScope
from codex_web.services.home_operator_host import HOST_INSPECTION


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


class HomeUpgradeCheckpoint(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    schema_version: str = Field(default='1.0', pattern=r'^1\.0$')
    plan_id: str
    plan_created_at: float
    release_id: str
    target_revision: str
    source_revision: str
    target_version: str
    organization_id: str
    workspace_id: str
    thread_id: str
    selection_revision: int
    requester_id: str
    assignment_id: str
    execution_id: str
    execution_workspace_id: str
    worker_id: str
    fence: int
    agent_profile: AgentProfileExecutionBinding
    execution_profile_id: str
    execution_profile_definition: DefinitionReference
    retained_queue_sha256: str
    retained_queue_ids: tuple[str, ...] = Field(max_length=256)
    held_action_ids: tuple[str, ...] = Field(max_length=256)


class HomeUpgradeCheckpointService:
    def __init__(self, *, restart_recovery, upgrades, artifacts, source_revision):
        self.recovery, self.upgrades, self.artifacts = restart_recovery, upgrades, artifacts
        self.home = restart_recovery.home_host
        self.source_revision = source_revision

    def refused(self, reason):
        self.recovery.refused(reason)

    def _operator(self, actor):
        # Fresh authentication belongs to the existing supervisor/API boundary;
        # this method retains canonical Upgrade authority and current identity.
        self.upgrades.require_operator(actor)
        self.home.broker.identity.actor_for_identity(actor.identity_id, scope=actor.tenant)

    def checkpoint(self, plan_id, *, snapshot, worker_actor, operator_actor):
        """Supervisor saves a real idle/active Home checkpoint BEFORE maintenance."""
        self._operator(operator_actor)
        plan = self.upgrades.get(plan_id, actor=operator_actor)
        origin = self.home.authorize_operation(snapshot, operation_id=HOST_INSPECTION.id,
            worker_actor=worker_actor, requester_id=snapshot.created_by, mutating=False)
        a, requester, binding = origin.assignment, origin.requester, origin.binding
        if ((plan.organization_id, plan.workspace_id) != (a.organization_id, a.workspace_id)
                or plan.status != 'draft' or plan.maintenance_mode or plan.completed_at is not None
                or plan.post_upgrade_evidence_id is not None):
            self.refused('home_upgrade_checkpoint_requires_new_plan')
        release = self.upgrades.releases.get(plan.release_id, actor=operator_actor)
        revision = self.source_revision()
        target = release.build.source_revision
        if not revision or not target or plan.target_app_version != release.version:
            self.refused('home_upgrade_source_unavailable')
        queues = self.recovery.execution.host._thread_queue(binding.thread_id)
        if len(queues) > 256:
            self.refused('home_upgrade_retained_queue_exceeded')
        held = self.recovery._held({a.execution_id}, actor=requester)
        checkpoint = HomeUpgradeCheckpoint(plan_id=plan.id, plan_created_at=plan.created_at,
            release_id=plan.release_id, source_revision=revision, target_revision=target,
            target_version=plan.target_app_version, organization_id=a.organization_id,
            workspace_id=a.workspace_id, thread_id=binding.thread_id, selection_revision=binding.revision,
            requester_id=requester.identity_id, assignment_id=a.id, execution_id=a.execution_id,
            execution_workspace_id=a.execution_workspace_id, worker_id=a.assigned_worker_id,
            fence=a.fence, agent_profile=a.agent_profile, execution_profile_id=a.execution_profile_id,
            execution_profile_definition=a.execution_profile_definition,
            retained_queue_sha256=sha(encoded([q.model_dump(mode='json') for q in queues])),
            retained_queue_ids=tuple(q.id for q in queues), held_action_ids=tuple(i for i,s in held))
        raw = encoded(checkpoint.model_dump(mode='json'))
        # Current authority is rechecked at the writer edge. Only identifiers and
        # hashes are recorded: no lease tokens, transcript, argv or output bytes.
        self.home.authorize_operation(snapshot, operation_id=HOST_INSPECTION.id,
            worker_actor=worker_actor, requester_id=requester.identity_id, mutating=False)
        return self.artifacts.create_artifact(ArtifactCreate(project_id='home',
            execution_id=a.execution_id, execution_workspace_id=a.execution_workspace_id,
            artifact_type='report', name='Home planned Upgrade checkpoint', provider='codex-web',
            source='home-upgrade-checkpoint', external_id=plan.id,
            digest=ArtifactDigest(value=sha(raw)), deny_model_context=True,
            metadata={'checkpoint':raw}), actor=requester,
            idempotency_key=sha(encoded([plan.id, binding.revision, a.id, a.fence])))

    def _checkpoint(self, checkpoint_id, actor):
        artifact = self.artifacts.get_artifact(checkpoint_id, actor=actor)
        raw = artifact.metadata.get('checkpoint')
        if (artifact.project_id != 'home' or artifact.source != 'home-upgrade-checkpoint'
                or artifact.lifecycle != 'active' or not isinstance(raw, str)
                or artifact.digest is None or artifact.digest.value != sha(raw)):
            self.refused('home_upgrade_checkpoint_unavailable')
        checkpoint = HomeUpgradeCheckpoint.model_validate_json(raw)
        if ((checkpoint.organization_id,checkpoint.workspace_id) != (actor.organization_id,actor.workspace_id)
                or checkpoint.requester_id != artifact.producer_identity_id
                or checkpoint.plan_id != artifact.external_id or checkpoint.execution_id != artifact.execution_id):
            self.refused('home_upgrade_checkpoint_origin_changed')
        return artifact, checkpoint

    def _completed(self, checkpoint, actor):
        plan = self.upgrades.get(checkpoint.plan_id, actor=actor)
        if (plan.created_at != checkpoint.plan_created_at or plan.release_id != checkpoint.release_id
                or plan.target_app_version != checkpoint.target_version or plan.status != 'completed'
                or plan.maintenance_mode or plan.completed_at is None
                or plan.deployment_source_revision != checkpoint.source_revision
                or plan.deployment_target_revision != checkpoint.target_revision
                or self.source_revision() != checkpoint.target_revision or not plan.post_upgrade_evidence_id):
            self.refused('home_upgrade_completion_unavailable')
        evidence = self.artifacts.get_evidence(plan.post_upgrade_evidence_id, actor=actor)
        if (evidence.lifecycle != 'valid' or evidence.result != 'pass'
                or evidence.source != 'upgrade-post-verification' or evidence.project_id != plan.project_id
                or evidence.metadata.get('upgrade_plan_id') != plan.id
                or evidence.metadata.get('target_app_version') != checkpoint.target_version):
            self.refused('home_upgrade_completion_evidence_unavailable')
        return plan, evidence

    def _requester(self, checkpoint):
        actor = self.home.broker.identity.actor_for_identity(checkpoint.requester_id,
            scope=TenantScope(organization_id=checkpoint.organization_id,workspace_id=checkpoint.workspace_id))
        binding = self.home.projects.get('home', actor.tenant).home_operator
        if (binding is None or binding.thread_id != checkpoint.thread_id
                or binding.revision != checkpoint.selection_revision or binding.selected_by != actor.identity_id):
            self.refused('home_upgrade_selected_conversation_changed')
        return actor

    def resume(self, checkpoint_id, *, operator_actor):
        """Called once by the existing supervisor after canonical verification."""
        self._operator(operator_actor)
        artifact, checkpoint = self._checkpoint(checkpoint_id, operator_actor)
        actor = self._requester(checkpoint)
        plan, completion = self._completed(checkpoint, operator_actor)
        if (plan.completed_at < artifact.created_at or completion.observed_at < artifact.created_at):
            self.refused('home_upgrade_old_completion_cannot_resume')
        message = ('The existing Upgrade supervisor completed the planned restart. Continue this SAME Home conversation '
            'from checkpoint ' + artifact.id + ', completed plan ' + plan.id + ' and verification ' + completion.id +
            '. Inspect retained goals, requests and original action receipts. Never replay uncertain host writes.')
        source = 'home-upgrade-resume'
        existing = self.artifacts.find_idempotent_evidence(actor=actor,source=source,idempotency_key=artifact.id)
        if existing is not None:
            self.refused('home_upgrade_resume_already_retained')
        receipt = self.artifacts.create_evidence(EvidenceCreate(project_id='home',
            evidence_type='runtime_result', provider='codex-web', source=source, result='info',
            artifact_ids=(artifact.id,), summary='Existing Upgrade supervisor retained Home continuation',
            observed_at=plan.completed_at,
            metadata={'upgrade_plan_id':plan.id,'completion_evidence_id':completion.id,
                'checkpoint_sha256':artifact.digest.value}), actor=actor, idempotency_key=artifact.id)
        proof = HomeUpgradeInputProof(checkpoint_id=artifact.id,checkpoint_sha256=artifact.digest.value,
            upgrade_plan_id=plan.id,completion_evidence_id=completion.id,resume_evidence_id=receipt.id,
            thread_id=checkpoint.thread_id,requested_by=actor.identity_id,message_sha256=sha(message))
        queued = self.recovery.execution.enqueue_turn(thread_id=checkpoint.thread_id,project_id='home',
            execution_id='home-upgrade-'+artifact.id, message=message, source='upgrade-recovery:home',
            execution_profile_id=checkpoint.execution_profile_id, agent_profile_id=checkpoint.agent_profile.profile_id,
            agent_profile_revision=checkpoint.agent_profile.profile_revision,agent_profile_actor_id=actor.identity_id,
            home_restart_input=proof)
        self.recovery.execution.schedule_queue_drain(checkpoint.thread_id)
        return queued

    def input_context(self, queued, *, attempt_id, claim_id):
        service = self
        def validate():
            proof = queued.home_restart_input
            # Scope comes from canonical retained queue admission, never a request
            # body. Preflight must prove this exact original snapshot and claim.
            attempt = service.recovery.preflight.store.get(attempt_id)
            if (attempt is None or attempt.queued_admission_claim_id != claim_id
                    or attempt.status != 'reconciliation_required' or attempt.queued_turn_snapshot is None
                    or {k:v for k,v in attempt.queued_turn_snapshot.items() if k != 'attempts'} !=
                       {k:v for k,v in queued.model_dump(mode='json').items() if k != 'attempts'}):
                service.refused('home_upgrade_current_native_admission_unavailable')
            actor = service.home.broker.identity.actor_for_identity(proof.requested_by,
                scope=TenantScope(organization_id=attempt.organization_id,workspace_id=attempt.workspace_id))
            artifact, checkpoint = service._checkpoint(proof.checkpoint_id,actor)
            plan, completion = service._completed(checkpoint,actor)
            actor = service._requester(checkpoint)
            receipt = service.artifacts.get_evidence(proof.resume_evidence_id,actor=actor)
            if (proof.checkpoint_sha256 != artifact.digest.value or proof.upgrade_plan_id != plan.id
                    or proof.completion_evidence_id != completion.id or queued.thread_id != checkpoint.thread_id
                    or proof.thread_id != checkpoint.thread_id or queued.source != 'upgrade-recovery:home'
                    or queued.project_id != 'home' or queued.execution_id != 'home-upgrade-'+artifact.id
                    or queued.canonical_ingress_id is not None or queued.agent_profile_actor_id != actor.identity_id
                    or sha(queued.message) != proof.message_sha256 or receipt.lifecycle != 'valid'
                    or receipt.source != 'home-upgrade-resume' or receipt.result != 'info'
                    or receipt.artifact_ids != (artifact.id,) or receipt.producer_identity_id != actor.identity_id
                    or receipt.metadata.get('upgrade_plan_id') != plan.id
                    or receipt.metadata.get('completion_evidence_id') != completion.id
                    or receipt.metadata.get('checkpoint_sha256') != artifact.digest.value):
                service.refused('home_upgrade_retained_input_changed')
            return checkpoint,actor
        class Authorizer:
            def validate_input(self,assignment,actor,project,source):
                checkpoint,current = validate()
                original = service.recovery._assignment(checkpoint.assignment_id)
                binding = service.home.bootstrap_bindings.get_by_thread(checkpoint.thread_id,current)
                if (source not in {'upgrade-recovery:home','queued:upgrade-recovery:home'} or project.id != 'home'
                        or actor.identity_id != current.identity_id or assignment.created_by != current.identity_id
                        or assignment.agent_profile != checkpoint.agent_profile
                        or assignment.execution_profile_definition != checkpoint.execution_profile_definition
                        or binding.assignment_id != assignment.id
                        or assignment.id != original.id and (original.status not in {'lost','cancelled'}
                            or original.lease is not None or assignment.execution_workspace_id == original.execution_workspace_id)):
                    service.refused('home_upgrade_current_custody_changed')
                return 'web'
        validate()
        return home_restart_context(Authorizer())
