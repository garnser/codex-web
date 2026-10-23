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
from codex_web.home_restart import HomeRestartInputProof, HomeUpgradeInputProof, home_restart_context
from codex_web.services.home_operator_host import HOST_INSPECTION
from codex_web.services.stale_active_turns import ActiveTurnRecoveryRecord
from codex_web.identity import TenantScope
from codex_web.models import ActiveThreadTurn, QueuedTurn


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
        self.planned_restart = None
        self.event_followup = None

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

    def _held(self, execution_ids, *, actor):
        from codex_web.storage.home_intent_query import HomeIntentRecoveryQuery
        held = []
        for execution_id in execution_ids:
            try:
                rows = self.intents.store.home_recovery_candidates(HomeIntentRecoveryQuery(
                    organization_id=actor.organization_id, workspace_id=actor.workspace_id,
                    execution_id=execution_id))
                for intent in rows:
                    if intent.status not in {'succeeded', 'failed', 'cancelled', 'expired'}:
                        held.append((intent.id, str(intent.status)))
                    elif intent.last_receipt_id:
                        if self.intents.store.recovery_last_receipt(intent).outcome == 'unknown':
                            held.append((intent.id, 'unknown_receipt'))
                    elif intent.execution_started_at is not None:
                        held.append((intent.id, 'missing_receipt'))
            except ValueError:
                self.refused('home_restart_original_action_inventory_unavailable')
        return tuple(held)

    def _known_attempts(self, queued_id):
        if not isinstance(queued_id,str) or not queued_id:
            self.refused('home_restart_retained_queue_identity_unavailable')
        attempt = self.preflight.store.get('preflight-queued-' + queued_id)
        if attempt is None or (attempt.queued_turn_snapshot or {}).get('id') != queued_id:
            self.refused('home_restart_retained_queue_identity_unavailable')
        return (attempt,)

    def _started_input(self, active, *, queued_id=None, proof=None):
        """Positive original START, not absence of an unknown response.

        The existing per-thread canonical index is bounded. Exhaustion/absence
        refuses; it never authorizes another submission of that original input.
        """
        from codex_web.services.queued_native_start import validate_attribution
        matches = []
        candidates = (self._known_attempts(queued_id) if queued_id is not None
            else self.preflight.store.for_thread(active.thread_id, limit=200))
        for attempt in candidates:
            receipt, correlation = attempt.queued_start_receipt, attempt.queued_start_correlation
            if receipt is None or correlation is None or attempt.status != 'started':
                continue
            if (receipt.provider_native_turn_id != active.turn_id
                    or receipt.provider_native_session_id != active.thread_id
                    or receipt.execution_id != active.execution_id
                    or correlation.assignment_id != active.assignment_id
                    or correlation.worker_id != active.worker_id or correlation.fence != active.fence):
                continue
            validate_attribution(self.preflight, attempt, correlation)
            queued = QueuedTurn.model_validate(attempt.queued_turn_snapshot)
            evidence = attempt.queued_start_evidence
            if (evidence is None or evidence.provider_native_turn_id != active.turn_id
                    or evidence.provider_native_session_id != active.thread_id
                    or evidence.runtime_generation != correlation.runtime_generation
                    or evidence.rpc_request_id != correlation.rpc_request_id
                    or receipt.admission_claim_id != correlation.admission_claim_id
                    or receipt.requested_execution_id != correlation.requested_execution_id
                    or queued.source != (active.source or '').removeprefix('queued:')
                    or queued.agent_profile_actor_id != active.agent_profile_actor_id
                    or queued_id is not None and queued.id != queued_id
                    or proof is not None and queued.home_restart_input != proof):
                self.refused('home_restart_started_input_changed')
            matches.append((attempt, queued))
        if len(matches) != 1:
            self.refused('home_restart_positive_native_start_required')
        return matches[0]

    def _settled_chain(self, previous):
        evidence = previous.evidence.get('home_restart')
        if (evidence is None or self.event_followup is None
                or self.event_followup.sessions is None or self.event_followup.runtime_usage is None):
            self.refused('home_restart_previous_terminal_usage_unavailable')
        matches = []
        for attempt in self._known_attempts(evidence.get('queued_id')):
            snapshot = attempt.queued_turn_snapshot
            if snapshot is None or snapshot.get('id') != evidence.get('queued_id'):
                continue
            receipt, correlation = attempt.queued_start_receipt, attempt.queued_start_correlation
            if receipt is None or correlation is None:
                continue
            active = previous.original.model_copy(update={'source':'queued:restart-recovery:home',
                'turn_id':receipt.provider_native_turn_id,'execution_id':correlation.execution_id,
                'assignment_id':correlation.assignment_id,'worker_id':correlation.worker_id,'fence':correlation.fence,
                'execution_workspace_id':self._assignment(correlation.assignment_id).execution_workspace_id})
            self._started_input(active,queued_id=evidence['queued_id'],
                proof=HomeRestartInputProof.model_validate(evidence['proof']))
            assignment = self._assignment(active.assignment_id)
            runtime = assignment.runtime_binding
            session = self.event_followup.sessions.find_by_native_id(active.thread_id,
                organization_id=assignment.organization_id,workspace_id=assignment.workspace_id,
                provider_id=runtime.provider_id,runtime_id=runtime.runtime_id)
            if session is None or session.assignment_id != active.assignment_id or session.execution_id != active.execution_id:
                self.refused('home_restart_previous_terminal_usage_unavailable')
            usage_id = 'runtime-usage-' + hashlib.sha256(f'{session.id}\n{active.turn_id}'.encode()).hexdigest()[:32]
            usage = self.event_followup.runtime_usage.get(usage_id)
            if (usage is None or usage.terminal_outcome == 'unknown' or usage.completed_at is None
                    or (usage.organization_id,usage.workspace_id,usage.assignment_id,usage.execution_id,
                        usage.provider_native_session_id,usage.provider_native_turn_id) !=
                        (assignment.organization_id,assignment.workspace_id,active.assignment_id,active.execution_id,active.thread_id,active.turn_id)):
                self.refused('home_restart_previous_terminal_usage_unavailable')
            if evidence.get('event_origin') is not None:
                origin = ActiveThreadTurn.model_validate(evidence['event_origin']['active'])
                run_id = origin.source.removeprefix('queued:').removeprefix('home-operator-automation:')
                run = self.event_followup.runs.store.get(run_id,organization_id=assignment.organization_id,
                    workspace_id=assignment.workspace_id)
                if run.status not in {'succeeded','failed','cancelled','expired'} or run.home_terminal is None:
                    self.refused('home_restart_previous_event_accounting_pending')
            matches.append(attempt.id)
        if len(matches) != 1:
            self.refused('home_restart_previous_terminal_usage_unavailable')

    def _origin(self, active, assignment, actor, project, previous):
        source = (active.source or '').removeprefix('queued:')
        if source == 'web':
            return None
        if source == 'restart-recovery:home':
            prior = previous.evidence.get('home_restart') if previous else None
            if prior is None or previous.original.turn_id == active.turn_id:
                self.refused('home_restart_original_already_retained')
            proof = HomeRestartInputProof.model_validate(prior['proof'])
            self._started_input(active, queued_id=prior.get('queued_id'), proof=proof)
            # The whole preceding record is retained in prior; a source label
            # alone cannot create a recovery generation or inherit event rights.
            if proof.selection_revision != self.home_host.projects.get('home', actor.tenant).home_operator.revision:
                self.refused('home_restart_selected_conversation_changed')
            origin = prior.get('event_origin')
            if origin is not None:
                self._validate_event_origin(origin, actor, project)
            return origin
        if source == 'upgrade-recovery:home' and self.planned_restart is not None:
            attempt, queued = self._started_input(active)
            self.planned_restart.validate_started_origin(active, queued, attempt)
            return None
        if source.startswith('home-operator-automation:') and self.event_followup is not None:
            attempt, queued = self._started_input(active)
            self.event_followup.restart_origin(active, assignment=assignment, actor=actor, project=project,
                requested_execution_id=attempt.execution_id, message=queued.message)
            return {'active': active.model_dump(mode='json'), 'queued_id':queued.id}
        self.refused('home_restart_original_provenance_unavailable')

    def _validate_event_origin(self, origin, actor, project, *, check_controls=True):
        if self.event_followup is None or not isinstance(origin, dict) or set(origin) != {'active','queued_id'}:
            self.refused('home_restart_event_authority_unavailable')
        original = ActiveThreadTurn.model_validate(origin['active'])
        assignment = self._assignment(original.assignment_id)
        attempt, queued = self._started_input(original,queued_id=origin['queued_id'])
        return self.event_followup.restart_origin(original, assignment=assignment, actor=actor, project=project,
            requested_execution_id=attempt.execution_id, message=queued.message, check_controls=check_controls)

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
        if (reference_for(record) != ref or actor is None
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
        previous = self.store.get(active.thread_id)
        origin = self._origin(active, assignment, actor, project, previous)
        fresh_chain = bool(previous and previous.evidence.get('home_restart')
            and (active.source or '').removeprefix('queued:') == 'web')
        archive_id = None
        if fresh_chain:
            self._started_input(active)
            self._settled_chain(previous)
            archive_id = self.store.archive(previous)
        execution_ids = {assignment.execution_id}
        if previous and (active.source or '').removeprefix('queued:') == 'restart-recovery:home':
            execution_ids.update(row['original']['execution_id'] for row in (*previous.prior, previous.model_dump(mode='json'))
                if row.get('evidence', {}).get('home_restart'))
        held = self._held(execution_ids, actor=actor)
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
                'source_assignment':assignment.model_dump(mode='json'), 'held_action_ids':[key for key,status in held],
                **({'event_origin':origin} if origin is not None else {}),
                **({'previous_history_id':archive_id} if archive_id is not None else
                   {'previous_history_id':previous.evidence['home_restart']['previous_history_id']}
                   if previous and previous.evidence.get('home_restart', {}).get('previous_history_id') else {}), 'state':'reserved'}}, updated_at=now)
        observed = previous.model_dump(mode='json') if previous else None
        def reserve(raw):
            if raw != observed:
                self.refused('home_restart_recovery_evidence_changed')
            if raw is not None:
                previous = ActiveTurnRecoveryRecord.model_validate(raw)
                prior = previous.evidence.get('home_restart')
                if prior and (previous.original.turn_id == active.turn_id
                        and previous.original.execution_id == active.execution_id):
                    self.refused('home_restart_original_already_retained')
                if not fresh_chain and len(previous.prior) >= 20:
                    self.refused('home_restart_generation_bound_exceeded')
                if previous.outcome == 'blocked' and previous.action_state != 'applied':
                    self.refused('home_restart_prior_recovery_blocked')
                proposed.prior = () if fresh_chain else (*previous.prior, previous.model_dump(mode='json', exclude={'prior'}))
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
        if evidence.get('event_origin') is not None:
            self._validate_event_origin(evidence['event_origin'], actor, self.home_host.projects.get('home', actor.tenant))
        return saved, original, actor

    def event_terminal_context(self, active):
        """Current canonical chain for terminal accounting, never new input authority."""
        if (active.source or '').removeprefix('queued:') != 'restart-recovery:home':
            return None
        saved = self.store.get(active.thread_id)
        evidence = saved.evidence.get('home_restart') if saved else None
        if not evidence or evidence.get('event_origin') is None:
            return None
        proof = HomeRestartInputProof.model_validate(evidence['proof'])
        self._started_input(active, queued_id=evidence['queued_id'], proof=proof)
        assignment = self._assignment(active.assignment_id)
        actor = self.home_host.broker.identity.actor_for_identity(assignment.created_by,
            scope=TenantScope(organization_id=assignment.organization_id,workspace_id=assignment.workspace_id))
        project, operator = self.home_host.authorize(assignment, actor)
        if operator.revision != proof.selection_revision:
            self.refused('home_restart_selected_conversation_changed')
        run = self._validate_event_origin(evidence['event_origin'], actor, project, check_controls=False)
        originals = [ActiveThreadTurn.model_validate(row['original'])
            for row in (*saved.prior, saved.model_dump(mode='json'))
            if row.get('evidence', {}).get('home_restart', {}).get('event_origin') is not None]
        return run, tuple(originals)

    async def record_terminal(self, active, *, succeeded):
        import asyncio
        context = await asyncio.to_thread(self.event_terminal_context, active)
        if context is not None:
            await self.event_followup.record_restarted_terminal(active, succeeded=succeeded, context=context)

    def prior_event_turns(self, terminal):
        # Normal event terminals do not borrow a previous restart generation.
        saved = self.store.get(terminal.native_session_id)
        evidence = saved.evidence.get('home_restart') if saved else None
        if not evidence or evidence.get('event_origin') is None:
            return ()
        proof = HomeRestartInputProof.model_validate(evidence['proof'])
        for attempt in self._known_attempts(evidence.get('queued_id')):
            receipt = attempt.queued_start_receipt
            if receipt is None or receipt.provider_native_turn_id != terminal.native_turn_id:
                return ()
            queued = QueuedTurn.model_validate(attempt.queued_turn_snapshot)
            if queued.home_restart_input != proof:
                return ()
            assignment = self._assignment(terminal.assignment_id)
            active = ActiveThreadTurn(thread_id=terminal.native_session_id,turn_id=terminal.native_turn_id,
                project_id='home',source='restart-recovery:home',assignment_id=terminal.assignment_id,
                execution_id=terminal.execution_id,worker_id=terminal.worker_id,fence=terminal.fence,
                execution_workspace_id=assignment.execution_workspace_id,
                agent_profile_actor_id=assignment.created_by,started_at=terminal.observed_at,updated_at=terminal.observed_at)
            context = self.event_terminal_context(active)
            return context[1] if context is not None else ()
        self.refused('home_restart_terminal_start_unavailable')

    def input_context(self, queued, *, attempt_id, claim_id):
        if isinstance(queued.home_restart_input, HomeUpgradeInputProof):
            if self.planned_restart is None:
                self.refused('home_upgrade_recovery_unavailable')
            return self.planned_restart.input_context(queued, attempt_id=attempt_id, claim_id=claim_id)
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
        return home_restart_context(authorizer)
