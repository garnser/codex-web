"""Worker-owned checkout bytes through existing ArtifactContent/Evidence owners."""
from __future__ import annotations

import base64
import hashlib
import json

from codex_web.artifact_evidence import ArtifactCreate, EvidenceCreate, sha256_digest
from codex_web.authority import AuthorityLevel
from codex_web.control_plane_broker import ControlPlaneBrokerOperation
from codex_web.services.worker_verifications import WorkerVerificationAdapter
from codex_web.source_capture import MAX_CONTENT_BYTES, MAX_FILES, METHOD, SourceCaptureRequest

OPERATION = ControlPlaneBrokerOperation(id='repository.source.capture', method='POST',
    path_template='/api/repository-source-captures', capability='api.artifacts.write',
    authority_level=AuthorityLevel.EXECUTE)
SOURCE = 'worker-repository-source-capture'


def repository_id(assignment, query):
    from codex_web.services.control_plane_broker import ControlPlaneBrokerDeniedError
    values = query.get('repository_resource_id')
    scope = assignment.repository_scope
    if (set(query) != {'repository_resource_id'} or not values or len(values) != 1
            or not values[0] or not assignment.project_id or scope is None
            or (scope.organization_id,scope.workspace_id,scope.project_id) !=
                (assignment.organization_id,assignment.workspace_id,assignment.project_id)):
        raise ControlPlaneBrokerDeniedError('Source capture requires one explicit admitted repository')
    allowed = set(scope.writable_repository_ids) | set(scope.read_only_repository_ids)
    if values[0] not in allowed or values[0] not in assignment.resource_ids:
        raise ControlPlaneBrokerDeniedError('Source capture repository outside admitted scope')
    return values[0]


class WorkerSourceCaptureAdapter:
    def __init__(self, broker):
        self.broker = broker
        self.owner = WorkerVerificationAdapter(broker)

    def current(self, assignment, worker, resolved, validator, request):
        if validator is None:
            self.owner.denied('Authenticated source capture assignment validation required')
        current = validator()
        immutable = ('id','organization_id','workspace_id','project_id','assigned_worker_id',
            'execution_id','execution_workspace_id','fence','created_by','subject','work_item_ref',
            'resource_ids','repository_scope','repository_target','sandbox','network',
            'execution_profile_id','execution_profile_definition','agent_profile','resource_context')
        if any(getattr(current,key) != getattr(assignment,key) for key in immutable):
            self.owner.denied('Source capture immutable assignment custody changed')
        principal, requester, _ = self.owner.principals(
            assignment,worker,resolved,validator,request.model_dump(mode='json'))
        if request.expected_fence != assignment.fence or repository_id(assignment,resolved.query) != request.repository_resource_id:
            self.owner.denied('Source capture request fence or repository differs')
        from codex_web.resources import ResourceLifecycle,ResourceType
        from codex_web.services.resources import ResourceCatalogError
        try:
            resource = self.broker.action_intents.execution.resources.get(request.repository_resource_id,requester)
        except ResourceCatalogError:
            self.owner.denied('Source capture repository is unavailable')
        if resource.resource_type != ResourceType.REPOSITORY or resource.lifecycle != ResourceLifecycle.ACTIVE:
            self.owner.denied('Source capture repository is no longer active')
        # Some canonical keyed Work readers return None for a missing key.
        # Keep this refusal local; do not reinterpret a missing Work as scope.
        try:
            work = self.broker.work_items.state_machine._work_item_state(request.work_item_ref)
        except KeyError:
            self.owner.denied('Source capture Work is unavailable')
        if work is None:
            self.owner.denied('Source capture Work is unavailable')
        if (work.organization_id,work.workspace_id,work.project_id) != (
                assignment.organization_id,assignment.workspace_id,assignment.project_id):
            self.owner.denied('Source capture Work is outside assignment tenant/Project scope')
        pin = assignment.agent_profile
        if (work.current_owner not in {pin.role_id,pin.profile_id}
                or request.repository_resource_id not in work.resource_ids
                or not set(work.resource_ids).issubset(assignment.resource_ids)):
            self.owner.denied('Source capture requires current scoped Work owner')
        context = assignment.resource_context
        if (assignment.work_item_ref != work.ref
                and (context is None or context.work_item_ref != work.ref)):
            # A bootstrap's workspace may be canonically bound by the existing
            # owner after admission. Task text is never a Work selector grant.
            workspace = self.broker.execution_workspaces.get(assignment.execution_workspace_id,requester)
            if workspace.work_item_ref != work.ref:
                recovery = workspace.publication_recovery
                if (recovery is None or recovery.work_item_ref != work.ref
                        or recovery.assignment_id != assignment.id or recovery.assignment_fence != assignment.fence
                        or recovery.expires_at <= self.broker._clock()):
                    self.owner.denied('Source capture Work is not admitted to this execution')
                self.broker.execution_workspaces.publication_recovery(workspace.id,
                    request.repository_resource_id,actor=requester)
        return principal,requester

    def checked_body(self, request, captured):
        if (not isinstance(captured,dict) or set(captured) != {'method','commit_sha','files'}
                or captured['method'] != METHOD or captured['commit_sha'] != request.commit_sha
                or not isinstance(captured['files'],list) or len(captured['files']) != len(request.files)):
            self.owner.denied('Source capture returned incompatible provenance')
        total = 0
        for expected, observed in zip(request.files,captured['files'],strict=True):
            if (not isinstance(observed,dict) or set(observed) != {'path','blob_sha','sha256','size_bytes','content_base64'}
                    or any(observed.get(k) != v for k,v in expected.model_dump().items())):
                self.owner.denied('Source capture file identity changed')
            try:
                body = base64.b64decode(observed['content_base64'],validate=True)
            except (ValueError,TypeError):
                self.owner.denied('Source capture content is not bounded canonical base64')
            total += len(body)
            if (type(observed['size_bytes']) is not int or observed['size_bytes'] != len(body)
                    or total > MAX_CONTENT_BYTES or hashlib.sha256(body).hexdigest() != expected.sha256
                    or hashlib.new('sha1' if len(expected.blob_sha)==40 else 'sha256',
                        b'blob '+str(len(body)).encode()+b'\0'+body).hexdigest() != expected.blob_sha):
                self.owner.denied('Source capture blob, digest or byte bound differs')
        raw = json.dumps(captured,sort_keys=True,separators=(',',':')).encode()
        if len(raw) > 262144:
            self.owner.denied('Source capture exceeds governed consumer record bound')
        return raw

    def capture(self, assignment, worker, resolved, payload, validator):
        from codex_web.local_execution_backend import LocalExecutionBackendError
        from codex_web.services.local_execution_worker import LocalExecutionWorkerRuntimeError
        from codex_web.services.execution_workers import WorkerConflictError,WorkerLeaseError
        from codex_web.services.control_plane_broker import ControlPlaneBrokerRequestError
        try:
            return self._capture(assignment,worker,resolved,payload,validator)
        except (LocalExecutionBackendError,LocalExecutionWorkerRuntimeError,WorkerConflictError,WorkerLeaseError) as exc:
            raise ControlPlaneBrokerRequestError('Source capture custody/content refused; no source acceptance',
                status_code=409,code='source_capture_custody_or_content_refused') from exc

    def _capture(self, assignment, worker, resolved, payload, validator):
        request = SourceCaptureRequest.model_validate(payload)
        principal,requester = self.current(assignment,worker,resolved,validator,request)
        service = self.owner.service()
        if service.content is None or service.governance is None:
            self.owner.denied('Source capture requires canonical governed content storage')
        request_sha = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
        key = f'{assignment.id}:{assignment.fence}:{request.request_id}'

        def existing():
            item = service.find_idempotent_artifact(actor=principal,source=SOURCE,idempotency_key=key)
            proof = service.find_idempotent_evidence(actor=principal,source=SOURCE,idempotency_key=key)
            if item is None and proof is None:
                return None
            if (item is None or proof is None or item.metadata.get('request_sha256') != request_sha
                    or item.content is None or item.content.state != 'available'
                    or item.lifecycle != 'active' or proof.lifecycle != 'valid' or proof.result != 'info'
                    or item.artifact_type != 'report' or item.revision != request.commit_sha or item.digest is None
                    or proof.evidence_type != 'artifact_verification' or proof.digest != item.digest
                    or any(record.provider != METHOD or record.source != SOURCE
                        or record.external_id != key or record.metadata.get('assignment_id') != assignment.id
                        or record.metadata.get('assignment_fence') != assignment.fence
                        or record.metadata.get('worker_id') != assignment.assigned_worker_id
                        or record.metadata.get('commit_sha') != request.commit_sha for record in (item,proof))
                    or proof.artifact_ids != (item.id,) or proof.metadata.get('request_sha256') != request_sha):
                self.owner.denied('Original source capture differs or is incomplete; preserve original request identity')
            from codex_web.data_governance import ContextFilterRequest
            for record in (item,proof):
                if (record.resource_ids != (request.repository_resource_id,)
                        or record.work_item_ref != request.work_item_ref
                        or record.execution_id != assignment.execution_id
                        or record.execution_workspace_id != assignment.execution_workspace_id
                        or record.producer_identity_id != principal.identity_id
                        or (record.organization_id,record.workspace_id,record.project_id) !=
                            (assignment.organization_id,assignment.workspace_id,assignment.project_id)
                        or not record.governance_record_id
                        or record.retention_expires_at is not None and record.retention_expires_at <= self.broker._clock()
                        or service.governance.filter_context(ContextFilterRequest(
                            record_ids=(record.governance_record_id,)),actor=principal).denied_record_ids):
                    self.owner.denied('Original source capture provenance or governance is no longer usable')
            return {'artifact_id':item.id,'evidence_id':proof.id,'request_sha256':request_sha,
                'content_sha256':item.digest.value,'source_information_only':True}

        previous = existing()
        if previous is not None:
            return {**previous,'deduplicated':True}
        runtime = self.broker.local_execution_worker
        if runtime is None:
            self.owner.denied('Current local worker source capture unavailable')
        _, member = runtime.source_capture_member(assignment,request)
        captured = runtime.capture_repository_source(assignment,request,
            validate_current=lambda:self.current(assignment,worker,resolved,validator,request))
        body = self.checked_body(request,captured)
        domains = self.owner.guard_rows(assignment,request.work_item_ref,validator)
        returned = []

        def persist(rows):
            current_principal,current_requester = self.current(assignment,worker,resolved,validator,request)
            if (not current_principal.has_same_authority(principal)
                    or not current_requester.has_same_authority(requester)):
                self.owner.denied('Source capture principals changed before persistence')
            _, current_member = runtime.source_capture_member(assignment,request)
            if current_member != member:
                self.owner.denied('Source capture physical member changed before persistence')
            previous = existing()
            if previous is not None:
                returned.append({**previous,'deduplicated':True})
                return {}
            state = service.store._decode(rows[service.store.namespace][None])
            governance = service.governance.store._decode(rows[service.governance.store.namespace][None])
            creation_domains = {service.store.namespace:(None,),service.governance.store.namespace:(None,)}

            def compose(creation):
                nonlocal state
                if creation.service is not service or creation.domains != creation_domains or creation.catalog_guard is not None:
                    self.owner.denied('Source capture canonical governance transaction differs')
                state = creation.updater(state,governance)

            def update(updater):
                nonlocal state
                state = updater(state)
                return state

            now = self.broker._clock()
            scope = dict(project_id=assignment.project_id,work_item_ref=request.work_item_ref,
                execution_id=assignment.execution_id,execution_workspace_id=assignment.execution_workspace_id,
                resource_ids=(request.repository_resource_id,),provider=METHOD,source=SOURCE,
                external_id=key,metadata={'assignment_id':assignment.id,'assignment_fence':assignment.fence,
                    'worker_id':assignment.assigned_worker_id,'requester_identity_id':requester.identity_id,
                    'request_sha256':request_sha,'commit_sha':request.commit_sha,'result':'source_info'})
            item = service.create_artifact(ArtifactCreate(**scope,artifact_type='report',
                name='Immutable admitted repository source',revision=request.commit_sha,
                digest=sha256_digest(body),produced_at=now),actor=principal,idempotency_key=key,_commit=compose)
            item = service.attach_artifact_content(item.id,(body,),actor=principal,media_type='application/json',
                expected_sha256=hashlib.sha256(body).hexdigest(),_state=state,_commit=update)
            proof = service.create_evidence(EvidenceCreate(**scope,evidence_type='artifact_verification',result='info',
                artifact_ids=(item.id,),digest=sha256_digest(body),observed_at=now,summary='Immutable source capture; not test, QA or acceptance'),
                actor=principal,idempotency_key=key,_state=state,_commit=compose)
            self.current(assignment,worker,resolved,validator,request)
            returned.append({'artifact_id':item.id,'evidence_id':proof.id,'request_sha256':request_sha,
                'content_sha256':item.digest.value,'source_information_only':True,'deduplicated':False})
            return {service.store.namespace:{None:state.model_dump(mode='json')},
                service.governance.store.namespace:{None:governance.model_dump(mode='json')}}

        service.store.store.record_mutate_many(domains,persist)
        return returned[0]


def public_contract():
    return {'request_schema':SourceCaptureRequest.model_json_schema(),'max_files':MAX_FILES,
        'max_content_bytes':MAX_CONTENT_BYTES,'result':'Governed source INFO only; no QA or acceptance',
        'sharing':'Existing governed derivative authority remains required; capture grants no cross-resource access',
        'response_loss':'Repeat only the exact original request identity to inspect/settle its immutable capture'}
