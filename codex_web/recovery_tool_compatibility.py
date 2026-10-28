"""Static source-compatible recovery-tool pins; never execution authority.

The caller must separately authenticate and match the returned Artifact and
independent CI/review Evidence against canonical records before any write, and
repeat this check at its existing write guards. No application imports occur here.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from types import ModuleType
from typing import Iterable

SCHEMA_VERSION = 'source-compatible-recovery-tool/1.0'
# Security boundary: additions require code review, never proof-supplied patterns.
ALLOWED_CHANGED_PATHS = frozenset({
    'codex_web/services/recovery.py',
    'codex_web/services/crypto_keys.py',
    'codex_web/services/recovery_envelope_stream.py',
    'codex_web/preinstall_backup_cli.py',
    'codex_web/preinstall_backup.py',
    'codex_web/recovery_tool_compatibility.py',
    'tests/test_crypto_envelope_stream.py',
    'tests/test_recovery_envelope_stream.py',
    'tests/test_recovery_stream_verification.py',
    'tests/test_preinstall_backup_cli.py',
    'tests/test_preinstall_backup.py',
    'tests/test_recovery_tool_compatibility.py',
    'tests/test_recovery_tool_qualification.py',
    'tests/helpers/recovery_envelope_memory_fixture.py',
    'docs/architecture/safe-upgrades.md',
    'docs/architecture/recovery-continuity.md',
    'docs/architecture/encryption-key-management.md',
})
_PROOF_KEYS = frozenset({
    'schema_version', 'base_revision', 'tool_revision', 'upgrade_target_revision',
    'base_tree_sha1', 'tool_tree_sha1', 'tool_archive_sha256', 'tool_artifact_id',
    'compatibility_evidence_id', 'review_evidence_id', 'changed_files',
})
_MAX_PROOF_BYTES = 1024 * 1024
_MAX_BLOB_BYTES = 16 * 1024 * 1024


class RecoveryToolCompatibilityError(ValueError):
    """Closed refusal without subprocess diagnostics or file contents."""


@dataclass(frozen=True)
class ChangedFile:
    path: str
    base_sha256: str | None
    tool_sha256: str


@dataclass(frozen=True)
class ValidatedRecoveryTool:
    proof_path: Path
    proof_sha256: str
    proof_identity: tuple[int, int, int, int, int, int]
    tool_checkout: Path
    base_revision: str
    tool_revision: str
    upgrade_target_revision: str
    base_tree_sha1: str
    tool_tree_sha1: str
    tool_archive_sha256: str
    tool_artifact_id: str
    compatibility_evidence_id: str
    review_evidence_id: str
    changed_files: tuple[ChangedFile, ...]

    @property
    def canonical_validation_required(self) -> bool:
        """Static pins cannot establish canonical authority, scope or CI validity."""
        return True


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise RecoveryToolCompatibilityError(reason)


def _hex(value: object, length: int, label: str) -> str:
    _require(type(value) is str and re.fullmatch('[0-9a-f]{%d}' % length, value) is not None,
             'invalid ' + label)
    return value


def _identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns,
            value.st_ctime_ns, value.st_mode)


def _no_symlink_path(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        _require(not stat.S_ISLNK(part.lstat().st_mode), 'symlink path refused')
    return path


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        _require(key not in result, 'duplicate JSON key')
        result[key] = value
    return result


def _read_proof(path: Path, expected_sha256: str) -> tuple[dict, tuple]:
    path = _no_symlink_path(path)
    before = path.lstat()
    _require(stat.S_ISREG(before.st_mode) and stat.S_IMODE(before.st_mode) == 0o600
             and before.st_uid == os.getuid() and before.st_nlink == 1
             and 0 < before.st_size <= _MAX_PROOF_BYTES, 'private proof file required')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        _require(_identity(os.fstat(fd)) == _identity(before), 'proof identity changed')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(_MAX_PROOF_BYTES + 1)
        _require(len(raw) == before.st_size and hashlib.sha256(raw).hexdigest() == expected_sha256,
                 'proof SHA mismatch')
        _require(_identity(os.fstat(fd)) == _identity(before)
                 and _identity(path.lstat()) == _identity(before), 'proof identity changed')
    finally:
        os.close(fd)
    try:
        proof = json.loads(raw, object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RecoveryToolCompatibilityError('invalid proof JSON') from exc
    _require(type(proof) is dict and proof.keys() == _PROOF_KEYS, 'invalid proof fields')
    return proof, _identity(before)


def _git(root: Path, *args: str) -> bytes:
    # Ignore ambient Git configuration/alternate object injection. Repository
    # configuration still supplies its normal worktree/object layout.
    env = {key: value for key, value in os.environ.items()
           if not key.startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT='0', GIT_NO_REPLACE_OBJECTS='1')
    try:
        result = subprocess.run(['git', '--no-pager', '-c', 'core.fsmonitor=false',
                                 '-c', 'core.hooksPath=' + os.devnull, '-C', str(root), *args],
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RecoveryToolCompatibilityError('Git source inspection unavailable') from exc
    _require(result.returncode == 0, 'Git source inspection refused')
    return result.stdout


def _tree(root: Path, revision: str) -> dict[str, tuple[str, str]]:
    result = {}
    for entry in _git(root, 'ls-tree', '-r', '-z', revision).split(b'\0'):
        if not entry:
            continue
        header, raw_path = entry.split(b'\t', 1)
        mode, kind, oid = header.decode('ascii').split(' ')
        _require(kind == 'blob' and mode in {'100644', '100755', '120000'},
                 'submodule or unsupported source entry')
        path = raw_path.decode('utf-8')
        _require(path not in result, 'duplicate Git path')
        result[path] = (mode, oid)
    return result


def _blob_sha(root: Path, oid: str) -> str:
    size = int(_git(root, 'cat-file', '-s', oid))
    _require(size <= _MAX_BLOB_BYTES, 'changed source blob exceeds bound')
    blob = _git(root, 'cat-file', 'blob', oid)
    _require(len(blob) == size, 'Git blob size changed')
    return hashlib.sha256(blob).hexdigest()


def _check_complete_checkout(root: Path, tree: dict[str, tuple[str, str]]) -> None:
    # Git status alone trusts assume-unchanged/skip-worktree index bits. Check
    # actual bytes and executable modes, including unchanged base files.
    for name, (mode, oid) in tree.items():
        source = root / name
        for parent in source.parents:
            if parent == root:
                break
            _require(not parent.is_symlink(), 'source directory symlink refused')
        metadata = source.lstat()
        if mode == '120000':
            _require(stat.S_ISLNK(metadata.st_mode), 'tracked symlink changed')
            blob = os.fsencode(os.readlink(source))
        else:
            _require(stat.S_ISREG(metadata.st_mode), 'complete regular source required')
            _require(bool(metadata.st_mode & 0o111) == (mode == '100755'),
                     'working source mode mismatch')
            _require(metadata.st_size <= _MAX_BLOB_BYTES, 'source file exceeds bound')
            fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                _require(_identity(os.fstat(fd)) == _identity(metadata), 'source identity changed')
                with os.fdopen(fd, 'rb', closefd=False) as stream:
                    blob = stream.read(_MAX_BLOB_BYTES + 1)
                _require(_identity(os.fstat(fd)) == _identity(metadata), 'source identity changed')
            finally:
                os.close(fd)
            _require(len(blob) == metadata.st_size, 'source size changed')
        actual = hashlib.sha1(b'blob ' + str(len(blob)).encode('ascii') + b'\0' + blob).hexdigest()
        _require(actual == oid, 'working source bytes mismatch')


def validate_recovery_tool_proof(
    path: Path, proof_sha256: str, *, base_revision: str, tool_revision: str,
    upgrade_target_revision: str, tool_checkout: Path,
) -> ValidatedRecoveryTool:
    """Validate local pins only; caller MUST match canonical Artifact/Evidence.

    Expected B/T/D and proof hash come from the caller's reviewed invocation,
    never from this proof. T is a full clean backport tree, not the upgrade target.
    No credential, application service, data store or canonical API is accessed.
    Reinvoke at existing guards to reject proof/source replacement after startup.
    """
    proof_sha256 = _hex(proof_sha256, 64, 'proof SHA')
    for revision in (base_revision, tool_revision, upgrade_target_revision):
        _hex(revision, 40, 'expected revision')
    _require(tool_revision != upgrade_target_revision and tool_revision != base_revision,
             'distinct backport tool required')
    proof, identity = _read_proof(Path(path), proof_sha256)
    _require(proof['schema_version'] == SCHEMA_VERSION, 'unsupported proof schema')
    for key, expected in [('base_revision', base_revision), ('tool_revision', tool_revision),
                          ('upgrade_target_revision', upgrade_target_revision)]:
        _require(_hex(proof[key], 40, key) == expected, 'proof revision mismatch')
    for key in ('base_tree_sha1', 'tool_tree_sha1'):
        _hex(proof[key], 40, key)
    _hex(proof['tool_archive_sha256'], 64, 'tool archive SHA')
    for key, prefix in [('tool_artifact_id', 'artifact'),
                        ('compatibility_evidence_id', 'evidence'), ('review_evidence_id', 'evidence')]:
        _require(type(proof[key]) is str and re.fullmatch(prefix + '-[0-9a-f]{32}', proof[key]) is not None,
                 'invalid canonical record ID')
    _require(proof['compatibility_evidence_id'] != proof['review_evidence_id'],
             'independent review Evidence required')
    rows = proof['changed_files']
    _require(type(rows) is list and 0 < len(rows) <= len(ALLOWED_CHANGED_PATHS),
             'invalid changed files')
    manifest = {}
    for row in rows:
        _require(type(row) is dict and row.keys() == {'path', 'base_sha256', 'tool_sha256'},
                 'invalid changed file fields')
        name = row['path']
        _require(type(name) is str and name in ALLOWED_CHANGED_PATHS and name not in manifest,
                 'changed path not allowed or duplicate')
        if row['base_sha256'] is not None:
            _hex(row['base_sha256'], 64, 'base file SHA')
        _hex(row['tool_sha256'], 64, 'tool file SHA')
        manifest[name] = ChangedFile(**row)
    root = _no_symlink_path(Path(tool_checkout))
    _require(_git(root, 'rev-parse', '--show-toplevel').decode().strip() == str(root),
             'exact Git checkout root required')
    _require(_git(root, 'rev-parse', 'HEAD').decode().strip() == tool_revision,
             'tool HEAD mismatch')
    _require(not _git(root, 'status', '--porcelain=v1', '--untracked-files=all',
                     '--ignore-submodules=none'), 'clean complete tool checkout required')
    _git(root, 'merge-base', '--is-ancestor', base_revision, tool_revision)
    for key, revision in [('base_tree_sha1', base_revision), ('tool_tree_sha1', tool_revision)]:
        _require(_git(root, 'rev-parse', revision + '^{tree}').decode().strip() == proof[key],
                 'source tree mismatch')
    old, new = _tree(root, base_revision), _tree(root, tool_revision)
    _check_complete_checkout(root, new)
    changed = {name for name in old.keys() | new.keys() if old.get(name) != new.get(name)}
    _require(changed == manifest.keys(), 'actual Git diff differs from proof')
    for name in sorted(changed):
        _require(name in new and new[name][0] == '100644', 'deleted, executable or symlink change refused')
        _require(name not in old or old[name][0] == new[name][0], 'source mode change refused')
        row = manifest[name]
        _require(row.base_sha256 == (_blob_sha(root, old[name][1]) if name in old else None)
                 and row.tool_sha256 == _blob_sha(root, new[name][1]), 'changed source SHA mismatch')
    _check_complete_checkout(root, new)
    # Catch concurrent proof/source mutation during the inspection, too.
    _, after = _read_proof(Path(path), proof_sha256)
    _require(after == identity and not _git(root, 'status', '--porcelain=v1', '--untracked-files=all')
             and _git(root, 'rev-parse', 'HEAD').decode().strip() == tool_revision,
             'source or proof changed during inspection')
    return ValidatedRecoveryTool(Path(os.path.abspath(path)), proof_sha256, identity, root,
        base_revision, tool_revision, upgrade_target_revision, proof['base_tree_sha1'],
        proof['tool_tree_sha1'], proof['tool_archive_sha256'], proof['tool_artifact_id'],
        proof['compatibility_evidence_id'], proof['review_evidence_id'],
        tuple(manifest[name] for name in sorted(manifest)))


def validate_tool_imports(facts: ValidatedRecoveryTool, modules: Iterable[ModuleType]) -> None:
    """Check supplied already-imported application modules; never import any.

    Caller supplies *all* loaded codex_web modules at its repeated guards. This
    complements canonical validation; it cannot grant authority on its own.
    """
    tree = _tree(facts.tool_checkout, facts.tool_revision)
    for module in modules:
        _require(isinstance(module, ModuleType) and module.__name__.split('.')[0] == 'codex_web',
                 'application module required')
        filename = getattr(module, '__file__', None)
        _require(type(filename) is str, 'module source file required')
        source = _no_symlink_path(Path(filename))
        try:
            name = source.relative_to(facts.tool_checkout).as_posix()
        except ValueError as exc:
            raise RecoveryToolCompatibilityError('mixed tool imports refused') from exc
        _require(name.startswith('codex_web/') and name.endswith('.py') and name in tree
                 and tree[name][0] == '100644', 'untracked module source refused')
        metadata = source.lstat()
        _require(stat.S_ISREG(metadata.st_mode) and metadata.st_size <= _MAX_BLOB_BYTES,
                 'bounded regular module source required')
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            _require(_identity(os.fstat(fd)) == _identity(metadata), 'module identity changed')
            with os.fdopen(fd, 'rb', closefd=False) as stream:
                blob = stream.read(_MAX_BLOB_BYTES + 1)
            _require(_identity(os.fstat(fd)) == _identity(metadata), 'module identity changed')
        finally:
            os.close(fd)
        actual_oid = hashlib.sha1(
            b'blob ' + str(len(blob)).encode('ascii') + b'\0' + blob).hexdigest()
        _require(len(blob) == metadata.st_size and actual_oid == tree[name][1],
                 'imported source mismatch')
