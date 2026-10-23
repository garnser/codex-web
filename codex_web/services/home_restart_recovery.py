"""Home startup continuation on existing recovery evidence and retained admission.

No timer, worker, controller or model/provider transport is introduced here.
Unknown writes/starts remain held by the original ActionIntent/QueuedPreflight.
"""
import hashlib
import json
import time
from contextlib import contextmanager

from codex_web.definitions import reference_for
from codex_web.execution_profiles import ExecutionProfileCatalogDefinition
from codex_web.execution_workers import ExecutionAssignment
from codex_web.home_restart import HomeRestartInputProof, home_restart_context
from codex_web.services.home_operator_host import HOST_INSPECTION
from codex_web.services.stale_active_turns import ActiveTurnRecoveryRecord
from codex_web.identity import TenantScope


RESTART_MESSAGE = (
    'codex-web restarted while this Home conversation had an active turn. '
    'Continue from the retained conversation, canonical goals/checkpoints and existing action receipts. '
    'Inspect completed work before taking the next authorized step; do not repeat completed actions. '
    'Never replay an uncertain external action. Preserve pending human requests and report concrete progress.'
)


def checksum(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class HomeRestartRecoveryService:
    def __init__(self, *, home_host, recovery_store, execution, intents, preflight):
        self.home_host, self.store, self.execution = home_host, recovery_store, execution
        self.intents, self.preflight = intents, preflight

    def refused(self, reason):
        from codex_web.runtime.execution import ExecutionPrestartRefusal
        raise ExecutionPrestartRefusal(status_code=409, detail={'code':reason,
            'message':'Home restart retains the same conversation and original request; inspect canonical recovery evidence.',
            'retryable':False})

    def _assignment(self, assignment_id):
        value = self.home_host.broker.execution_workers.store.assignment(assignment_id)
        if value is None:
            self.refused('home_restart_original_custody_unavailable')
        return value

    def _held(self, execution_ids):
        after = None
        held = []
        while True:
            rows, after = self.intents.store.intent_page(after=after, limit=100)
            for intent in rows:
                if intent.execution_id not in execution_ids:
                    continue
                if intent.status not in {'succeeded', 'failed', 'cancelled', 'expired'}:
                    held.append((intent.id, str(intent.status)))
                    continue
                if intent.last_receipt_id:
                    receipts = self.intents.store.load(intent_id=intent.id).receipts
                    if any(r.id == intent.last_receipt_id and r.outcome == 'unknown' for r in receipts):
                        held.append((intent.id, 'unknown_receipt'))
            if after is None:
                return tuple(held)

    def retain_startup(self, active, *, project, actor):
        """Reserve exact interrupted Home once, then enqueue through normal admission.

        Caller holds the existing same-thread start lock and deletes the old
        active marker only after this returns positive retained queue evidence.
        """
        if project.id != 'home' or not active.assignment_id:
            return None
        assignment = self._assignment(active.assignment_id)
        ref = assignment.execution_profile_definition
        if ref is None:
            return None
        record = self.home_host.broker.execution_profiles.registry.get_record(ref.record_id)
        profile = ExecutionProfileCatalogDefinition.model_validate(record.payload).profile_map.get(assignment.execution_profile_id)
        if profile is None or HOST_INSPECTION.id not in profile.control_plane_operations:
            return None
        if (reference_for(record) != ref or actor is None or active.source not in {'web', 'queued:web'}
                or active.thread_id is None or active.execution_id != assignment.execution_id
                or active.execution_workspace_id != assignment.execution_workspace_id
                or active.worker_id != assignment.assigned_worker_id
                or active.agent_profile != assignment.agent_profile
                or active.agent_profile_actor_id != assignment.created_by
                or active.fence not in {assignment.fence, assignment.fence - 1}
                or active.fence != assignment.fence and (assignment.status != 'lost' or assignment.lease is not None)):
            self.refused('home_restart_original_provenance_unavailable')
        project, operator = self.home_host.authorize(assignment, actor)
        if operator.thread_id != active.thread_id:
            self.refused('home_restart_selected_conversation_changed')
        held = self._held({assignment.execution_id})
        message = RESTART_MESSAGE + (' Held original actions (inspect/reconcile; never replay): ' + ', '.join(
            key + ':' + status for key,status in held[:32]) if held else '')
        original = active.model_dump(mode='json')
        proof = HomeRestartInputProof(recovery_id=f'home-restart:{active.thread_id}:{checksum(original)}',
            original_sha256=checksum(original), thread_id=active.thread_id, requested_by=actor.identity_id,
            selection_revision=operator.revision, message_sha256=hashlib.sha256(message.encode()).hexdigest())
        now = time.time()
        proposed = ActiveTurnRecoveryRecord(thread_id=active.thread_id, recovery_id=proof.recovery_id,
            source_updated_at=active.updated_at, observed_at=now, outcome='live',
            reason_code='home_restart_retained_input', actor_id=actor.identity_id, original=active,
            evidence={'home_restart':{'proof':proof.model_dump(mode='json'),
                'source_assignment':assignment.model_dump(mode='json'), 'held_action_ids':[key for key,status in held], 'state':'reserved'}}, updated_at=now)
        def reserve(raw):
            if raw is not None:
                previous = ActiveTurnRecoveryRecord.model_validate(raw)
                prior = previous.evidence.get('home_restart')
                if prior:
                    # A second startup does not resubmit a possibly issued native
                    # input. Existing retained preflight owns all retry/reconcile.
                    self.refused('home_restart_original_already_retained')
                if previous.outcome == 'blocked' and previous.action_state != 'applied':
                    self.refused('home_restart_prior_recovery_blocked')
                proposed.prior = (*previous.prior, previous.model_dump(mode='json', exclude={'prior'}))[-20:]
            return proposed.model_dump(mode='json')
        self.store.store.record_update(self.store.RECORD_NAMESPACE, active.thread_id, reserve, default=None)
        queued = self.execution.enqueue_turn(thread_id=active.thread_id, project_id='home', message=message,
            sandbox=active.sandbox, approval_policy=active.approval_policy, model=active.model,
            reasoning_effort=active.reasoning_effort, source='restart-recovery:home', reply_target=active.reply_target,
            execution_profile_id=assignment.execution_profile_id,
            agent_profile_id=assignment.agent_profile.profile_id,
            agent_profile_revision=assignment.agent_profile.profile_revision,
            agent_profile_actor_id=actor.identity_id, memory_context_binding=active.memory_context_binding,
            home_restart_input=proof)
        self._update(proof, state='queued', queued_id=queued.id, queued_execution_id=queued.execution_id)
        return queued

    def _update(self, proof, **changes):
        def update(raw):
            record = ActiveTurnRecoveryRecord.model_validate(raw)
            saved = record.evidence.get('home_restart')
            if not saved or saved.get('proof') != proof.model_dump(mode='json'):
                self.refused('home_restart_recovery_evidence_changed')
            record.evidence = {**record.evidence,'home_restart':{**saved,**changes}}
            record.updated_at = time.time()
            return record.model_dump(mode='json')
        self.store.store.record_update(self.store.RECORD_NAMESPACE, proof.thread_id, update, default=None)

    def validate(self, queued, *, attempt_id, claim_id):
        proof = queued.home_restart_input
        if proof is None:
            self.refused('home_restart_retained_proof_missing')
        saved = self.store.get(proof.thread_id)
        evidence = saved.evidence.get('home_restart') if saved else None
        if (saved is None or evidence is None or saved.recovery_id != proof.recovery_id
                or evidence.get('proof') != proof.model_dump(mode='json')
                or checksum(saved.original.model_dump(mode='json')) != proof.original_sha256
                or evidence.get('queued_id') != queued.id or evidence.get('queued_execution_id') != queued.execution_id
                or queued.thread_id != proof.thread_id or queued.project_id != 'home'
                or queued.source != 'restart-recovery:home' or queued.canonical_ingress_id is not None
                or hashlib.sha256(queued.message.encode()).hexdigest() != proof.message_sha256
                or queued.agent_profile_actor_id != proof.requested_by):
            self.refused('home_restart_retained_input_changed')
        attempt = self.preflight.store.get(attempt_id)
        if (attempt is None or attempt.queued_admission_claim_id != claim_id
                or attempt.status != 'reconciliation_required' or attempt.queued_turn_snapshot is None
                or {k:v for k,v in attempt.queued_turn_snapshot.items() if k != 'attempts'} !=
                    {k:v for k,v in queued.model_dump(mode='json').items() if k != 'attempts'}
                or attempt.execution_id != queued.execution_id):
            self.refused('home_restart_current_native_admission_unavailable')
        original = self._assignment(saved.original.assignment_id)
        pinned = ExecutionAssignment.model_validate(evidence['source_assignment'])
        if any(getattr(original, key) != getattr(pinned, key) for key in (
                'id','execution_id','created_by','organization_id','workspace_id','project_id',
                'subject','assigned_worker_id','execution_workspace_id','execution_profile_id',
                'execution_profile_definition','agent_profile','resource_ids','resource_context','runtime_binding')):
            self.refused('home_restart_original_custody_changed')
        actor = self.home_host.broker.identity.actor_for_identity(proof.requested_by,
            scope=TenantScope(organization_id=original.organization_id, workspace_id=original.workspace_id))
        home = self.home_host.projects.get('home', actor.tenant).home_operator
        if home is None or home.thread_id != proof.thread_id or home.revision != proof.selection_revision:
            self.refused('home_restart_selected_conversation_changed')
        return saved, original, actor

    @contextmanager
    def input_context(self, queued, *, attempt_id, claim_id):
        service = self
        class Authorizer:
            def validate_input(self, assignment, actor, project, source):
                saved, original, current_actor = service.validate(queued, attempt_id=attempt_id, claim_id=claim_id)
                if (source not in {'restart-recovery:home','queued:restart-recovery:home'} or project.id != 'home'
                        or actor.identity_id != current_actor.identity_id or assignment.created_by != actor.identity_id
                        or assignment.agent_profile != original.agent_profile
                        or assignment.execution_profile_definition != original.execution_profile_definition
                        or (assignment.organization_id, assignment.workspace_id) != (original.organization_id,original.workspace_id)):
                    service.refused('home_restart_current_input_origin_changed')
                # The existing common final native-input guard checks the new
                # live assignment/worker/fence/lease. Healing must bind SAME thread.
                binding = service.home_host.bootstrap_bindings.get_by_thread(queued.thread_id, current_actor)
                if binding.assignment_id != assignment.id:
                    service.refused('home_restart_current_bootstrap_changed')
                if assignment.id != original.id and (original.status not in {'lost','cancelled'} or original.lease is not None
                        or assignment.execution_workspace_id == original.execution_workspace_id):
                    service.refused('home_restart_previous_custody_not_fenced')
                return 'web'
        authorizer = Authorizer()
        self.validate(queued, attempt_id=attempt_id, claim_id=claim_id)
        with home_restart_context(authorizer):
            yield
