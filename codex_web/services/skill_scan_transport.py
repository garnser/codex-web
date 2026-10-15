"""Request-only SkillSpector operations on the canonical local execution worker.

No scanner code is imported by the control plane. The only executable operations
are version discovery and a local static JSON scan, on a credential-free scratch
assignment. Scanner installation remains an operator prerequisite.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
import uuid

from codex_web.artifact_evidence import ArtifactCreate, ArtifactDigest, ArtifactType, EvidenceCreate, EvidenceResult, EvidenceType
from codex_web.configuration import ConfigurationContext
from codex_web.execution_subjects import ExecutionSubject
from codex_web.execution_workers import ExecutionAssignmentCreate, WorkerCapability, WorkerResourceLimits
from codex_web.execution_workspaces import ExecutionWorkspaceAcquire, ExecutionWorkspaceRelease
from codex_web.services.skill_scanner_configuration import SCANNER_PROJECT, SCANNER_ROOT, validate_scanner_runtime
from codex_web.services.security_boundary import redact_boundary_payload
from codex_web.services.skill_scanners import SkillScannerError, SkillSpectorCliProvider, SkillSpectorJsonParser
from codex_web.skill_security import SkillScanMode


class ScannerUnavailable(SkillScannerError):
    """Only fixed, public blocker messages cross the API boundary."""

    def __init__(self, code, message, *, scanner_version=None, attribution=None):
        self.execution_attribution = attribution or {}
        self.scanner_version = scanner_version
        self.code = code
        super().__init__(message)


class WorkerSkillSpectorProvider:
    provider_id = 'nvidia-skillspector'

    def __init__(self, *, runtime, configuration, profiles, artifacts):
        self.runtime = runtime
        self.configuration = configuration
        self.profiles = profiles
        self.artifacts = artifacts
        self.parser = SkillSpectorJsonParser()
        self.write_admission = None

    def metadata(self):
        return {**SkillSpectorCliProvider.metadata(),
            'readiness': {'ready': False, 'state': 'not_inspected', 'blockers': [
                {'code': 'inspection_required', 'message': 'Inspect scanner readiness in the current tenant.'}]},
            'modeReadiness': {'semantic': {'ready': False, 'blockers': [
                {'code': 'semantic_transport_unavailable', 'message': 'Semantic mode requires a separately governed model credential and network transport; this transport supports static scans only.'}]}}}

    def _settings(self, actor):
        runtime = self.runtime
        if actor.tenant != runtime.control_actor.tenant:
            raise ScannerUnavailable('worker_scope_unavailable', 'No scanner worker is composed for this tenant.')
        context = ConfigurationContext(organization_id=actor.organization_id, workspace_id=actor.workspace_id)
        root_setting = self.configuration.resolve(SCANNER_ROOT, context)
        project_setting = self.configuration.resolve(SCANNER_PROJECT, context)
        if not root_setting.value or not project_setting.value:
            raise ScannerUnavailable('scanner_configuration_missing',
                f'Configure {SCANNER_ROOT} and {SCANNER_PROJECT} in Configuration before scanning.')
        root = Path(str(root_setting.value))
        try:
            root = validate_scanner_runtime(root)
        except SkillScannerError as exc:
            raise ScannerUnavailable('scanner_installation_missing', str(exc)) from exc
        project_id = str(project_setting.value)
        runtime.workspace_service._project(project_id, actor)  # Canonical tenant ownership, no ambient Project fallback.
        profile, reference = self.profiles.resolve('orchestration-only',
            organization_id=actor.organization_id, workspace_id=actor.workspace_id, project_id=project_id)
        if (profile.workspace_mode != 'scratch' or profile.repository_access != 'none'
                or profile.network_enabled or profile.host_mutation or profile.administrative_repository_ids
                or set(profile.required_worker_capabilities) != {'command_execution'}):
            raise ScannerUnavailable('scanner_profile_incompatible', 'The effective orchestration-only definition is incompatible with credential-free static scanning.')
        status = runtime.backend.probe()
        if not status.ready:
            raise ScannerUnavailable('isolated_transport_unavailable', 'The canonical isolated worker backend is unavailable. Inspect Execution Workers isolation diagnostics.')
        worker = runtime.worker_service.store.worker(runtime.worker.id)
        if (worker is None or worker.lifecycle != 'active'
                or WorkerCapability.COMMAND_EXECUTION not in worker.capabilities
                or 'skill-scan/1.0' not in worker.supported_execution_contract_versions
                or 'read-only' not in worker.supported_sandbox_profiles):
            raise ScannerUnavailable('scanner_worker_incompatible', 'Activate a compatible local worker advertising command_execution, read-only and skill-scan/1.0.')
        if runtime.worker_service._active_count(runtime.worker_service.store.load(), worker.id, time.time()) >= worker.max_concurrency:
            raise ScannerUnavailable('scanner_worker_busy', 'The scanner worker has no free concurrency slot. Retry explicitly when current assignments finish.')
        return root, project_id, reference, (root_setting, project_setting)

    def inspect(self, *, actor):
        result = self.metadata()
        try:
            root, project, reference, settings = self._settings(actor)
            completion = self._operation(actor, root, project, reference)
            version = self._version(completion.result.stdout)
            result['readiness'] = {'ready': True, 'state': 'ready', 'blockers': [],
                'scannerVersion': version, 'workerId': self.runtime.worker.id,
                'assignmentId': completion.assignment.id, 'executionWorkspaceId': completion.assignment.execution_workspace_id,
                'projectId': project, 'executionProfileDefinition': reference.model_dump(mode='json'),
                'configurationRecordIds': [item.record_id for item in settings]}
        except ScannerUnavailable as exc:
            result['readiness'] = {'ready': False, 'state': 'unavailable', 'scannerVersion': exc.scanner_version, 'blockers': [{'code': exc.code, 'message': str(exc)}]}
        except Exception:
            result['readiness'] = {'ready': False, 'state': 'unavailable', 'blockers': [{
                'code': 'scanner_discovery_failed', 'message': 'Isolated scanner discovery failed. Inspect canonical worker assignments and configuration.'}]}
        return result

    @staticmethod
    def _version(stdout):
        match = re.fullmatch(r'\s*SkillSpector v([0-9]+\.[0-9]+\.[0-9]+)\s*', stdout)
        if match is None or match.group(1) != '2.12.0':
            raise ScannerUnavailable('scanner_version_incompatible', 'This adapter supports the pinned SkillSpector 2.12.0 CLI contract. Inspect the installed scanner version.', scanner_version=match.group(1) if match else None)
        return match.group(1)

    def _operation(self, actor, *args, **kwargs):
        if self.write_admission is None:
            raise ScannerUnavailable('scanner_upgrade_required', 'Scanner persisted-contract upgrade admission is unavailable.')
        from codex_web.services.upgrades import UpgradeConflictError
        try:
            with self.write_admission(actor):
                return self._admitted_operation(actor, *args, **kwargs)
        except UpgradeConflictError as exc:
            raise ScannerUnavailable('scanner_upgrade_required', str(exc)) from exc

    def _admitted_operation(self, actor, root, project_id, reference, *, content=None, scan_id=None):
        runtime = self.runtime
        execution_id = scan_id or f'skill-discovery-{uuid.uuid4().hex}'
        subject = ExecutionSubject(kind='skill_scan', ref=execution_id)
        seconds = 120 if content is not None else 10
        workspace = runtime.workspace_service.acquire(ExecutionWorkspaceAcquire(subject=subject,
            execution_id=execution_id, project_id=project_id, resource_ids=(), scratch=True,
            ttl_seconds=seconds + 30, requested_disk_bytes=64 * 1024 * 1024), actor=actor)
        assignment = None
        try:
            assignment = runtime.worker_service.create_assignment(ExecutionAssignmentCreate(
                subject=subject, execution_id=execution_id, project_id=project_id, resource_ids=(),
                execution_contract_version='skill-scan/1.0', required_capabilities=(WorkerCapability.COMMAND_EXECUTION,),
                sandbox='read-only', approval_policy='never', deadline_at=time.time() + seconds + 10,
                limits=WorkerResourceLimits(cpu_seconds=seconds, wall_seconds=seconds, memory_bytes=2 * 1024**3,
                    disk_bytes=64 * 1024**2, process_count=64),
                execution_profile_id='orchestration-only', execution_profile_definition=reference,
                execution_workspace_id=workspace.id, expected_artifact_types=('report',)), actor=actor)
            with tempfile.TemporaryDirectory(prefix='skill-scan-input-') as directory:
                input_directory = None
                if content is not None:
                    input_directory = Path(directory)
                    path = input_directory / 'SKILL.md'
                    path.write_text(content, encoding='utf-8')
                    path.chmod(0o600)
                completion = runtime.execute_skill_scanner(assignment.id, runtime_root=root, input_directory=input_directory)
            if (completion.assignment.status != 'succeeded' or completion.result.output_truncated
                    or len(completion.result.stdout.encode()) > 2_000_000 or len(completion.result.stderr.encode()) > 64_000):
                raise ScannerUnavailable('scanner_execution_failed', 'The isolated scanner command failed or exceeded its limits. Inspect its canonical worker assignment.')
            return completion
        except Exception as exc:
            attribution = {"execution_id": execution_id, "execution_workspace_id": workspace.id}
            if assignment is not None:
                current = runtime.worker_service.store.assignment(assignment.id)
                attribution.update(assignment_id=assignment.id, worker_id=current.assigned_worker_id if current else None,
                    fence=current.fence if current else assignment.fence)
            evidence = self.artifacts.create_evidence(EvidenceCreate(project_id=project_id, execution_id=execution_id,
                execution_workspace_id=workspace.id, evidence_type=EvidenceType.POLICY_EVALUATION,
                result=EvidenceResult.FAIL, provider=self.provider_id, source=f'skill-scan:{execution_id}',
                summary='Isolated scanner transport did not produce an accepted result.', metadata=attribution), actor=actor)
            attribution['evidence_id'] = evidence.id
            raise ScannerUnavailable('scanner_execution_failed', 'The isolated scanner command failed or exceeded its limits. Inspect its canonical worker assignment.', attribution=attribution) from exc
        finally:
            runtime.workspace_service.release(workspace.id,
                ExecutionWorkspaceRelease(discard=True, reason='request-driven scanner operation terminal'), actor=actor)

    def scan_revision(self, *, view, content, mode, actor):
        if mode != SkillScanMode.STATIC:
            raise ScannerUnavailable('semantic_transport_unavailable', self.metadata()['modeReadiness']['semantic']['blockers'][0]['message'])
        if len(content.encode('utf-8')) > 512_000:
            raise ScannerUnavailable('scanner_input_limit', 'Skill content exceeds the 512 KiB scanner input limit.')
        root, project, reference, settings = self._settings(actor)
        executable = root / 'bin' / 'skillspector'
        if executable.stat().st_size > 1_000_000:
            raise ScannerUnavailable('scanner_launcher_incompatible', 'The scanner launcher exceeds its bounded discovery size.')
        launcher_checksum = hashlib.sha256(executable.read_bytes()).hexdigest()
        discovered = self._operation(actor, root, project, reference)
        version = self._version(discovered.result.stdout)
        scan_id = f'skill-scan-{uuid.uuid4().hex}'
        completion = self._operation(actor, root, project, reference, content=content, scan_id=scan_id)
        if hashlib.sha256(executable.read_bytes()).hexdigest() != launcher_checksum:
            raise ScannerUnavailable('scanner_installation_changed', 'Scanner installation changed during execution. Inspect installation before explicitly retrying.')
        try:
            report = redact_boundary_payload(json.loads(completion.result.stdout))
            parsed = self.parser.parse_native_static(report, scanner_version=version)
        except (ValueError, TypeError, SkillScannerError) as exc:
            attribution = dict(assignment_id=completion.assignment.id, execution_id=scan_id,
                execution_workspace_id=completion.assignment.execution_workspace_id,
                worker_id=completion.assignment.assigned_worker_id, fence=completion.assignment.fence)
            raise ScannerUnavailable('scanner_report_invalid', 'The isolated scanner report failed the supported JSON, completeness, static-mode or version contract.',
                scanner_version=version, attribution=attribution) from exc
        # Version is from the isolated executable, never the untrusted report.
        attribution = dict(assignment_id=completion.assignment.id, execution_id=scan_id,
            execution_workspace_id=completion.assignment.execution_workspace_id,
            worker_id=completion.assignment.assigned_worker_id, fence=completion.assignment.fence,
            skill_revision=view['revision'],
            input_sha256=hashlib.sha256(content.encode()).hexdigest(), scanner_launcher_sha256=launcher_checksum,
            discovery_assignment_id=discovered.assignment.id,
            runtime_configuration_record_id=settings[0].record_id, runtime_configuration_revision=settings[0].revision,
            project_configuration_record_id=settings[1].record_id, project_configuration_revision=settings[1].revision,
            execution_profile_record_id=reference.record_id, execution_profile_checksum=reference.checksum)
        safe_report = {**parsed.model_dump(mode='json'), 'scanner_version': version,
            'record_id': view['recordId'], 'checksum': view['checksum'], 'skill_id': view['skillId'],
            'attribution': attribution}
        report_bytes = json.dumps(safe_report, sort_keys=True).encode()
        artifact = self.artifacts.create_artifact(ArtifactCreate(project_id=project, execution_id=scan_id,
            execution_workspace_id=completion.assignment.execution_workspace_id, artifact_type=ArtifactType.REPORT,
            name='Skill static scan normalized report', provider=self.provider_id, source=f'skill:{view["recordId"]}',
            revision=view['checksum'], digest=ArtifactDigest(algorithm='sha256', value=hashlib.sha256(report_bytes).hexdigest()),
            deny_model_context=True, metadata={'record_id':view['recordId'], 'checksum':view['checksum'],
                'assignment_id':completion.assignment.id, 'scanner_version':version}), actor=actor)
        self.artifacts.attach_artifact_content(artifact.id, (report_bytes,), actor=actor)
        evidence = self.artifacts.create_evidence(EvidenceCreate(project_id=project, execution_id=scan_id,
            execution_workspace_id=completion.assignment.execution_workspace_id, artifact_ids=(artifact.id,),
            evidence_type=EvidenceType.ARTIFACT_VERIFICATION, result=EvidenceResult.PASS, provider=self.provider_id,
            source=f'skill:{view["recordId"]}', summary='Isolated scanner transport completed; findings require Skill security policy evaluation.',
            metadata={**attribution, 'record_id':view['recordId'], 'checksum':view['checksum'], 'scanner_version':version}), actor=actor)
        return parsed.model_copy(update={'scanner_version': version, 'report_artifact_id': artifact.id,
            'execution_attribution': {**attribution, 'evidence_id': evidence.id}})
