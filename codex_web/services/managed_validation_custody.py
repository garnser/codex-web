"""Managed native output transport through canonical Artifact/Evidence storage."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat

from codex_web.artifact_evidence import (
    ArtifactCreate, ArtifactLifecycle, ArtifactType, EvidenceCreate, EvidenceLifecycle,
    EvidenceResult, EvidenceType,
)
from codex_web.artifact_content import ArtifactContentState
from codex_web.runtime.managed_validation_runner import sanitize_report
from codex_web.storage.state_store import check_operation_budget


class ManagedValidationCustody:
    provider = 'native-managed-validation'

    def __init__(self, service, assignment, workspace_path, actor, registration=None):
        self.service, self.assignment, self.actor = service, assignment, actor
        self.workspace_path = Path(workspace_path)
        self.parts = ('.venv', '.codex-web-validation', assignment.id, str(assignment.fence))
        self.directory = self.workspace_path.joinpath(*self.parts)
        self.registration = registration
        self.state = 'unavailable'
        self.artifact_ids = []

    def prepare(self):
        if self.service is None or self.service.content is None:
            return
        current = self.workspace_path
        for part in self.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError('managed output directory cannot be a symlink')
            current.mkdir(mode=0o700,exist_ok=True)
        # Runtime-owned outputs never enter source checkpoints/publication.
        ignore = self.directory / '.gitignore'
        fd = os.open(ignore, os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'w') as stream: stream.write('*\n')
        assignment = self.assignment
        self.registration = self.service.create_artifact(ArtifactCreate(
            artifact_type=ArtifactType.REPORT,name='Registered native validation output slot',
            project_id=assignment.project_id,work_item_ref=assignment.work_item_ref,
            execution_id=assignment.execution_id,execution_workspace_id=assignment.execution_workspace_id,
            resource_ids=assignment.resource_ids,provider=self.provider,source='native-validation-registration',
            external_id=f'{assignment.id}:{assignment.fence}:slot',deny_model_context=True,
            metadata={'assignment_id':assignment.id,'worker_id':assignment.assigned_worker_id,
                      'fence':assignment.fence,'managed_output_schema':'1.0','result':'unstarted'}),actor=self.actor)
        self.state = 'registered'

    def _validate_artifact(self, artifact, body, *, require_content=False):
        """Check retained provenance before attaching any missing content."""
        assignment = self.assignment
        expected_scope = (self.actor.organization_id, self.actor.workspace_id,
            assignment.project_id, assignment.work_item_ref, assignment.execution_id,
            assignment.execution_workspace_id, tuple(assignment.resource_ids))
        if ((artifact.organization_id, artifact.workspace_id, artifact.project_id,
                artifact.work_item_ref, artifact.execution_id, artifact.execution_workspace_id,
                artifact.resource_ids) != expected_scope
                or artifact.producer_identity_id != self.actor.identity_id
                or artifact.source != 'native-validation-capture'
                or artifact.lifecycle != ArtifactLifecycle.ACTIVE
                or artifact.metadata.get('registration_artifact_id') != self.registration.id
                or artifact.metadata.get('assignment_id') != assignment.id
                or artifact.metadata.get('worker_id') != assignment.assigned_worker_id
                or artifact.metadata.get('fence') != assignment.fence
                or (artifact.digest is not None and artifact.digest.value != hashlib.sha256(body).hexdigest())):
            raise ValueError('managed capture content or provenance differs from registered report')
        if require_content or artifact.content is not None:
            if (artifact.content is None or artifact.content.state != ArtifactContentState.AVAILABLE
                    or artifact.digest is None or artifact.content.size_bytes != len(body)):
                raise ValueError('managed capture content or provenance differs from registered report')
        return expected_scope

    def _ensure_evidence(self, artifact, report, body):
        """Finish partial settlement without duplicating committed legacy Evidence."""
        assignment = self.assignment
        expected_scope = self._validate_artifact(artifact, body, require_content=True)
        candidates = [e for e in self.service.list_evidence(self.actor)
            if e.provider == self.provider and e.source == 'native-validation-capture'
            and (artifact.id in e.artifact_ids or e.external_id == artifact.external_id)]
        if len(candidates) > 1:
            raise ValueError('managed capture has conflicting Evidence records')
        if candidates:
            evidence = candidates[0]
            if ((evidence.organization_id, evidence.workspace_id, evidence.project_id,
                    evidence.work_item_ref, evidence.execution_id, evidence.execution_workspace_id,
                    evidence.resource_ids) != expected_scope
                    or evidence.producer_identity_id != self.actor.identity_id
                    or evidence.artifact_ids != (artifact.id,)
                    or evidence.evidence_type != EvidenceType.TEST_RESULT
                    or evidence.result != EvidenceResult.INFO
                    or evidence.lifecycle != EvidenceLifecycle.VALID
                    or evidence.metadata.get('observed_fence') != assignment.fence
                    or evidence.metadata.get('run_id') != report['run_id']
                    or evidence.metadata.get('observed_exit_code') != report['exit_code']
                    or evidence.metadata.get('result') != 'unknown'):
                raise ValueError('managed capture Evidence differs from registered observation')
            return  # Includes unkeyed Evidence committed by an earlier reader.
        self.service.create_evidence(EvidenceCreate(evidence_type=EvidenceType.TEST_RESULT,
            artifact_ids=(artifact.id,),project_id=assignment.project_id,work_item_ref=assignment.work_item_ref,
            execution_id=assignment.execution_id,execution_workspace_id=assignment.execution_workspace_id,
            resource_ids=assignment.resource_ids,provider=self.provider,source='native-validation-capture',
            external_id=artifact.external_id,
            observed_at=report['finished_at'] or report['started_at'],
            result=EvidenceResult.INFO,summary='Native validation interrupted; outcome unknown' if report['state']=='running'
                else 'Native command exit observed; validation remains unverified',deny_model_context=True,
            metadata={'observed_fence':assignment.fence,'run_id':report['run_id'],
                      'observed_exit_code':report['exit_code'],'result':'unknown'}),actor=self.actor,
            idempotency_key=artifact.external_id + ':' + artifact.digest.value)

    def capture(self):
        """Read only registered bounded JSON. Never discover arbitrary /tmp logs."""
        if self.registration is None or self.service is None or self.service.content is None:
            self.state = 'unavailable'
            return ()
        self.state = 'unavailable'  # A failed retry cannot retain an earlier completed state.
        descriptor = os.open(self.workspace_path, os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:
            for part in self.parts:
                replacement = os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=descriptor)
                os.close(descriptor); descriptor = replacement
            names = []
            with os.scandir(descriptor) as entries:
                for count, entry in enumerate(entries):
                    check_operation_budget()
                    if count >= 64:
                        raise ValueError('managed output slot exceeds entry bound')
                    if re.fullmatch(r'report-[0-9a-f]{32}\.json',entry.name):
                        names.append(entry.name)
            names.sort()
            if len(names) > 4:
                self.state = 'unavailable'
                return ()
            existing = {a.external_id:a for a in self.service.list_artifacts(self.actor)
                        if a.provider == self.provider and a.execution_id == self.assignment.execution_id}
            for name in names:
                check_operation_budget()
                fd = os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=descriptor)
                with os.fdopen(fd,'rb') as source:
                    if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                        raise ValueError('managed report must be regular')
                    body = source.read(16385)
                if len(body) > 16384:
                    raise ValueError('managed validation report exceeds contract')
                report = sanitize_report(json.loads(body))
                if name != f"report-{report['run_id']}.json":
                    raise ValueError('managed report identity differs from registration slot')
                body = json.dumps(report,sort_keys=True,separators=(',',':')).encode()
                assignment = self.assignment
                external_id = f"{assignment.id}:{assignment.fence}:{report['run_id']}"
                artifact = existing.get(external_id)
                if artifact is None:
                    artifact = self.service.create_artifact(ArtifactCreate(artifact_type=ArtifactType.REPORT,
                        name='Native validation observation (unverified)',project_id=assignment.project_id,
                        work_item_ref=assignment.work_item_ref,execution_id=assignment.execution_id,
                        execution_workspace_id=assignment.execution_workspace_id,resource_ids=assignment.resource_ids,
                        provider=self.provider,source='native-validation-capture',external_id=external_id,
                        deny_model_context=True,metadata={'registration_artifact_id':self.registration.id,
                            'assignment_id':assignment.id,'worker_id':assignment.assigned_worker_id,'fence':assignment.fence,
                            'run_id':report['run_id'],'command_digest':report['command_digest'],
                            'source_manifest_digest':hashlib.sha256(json.dumps(report['sources'],sort_keys=True).encode()).hexdigest(),
                            'result':'unknown','interrupted':report['state']=='running'}),actor=self.actor)
                self._validate_artifact(artifact, body)
                if artifact.content is None:
                    artifact = self.service.attach_artifact_content(artifact.id,(body,),actor=self.actor,media_type='application/json')
                self._ensure_evidence(artifact, report, body)
                if artifact.id not in self.artifact_ids:
                    self.artifact_ids.append(artifact.id)
            self.state = 'captured' if names else 'registered'
            return tuple(self.artifact_ids)
        finally:
            os.close(descriptor)

    def blocks_assignment_renewal(self):
        """Veto renewal for running/unknown output; this alone never proves idle."""
        if self.registration is None or self.state not in {'registered', 'captured'}:
            return True
        descriptor = None
        try:
            descriptor = os.open(self.workspace_path, os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            for part in self.parts:
                replacement = os.open(part, os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = replacement
            reports = []
            with os.scandir(descriptor) as entries:
                for count, entry in enumerate(entries):
                    check_operation_budget()
                    if count >= 64:
                        return True
                    if entry.name == '.gitignore':
                        continue
                    if not re.fullmatch(r'report-[0-9a-f]{32}\.json', entry.name):
                        return True
                    reports.append(entry.name)
            if len(reports) > 4:
                return True
            for name in reports:
                fd = os.open(name, os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK, dir_fd=descriptor)
                with os.fdopen(fd, 'rb') as source:
                    if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                        return True
                    body = source.read(16385)
                if len(body) > 16384:
                    return True
                report = sanitize_report(json.loads(body))
                if name != f"report-{report['run_id']}.json" or report['state'] != 'finished':
                    return True
            return False
        except (OSError, ValueError, TypeError):
            return True
        finally:
            if descriptor is not None:
                os.close(descriptor)
