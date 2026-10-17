"""Assignment-bound adapter for existing owner-proof Verification records.

Requester authority and authenticated worker attribution are deliberately
different. This module creates no principal, grant, Evidence or recovery effect.
"""
import base64
import hashlib
import re

from codex_web.api.authorization import ApiAuthorizationService, ApiAuthorizationError
from codex_web.artifact_evidence import VerificationCreate, VerificationResult
from codex_web.control_plane_broker import ControlPlaneBrokerOperation
from codex_web.authority import AuthorityLevel
from codex_web.data_governance import ContextFilterRequest
from codex_web.execution_workers import AssignmentStatus, WorkerLifecycle
from codex_web.identity import PrincipalKind
from codex_web.recovery import RecoveryReconcileRequest
from codex_web.services.artifact_evidence import ArtifactEvidenceError
from codex_web.services.recovery import RecoveryConflictError
from codex_web.services.recovery_reconciliation import PROOF_CONTRACT, fingerprint, validate_proof
from codex_web.storage.recovery import RecoveryStore
from codex_web.storage.scheduler import SchedulerStore
from pydantic import ValidationError


OPERATIONS = tuple(ControlPlaneBrokerOperation(id=name, method=method,
    path_template=path, capability=capability, authority_level=level)
    for name, method, path, capability, level in (
        ('verification.create', 'POST', '/api/verifications', 'api.verifications.write', AuthorityLevel.EXECUTE),
        ('verification.read', 'GET', '/api/verifications', 'api.verifications.read', AuthorityLevel.READ),
        ('verification.evidence.read', 'GET', '/api/evidence/{id}', 'api.evidence.read', AuthorityLevel.READ),
        ('verification.artifact.read', 'GET', '/api/artifacts/{id}', 'api.artifacts.read', AuthorityLevel.READ),
        ('verification.content.read', 'GET', '/api/artifacts/{id}/content', 'api.artifacts.read', AuthorityLevel.READ),
    ))


def resolve(method, path):
    if path == '/api/verifications':
        return next(((o, None) for o in OPERATIONS if o.path_template == path and o.method == method), None)
    match = re.fullmatch(r'/api/(evidence|artifacts)/([A-Za-z0-9._:-]{1,200})(/content)?', path)
    if match is None or method != 'GET' or match[1] == 'evidence' and match[3]:
        return None
    name = ('verification.content.read' if match[3] else
            'verification.evidence.read' if match[1] == 'evidence' else 'verification.artifact.read')
    return next(o for o in OPERATIONS if o.id == name), match[2]


class WorkerVerificationAdapter:
    def __init__(self, broker):
        self.broker = broker

    def denied(self, reason):
        from codex_web.services.control_plane_broker import ControlPlaneBrokerDeniedError
        raise ControlPlaneBrokerDeniedError(reason)

    def service(self):
        service = self.broker.action_intents.artifact_evidence if self.broker.action_intents else None
        if service is None:
            self.denied('Canonical Artifact/Evidence service unavailable')
        return service

    def scope(self, assignment, ref):
        if not ref:
            self.denied('Verification requires an exact Work Item reference')
        state = self.broker._state_for_target(assignment, ref)
        pin = assignment.agent_profile
        if pin is None or state.current_owner != pin.profile_id:
            self.denied('Verification requires current canonical Work Item ownership')
        if not set(state.resource_ids).issubset(set(assignment.resource_ids)):
            self.denied('Current Work Item resource scope changed')
        workspaces = self.broker.execution_workspaces
        if workspaces is None or not assignment.execution_workspace_id:
            self.denied('Current canonical execution workspace required')
        requester = self.broker._requester_actor(assignment)
        workspace = workspaces.get(assignment.execution_workspace_id, requester)
        lease = workspaces.store.lease(workspace.lease_id)
        if (workspace.status != 'active' or workspace.execution_id != assignment.execution_id
                or workspace.owner_identity_id != requester.identity_id
                or lease is None or lease.released_at is not None
                or lease.expires_at <= self.broker._clock()
                or lease.execution_workspace_id != workspace.id
                or lease.execution_id != assignment.execution_id
                or lease.subject != workspace.subject
                or lease.owner_identity_id != requester.identity_id
                or lease.resource_modes.get(self.broker._writable_repository_id(assignment)) != 'write'):
            self.denied('Verification workspace or writable lease changed')
        recovery = workspace.publication_recovery
        if recovery is not None:
            workspaces.publication_recovery(workspace.id,
                self.broker._writable_repository_id(assignment), actor=requester)
        expected = workspace.work_item_ref or (recovery.work_item_ref if recovery else None)
        if expected != ref or (recovery and (recovery.assignment_id != assignment.id
                or recovery.assignment_fence != assignment.fence
                or recovery.expires_at <= self.broker._clock())):
            self.denied('Verification Work Item is outside current assignment recovery scope')
        return state

    def visible(self, record, assignment, actor):
        self.scope(assignment, record.work_item_ref)
        if (getattr(record, 'project_id', None) not in (None, assignment.project_id)
                or not set(record.resource_ids).issubset(set(assignment.resource_ids))):
            self.denied('Original record is outside assignment Project/resource scope')
        if record.retention_expires_at is not None and record.retention_expires_at <= self.broker._clock():
            self.denied('Original record has expired')
        if record.governance_record_id is not None:
            governance = self.service().governance
            if governance is None or governance.filter_context(ContextFilterRequest(
                    record_ids=(record.governance_record_id,)), actor=actor).denied_record_ids:
                self.denied('Original record is unavailable to worker context')

    def principals(self, assignment, worker_actor, resolved, validator, payload):
        if validator is None:
            self.denied('Authenticated assignment validation is required')
        current = validator()
        if (current.id, current.fence, current.execution_id, current.assigned_worker_id) != (
                assignment.id, assignment.fence, assignment.execution_id, assignment.assigned_worker_id):
            self.denied('Verification assignment incarnation changed')
        if (current.agent_profile != assignment.agent_profile
                or current.execution_profile_definition != assignment.execution_profile_definition
                or current.execution_profile_id != assignment.execution_profile_id
                or current.project_id != assignment.project_id
                or current.resource_ids != assignment.resource_ids):
            self.denied('Verification assignment scope or pinned profiles changed')
        lease = current.lease
        if (lease is None or lease.fence != current.fence
                or lease.worker_id != current.assigned_worker_id
                or lease.expires_at <= self.broker._clock()
                or current.deadline_at is not None and current.deadline_at <= self.broker._clock()):
            self.denied('Verification assignment lease or deadline expired')
        worker, requester, _, decision = self.broker._dispatch_scope(current, worker_actor, resolved)
        if worker.service_token_id is not None:
            token = next((t for t in self.broker.identity.store.load().service_tokens
                if t.id == worker.service_token_id), None)
            if (token is None or token.revoked_at is not None
                    or token.expires_at is not None and token.expires_at <= self.broker._clock()
                    or token.service_identity_id != worker.identity_id
                    or (token.organization_id, token.workspace_id) !=
                        (worker.organization_id, worker.workspace_id)
                    or set(token.scopes) != set(worker.service_scopes)):
                self.denied('Authenticated worker token revoked, expired or rotated')
        if current.status != AssignmentStatus.RUNNING or worker.principal_kind != PrincipalKind.SERVICE:
            self.denied('Verification requires a live authenticated service worker')
        if self.broker.execution_workers is None:
            self.denied('Canonical worker registration unavailable')
        registered = self.broker.execution_workers.store.worker(current.assigned_worker_id)
        if (registered is None or registered.lifecycle != WorkerLifecycle.ACTIVE
                or registered.service_identity_id != worker.identity_id
                or (registered.organization_id, registered.workspace_id) !=
                    (worker.organization_id, worker.workspace_id)):
            self.denied('Current assigned worker identity or lifecycle changed')
        profiles = self.broker.execution_profiles
        if profiles is None or profiles.agent_profiles is None or current.agent_profile is None:
            self.denied('Current pinned execution/Agent Profile unavailable')
        _, reference = profiles.resolve(current.execution_profile_id,
            organization_id=current.organization_id, workspace_id=current.workspace_id,
            project_id=current.project_id)
        pin = current.agent_profile
        agent = profiles.agent_profiles.get(pin.profile_id, actor=requester)
        if (reference != current.execution_profile_definition or agent.lifecycle != 'active'
                or agent.record_id != pin.profile_record_id or agent.revision != pin.profile_revision
                or agent.execution_profile_id != pin.execution_profile_id
                or agent.role_id != pin.role_id or agent.authority_role_id != pin.authority_role_id
                or agent.authority_definition_ref != pin.authority_definition_ref):
            self.denied('Pinned execution/Agent Profile no longer effective')
        api_path = resolved.operation.path_template.replace('{id}', resolved.target_ref or '')
        try:
            ApiAuthorizationService(self.broker.authority).authorize_operation(
                resolved.operation.method, api_path, worker)
        except ApiAuthorizationError:
            self.denied('Current worker API policy refused this verification operation')
        procedures = self.broker.procedure_admission
        if procedures is None or not hasattr(procedures, 'inspect_current'):
            self.denied('Current canonical procedure admission unavailable')
        discovery = procedures.inspect_current(assignment=current, actor=requester,
            operation=resolved.operation, resource_ids=current.resource_ids, payload=payload)
        if not discovery.admitted or discovery.selected:
            self.denied('Verification requires current admitted unmediated procedure scope')
        return worker, requester, decision

    def guard_rows(self, assignment, ref):
        b, service = self.broker, self.service()
        store = service.store.store
        profiles = b.execution_profiles
        if profiles is None or b.execution_workers is None or b.execution_workspaces is None:
            self.denied('Canonical execution/profile stores unavailable')
        repositories = [b.identity.store, b.authority.registry.store, service.store,
            profiles.registry.store, b.action_intents.execution.resources.store]
        if profiles.agent_profiles is not None:
            repositories.append(profiles.agent_profiles.store)
        if service.governance is not None:
            repositories.append(service.governance.store)
        for repository in repositories:
            if repository.store is not store:
                self.denied('Verification admission requires one canonical transaction store')
        selected = {r.namespace: (None,) for r in repositories}
        work_items = getattr(b.work_items.state_machine.dependencies.get_state, '__self__', None)
        if work_items is None or getattr(work_items, 'store', None) is not store:
            self.denied('Canonical transactional Work Item repository required')
        work_items.get(ref)  # initialize an existing legacy collection before locks
        selected[work_items.namespace] = (ref,)
        workers, workspaces = b.execution_workers.store, b.execution_workspaces.store
        if workers.store is not store or workspaces.store is not store:
            self.denied('Execution custody stores differ from canonical Evidence store')
        workspace = workspaces.workspace(assignment.execution_workspace_id)
        if workspace is None:
            self.denied('Current execution workspace unavailable')
        selected.update({workers.assignment_namespace: (assignment.id,),
            workers.worker_namespace: (assignment.assigned_worker_id,),
            workspaces.workspace_namespace: (workspace.id,),
            workspaces.lease_namespace: (workspace.lease_id,),
            RecoveryStore.namespace: (None,), SchedulerStore.namespace: (None,)})
        security = b.procedure_admission.discovery.skills.security
        if security is not None:
            if security.store.state is not store:
                self.denied('Procedure security store differs from canonical store')
            for name in (security.store.SCANS, security.store.OVERRIDES, security.store.BASELINES):
                selected[name] = () if store.record_collection_exists(name) else (None,)
        return selected

    def create(self, assignment, worker_actor, resolved, payload, validator):
        service = self.service()
        try:
            request = VerificationCreate.model_validate(payload)
        except ValidationError:
            self.denied('Invalid VerificationCreate; caller identity/independence overrides are forbidden')
        if (request.method != PROOF_CONTRACT or not request.evidence_ids
                or len(request.evidence_ids) != 1 or len(request.artifact_ids) > 8
                or request.execution_id not in (None, assignment.execution_id)
                or not re.fullmatch(r'sha256:[0-9a-f]{64}', request.source or '')):
            self.denied('Exact owner-proof method, original Evidence and review digest required')
        self.scope(assignment, request.work_item_ref)
        original = service.get_evidence(request.evidence_ids[0], actor=self.broker._actor(assignment, worker_actor))
        # Referenced original artifacts participate in independence as well; a
        # caller cannot omit a B-produced linked artifact to manufacture it.
        artifact_ids = tuple(dict.fromkeys((*request.artifact_ids, *original.artifact_ids)))
        if len(artifact_ids) > 8:
            self.denied('Original proof exceeds bounded artifact review scope')
        request = request.model_copy(update={'execution_id': assignment.execution_id,
            'artifact_ids': artifact_ids})
        selected = self.guard_rows(assignment, request.work_item_ref)
        original_worker, _, _ = self.principals(assignment, worker_actor, resolved, validator, payload)
        attribution = {}
        def commit(updater):
            def apply(rows):
                worker, requester, decision = self.principals(
                    assignment, worker_actor, resolved, validator, payload)
                if worker != original_worker:
                    self.denied('Authenticated verifier changed before append')
                self.scope(assignment, request.work_item_ref)
                state = service.store._decode(rows[service.store.namespace][None])
                records = [service.get_evidence(i, actor=worker, state=state) for i in request.evidence_ids]
                records += [service.get_artifact(i, actor=worker, state=state) for i in request.artifact_ids]
                for record in records:
                    self.visible(record, assignment, worker)
                    if record.producer_identity_id != requester.identity_id:
                        self.denied('Owner proof must reference original requester-produced records')
                updated = updater(state)
                verification = updated.verifications[-1]
                if not verification.independent:
                    self.denied('Actual verifier is not independent of all original producers')
                proof = records[0]
                if not set(proof.artifact_ids).issubset(set(request.artifact_ids)):
                    self.denied('Original proof artifact references changed before append')
                recovery = RecoveryStore._decode(rows[RecoveryStore.namespace][None])
                schedules = SchedulerStore(service.store.store)._decode(rows[SchedulerStore.namespace][None])
                firing = recovery.scheduled_firings.get(proof.metadata.get('event_id'))
                schedule = schedules.schedules.get(firing.schedule_id) if firing is not None else None
                tenant = (worker.organization_id, worker.workspace_id)
                if (firing is None or schedule is None or recovery.policy is None
                        or (firing.organization_id, firing.workspace_id) != tenant
                        or (recovery.policy_organization_id, recovery.policy_workspace_id) != tenant
                        or (schedule.organization_id, schedule.workspace_id) != tenant):
                    self.denied('Original Recovery firing/schedule/policy unavailable in current tenant')
                snapshot = {'state_sha256': fingerprint(recovery), 'firing_sha256': fingerprint(firing),
                    'schedule_sha256': fingerprint(schedule),
                    'policy_fingerprint': recovery.policy.fingerprint()}
                if any(proof.metadata.get(k) != v for k, v in snapshot.items()):
                    self.denied('Original Recovery writer/effect proof snapshot changed')
                if request.result == VerificationResult.VERIFIED:
                    validate_proof(updated, RecoveryReconcileRequest(event_id=firing.event_id,
                        expected_state_sha256=snapshot['state_sha256'],
                        expected_firing_sha256=snapshot['firing_sha256'],
                        expected_schedule_sha256=snapshot['schedule_sha256'],
                        expected_policy_fingerprint=snapshot['policy_fingerprint'],
                        evidence_id=proof.id, verification_id=verification.id,
                        idempotency_key=request.source), firing, worker, self.broker._clock())
                self.principals(assignment, worker_actor, resolved, validator, payload)
                attribution.update(authorizer_identity_id=requester.identity_id,
                    verifier_identity_id=worker.identity_id, authority_decision_id=decision.id)
                return {service.store.namespace: {None: updated.model_dump(mode='json')}}
            service.store.store.record_mutate_many(selected, apply)
        try:
            record = service.create_verification(request, actor=original_worker, _commit=commit)
        except (ArtifactEvidenceError, RecoveryConflictError):
            self.denied('Current original records or owner/effect proof refused Verification append')
        return {'item': record.model_dump(mode='json'), 'attribution': attribution,
            'next_action': 'No retry on response loss; reconcile exact execution/method/source digest by read-only lookup'}

    def read(self, assignment, worker_actor, resolved, validator):
        worker, requester, _ = self.principals(assignment, worker_actor, resolved, validator, {})
        service = self.service()
        if resolved.operation.id == 'verification.read':
            query = resolved.query
            if set(query) - {'work_item_ref', 'source'} or any(len(v) != 1 for v in query.values()):
                self.denied('Verification lookup accepts exact Work Item/review digest only')
            ref, source = (query.get('work_item_ref') or [''])[0], (query.get('source') or [''])[0]
            self.scope(assignment, ref)
            if not re.fullmatch(r'sha256:[0-9a-f]{64}', source):
                self.denied('Exact review digest required for reconciliation lookup')
            items = [v for v in service.list_verifications(worker, work_item_ref=ref)
                if v.execution_id == assignment.execution_id and v.source == source
                and v.method == PROOF_CONTRACT and v.verifier_identity_id == worker.identity_id]
            self.principals(assignment, worker_actor, resolved, validator, {})
            self.scope(assignment, ref)
            return {'items': [v.model_dump(mode='json') for v in items[:32]],
                'truncated': len(items) > 32, 'outcome': 'unique' if len(items) == 1 else 'unknown',
                'authorizer_identity_id': requester.identity_id, 'verifier_identity_id': worker.identity_id}
        if resolved.query:
            self.denied('Record reads do not accept query overrides')
        evidence_read = resolved.operation.id == 'verification.evidence.read'
        record = (service.get_evidence if evidence_read else service.get_artifact)(resolved.target_ref, actor=worker)
        self.visible(record, assignment, worker)
        if resolved.operation.id != 'verification.content.read':
            self.principals(assignment, worker_actor, resolved, validator, {})
            self.visible(record, assignment, worker)
            return {'item': record.model_dump(mode='json')}
        if record.content is None or record.content.size_bytes > 256 * 1024:
            self.denied('Artifact content exceeds bounded worker read; use an approved bounded content workflow')
        pointer, stream = service.open_artifact_content(record.id, actor=worker)
        content = bytearray()
        try:
            for chunk in stream:
                content.extend(chunk)
                if len(content) > 256 * 1024:
                    self.denied('Artifact content exceeded declared bounded size')
        finally:
            close = getattr(stream, 'close', None)
            if close is not None:
                close()
        if (len(content) != pointer.size_bytes or record.digest is None
                or record.digest.algorithm != 'sha256'
                or hashlib.sha256(content).hexdigest() != record.digest.value):
            self.denied('Original content size/digest changed during observation')
        self.principals(assignment, worker_actor, resolved, validator, {})
        if service.get_artifact(record.id, actor=worker) != record:
            self.denied('Original artifact changed during content observation')
        self.visible(record, assignment, worker)
        return {'artifact_id': record.id, 'sha256': record.digest.value,
            'content_base64': base64.b64encode(content).decode('ascii'), 'complete': True}
