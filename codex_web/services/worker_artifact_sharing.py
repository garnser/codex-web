"""Assignment-bound directional data delivery, not execution authority (#2008)."""
from __future__ import annotations

import hashlib
import json
import re

from codex_web.artifact_evidence import ArtifactCreate, EvidenceCreate, sha256_digest
from codex_web.artifact_sharing_policy import ArtifactShareRequest, METHOD, SOURCE, SOURCE_METHOD
from codex_web.authority import AuthorityEvaluationRequest, AuthorityLevel
from codex_web.control_plane_broker import ControlPlaneBrokerOperation
from codex_web.data_governance import ContextFilterRequest
from codex_web.services.definitions import DefinitionError, DefinitionNotFoundError, DefinitionConflictError, DefinitionCompatibilityError
from codex_web.services.worker_verifications import WorkerVerificationAdapter
from codex_web.services.artifact_sharing import scoped_work

OPERATION = ControlPlaneBrokerOperation(id='verification.artifact.share', method='POST',
    path_template='/api/artifacts/shares', capability='api.artifacts.share',
    authority_level=AuthorityLevel.EXECUTE)
DELIVERY_SOURCE = 'worker-governed-artifact-sharing'


def resolve(method, path):
    return (OPERATION, None) if method == 'POST' and path == OPERATION.path_template else None


def source_repository_id(assignment, query):
    from codex_web.services.control_plane_broker import ControlPlaneBrokerDeniedError
    scope = assignment.repository_scope
    values = query.get('repository_resource_id')
    if (set(query) != {'repository_resource_id'} or not values or len(values) != 1
            or not values[0] or scope is None
            or (scope.organization_id,scope.workspace_id,scope.project_id) !=
                (assignment.organization_id,assignment.workspace_id,assignment.project_id)
            or values[0] not in (*scope.writable_repository_ids,*scope.read_only_repository_ids)
            or values[0] not in assignment.resource_ids):
        raise ControlPlaneBrokerDeniedError('Sharing requires one exact admitted source repository')
    return values[0]


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


class WorkerArtifactSharingAdapter:
    def __init__(self, broker, *, policies):
        self.broker, self.policies = broker, policies
        self.owner = WorkerVerificationAdapter(broker)

    def denied(self, reason):
        self.owner.denied(reason)

    def current(self, assignment, worker, resolved, validator, request):
        if self.policies is None or validator is None:
            self.denied('Current canonical sharing policy/session required')
        actual = validator()
        keys = ('id','organization_id','workspace_id','project_id','assigned_worker_id','execution_id',
            'execution_workspace_id','fence','created_by','subject','work_item_ref','resource_ids',
            'repository_scope','repository_target','sandbox','network','execution_profile_id',
            'execution_profile_definition','agent_profile','resource_context')
        if any(getattr(actual, k) != getattr(assignment, k) for k in keys):
            self.denied('Sharing assignment custody changed')
        principal, requester, _ = self.owner.principals(assignment, worker, resolved, validator,
            request.model_dump(mode='json'))
        if request.expected_fence != assignment.fence:
            self.denied('Sharing request fence changed')
        registered = self.broker.execution_workers.store.worker(assignment.assigned_worker_id)
        if getattr(registered, 'revoked_at', None) is not None:
            self.denied('Assigned worker was revoked')
        service = self.owner.service()
        if service.governance is None or service.content is None:
            self.denied('Sharing requires canonical governed managed content')
        proof = service.get_evidence(request.source_evidence_id, actor=principal)
        if len(proof.artifact_ids) != 1:
            self.denied('Sharing requires one exact source capture Artifact')
        artifact = service.get_artifact(proof.artifact_ids[0], actor=principal)
        now = self.broker._clock()
        source_work = scoped_work(self.broker,assignment,artifact.work_item_ref)
        target = scoped_work(self.broker,assignment,request.target_work_item_ref)
        preflights = getattr(self.broker.execution_preflights,'store',None)
        if preflights is None:
            self.denied('Canonical complete Work original index is unavailable')
        for work in (source_work,target):
            if preflights.unresolved_work_item(organization_id=work.organization_id,
                    workspace_id=work.workspace_id,project_id=work.project_id,ref=work.ref) is not None:
                self.denied('Unresolved native original prevents data delivery')
        pin = assignment.agent_profile
        if (pin is None or source_work.current_owner not in {pin.profile_id, pin.role_id}
                or not source_work.resource_ids or not set(source_work.resource_ids).issubset(assignment.resource_ids)
                or len(artifact.resource_ids) != 1 or not set(artifact.resource_ids).issubset(source_work.resource_ids)
                or not target.resource_ids or len(target.resource_ids) > 64):
            self.denied('Source ownership or canonical target Work/resource scope changed')
        scope = assignment.repository_scope
        source_repo = artifact.resource_ids[0]
        if source_repository_id(assignment,resolved.query) != source_repo:
            self.denied('Source selector differs from the canonical capture Artifact')
        if (scope is None or source_repo not in (*scope.writable_repository_ids, *scope.read_only_repository_ids)
                or (scope.organization_id, scope.workspace_id, scope.project_id) !=
                    (assignment.organization_id, assignment.workspace_id, assignment.project_id)):
            self.denied('Source repository is outside the actual admitted scope')
        workspace = self.broker.execution_workspaces.get(assignment.execution_workspace_id, requester)
        lease = self.broker.execution_workspaces.store.lease(workspace.lease_id)
        context = assignment.resource_context
        if (workspace.status != 'active' or workspace.execution_id != assignment.execution_id
                or workspace.owner_identity_id != requester.identity_id or lease is None
                or lease.released_at is not None or lease.expires_at <= now
                or lease.execution_workspace_id != workspace.id or lease.execution_id != assignment.execution_id
                or lease.owner_identity_id != requester.identity_id or lease.subject != workspace.subject
                or lease.resource_modes.get(source_repo) not in {'read', 'write'}):
            self.denied('Current source workspace/lease/Work binding changed')
        if source_work.ref not in {assignment.work_item_ref,workspace.work_item_ref,context.work_item_ref if context else None}:
            recovery = workspace.publication_recovery
            if (recovery is None or recovery.work_item_ref != source_work.ref
                    or recovery.assignment_id != assignment.id or recovery.assignment_fence != assignment.fence
                    or recovery.expires_at <= now):
                self.denied('Source Work is not bound to the current execution')
            self.broker.execution_workspaces.publication_recovery(workspace.id,source_repo,actor=requester)
        source_key = artifact.external_id
        prefix = f'{assignment.id}:{assignment.fence}:'
        if (not isinstance(source_key,str) or not source_key.startswith(prefix)
                or not re.fullmatch(r'[0-9a-f]{32}',source_key[len(prefix):])
                or proof.external_id != source_key
                or service.find_idempotent_artifact(actor=principal,source=SOURCE,idempotency_key=source_key) != artifact
                or service.find_idempotent_evidence(actor=principal,source=SOURCE,idempotency_key=source_key) != proof):
            self.denied('Source is not the canonical capture owner request identity')
        meta = ('assignment_id','assignment_fence','worker_id','requester_identity_id','request_sha256','commit_sha','result')
        for record in (artifact, proof):
            if ((record.organization_id,record.workspace_id,record.project_id,record.execution_id,
                    record.execution_workspace_id,record.work_item_ref,record.resource_ids,record.producer_identity_id) !=
                    (assignment.organization_id,assignment.workspace_id,assignment.project_id,assignment.execution_id,
                     assignment.execution_workspace_id,source_work.ref,artifact.resource_ids,principal.identity_id)
                    or record.provider != SOURCE_METHOD or record.source != SOURCE
                    or record.lifecycle not in {'active','valid'} or not record.governance_record_id
                    or record.metadata.get('assignment_id') != assignment.id
                    or record.metadata.get('assignment_fence') != assignment.fence
                    or record.metadata.get('worker_id') != assignment.assigned_worker_id
                    or record.metadata.get('requester_identity_id') != requester.identity_id
                    or record.metadata.get('result') != 'source_info'
                    or (getattr(record,'produced_at',None) or getattr(record,'observed_at',None) or 0) < (assignment.started_at or 0)
                    or record.retention_expires_at is not None and record.retention_expires_at <= now):
                self.denied('Source capture provenance/lifecycle no longer current')
            for actor in (principal, requester):
                if service.governance.filter_context(ContextFilterRequest(
                        record_ids=(record.governance_record_id,)), actor=actor).denied_record_ids:
                    self.denied('Source governance denied data delivery')
        if (artifact.artifact_type != 'report' or proof.evidence_type != 'artifact_verification'
                or proof.result != 'info' or proof.artifact_ids != (artifact.id,)
                or any(proof.metadata.get(k) != artifact.metadata.get(k) for k in meta)
                or artifact.revision != artifact.metadata.get('commit_sha')
                or artifact.digest is None or artifact.digest.algorithm != 'sha256' or proof.digest != artifact.digest
                or artifact.content is None or artifact.content.state != 'available'):
            self.denied('Source is not exact governed capture INFO')
        try:
            policy, reference = self.policies.resolve(assignment, requester, now=now)
        except (DefinitionError,DefinitionNotFoundError,DefinitionConflictError,DefinitionCompatibilityError,ValueError):
            self.denied('Current published Project sharing policy is unavailable')
        rules = [r for r in policy.rules if set(artifact.resource_ids).issubset(r.source_resource_ids)
            and set(target.resource_ids).issubset(r.target_resource_ids)
            and artifact.content.size_bytes <= r.max_content_bytes]
        if not rules:
            self.denied('No published directional rule permits this data target')
        # Choose deterministically; the narrowest matching byte ceiling remains
        # effective when several rules authorize the same directional pair.
        rule = min(rules, key=lambda r:(r.max_content_bytes,r.id))
        resources = self.broker.action_intents.execution.resources
        for resource_id in set(artifact.resource_ids) | set(target.resource_ids):
            resource = resources.get(resource_id, requester)
            if resource.lifecycle != 'active' or resource.resource_type != 'repository':
                self.denied('Current sharing resources must be active repositories')
        # Data-target authority deliberately does NOT admit target execution
        # resources or invoke administrative repository-target authorization.
        decisions = []
        for actor in (requester, principal):
            decision = self.broker.authority.evaluate(AuthorityEvaluationRequest(
                capability=OPERATION.capability,level=AuthorityLevel.EXECUTE,project_id=assignment.project_id,
                resource_ids=tuple(sorted(set(artifact.resource_ids) | set(target.resource_ids)))), actor=actor, now=now)
            if decision.outcome != 'allow' or decision.definition_ref is None:
                self.denied('Both current requester and worker need explicit scoped sharing authority')
            decisions.append(decision)
        return principal, requester, artifact, proof, target, reference, rule, decisions

    def share(self, assignment, worker, resolved, payload, validator):
        from codex_web.services.execution_workers import WorkerConflictError, WorkerLeaseError
        from codex_web.services.execution_workspaces import ExecutionWorkspaceConflictError
        from codex_web.services.artifact_evidence import ArtifactEvidenceError
        from codex_web.services.control_plane_broker import ControlPlaneBrokerRequestError
        try:
            return self._share(assignment,worker,resolved,payload,validator)
        except (WorkerConflictError,WorkerLeaseError,ExecutionWorkspaceConflictError,ArtifactEvidenceError) as exc:
            raise ControlPlaneBrokerRequestError('Sharing canonical custody/source refused; no accepted delivery',
                status_code=409,code='artifact_sharing_custody_or_source_refused') from exc

    def _share(self, assignment, worker, resolved, payload, validator):
        request = ArtifactShareRequest.model_validate(payload)
        current = self.current(assignment,worker,resolved,validator,request)
        principal, requester, artifact, proof, target, reference, rule, decisions = current
        service = self.owner.service()
        pointer, stream = service.open_artifact_content(artifact.id,actor=principal)
        content = bytearray()
        try:
            for chunk in stream:
                content.extend(chunk)
                if len(content) > rule.max_content_bytes:
                    self.denied('Source content exceeds published sharing limit')
        finally:
            close = getattr(stream,'close',None)
            if close is not None:
                close()
        body = bytes(content)
        if (len(body) != pointer.size_bytes or pointer != artifact.content
                or hashlib.sha256(body).hexdigest() != artifact.digest.value):
            self.denied('Source content changed during sharing observation')
        # The source writer's closed contract is data, not an executable payload.
        try:
            source = json.loads(body)
        except (ValueError,UnicodeError):
            self.denied('Source capture managed content is not canonical JSON')
        if (not isinstance(source,dict) or set(source) != {'method','commit_sha','files'}
                or source['method'] != SOURCE_METHOD or source['commit_sha'] != artifact.revision
                or not isinstance(source['files'],list) or not 1 <= len(source['files']) <= 8):
            self.denied('Source content does not match its registered capture contract')
        after_io = self.current(assignment,worker,resolved,validator,request)
        if not self.same(current, after_io):
            self.denied('Source, target, principals or exact policies changed during content I/O')
        key = f'{assignment.id}:{assignment.fence}:{request.request_id}'
        request_digest = hashlib.sha256(canonical(request.model_dump(mode='json'))).hexdigest()
        source_digest = artifact.digest.value
        domains = self.owner.guard_rows(assignment,artifact.work_item_ref,validator)
        work_store = self.broker.work_items.state_machine.dependencies.get_state.__self__
        work_store.get(target.ref)  # prepare canonical keyed row before writer
        domains[work_store.namespace] = tuple(dict.fromkeys((*domains[work_store.namespace],target.ref)))
        if self.policies.registry.store.store is not service.store.store:
            self.denied('Sharing policy store differs from canonical transaction')
        domains[self.policies.registry.store.namespace] = (None,)
        preflights = getattr(self.broker.execution_preflights,'store',None)
        if preflights.store is not service.store.store:
            self.denied('Retained native originals store differs from canonical transaction')
        domains[preflights.NAMESPACE] = ()
        returned = []

        def persist(rows):
            observed = self.current(assignment,worker,resolved,validator,request)
            if not self.same(current,observed):
                self.denied('Sharing original scope changed before canonical write')
            state = service.store._decode(rows[service.store.namespace][None])
            governance = service.governance.store._decode(rows[service.governance.store.namespace][None])
            previous = service.find_idempotent_artifact(actor=principal,source=DELIVERY_SOURCE,idempotency_key=key)
            previous_proof = service.find_idempotent_evidence(actor=principal,source=DELIVERY_SOURCE,idempotency_key=key)
            if previous is not None or previous_proof is not None:
                self.existing(previous,previous_proof,request_digest,source_digest,reference,target,assignment,principal,
                    artifact.id,proof.id,requester.identity_id)
                returned.append(self.result(previous,previous_proof,True))
                return {}
            creation_domains = {service.store.namespace:(None,),service.governance.store.namespace:(None,)}
            def compose(creation):
                nonlocal state
                if creation.service is not service or creation.domains != creation_domains or creation.catalog_guard is not None:
                    self.denied('Sharing requires the complete canonical governance writer')
                state = creation.updater(state,governance)
            def update(updater):
                nonlocal state
                state = updater(state)
                return state
            metadata = {'assignment_id':assignment.id,'assignment_fence':assignment.fence,
                'worker_id':assignment.assigned_worker_id,'requester_identity_id':requester.identity_id,
                'request_sha256':request_digest,'source_artifact_id':artifact.id,'source_evidence_id':proof.id,
                'source_content_sha256':source_digest,'source_work_item_ref':artifact.work_item_ref,
                'sharing_policy_ref':json.dumps(reference.model_dump(mode='json'),sort_keys=True),
                'sharing_rule_id':rule.id,'requester_authority_ref':json.dumps(decisions[0].definition_ref.model_dump(mode='json'),sort_keys=True),
                'worker_authority_ref':json.dumps(decisions[1].definition_ref.model_dump(mode='json'),sort_keys=True),
                'result':'shared_source_info'}
            scope = dict(project_id=assignment.project_id,work_item_ref=target.ref,
                execution_id=assignment.execution_id,execution_workspace_id=assignment.execution_workspace_id,
                resource_ids=tuple(target.resource_ids),provider=METHOD,source=DELIVERY_SOURCE,external_id=key,metadata=metadata)
            derived = service.create_artifact(ArtifactCreate(**scope,artifact_type='report',
                name='Governed shared repository source',revision=artifact.revision,digest=sha256_digest(body)),
                actor=principal,source_artifact_ids=(artifact.id,),source_evidence_ids=(proof.id,),idempotency_key=key,_commit=compose)
            derived = service.attach_artifact_content(derived.id,(body,),actor=principal,
                media_type='application/json',expected_sha256=source_digest,_state=state,_commit=update)
            info = service.create_evidence(EvidenceCreate(**scope,evidence_type='artifact_verification',
                result='info',artifact_ids=(derived.id,),digest=sha256_digest(body),
                summary='Governed source input delivery only; not independent QA, validation or acceptance'),
                actor=principal,idempotency_key=key,_state=state,_commit=compose)
            if not self.same(current,self.current(assignment,worker,resolved,validator,request)):
                self.denied('Sharing current authority changed before transaction commit')
            returned.append(self.result(derived,info,False))
            return {service.store.namespace:{None:state.model_dump(mode='json')},
                service.governance.store.namespace:{None:governance.model_dump(mode='json')}}
        service.store.store.record_mutate_many(domains,persist)
        return returned[0]

    @staticmethod
    def same(before, after):
        return (before[0].has_same_authority(after[0]) and before[1].has_same_authority(after[1])
            and before[2:7] == after[2:7]
            and tuple((d.definition_ref,d.matched_role_ids,d.matched_grant_ids,d.delegation_ids,d.expires_at) for d in before[7]) ==
                tuple((d.definition_ref,d.matched_role_ids,d.matched_grant_ids,d.delegation_ids,d.expires_at) for d in after[7]))

    def existing(self,item,proof,request_digest,source_digest,policy_ref,target,assignment,worker,source_artifact,source_evidence,requester_id):
        if (item is None or proof is None or item.lifecycle != 'active' or proof.lifecycle != 'valid'
                or proof.result != 'info' or proof.artifact_ids != (item.id,)
                or item.content is None or item.content.state != 'available'
                or item.digest is None or item.digest.value != source_digest or proof.digest != item.digest):
            self.denied('Original delivery incomplete/changed; inspect exact identity without a new request')
        for record in (item,proof):
            if (record.provider != METHOD or record.source != DELIVERY_SOURCE
                    or record.metadata.get('assignment_id') != assignment.id
                    or record.metadata.get('assignment_fence') != assignment.fence
                    or record.metadata.get('worker_id') != assignment.assigned_worker_id
                    or record.metadata.get('requester_identity_id') != requester_id
                    or record.metadata.get('source_artifact_id') != source_artifact
                    or record.metadata.get('source_evidence_id') != source_evidence
                    or record.metadata.get('request_sha256') != request_digest
                    or record.metadata.get('source_content_sha256') != source_digest
                    or record.metadata.get('sharing_policy_ref') != json.dumps(policy_ref.model_dump(mode='json'),sort_keys=True)
                    or record.work_item_ref != target.ref or record.resource_ids != tuple(target.resource_ids)
                    or record.producer_identity_id != worker.identity_id or record.execution_id != assignment.execution_id
                    or record.execution_workspace_id != assignment.execution_workspace_id
                    or record.project_id != assignment.project_id
                    or record.retention_expires_at is not None and record.retention_expires_at <= self.broker._clock()
                    or not record.governance_record_id
                    or self.owner.service().governance.filter_context(ContextFilterRequest(
                        record_ids=(record.governance_record_id,)),actor=worker).denied_record_ids):
                self.denied('Original delivery provenance/governance changed')

    @staticmethod
    def result(artifact,proof,deduplicated):
        return {'artifact_id':artifact.id,'evidence_id':proof.id,'target_work_item_ref':artifact.work_item_ref,
            'content_sha256':artifact.digest.value,'source_information_only':True,'deduplicated':deduplicated,
            'repository_execution_scope_changed':False,'independent_qa_or_acceptance':False}


def public_contract():
    return {'request_schema':ArtifactShareRequest.model_json_schema(),
        'authority':'Current requester AND authenticated worker api.artifacts.share on source+target Resources; exact Project policy additional',
        'result':'Target-scoped governed Artifact and INFO Evidence; no execution grant or QA',
        'response_loss':'Inspect/repeat only identical original request ID; changed sources/policy/scope refuse; no new derivative identity'}
