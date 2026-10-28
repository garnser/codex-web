"""Private temporary Git/JSON fixtures only; no application or canonical stores."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch

from codex_web import recovery_tool_compatibility as compatibility


class RecoveryToolCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.root = self.directory / 'tool'
        self.root.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.email', 'private-fixture@example.invalid')
        self.git('config', 'user.name', 'Private Fixture')
        self.write('codex_web/services/recovery.py', 'BASE = True\n')
        self.write('codex_web/models.py', 'SCHEMA = 1\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'base')
        self.base = self.git('rev-parse', 'HEAD').strip()
        self.base_tree = self.git('rev-parse', 'HEAD^{tree}').strip()
        self.write('codex_web/services/recovery.py', 'BASE = True\nBOUNDED = True\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'backport')
        self.tool = self.git('rev-parse', 'HEAD').strip()
        self.proof_path = self.directory / 'proof.json'
        self.proof = self.make_proof()
        self.save_proof()

    def git(self, *args):
        result = subprocess.run(['git', '-C', str(self.root), *args],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                check=True, timeout=10)
        return result.stdout.decode()

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def make_proof(self):
        old = self.git('show', self.base + ':codex_web/services/recovery.py').encode()
        new = (self.root / 'codex_web/services/recovery.py').read_bytes()
        return dict(schema_version=compatibility.SCHEMA_VERSION,
                    base_revision=self.base, tool_revision=self.tool,
                    upgrade_target_revision='d' * 40,
                    base_tree_sha1=self.base_tree,
                    tool_tree_sha1=self.git('rev-parse', 'HEAD^{tree}').strip(),
                    tool_archive_sha256='a' * 64,
                    tool_artifact_id='artifact-' + '1' * 32,
                    compatibility_evidence_id='evidence-' + '2' * 32,
                    review_evidence_id='evidence-' + '3' * 32,
                    changed_files=[dict(path='codex_web/services/recovery.py',
                        base_sha256=hashlib.sha256(old).hexdigest(),
                        tool_sha256=hashlib.sha256(new).hexdigest())])

    def save_proof(self):
        self.proof_path.write_text(json.dumps(self.proof))
        self.proof_path.chmod(0o600)
        self.sha = hashlib.sha256(self.proof_path.read_bytes()).hexdigest()

    def validate(self, **overrides):
        arguments = dict(base_revision=self.base, tool_revision=self.tool,
                         upgrade_target_revision='d' * 40, tool_checkout=self.root)
        arguments.update(overrides)
        return compatibility.validate_recovery_tool_proof(self.proof_path, self.sha, **arguments)

    def test_clean_narrow_backport_returns_pins_but_requires_canonical_validation(self):
        result = self.validate()
        self.assertTrue(result.canonical_validation_required)
        self.assertEqual(result.tool_artifact_id, self.proof['tool_artifact_id'])
        self.assertEqual(result.tool_archive_sha256, 'a' * 64)
        self.assertEqual(result.changed_files[0].path, 'codex_web/services/recovery.py')
        self.assertFalse(hasattr(result, 'approved'))

    def test_exact_expected_revisions_and_distinct_tool_are_required(self):
        for change in (dict(base_revision='a' * 40), dict(tool_revision='b' * 40),
                       dict(upgrade_target_revision=self.tool),
                       dict(tool_revision=self.base), dict(tool_revision='HEAD')):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.validate(**change)

    def test_private_file_hash_mode_symlink_and_hardlink_refusals(self):
        self.proof_path.chmod(0o640)
        with self.assertRaises(ValueError):
            self.validate()
        self.proof_path.chmod(0o600)
        with self.assertRaises(ValueError):
            compatibility.validate_recovery_tool_proof(self.proof_path, '0' * 64,
                base_revision=self.base, tool_revision=self.tool,
                upgrade_target_revision='d' * 40, tool_checkout=self.root)
        linked = self.directory / 'linked'
        os.link(self.proof_path, linked)
        with self.assertRaises(ValueError):
            self.validate()
        linked.unlink()
        actual = self.directory / 'actual.json'
        self.proof_path.rename(actual)
        self.proof_path.symlink_to(actual)
        with self.assertRaises(ValueError):
            self.validate()

    def test_closed_schema_ids_types_and_independent_review(self):
        original = json.loads(json.dumps(self.proof))
        variants = [dict(extra=True), dict(tool_artifact_id='artifact-x'),
                    dict(review_evidence_id=original['compatibility_evidence_id']),
                    dict(base_revision=True), dict(changed_files=[])]
        for changes in variants:
            self.proof = {**original, **changes}
            self.save_proof()
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.validate()

    def test_duplicate_json_key_refuses_even_with_matching_file_hash(self):
        raw = json.dumps(self.proof)
        self.proof_path.write_text(raw[:-1] + ', "schema_version": "x"}')
        self.sha = hashlib.sha256(self.proof_path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            self.validate()

    def test_self_declared_diff_does_not_authorize_model_or_storage_changes(self):
        self.write('codex_web/models.py', 'SCHEMA = 2\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'forbidden model')
        self.tool = self.git('rev-parse', 'HEAD').strip()
        self.proof = self.make_proof()
        self.proof['changed_files'].append(dict(path='codex_web/models.py',
            base_sha256='a' * 64, tool_sha256='b' * 64))
        self.save_proof()
        with self.assertRaisesRegex(ValueError, 'path not allowed'):
            self.validate()

    def test_omitted_actual_changes_and_wrong_file_digest_refuse(self):
        self.proof['changed_files'][0]['tool_sha256'] = 'a' * 64
        self.save_proof()
        with self.assertRaisesRegex(ValueError, 'SHA mismatch'):
            self.validate()
        self.write('codex_web/services/crypto_keys.py', 'NEW = True\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'second seam')
        self.tool = self.git('rev-parse', 'HEAD').strip()
        self.proof = self.make_proof()
        self.save_proof()
        with self.assertRaisesRegex(ValueError, 'actual Git diff'):
            self.validate()

    def test_dirty_untracked_and_assume_unchanged_source_refuse(self):
        self.write('untracked.py', 'unexpected')
        with self.assertRaises(ValueError):
            self.validate()
        (self.root / 'untracked.py').unlink()
        self.git('update-index', '--assume-unchanged', 'codex_web/models.py')
        self.write('codex_web/models.py', 'SCHEMA = 999\n')
        with self.assertRaisesRegex(ValueError, 'working source bytes'):
            self.validate()

    def test_new_exact_allowlisted_file_is_supported(self):
        name = 'codex_web/services/recovery_envelope_stream.py'
        self.write(name, 'BOUNDED = True\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'stream seam')
        self.tool = self.git('rev-parse', 'HEAD').strip()
        self.proof = self.make_proof()
        self.proof['changed_files'].append(dict(path=name, base_sha256=None,
            tool_sha256=hashlib.sha256((self.root / name).read_bytes()).hexdigest()))
        self.save_proof()
        self.assertEqual(len(self.validate().changed_files), 2)

    def test_imports_must_be_tracked_sources_in_the_one_tool_tree(self):
        facts = self.validate()
        module = ModuleType('codex_web.services.recovery')
        module.__file__ = str(self.root / 'codex_web/services/recovery.py')
        compatibility.validate_tool_imports(facts, [module])
        module.__file__ = str(self.directory / 'other/recovery.py')
        with self.assertRaises((ValueError, OSError)):
            compatibility.validate_tool_imports(facts, [module])
        self.write('codex_web/untracked.py', 'EVIL = True\n')
        module.__file__ = str(self.root / 'codex_web/untracked.py')
        with self.assertRaises(ValueError):
            compatibility.validate_tool_imports(facts, [module])

    def test_proof_replacement_is_rejected_on_repeated_guard(self):
        self.validate()
        self.proof['tool_archive_sha256'] = 'b' * 64
        self.proof_path.write_text(json.dumps(self.proof))
        with self.assertRaisesRegex(ValueError, 'proof SHA'):
            self.validate()

    def test_unrelated_base_is_not_an_ancestor(self):
        # A real separate root commit, not an invalid/nonexistent revision.
        tree = self.git('rev-parse', self.base + '^{tree}').strip()
        unrelated = self.git('commit-tree', tree, '-m', 'unrelated root').strip()
        self.proof['base_revision'] = unrelated
        self.save_proof()
        with self.assertRaisesRegex(ValueError, 'Git source inspection refused'):
            self.validate(base_revision=unrelated)

    def test_changed_symlink_executable_and_deletion_refuse(self):
        path = self.root / 'codex_web/services/recovery.py'
        path.chmod(0o755)
        self.git('add', '.')
        self.git('commit', '-qm', 'executable change')
        self.tool = self.git('rev-parse', 'HEAD').strip()
        self.proof = self.make_proof()
        self.save_proof()
        with self.assertRaisesRegex(ValueError, 'executable'):
            self.validate()
        self.git('reset', '--hard', self.base)
        path.unlink()
        path.symlink_to('../models.py')
        self.git('add', '.')
        self.git('commit', '-qm', 'symlink change')
        self.tool = self.git('rev-parse', 'HEAD').strip()
        self.proof = self.make_proof()
        self.save_proof()
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.validate()
        path.unlink()
        self.git('add', '.')
        self.git('commit', '-qm', 'deletion')
        self.tool = self.git('rev-parse', 'HEAD').strip()
        self.proof['tool_revision'] = self.tool
        self.proof['tool_tree_sha1'] = self.git('rev-parse', 'HEAD^{tree}').strip()
        self.save_proof()
        with self.assertRaisesRegex(ValueError, 'deleted'):
            self.validate()

    def test_many_import_checks_use_constant_git_inspections(self):
        facts = self.validate()
        modules = []
        for index in range(40):
            module = ModuleType('codex_web.fixture_alias_' + str(index))
            module.__file__ = str(self.root / 'codex_web/services/recovery.py')
            modules.append(module)
        # Validate many aliases to a real tracked file without spawning Git for
        # each module. The one tree inspection is independent of module count.
        with patch.object(compatibility, '_git', wraps=compatibility._git) as git_calls, \
             patch.object(compatibility, '_blob_sha',
                          side_effect=AssertionError('per-module Git blob read')):
            compatibility.validate_tool_imports(facts, modules)
        self.assertEqual(git_calls.call_count, 1)
        self.assertEqual(git_calls.call_args.args[1:3], ('ls-tree', '-r'))
        # Local identity verification still refuses modified imported bytes.
        self.write('codex_web/services/recovery.py', 'REPLACED = True\n')
        with self.assertRaisesRegex(ValueError, 'imported source mismatch'):
            compatibility.validate_tool_imports(facts, modules)
