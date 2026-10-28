"""Synthetic two-process Recovery memory proof; never use operational data.

Root runs prepare in a private 2GiB/swap0 unit, positively joins it, then runs
original/bounded in distinct 1152MiB/swap0 units against ONLY that private fixture.
All admission checks use actual host/cgroup capacity. No estimator is mocked.
"""
from __future__ import annotations

import argparse
from contextlib import nullcontext
import importlib.abc
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
from unittest.mock import patch

PREPARE_CAP = 2 * 1024**3
VERIFY_CAP = 1152 * 1024**2
ROW_BYTES = 16 * 1024**2
ROWS = 4
MARKER = 'synthetic-envelope-memory-fixture.json'


class _NoApplication(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in ('codex_web.application', 'codex_web.server', 'codex_web.native') or fullname.startswith('codex_web.native.'):
            raise RuntimeError('application_or_native_import_forbidden')


def _private(path, directory):
    info = path.lstat()
    if path.is_symlink() or info.st_uid != os.geteuid():
        raise RuntimeError('private_fixture_owner_required')
    if directory:
        if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
            raise RuntimeError('private_fixture_directory_required')
    elif not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        raise RuntimeError('private_fixture_file_required')


def _save(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w') as handle:
        json.dump(value, handle, sort_keys=True)
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())


def _counters():
    from codex_web.services.recovery_memory import _unified_cgroup
    group, _ = _unified_cgroup(Path('/proc'), Path('/sys/fs/cgroup'))
    selected = {}
    for line in (group / 'memory.stat').read_text().splitlines():
        key, value = line.split()
        if key in ('anon', 'file', 'kernel', 'file_dirty', 'file_writeback'):
            selected[key] = int(value)
    return dict(memory_max=(group / 'memory.max').read_text().strip(),
                memory_swap_max=(group / 'memory.swap.max').read_text().strip(),
                memory_current=int((group / 'memory.current').read_text()),
                memory_peak=int((group / 'memory.peak').read_text()), stat=selected)


def _owners(root, prepare):
    # Recompose the existing Recovery test's canonical private owners; do not
    # invoke its setUp (which would create a DIFFERENT temporary database/key).
    from tests import test_recovery as fixture
    from codex_web.services.artifact_evidence import ArtifactEvidenceService
    from codex_web.storage.artifact_evidence import ArtifactEvidenceStore
    state = fixture.SQLiteStateStore(root / 'state.sqlite3', initialize=prepare)
    actor = fixture.AuthenticationActor(
        identity_id='synthetic-admin', principal_kind=fixture.PrincipalKind.HUMAN,
        organization_id='synthetic-org', workspace_id='synthetic-ws',
        roles=(fixture.MembershipRole.ADMIN,), assurance=fixture.AuthenticationAssurance.LOCAL_TRUSTED)
    crypto = fixture.CryptoKeyService(fixture.CryptoKeyStore(state),
                                    {'local': fixture.LocalFileKeyBackend(root / 'keys')})
    audit_store = fixture.AutonomyAuditStore(state)
    audit = fixture.AutonomyAuditService(audit_store)
    evidence = ArtifactEvidenceService(ArtifactEvidenceStore(state))
    destination = fixture.LocalBackupDestination(root / 'backups')
    service = fixture.RecoveryService(fixture.RecoveryStore(state), state_store=state,
                                     crypto=crypto, evidence=evidence, audit=audit,
                                     destinations=(destination,))
    if prepare:
        key = crypto.create_key(fixture.ManagedKeyCreate(purpose=fixture.KeyPurpose.BACKUP,
                                                        backend_type='local'), actor=actor)
        audit_store.append(fixture.AutonomyAuditPayload(
            kind=fixture.AutonomyAuditKind.INTEGRITY_VERIFICATION,
            organization_id=actor.organization_id, workspace_id=actor.workspace_id,
            outcome='verified', reason_code='synthetic-memory-fixture'))
        service.configure(fixture.RecoveryPolicy(
            deployment_mode=fixture.RecoveryDeploymentMode.LOCAL, backup_key_id=key.id,
            destination_id='local', backup_interval_seconds=3600,
            restore_verification_interval_seconds=86400, retention_count=2,
            require_audit_integrity=True), actor=actor)
    return state, actor, crypto, evidence, destination, service


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=('prepare', 'original', 'bounded'), required=True)
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    root = args.root
    if not root.is_absolute() or root.resolve() != root:
        raise RuntimeError('exact_private_root_required')
    os.umask(0o077)
    if args.mode == 'prepare':
        _private(root.parent, True)
        root.mkdir(mode=0o700, exist_ok=False)
    _private(root, True)
    output = root / (args.mode + '-resource-result.json')
    if output.exists() or output.is_symlink():
        raise RuntimeError('create_only_result_required')
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    sys.meta_path.insert(0, _NoApplication())
    from codex_web.services import recovery as recovery_module
    from codex_web.services.recovery_memory import require_process_memory_limit
    from codex_web.storage.state_store import StorageOperationBudget, storage_operation_budget
    cap = PREPARE_CAP if args.mode == 'prepare' else VERIFY_CAP
    limits = require_process_memory_limit(maximum_bytes=cap, maximum_swap_bytes=0)
    if limits.memory_max_bytes != cap:
        raise RuntimeError('exact_fixture_unit_memory_max_required')
    before, started = _counters(), time.monotonic()
    budget = StorageOperationBudget(120)
    token = storage_operation_budget.set(budget)
    try:
        owners = _owners(root, args.mode == 'prepare')
        state, actor, crypto, evidence, destination, service = owners
        if args.mode == 'prepare':
            for index in range(ROWS):
                # Four rows individually below canonical SnapshotCapture's32MiB
                # row limit. Public repeated text is the entire synthetic data.
                state.put('synthetic_public_memory_' + str(index), {'value': 'x' * ROW_BYTES})
            manifest = service.create_backup(actor=actor)
            marker = dict(schema='synthetic-recovery-envelope-memory/1', synthetic_only=True,
                          public_payload_bytes=ROWS * ROW_BYTES, rows=ROWS, row_bytes=ROW_BYTES,
                          backup_id=manifest.id, envelope_size=destination.size(manifest.destination_ref),
                          envelope_sha256=manifest.envelope_sha256)
            _save(root / MARKER, marker)
            result = dict(mode=args.mode, prepared=True, synthetic_only=True,
                          public_payload_bytes=ROWS * ROW_BYTES, envelope_size=marker['envelope_size'])
        else:
            _private(root / MARKER, False)
            if (root / MARKER).stat().st_size > 4096:
                raise RuntimeError('bounded_fixture_marker_required')
            marker = json.loads((root / MARKER).read_text())
            if (marker.get('schema') != 'synthetic-recovery-envelope-memory/1'
                    or marker.get('synthetic_only') is not True
                    or marker.get('public_payload_bytes') != ROWS * ROW_BYTES
                    or marker.get('rows') != ROWS or marker.get('row_bytes') != ROW_BYTES):
                raise RuntimeError('synthetic_fixture_geometry_required')
            recovery_before = service.store.load()
            manifest = recovery_before.backups[marker['backup_id']]
            if (manifest.envelope_sha256 != marker['envelope_sha256']
                    or destination.size(manifest.destination_ref) != marker['envelope_size']):
                raise RuntimeError('original_fixture_backup_required')
            evidence_before = evidence.store.load()
            stages = []
            original_admit = recovery_module._memory_admit
            def observe_admission(stage, allocation_bytes):
                stages.append(dict(stage=stage, allocation_bytes=allocation_bytes))
                return original_admit(stage, allocation_bytes)
            # Sole algorithm selection: retain EXACT original admission and
            # source reader when destination lacks the new optional primitive.
            selection = patch.object(destination, 'open_bounded', None) if args.mode == 'original' else nullcontext()
            refusal = None
            verification = None
            with selection, patch.object(recovery_module, '_memory_admit', side_effect=observe_admission):
                try:
                    verification = service.verify_restore(manifest.id, actor=actor)
                except recovery_module.RecoveryMemoryConflictError as error:
                    match = re.search(r'stage=([a-z_]+); estimated_required_bytes=(\d+); observed_available_bytes=(\d+);', str(error))
                    if match:
                        refusal = dict(stage=match[1], required_bytes=int(match[2]), available_bytes=int(match[3]))
                    else:
                        raise RuntimeError('unrecognized_memory_refusal') from None
            if args.mode == 'original':
                if (verification is not None or refusal is None or refusal['stage'] != 'restore_snapshot'
                        or service.store.load() != recovery_before or evidence.store.load() != evidence_before):
                    raise RuntimeError('original_exact_memory_refusal_without_publication_required')
                result = dict(mode=args.mode, expected_original_memory_refusal=True,
                              no_verification_or_evidence_published=True, refusal=refusal)
            else:
                if refusal is not None or verification is None:
                    raise RuntimeError('bounded_actual_verification_required')
                fields = ('checksum_valid', 'schema_compatible', 'key_manifest_valid', 'audit_continuity_valid')
                if (verification.status.value != 'pass' or verification.blockers
                        or any(getattr(verification, field) is not True for field in fields)
                        or verification.isolated is not True or verification.side_effects_enabled is not False
                        or not verification.evidence_id):
                    raise RuntimeError('bounded_genuine_semantic_pass_required')
                if not {'restore_envelope_metadata', 'restore_envelope_stream', 'restore_decode'} <= {row['stage'] for row in stages}:
                    raise RuntimeError('real_bounded_and_decode_admission_required')
                result = dict(mode=args.mode, actual_verification_status=verification.status.value,
                              semantic_checks={field: getattr(verification, field) for field in fields},
                              isolated=verification.isolated, side_effects_enabled=verification.side_effects_enabled,
                              genuine_canonical_evidence_published=True)
            result.update(synthetic_only=True, public_payload_bytes=ROWS * ROW_BYTES,
                          envelope_size=marker['envelope_size'], admission_stages=stages)
        result.update(actual_memory_max_bytes=limits.memory_max_bytes, actual_swap_max_bytes=limits.memory_swap_max_bytes,
                      before=before, after=_counters(), elapsed_seconds=time.monotonic() - started,
                      no_operational_data_or_capacity_guarantee=True)
        _save(output, result)
    finally:
        storage_operation_budget.reset(token)
    print(json.dumps({'synthetic_resource_worker_completed': True, 'mode': args.mode,
                      'elapsed_seconds': result['elapsed_seconds']}))


if __name__ == '__main__':
    try:
        main()
    except BaseException as error:
        print(json.dumps({'synthetic_resource_worker_failed': True, 'exception_type': type(error).__name__,
                          'no_raw_diagnostics': True}))
        raise SystemExit(1)
