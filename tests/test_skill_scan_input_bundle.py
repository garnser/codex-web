"""Structured Skill bytes stay data; bundle validation precedes scanner custody."""
import contextlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from codex_web.local_execution_backend import LocalExecutionResult
from codex_web.services.skill_scan_input import build_skill_scan_input, PROCEDURE_PATH, ASSET_METADATA_PATH
from codex_web.services.skill_scan_transport import ScannerUnavailable
from codex_web.services.skill_security import SkillSecurityService
from tests import test_skill_scanner_phase_evidence as scanner_fixture


def view():
    return {'skillId': 'private', 'recordId': 'private-record', 'checksum': 'b' * 64,
            'revision': 1, 'skill': {'name': 'Private structured Skill',
                'description': 'Original description',
                'instructions': 'Inspect [policy](reference/policy.txt) without changing work.',
                'assets': [{'path': 'reference/policy.txt', 'content': 'Original policy bytes.\n', 'kind': 'reference'}],
                'operational_procedure': {'schema_version': '1.2',
                    'input_protocol': 'code-host-mr-procedure-input.v1',
                    'operation': 'repository.read.metadata', 'steps': []}}}


class SkillScanInputTests(unittest.TestCase):
    def test_full_procedure_and_assets_are_preserved_as_inspected_files(self):
        original = view()
        bundle = build_skill_scan_input(original)
        files = dict(bundle.files)
        self.assertEqual(json.loads(files[PROCEDURE_PATH]), original['skill']['operational_procedure'])
        self.assertEqual(files['reference/policy.txt'], b'Original policy bytes.\n')
        self.assertEqual(json.loads(files[ASSET_METADATA_PATH]),
                         [{'path': 'reference/policy.txt', 'kind': 'reference'}])
        primary = files['SKILL.md'].decode()
        self.assertIn(original['skill']['instructions'], primary)
        self.assertIn(PROCEDURE_PATH, primary)
        self.assertNotIn('code-host-mr-procedure-input.v1', primary)
        self.assertEqual(bundle.manifest_sha256, build_skill_scan_input(original).manifest_sha256)
        changed = view(); changed['skill']['assets'][0]['content'] += 'Changed.'
        self.assertNotEqual(bundle.manifest_sha256, build_skill_scan_input(changed).manifest_sha256)
        with tempfile.TemporaryDirectory() as directory:
            bundle.write(Path(directory))
            self.assertEqual((Path(directory) / 'reference/policy.txt').read_bytes(), files['reference/policy.txt'])
            self.assertEqual((Path(directory) / PROCEDURE_PATH).stat().st_mode & 0o111, 0)

    def test_ordinary_content_without_bundle_data_retains_original_primary(self):
        original = view(); original['skill']['assets'] = []; original['skill']['operational_procedure'] = None
        self.assertEqual(dict(build_skill_scan_input(original).files)['SKILL.md'].decode(),
                         SkillSecurityService._content(original))

    def test_contained_url_encoded_asset_link_preserves_filename(self):
        original = view(); original['skill']['assets'][0]['path'] = 'reference/policy (reviewed).txt'
        files = dict(build_skill_scan_input(original).files)
        self.assertIn('reference/policy (reviewed).txt', files)
        self.assertIn(b'reference/policy%20%28reviewed%29.txt', files['SKILL.md'])
        nested = 'é' * 100 + '/' + 'é' * 100
        original['skill']['assets'][0]['path'] = nested
        self.assertIn(nested, dict(build_skill_scan_input(original).files))

    def test_invalid_unsupported_and_reserved_paths_refuse(self):
        for path in ('../escape', '/absolute', 'a/../escape', 'a\\b', 'a//b', 'a/./b',
                     'a\nb', '.hidden', 'a/.hidden', 'SKILL.md', PROCEDURE_PATH, ASSET_METADATA_PATH, 'é' * 130 + '.txt', '\ud800.txt'):
            with self.subTest(path=path):
                original = view(); original['skill']['assets'][0]['path'] = path
                with self.assertRaises(ValueError): build_skill_scan_input(original)

    def test_duplicate_and_file_directory_collisions_refuse(self):
        for paths in (('a', 'a'), ('a', 'a/b'), ('SKILL.md/a', 'other')):
            with self.subTest(paths=paths):
                original = view(); original['skill']['assets'] = [
                    {'path': path, 'content': 'bytes'} for path in paths]
                with self.assertRaises(ValueError): build_skill_scan_input(original)

    def test_count_and_aggregate_byte_bounds_refuse(self):
        original = view(); original['skill']['assets'] = [
            {'path': str(index) + '.txt', 'content': ''} for index in range(33)]
        with self.assertRaises(ValueError): build_skill_scan_input(original)
        original = view(); original['skill']['assets'][0]['content'] = 'x' * 512_000
        with self.assertRaises(ValueError): build_skill_scan_input(original)


class SkillScanInputTransportTests(unittest.TestCase):
    setUp = scanner_fixture.SkillScannerPhaseEvidenceTests.setUp

    def configure(self, *, complete=True):
        from codex_web.services.artifact_content import ArtifactContentRegistry, ArtifactContentService
        from codex_web.services.local_artifact_content import LocalArtifactContentStore
        registry = ArtifactContentRegistry()
        registry.register(LocalArtifactContentStore(self.root / 'artifacts'))
        self.artifacts.content = ArtifactContentService(registry)
        (self.root / 'bin').mkdir()
        (self.root / 'bin/skillspector').write_text('private inert launcher')
        self.provider._settings = lambda actor: (self.root, 'scan-test', self.reference,
                                                 (self.reference, self.reference))
        self.provider.write_admission = lambda actor: contextlib.nullcontext()
        self.input_files = None
        run = self.backend.run
        def execute(assignment, *, argv, trusted_readonly_mounts=(), **kwargs):
            if '--version' in argv:
                output = 'SkillSpector v2.12.0\n'
            else:
                self.assertEqual(tuple(argv[1:]), ('scan', '/opt/codex-skill-input',
                                                   '--format', 'json', '--no-llm'))
                root = next(Path(host) for host, target in trusted_readonly_mounts
                            if str(target) == '/opt/codex-skill-input')
                self.input_files = {str(path.relative_to(root)): path.read_bytes()
                                    for path in root.rglob('*') if path.is_file()}
                output = json.dumps({'execution_successful': True,
                    'analysis_completeness': {'execution_successful': True,
                        'is_complete': complete, 'status': 'complete' if complete else 'partial'},
                    'metadata': {'skillspector_version': '2.12.0', 'llm_requested': False,
                        'meta_analysis_applied': False, 'llm_calls_attempted': 0, 'inference_usage': []},
                    'risk_assessment': {'score': 0}, 'issues': []})
            self.backend.result = LocalExecutionResult('skillspector', 'sha256:' + 'a' * 64,
                                                        0, output, '', .1)
            return run(assignment, argv=argv, trusted_readonly_mounts=trusted_readonly_mounts, **kwargs)
        self.backend.run = execute

    def test_real_worker_fenced_completion_scans_entire_bundle_and_records_manifest(self):
        self.configure()
        original = view(); content = SkillSecurityService._content(original)
        result = self.provider.scan_revision(view=original, content=content, mode='static', actor=self.actor)
        self.assertEqual(self.input_files, dict(build_skill_scan_input(original).files))
        self.assertEqual(result.execution_attribution['scanner_input_manifest_sha256'],
                         build_skill_scan_input(original).manifest_sha256)
        self.assertEqual(result.execution_attribution['scanner_input_format'], 'skill-static-input/1.0')
        self.assertEqual(len(self.backend.calls), 2)
        assignments = self.workers.store.load().assignments
        self.assertEqual(len(assignments), 2)
        self.assertTrue(all(item.status == 'succeeded' and item.lease is None for item in assignments))
        self.assertIsNotNone(result.report_artifact_id)

    def test_invalid_bundle_refuses_before_configuration_workspace_or_assignment(self):
        self.provider._settings = Mock(side_effect=AssertionError('must not resolve runtime'))
        before = self.workers.store.load()
        for path in ('../escape', '\ud800.txt'):
            original = view(); original['skill']['assets'][0]['path'] = path
            with self.subTest(path=path), self.assertRaises(ScannerUnavailable) as caught:
                self.provider.scan_revision(view=original, content=SkillSecurityService._content(original),
                                            mode='static', actor=self.actor)
            self.assertEqual(caught.exception.code, 'scanner_input_invalid')
            self.assertEqual(caught.exception.execution_attribution['scanner_assignment_created'], 0)
        self.provider._settings.assert_not_called()
        self.assertEqual(self.workers.store.load(), before)
        self.assertEqual(self.workspaces.store.load().workspaces, [])
        self.assertEqual(self.backend.calls, [])

    def test_partial_report_stays_blocked_after_genuine_worker_success(self):
        self.configure(complete=False)
        original = view()
        with self.assertRaises(ScannerUnavailable) as caught:
            self.provider.scan_revision(view=original, content=SkillSecurityService._content(original),
                                        mode='static', actor=self.actor)
        self.assertEqual(caught.exception.code, 'scanner_report_invalid')
        self.assertEqual(len(self.backend.calls), 2)
        self.assertTrue(all(item.status == 'succeeded' for item in self.workers.store.load().assignments))
