"""Derive fixed publication inputs; no arbitrary uploads or publication effects."""
import asyncio
import hashlib
import json
import re

from codex_web.artifact_evidence import ArtifactCreate, EvidenceCreate, sha256_digest
from codex_web.data_governance import ContextFilterRequest
from codex_web.publication_input import METHOD, PublicationInputRequest
from codex_web.skills import SkillDefinition
from codex_web.services.async_operations import run_settled
from codex_web.services.code_host_action_contract import CODE_HOST_BRANCH_PUBLISH_ACTION_ID


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


class WorkerPublicationInputAdapter:
    def __init__(self, adapter):
        self.owner, self.broker = adapter, adapter.broker

    def origin(self, assignment, request):
        return 'sha256:'+hashlib.sha256(encoded({'assignment_id':assignment.id,
            'fence':assignment.fence,'request_id':request.request_id,'method':METHOD})).hexdigest()

    def current(self, assignment, worker, resolved, validator, request):
        if (assignment.id != request.expected_assignment_id or assignment.fence != request.expected_fence
                or self.broker._writable_repository_id(assignment) != request.repository_resource_id):
            self.owner.denied('Publication input expected origin or WRITE repository differs')
        principal, requester, _ = self.owner.principals(assignment,worker,resolved,validator,request.model_dump(mode='json'))
        workspace = self.broker.execution_workspaces.get(assignment.execution_workspace_id, requester)
        recovery = workspace.publication_recovery
        ref = workspace.work_item_ref or (recovery.work_item_ref if recovery else None)
        work = self.owner.scope(assignment,ref)
        repository, separator, number = ref.rpartition('!')
        identity = work.source_identity
        if (not separator or not number.isdecimal() or str(int(number)) != number or int(number) < 1
                or work.kind != 'merge_request' or work.closed_at is not None or work.current_stage == 'closed'
                or work.project_path != repository or identity is None or identity.source_type != 'gitlab'
                or identity.external_id != ref or work.resource_ids != [request.repository_resource_id]):
            self.owner.denied('Publication input requires one current owned canonical GitLab MR')
        members = [row for row in workspace.repository_members if row.resource_id == request.repository_resource_id]
        if len(members) != 1 or members[0].access_mode != 'write':
            self.owner.denied('Publication input requires the actual current WRITE member')
        skills = self.broker.procedure_admission.discovery.skills
        skills.validate_required_procedure(request.definition,actor=requester,project_id=assignment.project_id)
        procedure = SkillDefinition.model_validate(skills.definitions.get_record(request.definition.record_id).payload).operational_procedure
        operation = self.broker._resolve_operation('POST','/api/repository-actions/branch/publish').operation
        match = self.broker.procedure_admission.inspect_current(assignment=assignment,actor=requester,
            operation=operation,resource_ids=(request.repository_resource_id,),payload={})
        if not match.admitted or request.definition not in match.selected or procedure is None or procedure.input_protocol is not None:
            self.owner.denied('Exact current publication procedure is not applicable to this assignment')
        fields = {row.name:row.type for row in procedure.inputs}
        base = {'execution_workspace_id':'string','branch':'string','head_revision':'string',
            'expected_remote_revision':'string','pull_request_number':'integer'}
        if fields not in (base, {**base,'existing_change_request_number':'integer'}):
            self.owner.denied('Published publication input contract is unsupported; no guessed inputs')
        if not any(step.action and step.action.action_id == CODE_HOST_BRANCH_PUBLISH_ACTION_ID for step in procedure.steps):
            self.owner.denied('Published procedure does not declare branch publication')
        return principal, requester, workspace, members[0], work, fields

    def existing(self, assignment, worker, source, request_sha=None):
        service = self.owner.service()
        artifact = service.find_idempotent_artifact(actor=worker,source=METHOD,idempotency_key=source)
        evidence = service.find_idempotent_evidence(actor=worker,source=METHOD,idempotency_key=source)
        if artifact is None and evidence is None:
            return None
        if (artifact is None or evidence is None or artifact.content is None or artifact.content.state != 'available'
                or artifact.lifecycle != 'active' or evidence.lifecycle != 'valid' or evidence.result != 'info'
                or evidence.artifact_ids != (artifact.id,) or artifact.digest != evidence.digest
                or request_sha is not None and artifact.metadata.get('request_sha256') != request_sha):
            self.owner.denied('Original publication input differs or is incomplete; reconcile without a new key')
        self.owner.scope(assignment,artifact.work_item_ref)
        # Each canonical owner hashes its own distinct Artifact/Evidence
        # creation payload. Those per-record hashes are intentionally unequal;
        # all collector provenance, including request/content digests, must match.
        metadata = lambda record: {key:value for key,value in record.metadata.items()
            if key != 'idempotency_payload_sha256'}
        if (artifact.resource_ids != (self.broker._writable_repository_id(assignment),)
                or evidence.resource_ids != artifact.resource_ids or evidence.work_item_ref != artifact.work_item_ref
                or metadata(evidence) != metadata(artifact) or artifact.external_id != source or evidence.external_id != source
                or any(not isinstance(record.metadata.get('idempotency_payload_sha256'),str)
                    or not re.fullmatch(r'[0-9a-f]{64}',record.metadata['idempotency_payload_sha256'])
                    for record in (artifact,evidence))):
            self.owner.denied('Original publication input scope or provenance differs')
        for record in (artifact,evidence):
            if (record.producer_identity_id != worker.identity_id or record.execution_id != assignment.execution_id
                    or record.execution_workspace_id != assignment.execution_workspace_id
                    or record.project_id != assignment.project_id or record.provider != METHOD
                    or record.metadata.get('assignment_id') != assignment.id
                    or record.metadata.get('assignment_fence') != assignment.fence
                    or record.retention_expires_at is not None and record.retention_expires_at <= self.broker._clock()
                    or not record.governance_record_id or service.governance is None
                    or service.governance.filter_context(ContextFilterRequest(record_ids=(record.governance_record_id,)),actor=worker).denied_record_ids):
                self.owner.denied('Original publication input provenance or governance changed')
        return {'procedure_input':artifact.model_dump(mode='json'),'evidence':evidence.model_dump(mode='json'),
            'source':source,'request_sha256':artifact.metadata['request_sha256'],
            'source_information_only':True,'deduplicated':True}

    def lookup(self, assignment, worker, resolved, validator):
        if (set(resolved.query) != {'method','source'} or resolved.query['method'] != [METHOD]
                or len(resolved.query['source']) != 1 or not re.fullmatch(r'sha256:[0-9a-f]{64}',resolved.query['source'][0])):
            self.owner.denied('Exact original publication method/source required')
        principal,_,_ = self.owner.principals(assignment,worker,resolved,validator,{})
        original = self.existing(assignment,principal,resolved.query['source'][0])
        return {'outcome':'unique' if original else 'unknown','original':original,'absence_is_not_permission_to_replay':True}

    async def create(self, assignment, worker, resolved, payload, validator):
        from codex_web.code_hosts import CodeHostError
        from codex_web.local_execution_backend import LocalExecutionBackendError
        from codex_web.services.local_execution_worker import LocalExecutionWorkerRuntimeError
        from codex_web.services.control_plane_broker import ControlPlaneBrokerRequestError
        try:
            return await self._create(assignment,worker,resolved,payload,validator)
        except (ValueError,CodeHostError,LocalExecutionBackendError,LocalExecutionWorkerRuntimeError) as exc:
            raise ControlPlaneBrokerRequestError('Publication input observation refused; preserve the original request identity',
                status_code=409,code='publication_input_observation_refused') from exc

    async def _create(self, assignment, worker, resolved, payload, validator):
        request = PublicationInputRequest.model_validate(payload)
        initial = await asyncio.to_thread(self.current,assignment,worker,resolved,validator,request)
        principal, requester, workspace, member, work, fields = initial
        source = self.origin(assignment,request)
        request_sha = hashlib.sha256(encoded(request.model_dump(mode='json'))).hexdigest()
        previous = await asyncio.to_thread(self.existing,assignment,principal,source,request_sha)
        if previous is not None:
            return previous
        runtime = self.broker.local_execution_worker
        if runtime is None:
            self.owner.denied('Canonical isolated Git inspection unavailable')
        def local():
            from pathlib import Path
            observed = runtime.inspect_git_metadata(assignment,workspace_path=Path(member.workspace_path),source_path=Path(member.source_path))
            if observed['head'] != request.expected_head_sha or observed['branch'] != member.branch_name or observed['dirty']:
                self.owner.denied('Publication input local HEAD/branch is changed or dirty')
            return observed
        observed = await asyncio.to_thread(local)
        number = int(work.ref.rpartition('!')[2])
        reads = [self.broker._resolve_operation('GET',path+'?repository_resource_id='+request.repository_resource_id)
            for path in ('/api/repository-facts','/api/repository-facts/pull-requests/'+str(number),'/api/repository-facts/refs')]
        def read_scope():
            current = self.current(assignment,worker,resolved,validator,request)
            if (not current[0].has_same_authority(initial[0])
                    or not current[1].has_same_authority(initial[1]) or current[2:] != initial[2:]):
                self.owner.denied('Publication input current Work/workspace/procedure scope changed')
            selected = []
            for read in reads:
                actor, _,_,_ = self.broker._dispatch_scope(assignment,worker,read)
                # A Resource-context requester is distinct from its allocation
                # custodian. Reauthorize the same effective current requester
                # returned by principals(), never substitute the custodian.
                self.broker._authorize(assignment,read.operation,actor=current[1],resource_ids=(request.repository_resource_id,))
                binding_id,repository = self.broker._code_host_binding(assignment=assignment,actor=actor,
                    query=read.query,operation_id=read.operation.id)
                selected.append((binding_id,repository))
            if len(set(selected)) != 1:
                self.owner.denied('Publication input reads select different provider bindings')
            binding = self.broker.action_intents.execution.registry.binding(selected[0][0].removeprefix('control-plane-read:'),principal)
            read_binding,_ = self.broker.code_hosts.registry.resolve(selected[0][0],actor=principal)
            # Canonical GitLab TaskSource identities use the API base, not the
            # action provider's display/instance label. Match its existing
            # strip/rstrip normalization; never infer hosts or source aliases.
            if (read_binding.provider_type != 'gitlab'
                    or work.source_identity.source_instance.strip().rstrip('/') != read_binding.base_url.strip().rstrip('/')):
                self.owner.denied('Publication Work source instance differs from the bound GitLab API')
            return selected[0],binding.model_dump(mode='json')
        selected,binding_pin = await asyncio.to_thread(read_scope)
        async def checkpoint():
            if await asyncio.to_thread(read_scope) != (selected,binding_pin):
                self.owner.denied('Publication input provider binding changed')
        fact = await self.broker.code_hosts.publication_input(*selected,number,actor=principal,checkpoint=checkpoint)
        if fact.repository != work.project_path or fact.number != number:
            self.owner.denied('Publication input provider repository/MR differs')
        values = {'execution_workspace_id':workspace.id,'branch':fact.branch,'head_revision':observed['head'],
            'expected_remote_revision':fact.head_revision,'pull_request_number':number}
        if 'existing_change_request_number' in fields:
            values['existing_change_request_number'] = number
        elif fact.branch != member.branch_name:
            self.owner.denied('Five-field contract cannot express existing-MR recovery branch mapping')
        # INFO describes observed input, not permission to publish. The normal
        # publisher alone owns final local ancestry/recovery/provider CAS
        # attestation. Do not call its host-Git path from a worker collector.
        action_registry = self.broker.action_intents.execution.registry
        await checkpoint()
        if await asyncio.to_thread(local) != observed:
            self.owner.denied('Publication input local facts changed during observation')
        service = self.owner.service()
        if service.governance is None or service.content is None:
            self.owner.denied('Canonical governed content owner unavailable')
        domains = self.owner.guard_rows(assignment,work.ref,validator)
        registry_store = action_registry.store
        if registry_store.store is not service.store.store:
            self.owner.denied('Publication provider registry requires canonical transaction')
        domains[registry_store.namespace] = (None,)
        content = encoded(values)
        returned = []
        def persist(rows):
            if read_scope() != (selected,binding_pin):
                self.owner.denied('Publication input authority/provider/current facts changed before append')
            previous = self.existing(assignment,principal,source,request_sha)
            if previous:
                returned.append(previous); return {}
            state = service.store._decode(rows[service.store.namespace][None])
            governance = service.governance.store._decode(rows[service.governance.store.namespace][None])
            creation_domains = {service.store.namespace:(None,),service.governance.store.namespace:(None,)}
            def compose(creation):
                nonlocal state
                if creation.service is not service or creation.domains != creation_domains or creation.catalog_guard is not None:
                    self.owner.denied('Publication input governance transaction differs')
                state = creation.updater(state,governance)
            def update(updater):
                nonlocal state
                state = updater(state); return state
            metadata = {'assignment_id':assignment.id,'assignment_fence':assignment.fence,'worker_id':assignment.assigned_worker_id,
                'requester_identity_id':requester.identity_id,'request_sha256':request_sha,
                'definition':request.definition.model_dump_json(),'provider_binding_sha256':hashlib.sha256(encoded(binding_pin)).hexdigest(),
                'provider_observation':fact.model_dump_json()}
            scope = dict(project_id=assignment.project_id,work_item_ref=work.ref,execution_id=assignment.execution_id,
                execution_workspace_id=workspace.id,resource_ids=(request.repository_resource_id,),provider=METHOD,
                source=METHOD,external_id=source,metadata=metadata)
            artifact = service.create_artifact(ArtifactCreate(**scope,artifact_type='report',name='Governed existing-MR publication input',
                revision=observed['head'],digest=sha256_digest(content)),actor=principal,idempotency_key=source,_commit=compose)
            artifact = service.attach_artifact_content(artifact.id,(content,),actor=principal,media_type='application/json',
                expected_sha256=hashlib.sha256(content).hexdigest(),_state=state,_commit=update)
            evidence = service.create_evidence(EvidenceCreate(**scope,evidence_type='artifact_verification',result='info',
                artifact_ids=(artifact.id,),digest=sha256_digest(content),observed_at=fact.observed_at,
                summary='Current publication input observation; not publication, QA or acceptance'),actor=principal,
                idempotency_key=source,_state=state,_commit=compose)
            returned.append({'procedure_input':artifact.model_dump(mode='json'),'evidence':evidence.model_dump(mode='json'),
                'source':source,'request_sha256':request_sha,'source_information_only':True,'deduplicated':False})
            return {service.store.namespace:{None:state.model_dump(mode='json')},
                service.governance.store.namespace:{None:governance.model_dump(mode='json')}}
        await run_settled(lambda:service.store.store.record_mutate_many(domains,persist))
        return returned[0]
