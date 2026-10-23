"""Scanner observations preserve real returned metadata without accepting a scan."""
import contextlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from codex_web.execution_workspace_backend import LocalGitWorkspaceBackend
from codex_web.execution_workers import WorkerCapability
from codex_web.local_execution_backend import LocalExecutionResult
from codex_web.models import Project
from codex_web.services.artifact_evidence import ArtifactEvidenceService
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.services.execution_profile_definitions import install_execution_profile_definitions
from codex_web.services.execution_workers import ExecutionWorkerService
from codex_web.services.execution_workspaces import ExecutionWorkspaceService
from codex_web.services.identity import IdentityService
from codex_web.services.local_execution_worker import LocalExecutionWorkerRuntime
from codex_web.services.resources import ResourceCatalogService
from codex_web.services.skill_scan_transport import ScannerUnavailable, WorkerSkillSpectorProvider
from codex_web.storage.artifact_evidence import ArtifactEvidenceStore
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.execution_workers import ExecutionWorkerStore
from codex_web.storage.execution_workspaces import ExecutionWorkspaceStateStore
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.resource_catalog import ResourceCatalogStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from tests.test_local_execution_worker import _FakeExecutionBackend


class SkillScannerPhaseEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        state = SQLiteStateStore(self.root / 'state.sqlite3')
        self.identity = IdentityService(IdentityStateStore(state))
        self.identity.bootstrap_local()
        self.actor = self.identity.local_trusted_actor()
        worker_actor = self.identity.bootstrap_service_actor(identity_id='scanner-test', name='Scanner',
            scope=self.actor.tenant, service_scopes=('execution-worker:run',))
        project = Project(id='scan-test', name='Scan', path=str(self.root),
            organization_id=self.actor.organization_id, workspace_id=self.actor.workspace_id)
        self.workspaces = ExecutionWorkspaceService(ExecutionWorkspaceStateStore(state),
            LocalGitWorkspaceBackend(self.root/'workspaces'), ResourceCatalogService(ResourceCatalogStore(state)),
            lambda project_id: project)
        self.workers = ExecutionWorkerService(ExecutionWorkerStore(state), identity=self.identity,
            workspaces=self.workspaces)
        # This fixture exercises the worker/transport, not Upgrade authorization.
        self.workers.skill_scan_admission = lambda actor: contextlib.nullcontext()
        self.workspaces.skill_scan_admission = lambda actor: contextlib.nullcontext()
        worker = self.workers.ensure_local_worker(service_identity_id=worker_actor.identity_id,
            version='fixture', capabilities=(WorkerCapability.COMMAND_EXECUTION,),
            supported_execution_contract_versions=('skill-scan/1.0',), actor=self.actor)
        self.result = LocalExecutionResult('skillspector', 'sha256:'+'a'*64, 0,
            'PRIVATE STDOUT', 'PRIVATE STDERR', .125)
        self.backend = _FakeExecutionBackend(self.result)
        self.artifacts = ArtifactEvidenceService(ArtifactEvidenceStore(state))
        self.runtime = LocalExecutionWorkerRuntime(self.workers, self.workspaces, self.backend,
            worker=worker, worker_actor=worker_actor, control_actor=self.actor)
        profiles = install_execution_profile_definitions(DefinitionRegistryService(DefinitionRegistryStore(state)))
        _, self.reference = profiles.resolve('orchestration-only', organization_id=self.actor.organization_id,
            workspace_id=self.actor.workspace_id, project_id=project.id)
        self.provider = WorkerSkillSpectorProvider(runtime=self.runtime, configuration=None,
            profiles=profiles, artifacts=self.artifacts)
        # No scanner is installed/launched: only the backend result is controlled.
        validator = patch('codex_web.services.skill_scanner_configuration.validate_scanner_runtime',
                          side_effect=lambda root: root)
        validator.start()
        self.addCleanup(validator.stop)

    def run_failure(self):
        with self.assertRaises(ScannerUnavailable) as caught:
            self.provider._admitted_operation(self.actor, self.root, 'scan-test', self.reference)
        attribution = caught.exception.execution_attribution
        evidence = self.artifacts.get_evidence(attribution['evidence_id'], actor=self.actor)
        self.assertFalse(evidence.metadata.get('scanner_result_accepted', False))
        self.assertNotIn('PRIVATE', json.dumps(evidence.model_dump(mode='json')))
        self.assertNotIn('scanner_duration_seconds', attribution, 'public attribution shape stays unchanged')
        return evidence.metadata, self.workers.store.assignment(attribution['assignment_id'])

    def test_actual_returned_result_survives_canonical_completion_failure(self):
        self.workers.complete = Mock(side_effect=RuntimeError('completion storage unavailable'))
        observed, assignment = self.run_failure()
        self.assertEqual(observed.get('scanner_phase'), 'backend_returned')
        self.assertEqual(observed['scanner_exit_code'], 0)
        self.assertEqual(observed['scanner_duration_seconds'], .125)
        self.assertFalse(observed['scanner_completion_returned'])
        self.assertFalse(observed['scanner_timed_out'])
        self.assertEqual(assignment.status, 'running')
        self.assertEqual(len(self.backend.calls), 1)
        self.workers.complete.assert_called_once()

    def test_backend_exception_retains_unknown_result_without_retry(self):
        self.backend.run = Mock(side_effect=RuntimeError('backend outcome unavailable'))
        observed, _ = self.run_failure()
        self.assertEqual(observed['scanner_phase'], 'backend_unresolved')
        self.assertIsNone(observed['scanner_exit_code'])
        self.assertIsNone(observed['scanner_duration_seconds'])
        self.backend.run.assert_called_once()

    def test_prebackend_refusal_never_claims_process_started(self):
        self.backend.validate_assignment = Mock(side_effect=RuntimeError('backend incompatible'))
        observed, _ = self.run_failure()
        self.assertEqual(observed['scanner_phase'], 'before_backend')
        self.assertIsNone(observed['scanner_exit_code'])
        self.assertEqual(self.backend.calls, [])

    def test_final_poll_refusal_preserves_returned_result_without_acceptance(self):
        run = self.backend.run

        def expire_after_result(assignment, **kwargs):
            result = run(assignment, **kwargs)
            def expire(state):
                current = state.assignments[0]
                state.assignments[0] = current.model_copy(update={
                    'lease': current.lease.model_copy(update={'expires_at': time.time() - 1})})
                return state
            self.workers.store.update_assignment(self.runtime.worker.id, assignment.id, expire)
            return result

        self.backend.run = expire_after_result
        observed, assignment = self.run_failure()
        self.assertEqual(observed['scanner_phase'], 'backend_returned')
        self.assertEqual(observed['scanner_exit_code'], 0)
        self.assertFalse(observed['scanner_completion_returned'])
        self.assertNotEqual(assignment.status, 'succeeded')
        self.assertEqual(len(self.backend.calls), 1)

    def test_terminal_failed_result_is_observation_not_scan_acceptance(self):
        self.backend.result = LocalExecutionResult('skillspector', 'sha256:'+'b'*64, 7,
            'PRIVATE STDOUT', 'PRIVATE STDERR', .25)
        observed, assignment = self.run_failure()
        self.assertEqual(observed['scanner_phase'], 'backend_returned')
        self.assertEqual(observed['scanner_exit_code'], 7)
        self.assertTrue(observed['scanner_completion_returned'])
        self.assertEqual(observed['scanner_assignment_status'], 'failed')
        self.assertEqual(assignment.status, 'failed')
        self.assertEqual(len(self.backend.calls), 1)

    def test_successful_scan_keeps_existing_completion_and_no_failure_evidence(self):
        completed = self.provider._admitted_operation(self.actor, self.root, 'scan-test', self.reference)
        self.assertEqual(completed.assignment.status, 'succeeded')
        self.assertEqual(completed.result, self.result)
        self.assertEqual(self.artifacts.list_evidence(self.actor), [])
        self.assertEqual(len(self.backend.calls), 1)
