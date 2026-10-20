"""Isolated reproduction only. Production source is intentionally unchanged."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests import test_artifact_evidence as fixtures
from codex_web.artifact_evidence import ArtifactCreate, ArtifactType, EvidenceCreate, EvidenceResult, EvidenceType
from codex_web.runtime import managed_validation_runner
from codex_web.services.managed_validation_custody import ManagedValidationCustody
from codex_web.services.artifact_evidence import ArtifactEvidenceService
from codex_web.services.data_governance import DataGovernanceService
from codex_web.storage.artifact_evidence import ArtifactEvidenceStore
from codex_web.storage.data_governance import DataGovernanceStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class InjectedCaptureFault(RuntimeError):
    pass


class ManagedCaptureSettlementTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ArtifactEvidenceTests()
        self.fixture.setUp()
        self.service = self.fixture.service
        self.assignment = SimpleNamespace(
            id='assignment-isolated-partial-capture', fence=1,
            project_id='home', work_item_ref=self.fixture.work_item.ref,
            execution_id='execution-isolated-partial-capture',
            execution_workspace_id='workspace-isolated-partial-capture',
            resource_ids=(), assigned_worker_id='worker-isolated-partial-capture')
        self.workspace = Path(self.fixture.temp.name) / 'workspace'
        self.workspace.mkdir()
        (self.workspace / 'source.py').write_text('bounded fixture source\n')
        self.capture = ManagedValidationCustody(self.service, self.assignment,
            self.workspace, self.fixture.producer)
        self.capture.prepare()
        subprocess.run([sys.executable, managed_validation_runner.__file__,
            '--source', 'source.py', '--', sys.executable, '-c', 'pass'],
            cwd=self.workspace, env={**os.environ,
                'CODEX_WEB_MANAGED_VALIDATION_DIR': str(self.capture.directory)}, check=True)
        reports = list(self.capture.directory.glob('report-*.json'))
        self.assertEqual(len(reports), 1)
        self.report = json.loads(reports[0].read_text())
        self.assertEqual((self.report['state'], self.report['exit_code']), ('finished', 0))

    def tearDown(self):
        self.fixture.tearDown()

    def independent(self):
        backend = SQLiteStateStore(Path(self.fixture.temp.name) / 'state.sqlite3')
        return ArtifactEvidenceService(ArtifactEvidenceStore(backend),
            governance=DataGovernanceService(DataGovernanceStore(backend)),
            content=self.fixture.content_service,
            content_backend_resolver=lambda _actor, _project: 'local')

    def captures_and_evidence(self):
        service = self.independent()
        return ([a for a in service.list_artifacts(self.fixture.producer)
                 if a.source == 'native-validation-capture'],
                [e for e in service.list_evidence(self.fixture.producer)
                 if e.source == 'native-validation-capture'])

    def assert_complete_once(self):
        artifacts, evidence = self.captures_and_evidence()
        self.assertEqual(len(artifacts), 1, 'one canonical report Artifact per registered run')
        self.assertIsNotNone(artifacts[0].content)
        self.assertEqual(len(evidence), 1,
            'capture retry must complete exactly one informational Evidence after content persisted')
        self.assertEqual(evidence[0].artifact_ids, (artifacts[0].id,))
        self.assertEqual(evidence[0].metadata['run_id'], self.report['run_id'])
        self.assertEqual(evidence[0].result, EvidenceResult.INFO)
        self.assertEqual(evidence[0].metadata['result'], 'unknown')
        self.assertTrue(artifacts[0].governance_record_id)
        self.assertTrue(evidence[0].governance_record_id)
        _, content = self.independent().open_artifact_content(artifacts[0].id,
            actor=self.fixture.producer)
        self.assertEqual(json.loads(b''.join(content)), self.report)

    def test_evidence_precommit_fault_then_retry_must_finish_missing_evidence(self):
        with patch.object(self.service, 'create_evidence',
                          side_effect=InjectedCaptureFault('isolated precommit Evidence fault')):
            with self.assertRaises(InjectedCaptureFault):
                self.capture.capture()
        artifacts, evidence = self.captures_and_evidence()
        self.assertEqual(len(artifacts), 1)
        self.assertIsNotNone(artifacts[0].content)
        self.assertEqual(evidence, [])
        self.capture.capture()
        self.assertEqual(self.capture.state, 'captured')
        self.assert_complete_once()

    def test_evidence_commit_then_response_loss_retry_must_not_duplicate(self):
        create = self.service.create_evidence
        def commit_then_lose_response(*args, **kwargs):
            create(*args, **kwargs)
            raise InjectedCaptureFault('isolated response loss after real Evidence commit')
        with patch.object(self.service, 'create_evidence', side_effect=commit_then_lose_response):
            with self.assertRaises(InjectedCaptureFault):
                self.capture.capture()
        self.assert_complete_once()
        self.capture.capture()
        self.capture.capture()
        self.assert_complete_once()

    def test_content_precommit_fault_retry_reuses_existing_artifact(self):
        with patch.object(self.service, 'attach_artifact_content',
                          side_effect=InjectedCaptureFault('isolated precommit content fault')):
            with self.assertRaises(InjectedCaptureFault):
                self.capture.capture()
        artifacts, evidence = self.captures_and_evidence()
        self.assertEqual(len(artifacts), 1)
        self.assertIsNone(artifacts[0].content)
        self.assertEqual(evidence, [])
        self.capture.capture()
        self.assert_complete_once()

    def test_committed_unkeyed_legacy_evidence_is_reused(self):
        create = self.service.create_evidence
        def legacy_commit_then_lose_response(payload, *, actor, **_kwargs):
            create(payload.model_copy(update={'external_id': None, 'observed_at': None}), actor=actor)
            raise InjectedCaptureFault('legacy Evidence committed before response loss')
        with patch.object(self.service, 'create_evidence', side_effect=legacy_commit_then_lose_response):
            with self.assertRaises(InjectedCaptureFault):
                self.capture.capture()
        self.capture.capture()
        self.capture.capture()
        self.assert_complete_once()
        _, evidence = self.captures_and_evidence()
        self.assertIsNone(evidence[0].external_id)
        self.assertNotIn('idempotency_payload_sha256', evidence[0].metadata)

    def test_changed_report_does_not_reinterpret_committed_content(self):
        self.capture.capture()
        path = next(self.capture.directory.glob('report-*.json'))
        changed = {**self.report, 'exit_code': 1}
        path.write_text(json.dumps(changed))
        with self.assertRaisesRegex(ValueError, 'content or provenance differs'):
            self.capture.capture()
        self.assertEqual(self.capture.state, 'unavailable')
        self.assert_complete_once()  # Original immutable report and INFO Evidence remain exact.

    def test_other_fence_evidence_cannot_complete_capture(self):
        with patch.object(self.service, 'create_evidence', side_effect=InjectedCaptureFault):
            with self.assertRaises(InjectedCaptureFault):
                self.capture.capture()
        artifacts, _ = self.captures_and_evidence()
        self.service.create_evidence(EvidenceCreate(
            evidence_type=EvidenceType.TEST_RESULT, artifact_ids=(artifacts[0].id,),
            project_id=self.assignment.project_id, work_item_ref=self.assignment.work_item_ref,
            execution_id=self.assignment.execution_id,
            execution_workspace_id=self.assignment.execution_workspace_id,
            provider=self.capture.provider, source='native-validation-capture',
            result=EvidenceResult.INFO, metadata={'observed_fence': 2,
                'run_id': self.report['run_id'], 'observed_exit_code': 0, 'result': 'unknown'}),
            actor=self.fixture.producer)
        with self.assertRaisesRegex(ValueError, 'Evidence differs'):
            self.capture.capture()
        self.assertEqual(self.capture.state, 'unavailable')
        self.assertEqual(len(self.captures_and_evidence()[1]), 1)

    def test_invalidated_artifact_is_not_recaptured(self):
        self.capture.capture()
        artifacts, _ = self.captures_and_evidence()
        self.service.invalidate_artifact(artifacts[0].id, 'Independent invalidation',
            actor=self.fixture.producer)
        with self.assertRaisesRegex(ValueError, 'content or provenance differs'):
            self.capture.capture()
        self.assertEqual(self.capture.state, 'unavailable')
        self.assertEqual(len(self.captures_and_evidence()[0]), 1)

    def test_invalidated_evidence_is_not_replaced(self):
        self.capture.capture()
        _, evidence = self.captures_and_evidence()
        self.service.invalidate_evidence(evidence[0].id, 'Independent invalidation',
            actor=self.fixture.producer)
        with self.assertRaisesRegex(ValueError, 'Evidence differs'):
            self.capture.capture()
        self.assertEqual(self.capture.state, 'unavailable')
        self.assertEqual(len(self.captures_and_evidence()[1]), 1)

    def test_partial_artifact_with_wrong_fence_refuses_without_writing_content(self):
        artifact = self.service.create_artifact(ArtifactCreate(
            artifact_type=ArtifactType.REPORT, name='Conflicting partial report',
            project_id=self.assignment.project_id, work_item_ref=self.assignment.work_item_ref,
            execution_id=self.assignment.execution_id,
            execution_workspace_id=self.assignment.execution_workspace_id,
            provider=self.capture.provider, source='native-validation-capture',
            external_id=f'{self.assignment.id}:1:{self.report["run_id"]}',
            deny_model_context=True, metadata={
                'registration_artifact_id': self.capture.registration.id,
                'assignment_id': self.assignment.id, 'worker_id': self.assignment.assigned_worker_id,
                'fence': 2, 'run_id': self.report['run_id'], 'result': 'unknown'}),
            actor=self.fixture.producer)
        self.assertIsNone(artifact.content)
        def canonical_rows():
            with sqlite3.connect(Path(self.fixture.temp.name) / 'state.sqlite3') as connection:
                return (connection.execute('SELECT * FROM state_documents ORDER BY namespace').fetchall(),
                    connection.execute('SELECT * FROM state_metadata ORDER BY key').fetchall())
        before = canonical_rows()
        with patch.object(self.service, 'attach_artifact_content',
                          wraps=self.service.attach_artifact_content) as attach:
            with self.assertRaisesRegex(ValueError, 'content or provenance differs'):
                self.capture.capture()
        attach.assert_not_called()
        self.assertEqual(canonical_rows(), before)
        self.assertEqual(self.capture.state, 'unavailable')
        self.assertIsNone(self.captures_and_evidence()[0][0].content)

    def test_concurrent_evidence_retry_settles_once_on_existing_content(self):
        with patch.object(self.service, 'create_evidence', side_effect=InjectedCaptureFault):
            with self.assertRaises(InjectedCaptureFault):
                self.capture.capture()
        second = ManagedValidationCustody(self.service, self.assignment, self.workspace,
            self.fixture.producer, registration=self.capture.registration)
        barrier = threading.Barrier(2)
        create = self.service.create_evidence
        def together(*args, **kwargs):
            barrier.wait(timeout=5)
            return create(*args, **kwargs)
        with patch.object(self.service, 'create_evidence', side_effect=together):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda item: item.capture(), (self.capture, second)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(self.capture.state, 'captured')
        self.assertEqual(second.state, 'captured')
        self.assert_complete_once()


if __name__ == '__main__':
    unittest.main()
