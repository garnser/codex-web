"""Managed native output transport through canonical Artifact/Evidence storage."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat

from codex_web.artifact_evidence import ArtifactCreate, ArtifactType, EvidenceCreate, EvidenceType, EvidenceResult
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

    def capture(self):
        """Read only registered bounded JSON. Never discover arbitrary /tmp logs."""
        if self.registration is None or self.service is None or self.service.content is None:
            self.state = 'unavailable'
            return ()
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
                if artifact is not None and artifact.content is not None:
                    self.artifact_ids.append(artifact.id)
                    continue
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
                self.service.attach_artifact_content(artifact.id,(body,),actor=self.actor,media_type='application/json')
                self.service.create_evidence(EvidenceCreate(evidence_type=EvidenceType.TEST_RESULT,
                    artifact_ids=(artifact.id,),project_id=assignment.project_id,work_item_ref=assignment.work_item_ref,
                    execution_id=assignment.execution_id,execution_workspace_id=assignment.execution_workspace_id,
                    resource_ids=assignment.resource_ids,provider=self.provider,source='native-validation-capture',
                    result=EvidenceResult.INFO,summary='Native validation interrupted; outcome unknown' if report['state']=='running'
                        else 'Native command exit observed; validation remains unverified',deny_model_context=True,
                    metadata={'observed_fence':assignment.fence,'run_id':report['run_id'],
                              'observed_exit_code':report['exit_code'],'result':'unknown'}),actor=self.actor)
                self.artifact_ids.append(artifact.id)
            self.state = 'captured' if names else 'registered'
            return tuple(self.artifact_ids)
        finally:
            os.close(descriptor)
