"""Existing worker Verification owner for bounded original CI prerequisite review.

This never produces the input facts or grants helper authority. The actual
canonical worker must be independent of every original referenced producer.
"""
from __future__ import annotations

import hashlib
import json
from typing import Literal
from types import SimpleNamespace

from pydantic import BaseModel, ConfigDict, Field

from codex_web.artifact_evidence import VerificationCreate
from codex_web.services.code_host_ci_observation import (
    PREREQUISITE_RECEIPT_METHOD, CanonicalCIPrerequisiteValidator, parse_ci_manifest,
)

CI_METHOD = PREREQUISITE_RECEIPT_METHOD


class CIPrerequisiteReviewRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, strict=True)
    method: Literal['ci-dependency-readiness.v1']
    kind: Literal['dependency', 'runtime', 'target']
    work_item_ref: str = Field(min_length=1, max_length=240)
    artifact_id: str = Field(pattern=r'^artifact-[0-9a-f]{32}$')
    evidence_id: str = Field(pattern=r'^evidence-[0-9a-f]{32}$')
    expected_manifest_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    expected_head_sha: str = Field(pattern=r'^[0-9a-f]{40}$')
    target_resource_id: str = Field(pattern=r'^[A-Za-z0-9._:-]{1,200}$')
    request_id: str = Field(pattern=r'^[0-9a-f]{32}$')


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':')).encode()


class WorkerCIPrerequisiteVerificationAdapter:
    def __init__(self, adapter):
        self.adapter, self.broker = adapter, adapter.broker

    @staticmethod
    def assignment_marker(assignment):
        return 'ci_readiness_reviewer:' + hashlib.sha256(canonical({
            'assignment':assignment.id,'execution':assignment.execution_id,'fence':assignment.fence})).hexdigest()

    def source(self, assignment, request):
        return 'sha256:' + hashlib.sha256(canonical({'reviewer':self.assignment_marker(assignment),
            'request':request.model_dump(mode='json')})).hexdigest()

    def existing(self, assignment, worker, ref, source, *, expected=None):
        records = [v for v in self.adapter.service().list_verifications(worker,work_item_ref=ref)
            if v.method == CI_METHOD and v.source == source and v.verifier_identity_id == worker.identity_id]
        if len(records) > 1:
            self.adapter.denied('Ambiguous original CI review; reconcile without replay')
        if not records:
            return None
        record = records[0]
        if (self.assignment_marker(assignment) not in record.findings
                or record.result != 'verified' or not record.independent
                or expected is not None and (record.artifact_ids != (expected.artifact_id,)
                    or record.evidence_ids != (expected.evidence_id,)
                    or 'ci_readiness_manifest_sha256:'+expected.expected_manifest_sha256 not in record.findings)):
            self.adapter.denied('Original CI review identity or attribution changed')
        service = self.adapter.service()
        for item in [*(service.get_artifact(i,actor=worker) for i in record.artifact_ids),
                     *(service.get_evidence(i,actor=worker) for i in record.evidence_ids)]:
            self.adapter.visible(item,assignment,worker)
        return {'item':record.model_dump(mode='json'),'deduplicated':True,
            'no_privileged_operation_authorization':True,'requires_fresh_readiness_validation':True}

    def lookup(self, assignment, worker_actor, resolved, validator):
        import re
        q = resolved.query
        if (set(q) != {'method','work_item_ref','source'} or any(len(v)!=1 for v in q.values())
                or q['method'] != [CI_METHOD] or not re.fullmatch(r'sha256:[0-9a-f]{64}',q['source'][0])):
            self.adapter.denied('CI lookup requires exact method, Work and original source digest')
        worker,_,_ = self.adapter.principals(assignment,worker_actor,resolved,validator,{})
        self.adapter.scope(assignment,q['work_item_ref'][0])
        original = self.existing(assignment,worker,q['work_item_ref'][0],q['source'][0])
        self.adapter.principals(assignment,worker_actor,resolved,validator,{})
        return {'outcome':'unique' if original else 'unknown','original':original,
            'absence_is_not_permission_to_replay':True}

    def create(self, assignment, worker_actor, resolved, payload, validator):
        try:
            value = CIPrerequisiteReviewRequest.model_validate(payload)
        except ValueError:
            self.adapter.denied('Invalid CI review; caller facts, result and identity overrides forbidden')
        worker,requester,_ = self.adapter.principals(assignment,worker_actor,resolved,validator,payload)
        self.adapter.scope(assignment,value.work_item_ref)
        if value.target_resource_id not in assignment.resource_ids:
            self.adapter.denied('CI target requires current explicit assignment Resource admission')
        self.broker._authorize(assignment,resolved.operation,actor=requester,
            resource_ids=(value.target_resource_id,))
        source = self.source(assignment,value)
        previous = self.existing(assignment,worker,value.work_item_ref,source,expected=value)
        if previous:
            return previous
        service = self.adapter.service()
        artifact = service.get_artifact(value.artifact_id,actor=worker)
        evidence = service.get_evidence(value.evidence_id,actor=worker)
        for item in (artifact,evidence):
            self.adapter.visible(item,assignment,worker)
            self.adapter.visible(item,assignment,requester)
        if (artifact.producer_identity_id != requester.identity_id or evidence.producer_identity_id != requester.identity_id
                or worker.identity_id in (artifact.producer_identity_id,evidence.producer_identity_id)):
            self.adapter.denied('CI review requires actual principal independence from original requester-produced records')
        pointer,stream = service.open_artifact_content(artifact.id,actor=worker)
        data = bytearray()
        try:
            for chunk in stream:
                data.extend(chunk)
                if len(data)>16384:
                    self.adapter.denied('CI review content exceeds bounded contract')
        finally:
            close=getattr(stream,'close',None)
            if close: close()
        digest = hashlib.sha256(data).hexdigest()
        if (digest != value.expected_manifest_sha256 or len(data)!=pointer.size_bytes
                or artifact.digest is None or artifact.digest.value!=digest
                or evidence.digest is None or evidence.digest.value!=digest):
            self.adapter.denied('CI original Artifact/Evidence reviewed digest mismatch')
        request = SimpleNamespace(organization_id=assignment.organization_id,workspace_id=assignment.workspace_id,
            project_id=assignment.project_id,resource_ids=(self.broker._writable_repository_id(assignment),))
        try:
            body = parse_ci_manifest(data,kind=value.kind,request=request,p=value,now=self.broker._clock())
        except ValueError:
            self.adapter.denied('Original CI manifest scope, typed facts or validity is unproven')
        checker = CanonicalCIPrerequisiteValidator(service,self.broker.identity,
            self.broker.action_intents.execution.resources,workers=self.broker.execution_workers,clock=self.broker._clock)
        selected = self.adapter.guard_rows(assignment,value.work_item_ref,validator)
        if value.kind == 'runtime':
            for namespace,key in ((self.broker.execution_workers.store.assignment_namespace,body['facts']['assignment_id']),
                    (self.broker.execution_workers.store.worker_namespace,body['facts']['worker_id'])):
                selected[namespace] = tuple(dict.fromkeys((*selected.get(namespace,()),key)))
        actual={}
        def commit(updater):
            def apply(rows):
                current_worker,current_requester,decision=self.adapter.principals(assignment,worker_actor,resolved,validator,payload)
                if (not current_worker.has_same_authority(worker)
                        or not current_requester.has_same_authority(requester)):
                    self.adapter.denied('CI reviewer or requester changed before append')
                self.adapter.scope(assignment,value.work_item_ref)
                state=service.store._decode(rows[service.store.namespace][None])
                current_artifact=service.get_artifact(value.artifact_id,actor=worker,state=state)
                current_evidence=service.get_evidence(value.evidence_id,actor=worker,state=state)
                if current_artifact!=artifact or current_evidence!=evidence:
                    self.adapter.denied('Original CI records changed before review append')
                if any(v.method==CI_METHOD and v.source==source for v in state.verifications):
                    self.adapter.denied('CI original review appeared concurrently; reconcile exact source')
                if (tuple(evidence.artifact_ids)!=(artifact.id,) or evidence.result!='pass'
                        or evidence.lifecycle!='valid' or artifact.lifecycle!='active' or artifact.superseded_by_id is not None
                        or artifact.revision!=value.expected_head_sha
                        or any((x.organization_id,x.workspace_id,x.project_id,x.work_item_ref)!=(
                            assignment.organization_id,assignment.workspace_id,assignment.project_id,value.work_item_ref)
                            for x in (artifact,evidence))):
                    self.adapter.denied('CI source record scope, result or lifecycle is unproven')
                target=checker.resources.get(value.target_resource_id,requester)
                if target.lifecycle!='active' or target.operational_context is None or target.operational_context.environment!='development':
                    self.adapter.denied('CI actual target is not current Development')
                if body['expires_at']<=self.broker._clock():
                    self.adapter.denied('CI reviewed facts expired before append')
                if value.kind=='runtime':
                    facts=body['facts']
                    if facts['execution_target_kind']!='canonical-native-assignment':
                        self.adapter.denied('External CI runtime requires its own canonical attestor; native review cannot substitute')
                    checker.validate_runtime(facts,request,requester)
                    if (artifact.execution_id!=facts['execution_id'] or evidence.execution_id!=facts['execution_id']
                            or artifact.execution_workspace_id!=facts['execution_workspace_id']
                            or evidence.execution_workspace_id!=facts['execution_workspace_id']):
                        self.adapter.denied('CI runtime original attribution changed')
                updated=updater(state)
                proof=updated.verifications[-1]
                if not proof.independent:
                    self.adapter.denied('Actual CI verifier is not independent')
                self.adapter.principals(assignment,worker_actor,resolved,validator,payload)
                actual.update(record=proof,authorizer_identity_id=requester.identity_id,
                    verifier_identity_id=worker.identity_id,authority_decision_id=decision.id)
                return {service.store.namespace:{None:updated.model_dump(mode='json')}}
            service.store.store.record_mutate_many(selected,apply)
        try:
            service.create_verification(VerificationCreate(work_item_ref=value.work_item_ref,
            execution_id=artifact.execution_id if value.kind=='runtime' else assignment.execution_id,
            artifact_ids=(artifact.id,),evidence_ids=(evidence.id,),method=CI_METHOD,result='verified',source=source,
            findings=('ci_readiness_manifest_sha256:'+digest,self.assignment_marker(assignment))),actor=worker,_commit=commit)
        except ValueError:
            self.adapter.denied('CI original records or canonical current runtime refused review append')
        return {'item':actual.pop('record').model_dump(mode='json'),'attribution':actual,
            'source':source,'no_privileged_operation_authorization':True,
            'next_action':'Read exact Work/method/source after response loss; never blindly repeat POST'}
