"""Planned Home continuity through the EXISTING external Upgrade supervisor.

Internal composition only: no restart service, signal, model loop or API identity
is introduced. The supervisor must hold its ordinary canonical Upgrade authority.
"""
import hashlib
import json
import math
import time
try:
    from psycopg.errors import QueryCanceled as _HomeQueryCanceled
except ImportError:  # SQLite-only installations have no PostgreSQL exception type.
    _HomeQueryCanceled = ()
from pydantic import BaseModel, ConfigDict, Field
from codex_web.services.home_checkpoint_telemetry import checkpoint_stage, checkpoint_timings
from codex_web.agent_profiles import AgentProfileExecutionBinding
from codex_web.artifact_evidence import ArtifactCreate, ArtifactDigest, EvidenceCreate
from codex_web.definitions import DefinitionReference
from codex_web.home_restart import HomeUpgradeInputProof, home_restart_context
from codex_web.identity import TenantScope
from codex_web.services.home_operator_host import HOST_INSPECTION
from codex_web.services.artifact_evidence import ArtifactNotFoundError, _ArtifactDecodeBeforeCreation


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


_UNOBSERVED_SELECTION = object()


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
    def __init__(self, *, restart_recovery, upgrades, artifacts, source_revision,
                 runner_observer=None, goal_inspection=None, sessions=None):
        self.recovery, self.upgrades, self.artifacts = restart_recovery, upgrades, artifacts
        self.home = restart_recovery.home_host
        self.source_revision = source_revision
        self.runner_observer, self.goal_inspection, self.sessions = runner_observer, goal_inspection, sessions

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
        if self._dormant_artifact(plan_id, requester) is not None:
            self.refused('home_upgrade_preservation_ambiguous')
        if ((plan.organization_id, plan.workspace_id) != (a.organization_id, a.workspace_id)
                or plan.status not in {'draft','ready','preflight_blocked'} or plan.maintenance_mode or plan.completed_at is not None
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
            idempotency_key=plan.id)

    def _selected_host(self, operator_actor, *, _expected_selection=_UNOBSERVED_SELECTION):
        binding = self.home.projects.get('home', operator_actor.tenant).home_operator
        if _expected_selection is not _UNOBSERVED_SELECTION and binding != _expected_selection:
            self.refused('home_upgrade_selected_navigation_changed')
        if binding is None or not binding.thread_id or not binding.agent_profile_id:
            return None
        requester = self.home.broker.identity.actor_for_identity(binding.selected_by, scope=operator_actor.tenant)
        profiles = self.home.broker.execution_profiles
        selected,_ = profiles.agent_profiles.resolve_for_execution(binding.agent_profile_id,actor=requester,project_id='home')
        if selected.revision != binding.agent_profile_revision:
            self.refused('home_upgrade_selected_profile_changed')
        profile,_ = profiles.resolve(selected.execution_profile_id,
            organization_id=requester.organization_id,workspace_id=requester.workspace_id,project_id='home')
        if HOST_INSPECTION.id not in profile.control_plane_operations:
            return None
        return binding,requester

    def _operation_held_observer(self):
        """Reuse one full original-ledger read only while its canonical revision holds.

        This observation lives for one preservation operation, never a retry or
        resume. Missing/changed revisions use the original full query. Every
        other snapshot observation, including retained intent metadata, stays
        fresh. Revision tracking belongs to the existing canonical StateStore
        writers; direct database changes outside that contract are not covered.
        """
        repository = self.recovery.intents.store
        state, namespace = repository.store, repository.records_namespace
        reader = getattr(state, 'namespace_revision', None)
        observed = None

        def revision():
            value = reader(namespace) if callable(reader) else None
            if type(value) not in (int, float):
                return None
            try:
                return value if math.isfinite(value) and value > 0 else None
            except OverflowError:
                return None

        def held(execution_ids, *, actor):
            nonlocal observed
            if (self.recovery.intents.store is not repository or repository.store is not state
                    or repository.records_namespace != namespace):
                observed = None
                return self.recovery._held(execution_ids, actor=actor)
            scope = (tuple(sorted(execution_ids)), actor.organization_id,
                     actor.workspace_id, actor.identity_id)
            before = revision()
            if before is not None and observed is not None and observed[:2] == (scope, before):
                return observed[2]
            observed = None
            result = self.recovery._held(execution_ids, actor=actor)
            after = revision()
            if before is not None and before == after:
                observed = (scope, after, result)
            return result

        return held

    def _dormant_snapshot(self, binding, requester, *, require_process=True, process_evidence=None,
                          held_observer=None):
        """Supervisor metadata only; never a Host grant or native completion."""
        from codex_web.storage.state_store import check_operation_budget
        with checkpoint_stage('custody'):
            bootstrap = self.home.bootstrap_bindings.get_by_thread(binding.thread_id, requester)
            assignment = self.recovery._assignment(bootstrap.assignment_id)
        # This existing metadata validator retains current Human/Admin/Profile,
        # tenant, authority and selected bootstrap checks. Host.guard remains
        # unchanged and still requires a live lease for every Host operation.
        with checkpoint_stage('custody_authorization'):
            _, selected = self.home._authorize_context(assignment, requester)
        if selected != binding or assignment.status != 'lost' or assignment.lease is not None:
            self.refused('home_upgrade_dormant_custody_changed')
        if self.runner_observer is None or self.goal_inspection is None or self.sessions is None:
            self.refused('home_upgrade_dormant_observation_unavailable')
        observed = self.runner_observer(binding.thread_id)
        if not isinstance(observed, dict) or (observed.get('assignmentId'), observed.get('fence')) != (assignment.id, assignment.fence):
            self.refused('home_upgrade_dormant_observation_changed')
        if (observed.get('assignmentStatus') != 'lost' or observed.get('live') is not False
                or observed.get('leaseValid') is not False):
            self.refused('home_upgrade_dormant_custody_changed')
        if require_process:
            if (observed.get('processRunning') is False
                    and type(observed.get('pendingExecutorWork')) is int
                    and observed['pendingExecutorWork'] == 0):
                proof = {'state':'stopped_observed', 'process_running':False, 'pending_executor_work':0}
            else:
                ownership = observed.get('processOwnershipObservation')
                if (observed.get('processRunning') is not None or observed.get('pendingExecutorWork') is not None
                        or not isinstance(ownership, dict)
                        or ownership.get('schema_version') != '1.0' or ownership.get('state') != 'unowned_lost'
                        or (ownership.get('assignment_id'), ownership.get('fence'), ownership.get('worker_id'),
                            ownership.get('execution_id'), ownership.get('execution_workspace_id')) !=
                           (assignment.id, assignment.fence, assignment.assigned_worker_id,
                            assignment.execution_id, assignment.execution_workspace_id)
                        or type(ownership.get('manager_started_at')) not in (int,float)
                        or not assignment.updated_at < ownership['manager_started_at'] <= time.time()):
                    self.refused('home_upgrade_dormant_process_unsettled')
                proof = {'state':'unowned_lost', 'process_running':None, 'pending_executor_work':None,
                    'manager_started_at':ownership['manager_started_at']}
            if process_evidence is not None:
                process_evidence.update(proof)
        elif ((observed.get('processRunning') is not None and observed.get('processRunning') is not False)
                or (observed.get('pendingExecutorWork') is not None and
                    (type(observed['pendingExecutorWork']) is not int or observed['pendingExecutorWork'] != 0))):
            # After a COMPLETED Upgrade the old session manager may no longer
            # exist. Omission is not new process-zero proof; the original stopped
            # observation remains historical and this path grants NO input.
            self.refused('home_upgrade_dormant_process_unsettled')
        if self.recovery.execution.thread_is_active(binding.thread_id):
            self.refused('home_upgrade_dormant_active_input')
        queues = self.recovery.execution.host._thread_queue(binding.thread_id)
        if queues:
            self.refused('home_upgrade_dormant_pending_input')
        with checkpoint_stage('goals'):
            goals = self.goal_inspection.inspect(requester, expected_revision=binding.revision)
        if goals['items'] or goals['has_more']:
            self.refused('home_upgrade_dormant_goal_present')
        runtime = assignment.runtime_binding
        if runtime is None:
            self.refused('home_upgrade_dormant_session_unavailable')
        with checkpoint_stage('session'):
            session = self.sessions.find_unique_by_native_id(binding.thread_id,
                provider_id=runtime.provider_id, runtime_id=runtime.runtime_id)
        if (session is None or tuple(session.resource_ids) != tuple(assignment.resource_ids)
                or (session.organization_id, session.workspace_id, session.project_id,
                session.assignment_id, session.execution_id, session.execution_workspace_id, session.worker_id) !=
                (assignment.organization_id, assignment.workspace_id, 'home', assignment.id,
                 assignment.execution_id, assignment.execution_workspace_id, assignment.assigned_worker_id)):
            self.refused('home_upgrade_dormant_session_changed')
        with checkpoint_stage('held'):
            held = (self.recovery._held if held_observer is None else held_observer)(
                {assignment.execution_id}, actor=requester)
        retained = []
        for intent_id, outcome in held:
            intent = self.recovery.intents.get(intent_id, requester)
            if intent.status not in {'succeeded', 'failed', 'cancelled', 'expired'}:
                self.refused('home_upgrade_dormant_action_unsettled')
            # Terminal cancellation is not an outcome correction: original
            # UNKNOWN receipts remain retained and cannot authorize execution.
            retained.append({'id':intent.id, 'status':str(intent.status),
                'last_receipt_id':intent.last_receipt_id, 'outcome':outcome})
        check_operation_budget()
        return json.loads(encoded({'schema_version':'1.0', 'thread_id':binding.thread_id,
            'selection':binding.model_dump(mode='json'),
            'assignment':{key:getattr(assignment,key) for key in ('id','fence','execution_id',
                'execution_workspace_id','assigned_worker_id','organization_id','workspace_id','project_id')},
            'session':{key:getattr(session,key) for key in ('id','provider_id','runtime_id',
                'provider_native_session_id','assignment_id','execution_id','execution_workspace_id','worker_id',
                'model','model_class','capability_revision','resource_ids','status','recovery_attempts',
                'last_recovered_at','created_at','updated_at')},
            'retained_queue_ids':[], 'goal_binding_ids':[], 'retained_actions':retained,
            'historical_native_outcome':'UNKNOWN', 'native_input_authorized':False}))

    def _dormant_artifact(self, plan_id, requester):
        return self.artifacts.find_idempotent_artifact(actor=requester,
            source='home-upgrade-dormant-preservation', idempotency_key=plan_id)

    def _preserve_dormant(self, plan_id, binding, requester, operator_actor):
        # This boundary reports incomplete preservation, never absence of effects.
        # A final observation can fail after the real Artifact writer committed.
        phase = 'plan_validation'
        def observing(value):
            nonlocal phase
            phase = value
        try:
            with checkpoint_stage('preservation'):
                return self._preserve_dormant_observed(plan_id, binding, requester,
                    operator_actor, observing=observing)
        except Exception as exc:
            if isinstance(exc, TimeoutError):
                category = 'TimeoutError'
            elif isinstance(exc, _HomeQueryCanceled):
                category = 'QueryCanceled'
            else:
                raise
            from codex_web.runtime.execution import ExecutionPrestartRefusal
            raise ExecutionPrestartRefusal(status_code=500, detail={
                'code':'home_upgrade_preservation_incomplete',
                'message':'Home preservation did not complete. Inspect the original Upgrade and Artifact evidence before another attempt.',
                'phase':phase, 'exception_type':category,
                'preservation_outcome':'unknown', 'retryable':False}) from exc

    def _preserve_dormant_observed(self, plan_id, binding, requester, operator_actor, *, observing):
        from codex_web.storage.home_intent_query import recovery_query_budget
        with recovery_query_budget():
            plan = self.upgrades.get(plan_id, actor=operator_actor)
            if (plan.maintenance_mode or plan.status not in {'draft','ready','preflight_blocked'}
                    or plan.completed_at is not None or plan.post_upgrade_evidence_id is not None
                    or (plan.organization_id,plan.workspace_id) != (requester.organization_id,requester.workspace_id)):
                self.refused('home_upgrade_dormant_requires_new_plan')
            release = self.upgrades.releases.get(plan.release_id, actor=operator_actor)
            source, target = self.source_revision(), release.build.source_revision
            if not source or not target or release.version != plan.target_app_version:
                self.refused('home_upgrade_source_unavailable')
            # Observe current metadata outside Artifact/Governance locks. These
            # canonical owners may initialize their own readiness/migration rows;
            # neither observation grants input or changes retained outcomes.
            process = {}
            held_observer = self._operation_held_observer()
            observing('initial_snapshot')
            with checkpoint_stage('initial_snapshot'):
                snapshot = self._dormant_snapshot(binding, requester, process_evidence=process,
                    held_observer=held_observer)
            preservation = {'schema_version':'1.0', 'plan_id':plan.id, 'plan_created_at':plan.created_at,
                'release_id':plan.release_id, 'source_revision':source, 'target_revision':target,
                'target_version':plan.target_app_version, 'snapshot':snapshot,
                'stopped_process_observed':True, 'pending_executor_work_observed':0}
            if process['state'] == 'unowned_lost':
                preservation.update(schema_version='1.1', stopped_process_observed=None,
                    pending_executor_work_observed=None, process_ownership=process)
            raw = encoded(preservation)
            def prior_or_refuse(checkpoint, prior):
                if checkpoint is not None:
                    self.refused('home_upgrade_preservation_ambiguous')
                if prior is not None:
                    if (prior.lifecycle != 'active' or prior.source != 'home-upgrade-dormant-preservation'
                            or prior.external_id != plan.id or prior.project_id != 'home'
                            or prior.producer_identity_id != requester.identity_id
                            or prior.retention_expires_at is not None and prior.retention_expires_at <= time.time()
                            or prior.metadata.get('dormant_preservation') != raw
                            or prior.digest is None or prior.digest.value != sha(raw)):
                        self.refused('home_upgrade_dormant_preservation_changed')
                return prior

            def catalog_guard(current):
                found = []
                for kind in ('home-upgrade-checkpoint', 'home-upgrade-dormant-preservation'):
                    key = self.artifacts._idempotent_evidence_id(requester, kind, plan_id).replace(
                        'evidence-', 'artifact-', 1)
                    try:
                        found.append(self.artifacts.get_artifact(key, actor=requester, state=current))
                    except ArtifactNotFoundError:
                        found.append(None)
                prior = prior_or_refuse(*found)
                # Ordinary creation's generic idempotency lookup is unscoped.
                # A corrupt foreign-only collision must never become reuse;
                # a valid scoped prior still wins even if a foreign row is first.
                if prior is None and any(item.id == key for item in current.artifacts):
                    self.refused('home_upgrade_dormant_preservation_changed')
                return prior

            observing('before_artifact_snapshot')
            with checkpoint_stage('before_artifact_snapshot'):
                if self._dormant_snapshot(binding, requester, held_observer=held_observer) != snapshot:
                    self.refused('home_upgrade_dormant_preservation_changed')
            observing('artifact_write')
            try:
                with checkpoint_stage('artifact_write'):
                    artifact = self.artifacts.create_artifact(ArtifactCreate(project_id='home',
                        execution_id=snapshot['assignment']['execution_id'],
                        execution_workspace_id=snapshot['assignment']['execution_workspace_id'],
                        artifact_type='report', name=('Unowned LOST Home preserved; process/executor UNKNOWN; no continuation'
                            if process['state'] == 'unowned_lost' else
                            'Dormant Home preserved; native outcome UNKNOWN; no continuation'),
                        provider='codex-web', source='home-upgrade-dormant-preservation', external_id=plan.id,
                        digest=ArtifactDigest(value=sha(raw)), deny_model_context=True,
                        metadata={'dormant_preservation':raw}), actor=requester, idempotency_key=plan.id, _catalog_guard=catalog_guard)
            except _ArtifactDecodeBeforeCreation as refusal:
                # Only the explicitly typed pre-write Artifact domain decoder
                # can enter this compatibility path. Guard/updater/DB/export
                # errors never use a second read or become successful reuse.
                observing('artifact_fallback_lookup')
                checkpoint, prior = self.artifacts.find_idempotent_artifacts(actor=requester, requests=(
                    ('home-upgrade-checkpoint', plan_id),
                    ('home-upgrade-dormant-preservation', plan_id)))
                artifact = prior_or_refuse(checkpoint, prior)
                if artifact is None:
                    raise refusal.original
            observing('final_snapshot')
            with checkpoint_stage('final_snapshot'):
                if self._dormant_snapshot(binding, requester, held_observer=held_observer) != snapshot:
                    self.refused('home_upgrade_dormant_preservation_changed')
            return artifact

    def checkpoint_for_service_drain(self, plan_id, *, worker_runtime, operator_actor):
        """Best-effort independent Home metadata; never global drain authority.

        Only the synchronous owner may classify its settled typed refusal. No
        second Artifact attempt, recovery input or readiness grant follows it.
        """
        from codex_web.runtime.execution import ExecutionPrestartRefusal
        from codex_web.storage.home_intent_query import recovery_query_budget
        from codex_web.upgrades import HomeCheckpointDisposition
        # Upgrade authority is mandatory and is not an optional Home refusal.
        self._operator(operator_actor)
        observed = {}
        try:
            with recovery_query_budget():
                binding = self.home.projects.get('home', operator_actor.tenant).home_operator
                if binding is not None:
                    observed['selection'] = binding
                    # Hash-only diagnostic identity, not an authenticated actor or grant.
                    # The existing checkpoint owner performs live selected-owner checks.
                    requester = operator_actor.model_copy(update={'identity_id': binding.selected_by})
                    for source, field in (("home-upgrade-checkpoint", "checkpoint_artifact_id"),
                            ("home-upgrade-dormant-preservation", "preservation_artifact_id")):
                        observed[field] = self.artifacts._idempotent_evidence_id(
                            requester, source, plan_id).replace('evidence-', 'artifact-', 1)
                    if not self.upgrades._pin_home_checkpoint_observation(plan_id, operator_actor, observed):
                        return HomeCheckpointDisposition(outcome="deferred_unknown",
                            reason_code="home_checkpoint_original_outcome_unknown", **observed)
                artifact = self.checkpoint_current(plan_id, worker_runtime=worker_runtime,
                    operator_actor=operator_actor, _expected_selection=binding)
                if artifact is None:
                    return HomeCheckpointDisposition(outcome="not_applicable", **observed)
                return HomeCheckpointDisposition(outcome="recorded",
                    operation="dormant_preservation" if artifact.source == "home-upgrade-dormant-preservation" else "checkpoint",
                    recorded_artifact_id=artifact.id, **observed)
        except ExecutionPrestartRefusal as error:
            detail = error.detail
            code = detail.get('code') if isinstance(detail, dict) else None
            if (not isinstance(code, str) or not code.startswith(('home_upgrade_', 'home_restart_'))
                    or not code.replace('_', '').isalnum() or len(code) > 125):
                raise
            unknown = code == 'home_upgrade_preservation_incomplete'
            fields = {}
            if unknown:
                # Closed fields originate in this owner's preservation wrapper;
                # neither exception prose nor an arbitrary HTTP500 is consumed.
                fields = {'phase': detail['phase'], 'exception_type': detail['exception_type']}
            return HomeCheckpointDisposition(outcome="deferred_unknown" if unknown else "blocked",
                reason_code=code, operation="dormant_preservation" if unknown else "unresolved",
                **fields, **observed)
        except TimeoutError:
            return HomeCheckpointDisposition(outcome="deferred_unknown",
                reason_code="home_upgrade_checkpoint_budget_incomplete",
                phase="checkpoint_current", exception_type="TimeoutError", **observed)
        except _HomeQueryCanceled:
            return HomeCheckpointDisposition(outcome="deferred_unknown",
                reason_code="home_upgrade_checkpoint_query_incomplete",
                phase="checkpoint_current", exception_type="QueryCanceled", **observed)

    def checkpoint_current(self, plan_id, *, worker_runtime, operator_actor,
                           _expected_selection=_UNOBSERVED_SELECTION):
        with checkpoint_timings(operator_actor), checkpoint_stage('checkpoint_current'):
            return self._checkpoint_current_observed(plan_id, worker_runtime=worker_runtime,
                operator_actor=operator_actor, _expected_selection=_expected_selection)

    def _checkpoint_current_observed(self, plan_id, *, worker_runtime, operator_actor,
                                     _expected_selection=_UNOBSERVED_SELECTION):
        """Existing supervisor boundary: typed denial, never a lease bypass."""
        from codex_web.services.control_plane_broker import ControlPlaneBrokerDeniedError
        from codex_web.services.agent_profiles import AgentProfileError
        from codex_web.services.home_operator import HomeOperatorConflict
        from codex_web.services.identity import IdentityError
        self._operator(operator_actor)
        try:
            return self._checkpoint_current(plan_id, worker_runtime=worker_runtime, operator_actor=operator_actor,
                _expected_selection=_expected_selection)
        except HomeOperatorConflict:
            self.refused('home_upgrade_selection_changed')
        except (ControlPlaneBrokerDeniedError, AgentProfileError, IdentityError):
            self.refused('home_upgrade_current_authority_unavailable')

    def _checkpoint_current(self, plan_id, *, worker_runtime, operator_actor,
                            _expected_selection=_UNOBSERVED_SELECTION):
        """Unconfigured Home is a no-op; live Host authority stays canonical."""
        selected = self._selected_host(operator_actor, _expected_selection=_expected_selection)
        if selected is None:
            return None
        binding,requester = selected
        bootstrap = self.home.bootstrap_bindings.get_by_thread(binding.thread_id, requester)
        assignment = self.recovery._assignment(bootstrap.assignment_id)
        if assignment.status == 'lost':
            return self._preserve_dormant(plan_id, binding, requester, operator_actor)
        if self._dormant_artifact(plan_id, requester) is not None:
            self.refused('home_upgrade_preservation_ambiguous')
        # Only the ALREADY configured registered worker is usable here. No
        # identity construction, new token or arbitrary worker impersonation.
        if (worker_runtime is None or worker_runtime.worker is None or worker_runtime.worker_actor is None
                or assignment.assigned_worker_id != worker_runtime.worker.id):
            self.refused('home_upgrade_configured_worker_unavailable')
        return self.checkpoint(plan_id, snapshot=assignment, worker_actor=worker_runtime.worker_actor,
            operator_actor=operator_actor)

    def resume_completed(self, plan_id, *, operator_actor):
        self._operator(operator_actor)
        plan = self.upgrades.get(plan_id, actor=operator_actor)
        disposition = plan.home_checkpoint_disposition
        if disposition is not None and disposition.outcome in {"pending_unknown", "deferred_unknown", "blocked"}:
            # Even a late committed optional Artifact is not continuation proof.
            # Preserve the independent warning; never retry, fetch-and-promote,
            # clear navigation or enqueue Home from this diagnostic.
            return None
        selected = self._selected_host(operator_actor)
        if selected is None:
            return None
        binding,requester = selected
        artifact = self.artifacts.find_idempotent_artifact(actor=requester,
            source='home-upgrade-checkpoint', idempotency_key=plan_id)
        dormant = self._dormant_artifact(plan_id, requester)
        if dormant is not None:
            if artifact is not None:
                self.refused('home_upgrade_preservation_ambiguous')
            raw = dormant.metadata.get('dormant_preservation')
            saved = json.loads(raw) if isinstance(raw,str) else None
            plan = self.upgrades.get(plan_id, actor=operator_actor)
            if (saved is None or dormant.lifecycle != 'active' or dormant.external_id != plan_id
                    or dormant.source != 'home-upgrade-dormant-preservation'
                    or dormant.retention_expires_at is not None and dormant.retention_expires_at <= time.time()
                    or dormant.digest is None or dormant.digest.value != sha(raw)
                    or saved['schema_version'] not in {'1.0','1.1'} or saved['plan_id'] != plan.id
                    or saved['plan_created_at'] != plan.created_at or saved['release_id'] != plan.release_id
                    or plan.status != 'completed' or plan.maintenance_mode
                    or plan.post_upgrade_evidence_id is None or self.source_revision() != saved['target_revision']
                    or plan.target_app_version != saved['target_version']):
                self.refused('home_upgrade_dormant_completion_changed')
            from types import SimpleNamespace
            actual, completion = self._completed(SimpleNamespace(**saved), operator_actor)
            if (actual.completed_at < dormant.created_at or completion.observed_at < dormant.created_at
                    or dormant.project_id != 'home' or dormant.producer_identity_id != requester.identity_id
                    or dormant.execution_id != saved['snapshot']['assignment']['execution_id']):
                self.refused('home_upgrade_dormant_completion_changed')
            if self._dormant_snapshot(binding,requester,require_process=False) != saved['snapshot']:
                self.refused('home_upgrade_dormant_preservation_changed')
            return None  # Explicit preservation disposition, NEVER a native continuation.
        if artifact is None:
            self.refused('home_upgrade_checkpoint_missing_for_completed_plan')
        return self.resume(artifact.id, operator_actor=operator_actor)

    def _checkpoint(self, checkpoint_id, actor):
        artifact = self.artifacts.get_artifact(checkpoint_id, actor=actor)
        raw = artifact.metadata.get('checkpoint')
        if (artifact.project_id != 'home' or artifact.source != 'home-upgrade-checkpoint'
                or artifact.lifecycle != 'active' or artifact.retention_expires_at is not None and artifact.retention_expires_at <= time.time()
                or not isinstance(raw, str)
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
        diagnostic = plan.home_checkpoint_disposition
        if diagnostic is not None and diagnostic.outcome in {'pending_unknown', 'deferred_unknown', 'blocked'}:
            self.refused('home_upgrade_checkpoint_diagnostic_unavailable')
        if (plan.created_at != checkpoint.plan_created_at or plan.release_id != checkpoint.release_id
                or plan.target_app_version != checkpoint.target_version or plan.status != 'completed'
                or plan.maintenance_mode or plan.completed_at is None
                or plan.deployment_source_revision != checkpoint.source_revision
                or plan.deployment_target_revision != checkpoint.target_revision
                or self.source_revision() != checkpoint.target_revision or not plan.post_upgrade_evidence_id):
            self.refused('home_upgrade_completion_unavailable')
        evidence = self.artifacts.get_evidence(plan.post_upgrade_evidence_id, actor=actor)
        if (evidence.lifecycle != 'valid' or evidence.result != 'pass'
                or evidence.retention_expires_at is not None and evidence.retention_expires_at <= time.time()
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
                'checkpoint_sha256':artifact.digest.value,'message_sha256':sha(message)}), actor=actor, idempotency_key=artifact.id)
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

    def _validate_proof(self, queued, attempt):
        proof = queued.home_restart_input
        if not isinstance(proof, HomeUpgradeInputProof):
            self.refused('home_upgrade_retained_proof_unavailable')
        actor = self.home.broker.identity.actor_for_identity(proof.requested_by,
            scope=TenantScope(organization_id=attempt.organization_id,workspace_id=attempt.workspace_id))
        artifact, checkpoint = self._checkpoint(proof.checkpoint_id,actor)
        plan, completion = self._completed(checkpoint,actor)
        if plan.completed_at < artifact.created_at or completion.observed_at < artifact.created_at:
            self.refused('home_upgrade_old_completion_cannot_resume')
        actor = self._requester(checkpoint)
        receipt = self.artifacts.get_evidence(proof.resume_evidence_id,actor=actor)
        if (proof.checkpoint_sha256 != artifact.digest.value or proof.upgrade_plan_id != plan.id
                or proof.completion_evidence_id != completion.id or proof.requested_by != checkpoint.requester_id
                or queued.thread_id != checkpoint.thread_id
                or proof.thread_id != checkpoint.thread_id or queued.source != 'upgrade-recovery:home'
                or queued.project_id != 'home' or queued.execution_id != 'home-upgrade-'+artifact.id
                or queued.canonical_ingress_id is not None or queued.agent_profile_actor_id != actor.identity_id
                or sha(queued.message) != proof.message_sha256 or receipt.lifecycle != 'valid'
                or receipt.source != 'home-upgrade-resume' or receipt.result != 'info'
                or receipt.artifact_ids != (artifact.id,) or receipt.producer_identity_id != actor.identity_id
                or receipt.metadata.get('upgrade_plan_id') != plan.id
                or receipt.metadata.get('completion_evidence_id') != completion.id
                or receipt.metadata.get('checkpoint_sha256') != artifact.digest.value
                or receipt.metadata.get('message_sha256') != proof.message_sha256):
            self.refused('home_upgrade_retained_input_changed')
        return checkpoint,actor
    def validate_started_origin(self, active, queued, attempt):
        """Read-only provenance for a genuinely STARTED planned continuation.

        Returns (checkpoint, fresh requester). No new input or replay is
        authorized: current restart recovery separately owns its retained claim.
        """
        current = self.recovery.preflight.store.get(attempt.id)
        if current != attempt or current.status != 'started':
            self.refused('home_upgrade_started_origin_unavailable')
        receipt, correlation, evidence = (current.queued_start_receipt,
            current.queued_start_correlation, current.queued_start_evidence)
        if (receipt is None or correlation is None or evidence is None
                or current.queued_turn_snapshot is None
                or {k:v for k,v in current.queued_turn_snapshot.items() if k != 'attempts'} !=
                   {k:v for k,v in queued.model_dump(mode='json').items() if k != 'attempts'}
                or active.source not in {'upgrade-recovery:home','queued:upgrade-recovery:home'}
                or (active.thread_id,active.turn_id,active.execution_id) !=
                   (receipt.provider_native_session_id,receipt.provider_native_turn_id,receipt.execution_id)
                or receipt.thread_id != queued.thread_id or receipt.requested_execution_id != queued.execution_id
                or receipt.admission_claim_id != current.queued_admission_claim_id
                or (correlation.assignment_id,correlation.worker_id,correlation.fence,correlation.execution_id) !=
                   (active.assignment_id,active.worker_id,active.fence,active.execution_id)
                or correlation.admission_claim_id != receipt.admission_claim_id
                or correlation.requested_execution_id != queued.execution_id
                or (correlation.runtime_generation,correlation.rpc_request_id) !=
                   (evidence.runtime_generation,evidence.rpc_request_id)
                or (evidence.provider_native_session_id,evidence.provider_native_turn_id) !=
                   (receipt.provider_native_session_id,receipt.provider_native_turn_id)):
            self.refused('home_upgrade_started_origin_changed')
        return self._validate_proof(queued,current)

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
            return service._validate_proof(queued, attempt)
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
