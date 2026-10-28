"""One canonical backup while an operator-controlled, drained service is stopped.

This command never stops, starts, upgrades or rolls back a service. The reviewed
operator procedure owns stop/restoration and its independent deadline guard.
It acquires the existing deploy lock, obtains genuine pre-stop observations,
waits at most 30 seconds for a clean stop, and captures once. An interrupted
capture is an unknown outcome requiring canonical inspection, never a retry.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import asyncio
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import subprocess
import sys
import time
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from codex_web.canonical_json import canonical_json_bytes
from codex_web.local_deployment import LOCAL_DEPLOY_LOCK
from codex_web.preinstall_backup import OfflineCaptureProof, compose_capture_runtime, qualify_capture
from codex_web.upgrades import UpgradePlan


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RuntimeError('Local operator API redirects are unavailable')


def local_api(base, token, method, path):
    parsed = urlsplit(base)
    if (parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', '::1'}
            or parsed.username or parsed.password or parsed.path not in {'', '/'}
            or parsed.query or parsed.fragment):
        raise ValueError('A loopback canonical API origin is required')
    request = Request(base.rstrip('/') + path, method=method,
        data=b'{}' if method == 'POST' else None,
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=25) as response:
        body = response.read(16 * 1024**2 + 1)
        if response.status != 200 or len(body) > 16 * 1024**2:
            raise RuntimeError('Canonical API observation unavailable')
        return json.loads(body)


def record(directory, name, value):
    # O_EXCL and fsync make each checkpoint durable BEFORE the next effect.
    path = directory / (name + '.json')
    with path.open('x', encoding='utf-8') as output:
        json.dump({'recorded_at': time.time(), **value}, output, sort_keys=True, indent=2)
        output.flush()
        os.fsync(output.fileno())
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def git(checkout, *args):
    return subprocess.check_output(['git', '-c', 'core.hooksPath=/dev/null', '-C',
        str(checkout), *args], text=True, timeout=15).strip()


class OfflineHostGuard:
    """Read actual host state; no service-control mutation or credentials."""
    def __init__(self, options, lock_fd):
        self.options, self.lock_fd = options, lock_fd
        self.data_root = (options.runtime_checkout / 'data').resolve(strict=True)
        self.canonical_paths = {name: (self.data_root / relative).resolve()
            for name, relative in (('key_material_directory', 'keys'),
                ('backup_directory', 'backups'), ('sqlite_path', 'codex-web.db'))}
        self.initial = self.service()
        self.stopped = False
        self.start_time = None
        self.tool_proof = (source_compatible_tool_facts(options)
            if (getattr(options, 'source_compatible_tool_proof', None) is not None
                or getattr(options, 'source_compatible_tool_proof_sha256', None) is not None)
            else None)

    def service(self):
        values = subprocess.check_output(['systemctl', 'show', self.options.unit,
            '--property=ActiveState,MainPID,InvocationID,Result,ExecMainCode,ExecMainStatus,ExecMainPID,ExecMainStartTimestampMonotonic,ExecMainExitTimestampMonotonic'],
            text=True, timeout=10)
        return dict(line.split('=', 1) for line in values.splitlines() if '=' in line)

    def check(self):
        options = self.options
        if self.tool_proof is not None:
            upgrade_target_revision(options)
        elif getattr(options, 'expected_upgrade_target_revision', None) is not None:
            source_compatible_tool_target(options)
        else:
            upgrade_target_revision(options)
        current_tool = (source_compatible_tool_facts(options)
            if (self.tool_proof is not None
                or getattr(options, 'source_compatible_tool_proof', None) is not None
                or getattr(options, 'source_compatible_tool_proof_sha256', None) is not None)
            else None)
        if current_tool != self.tool_proof:
            raise RuntimeError('Pinned compatible Recovery tool proof changed')
        fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_stat = os.fstat(self.lock_fd)
        path_stat = LOCAL_DEPLOY_LOCK.stat()
        if (lock_stat.st_dev, lock_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino):
            raise RuntimeError('Deploy lock identity changed')
        if (options.runtime_checkout / 'data').resolve(strict=True) != self.data_root:
            raise RuntimeError('Canonical runtime data directory changed')
        for name, expected in self.canonical_paths.items():
            relative = {'key_material_directory': 'keys', 'backup_directory': 'backups',
                'sqlite_path': 'codex-web.db'}[name]
            if (getattr(options, name).resolve() != expected
                    or (self.data_root / relative).resolve() != expected):
                raise RuntimeError('Backup producer differs from canonical runtime storage paths')
        for checkout, revision in ((options.runtime_checkout, options.expected_runtime_revision),
                                   (options.producer_checkout, options.expected_producer_revision)):
            if git(checkout, 'rev-parse', 'HEAD') != revision or git(checkout,
                    'status', '--porcelain', '--untracked-files=all', '--', 'codex_web',
                    'scripts', 'server.py', 'pyproject.toml', 'requirements.txt', 'requirements-dev.txt'):
                raise RuntimeError('Reviewed executable source changed')
        current = self.service()
        if (not self.initial.get('InvocationID')
                or current.get('ExecMainPID') != str(options.expected_runtime_pid)
                or not self.initial.get('ExecMainStartTimestampMonotonic', '').isdigit()
                or int(self.initial['ExecMainStartTimestampMonotonic']) <= 0
                or current.get('ExecMainStartTimestampMonotonic') != self.initial['ExecMainStartTimestampMonotonic']):
            raise RuntimeError('Pinned service invocation changed')
        if self.stopped:
            # Uvicorn re-raises a captured shutdown signal after cleanup.
            # systemd therefore reports a successful requested SIGTERM stop
            # as CLD_KILLED/15, alongside the ordinary CLD_EXITED/0 case.
            successful_exit = (current.get('Result') == 'success'
                and (current.get('ExecMainCode'), current.get('ExecMainStatus'))
                    in {('1', '0'), ('2', '15')})
            if (current.get('ActiveState') != 'inactive' or current.get('MainPID') != '0'
                    or current.get('InvocationID') not in {'', self.initial['InvocationID']}
                    or not successful_exit
                    or not current.get('ExecMainExitTimestampMonotonic', '').isdigit()
                    or int(current['ExecMainExitTimestampMonotonic']) < int(self.initial['ExecMainStartTimestampMonotonic'])
                    or Path('/proc', str(options.expected_runtime_pid)).exists()):
                raise RuntimeError('Pinned service is not positively cleanly stopped')
        else:
            if (current.get('ActiveState') != 'active' or current.get('MainPID') != str(options.expected_runtime_pid)
                    or current.get('InvocationID') != self.initial['InvocationID']):
                raise RuntimeError('Pinned service is not running')
            proc = Path('/proc', str(options.expected_runtime_pid))
            if proc.stat().st_uid != os.getuid() or (proc/'cwd').resolve() != options.runtime_checkout.resolve():
                raise RuntimeError('Pinned runtime process ownership/source changed')
            start_time = (proc/'stat').read_text().rsplit(')', 1)[1].split()[19]
            if self.start_time is not None and start_time != self.start_time:
                raise RuntimeError('Pinned runtime process was replaced')
            self.start_time = start_time

    def wait_stopped(self, deadline):
        while time.time() <= deadline:
            current = self.service()
            if time.time() > deadline:
                raise RuntimeError('Fresh pre-stop observation expired during service observation')
            if current.get('ActiveState') == 'inactive':
                self.stopped = True
                self.check()
                if time.time() > deadline:
                    raise RuntimeError('Fresh pre-stop observation expired during stop qualification')
                return
            if current.get('ActiveState') not in {'active', 'deactivating'}:
                raise RuntimeError('Service stop did not complete cleanly')
            time.sleep(0.25)
        raise RuntimeError('Fresh pre-stop observation expired before clean service stop')


def require_recovery_idle(observation):
    executor = observation.get('scheduled_work', {}).get('local_executor', {})
    if (executor.get('accepting') is not True
            or type(executor.get('waiting_or_running')) is not int or executor['waiting_or_running'] != 0
            or type(executor.get('inflight_work')) is not int or executor['inflight_work'] != 0
            or executor.get('quiescence_guard') is not False):
        raise RuntimeError('Installed Recovery executor is not positively idle')
    return executor


def recovery_custody(store):
    from codex_web.storage.recovery import RecoveryStore
    state = RecoveryStore._decode(store.get(RecoveryStore.namespace))
    if state.policy is None or any(item.status == 'running' for item in state.scheduled_firings.values()):
        raise RuntimeError('Canonical Recovery policy or scheduled custody is unavailable')
    # Capturing may legitimately add a backup and enforce retention. It cannot
    # change policy, schedules or the disposition of another scheduled owner.
    value = {'policy': state.policy.model_dump(mode='json'),
        'backup_schedule_id': state.backup_schedule_id,
        'verification_schedule_id': state.verification_schedule_id,
        'firings': {key: item.model_dump(mode='json') for key, item in state.scheduled_firings.items()},
        'reconciliations': {key: item.model_dump(mode='json') for key, item in state.reconciliations.items()}}
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


DEFAULT_MEMORY_BUDGET_BYTES = 6 * 1024**3


def memory_budget_bytes(value: str) -> int:
    """An explicit finite operation ceiling, never an observed capacity."""
    if type(value) is not str:
        raise argparse.ArgumentTypeError('memory budget must be an integer byte count')
    try:
        amount = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError('memory budget must be an integer byte count') from error
    if not 0 < amount <= 2**63 - 1:
        raise argparse.ArgumentTypeError('memory budget must be a positive finite byte count')
    return amount


def upgrade_target_revision(options):
    """Resolve plan target independently of the reviewed executable checkout."""
    source = options.expected_runtime_revision
    tool = options.expected_producer_revision
    target = getattr(options, 'expected_upgrade_target_revision', None)
    if target is None:
        target = tool
    if any(type(value) is not str or not re.fullmatch(r'[0-9a-f]{40}', value)
            for value in (source, tool, target)):
        raise ValueError('Exact source, executable tool and upgrade target revisions are required')
    return target


def validate_source_compatible_tool_evidence(facts, evidence_service, *, actor, now):
    """Read current canonical tool qualification; local pins grant no authority.

    Compatibility and independent REVIEW bind every B/T/D/tree/archive/diff
    fingerprint. They reference one current CI_CHECK for the exact executable
    tool, not the upgrade target's CI. All reads repeat on every invocation.
    """
    import math
    from codex_web.artifact_evidence import (
        Artifact, ArtifactLifecycle, ArtifactType, Evidence, EvidenceLifecycle,
        EvidenceResult, EvidenceType,
    )
    from codex_web.recovery_tool_compatibility import (
        RecoveryToolCompatibilityError, ValidatedRecoveryTool,
    )

    def require(condition, reason):
        if not condition:
            raise RecoveryToolCompatibilityError(reason)

    def text(value):
        return type(value) is str and 0 < len(value.strip()) <= 2048

    require(isinstance(facts, ValidatedRecoveryTool), 'validated tool facts required')
    require(type(now) in (int, float) and math.isfinite(now), 'current qualification time required')
    changed = [{'path': row.path, 'base_sha256': row.base_sha256,
                'tool_sha256': row.tool_sha256}
               for row in sorted(facts.changed_files, key=lambda item: item.path)]
    diff_sha = hashlib.sha256(canonical_json_bytes(changed)).hexdigest()
    fingerprints = {
        'base_revision': facts.base_revision, 'tool_revision': facts.tool_revision,
        'upgrade_target_revision': facts.upgrade_target_revision,
        'base_tree_sha1': facts.base_tree_sha1, 'tool_tree_sha1': facts.tool_tree_sha1,
        'tool_archive_sha256': facts.tool_archive_sha256, 'changed_files_sha256': diff_sha,
    }
    artifact = evidence_service.get_artifact(facts.tool_artifact_id, actor=actor)
    require(isinstance(artifact, Artifact) and artifact.id == facts.tool_artifact_id,
            'exact canonical tool Artifact required')

    def scoped_current(record):
        require((record.organization_id, record.workspace_id) ==
                (actor.organization_id, actor.workspace_id), 'tool qualification tenant mismatch')
        require(record.project_id == artifact.project_id, 'tool qualification Project mismatch')
        require(record.invalidated_at is None and record.invalidation_reason is None,
                'tool qualification invalidated')
        expiry = record.retention_expires_at
        require(expiry is None or (math.isfinite(expiry) and expiry > now),
                'tool qualification expired')

    scoped_current(artifact)
    require(artifact.artifact_type in {ArtifactType.BUILD, ArtifactType.PACKAGE}
            and artifact.lifecycle == ArtifactLifecycle.ACTIVE,
            'active BUILD or PACKAGE tool Artifact required')
    require(artifact.revision == facts.tool_revision and artifact.digest is not None
            and artifact.digest.algorithm == 'sha256'
            and artifact.digest.value == facts.tool_archive_sha256,
            'tool Artifact revision or archive mismatch')
    compatibility = evidence_service.get_evidence(facts.compatibility_evidence_id, actor=actor)
    review = evidence_service.get_evidence(facts.review_evidence_id, actor=actor)

    def current_evidence(record, expected_id, kinds):
        require(isinstance(record, Evidence) and record.id == expected_id,
                'exact canonical qualification Evidence required')
        scoped_current(record)
        require(record.evidence_type in kinds and record.lifecycle == EvidenceLifecycle.VALID
                and record.result == EvidenceResult.PASS,
                'current PASS qualification Evidence required')
        require(facts.tool_artifact_id in record.artifact_ids,
                'qualification Evidence tool Artifact link required')
        require(all(text(value) for value in
                    (record.provider, record.source, record.external_id, record.deep_link)),
                'external qualification provenance required')

    current_evidence(compatibility, facts.compatibility_evidence_id,
                     {EvidenceType.TEST_RESULT, EvidenceType.POLICY_EVALUATION})
    current_evidence(review, facts.review_evidence_id, {EvidenceType.REVIEW})
    require(compatibility.id != review.id, 'distinct independent REVIEW required')
    for record in (compatibility, review):
        require(all(record.metadata.get(key) == value for key, value in fingerprints.items()),
                'tool qualification fingerprint mismatch')
    require(compatibility.metadata.get('review_evidence_id') == review.id
            and review.metadata.get('compatibility_evidence_id') == compatibility.id,
            'compatibility and independent REVIEW references required')
    require(all(compatibility.metadata.get(key) is True for key in
                ('downstream_checks_unchanged', 'measured_memory_qualification', 'contracts_unchanged')),
            'measured memory and unchanged downstream contract qualification required')
    ci_id = compatibility.metadata.get('ci_evidence_id')
    require(type(ci_id) is str and re.fullmatch('evidence-(?:[0-9a-f]{32}|[0-9a-f]{64})', ci_id) is not None
            and ci_id not in {compatibility.id, review.id}
            and review.metadata.get('ci_evidence_id') == ci_id,
            'distinct referenced canonical CI Evidence required')
    ci = evidence_service.get_evidence(ci_id, actor=actor)
    current_evidence(ci, ci_id, {EvidenceType.CI_CHECK})
    require(all(ci.metadata.get(key) == fingerprints[key] for key in
                ('tool_revision', 'tool_tree_sha1', 'tool_archive_sha256'))
            and ci.metadata.get('all_applicable_checks_passed') is True
            and text(ci.metadata.get('ci_run_id')),
            'exact tool CI run and all applicable checks required')
    require(text(review.metadata.get('reviewer_source'))
            and review.metadata.get('reviewer_source') == review.source
            and review.metadata.get('review_reference') == review.external_id
            and review.external_id != ci.external_id,
            'independent external REVIEW provenance required')
    return {**fingerprints, 'tool_artifact_id': artifact.id,
            'compatibility_evidence_id': compatibility.id, 'review_evidence_id': review.id,
            'ci_evidence_id': ci.id, 'ci_run_id': ci.metadata['ci_run_id'],
            'reviewer_source': review.source, 'review_reference': review.external_id}


def source_compatible_tool_facts(options):
    """Validate an explicit backport before credentials; never grant authority."""
    target = upgrade_target_revision(options)
    proof = getattr(options, 'source_compatible_tool_proof', None)
    digest = getattr(options, 'source_compatible_tool_proof_sha256', None)
    if proof is None and digest is None:
        if options.expected_producer_revision != options.expected_runtime_revision:
            raise ValueError('Pre-install verification writers must use the installed source revision')
        return None
    if proof is None or digest is None or getattr(options, 'expected_upgrade_target_revision', None) is None:
        raise ValueError('An explicit compatible tool proof, SHA and separate upgrade target are required')
    from codex_web.recovery_tool_compatibility import validate_recovery_tool_proof, validate_tool_imports
    facts = validate_recovery_tool_proof(proof, digest,
        base_revision=options.expected_runtime_revision,
        tool_revision=options.expected_producer_revision,
        upgrade_target_revision=target, tool_checkout=options.producer_checkout)
    validate_tool_imports(facts, tuple(module for name, module in sys.modules.items()
        if name == 'codex_web' or name.startswith('codex_web.')))
    return facts


def qualify_source_compatible_tool(options, evidence_service, *, actor, now):
    """Repeat static pins and current canonical qualification at write guards."""
    facts = source_compatible_tool_facts(options)
    if facts is None:
        return None
    return {**validate_source_compatible_tool_evidence(facts, evidence_service, actor=actor, now=now),
            'proof_sha256': facts.proof_sha256}


def source_compatible_tool_target(options):
    """Require source code for a verifier that publishes before source restart.

    Use the entire source package; do not down-convert target records. Capture
    admission remains separately governed: a capture's audited write set is not
    permission for a verifier to publish Evidence through target-schema code.
    """
    target = upgrade_target_revision(options)
    source_compatible_tool_facts(options)
    return target


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    for name in ('plan-id', 'service-instance-id', 'expected-runtime-revision', 'expected-producer-revision'):
        result.add_argument('--' + name, required=True)
    for name in ('runtime-checkout', 'producer-checkout', 'sqlite-path', 'key-material-directory',
                 'backup-directory', 'journal-directory', 'qualification-lock'):
        result.add_argument('--' + name, type=Path, required=True)
    result.add_argument('--expected-upgrade-target-revision',
        help='Exact plan/Release target, separate from source-compatible executable producer revision')
    result.add_argument('--source-compatible-tool-proof', type=Path,
        help='Explicit private recovery-only backport proof; requires its SHA and independent canonical qualification')
    result.add_argument('--source-compatible-tool-proof-sha256',
        help='Exact SHA256 of the explicit compatible-tool proof; never a grant or substitute for installed-source checks')
    result.add_argument('--unit', default='codex-web.service')
    result.add_argument('--api', default='http://127.0.0.1:8765')
    result.add_argument('--expected-runtime-pid', type=int, required=True)
    result.add_argument('--operator-token-fd', type=int, required=True)
    result.add_argument('--memory-budget-bytes', type=memory_budget_bytes,
        default=DEFAULT_MEMORY_BUDGET_BYTES,
        help='Reviewed ceiling for this supervised helper only (default: 6 GiB); actual finite cgroup and headroom checks remain required')
    result.add_argument('--execute', action='store_true', required=True)
    return result


def main(argv=None):
    options = parser().parse_args(argv)
    upgrade_target = (source_compatible_tool_target(options)
        if (options.expected_upgrade_target_revision is not None
            or options.source_compatible_tool_proof is not None
            or options.source_compatible_tool_proof_sha256 is not None)
        else upgrade_target_revision(options))
    if (not re.fullmatch(r'[A-Za-z0-9_.@-]+\.service', options.unit)
            or any(not re.fullmatch(r'[0-9a-f]{40}', value) for value in
                (options.expected_runtime_revision, options.expected_producer_revision))
            or options.expected_runtime_pid <= 0
            or Path(__file__).resolve().parents[1] != options.producer_checkout.resolve()):
        raise ValueError('Explicit source/process/unit pins are required')
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    options.journal_directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    stage, runtime, token, budget = 'preflight', None, None, None
    memory_errors = ()
    lock_fds = []
    try:
        for path in (LOCAL_DEPLOY_LOCK, options.qualification_lock):
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            lock_fds.append(fd)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        host = OfflineHostGuard(options, lock_fds[0])
        host.check()
        token = os.read(options.operator_token_fd, 16385).decode().strip()
        if not token or len(token) > 16384 or '\n' in token or '\r' in token:
            raise ValueError('Existing operator credential is unavailable')
        # The explicit reviewed operation ceiling is not capacity or a grant.
        # Require an actual finite cgroup at entry and each guarded boundary;
        # canonical host/ancestor headroom and Recovery estimates are unchanged.
        from codex_web.services.recovery_memory import require_process_memory_limit, RecoveryMemoryRefused
        from codex_web.services.recovery import RecoveryMemoryConflictError
        memory_errors = (RecoveryMemoryRefused, RecoveryMemoryConflictError)
        cap = require_process_memory_limit(maximum_bytes=options.memory_budget_bytes, maximum_swap_bytes=0)
        from codex_web.storage.state_store import build_state_store, StorageOperationBudget, storage_operation_budget
        store = build_state_store(sqlite_path=options.sqlite_path,
            backend=os.environ.get('CODEX_WEB_STATE_BACKEND', 'sqlite'),
            postgres_dsn=os.environ.get('CODEX_WEB_POSTGRES_DSN'), initialize=False)
        observed = {}
        def refresh():
            return observed['plan']
        custody = None
        def guard():
            host.check()
            require_process_memory_limit(maximum_bytes=options.memory_budget_bytes, maximum_swap_bytes=0)
            if custody is not None and recovery_custody(store) != custody:
                raise RuntimeError('Canonical Recovery custody changed')
            if runtime is not None and options.source_compatible_tool_proof is not None:
                qualify_source_compatible_tool(options, runtime.upgrades.evidence,
                    actor=runtime.authenticate(), now=time.time())
        def qualify_tool(evidence, actor):
            return qualify_source_compatible_tool(options, evidence, actor=actor, now=time.time())
        runtime = compose_capture_runtime(store, operator_token=token,
            key_material_dir=options.key_material_directory, backup_dir=options.backup_directory,
            refresh=refresh, check_runtime=guard,
            journal=lambda kind, value: record(options.journal_directory, kind, value),
            source_revision=options.expected_runtime_revision,
            target_revision=upgrade_target,
            service_instance_id=options.service_instance_id,
            qualify_tool=qualify_tool if options.source_compatible_tool_proof is not None else None)
        if local_api(options.api, token, 'GET', '/api/version')['gitRevision'] != options.expected_runtime_revision:
            raise RuntimeError('Installed API source changed')
        recovery_observation = local_api(options.api, token, 'GET', '/api/recovery/status')
        executor = require_recovery_idle(recovery_observation)
        observed['plan'] = UpgradePlan.model_validate(local_api(options.api, token, 'POST',
            '/api/upgrades/' + quote(options.plan_id, safe='') + '/preflight')['item'])
        prepared_at = time.time()
        qualify_capture(runtime.upgrades, observed['plan'], actor=runtime.authenticate(),
            source_revision=options.expected_runtime_revision,
            target_revision=upgrade_target,
            service_instance_id=options.service_instance_id, now=prepared_at)
        custody = recovery_custody(store)
        host.check()
        record(options.journal_directory, 'pre-stop-qualified', {
            'pre_stop_plan': observed['plan'].model_dump(mode='json'),
            'historical_observation_at': prepared_at, 'recovery_executor': executor,
            'recovery_custody_sha256': custody, 'memory_budget_bytes': options.memory_budget_bytes,
            'memory_cap': asdict(cap),
            'runtime_pid': options.expected_runtime_pid, 'service_invocation': host.initial['InvocationID'],
            'runtime_revision': options.expected_runtime_revision,
            'producer_revision': options.expected_producer_revision,
            'upgrade_target_revision': upgrade_target,
            'source_compatible_tool': (qualify_tool(runtime.upgrades.evidence, runtime.authenticate())
                if options.source_compatible_tool_proof is not None else None),
            'canonical_data_directory': str(host.data_root),
            'operator_must_restore_service': True})
        stage = 'waiting_for_operator_clean_stop'
        host.wait_stopped(min(prepared_at + 30, observed['plan'].preflight.evaluated_at + 30))
        guard()
        record(options.journal_directory, 'service-stopped', {'stopped_at': time.time(),
            'service': host.service(), 'historical_preflight_not_refreshed': True})
        runtime.offline = OfflineCaptureProof(hashlib.sha256(canonical_json_bytes(
            observed['plan'].model_dump(mode='json'))).hexdigest(), prepared_at, prepared_at + 600, guard)
        stage = 'capture_once'
        # Existing cooperative storage deadlines reach the owned Recovery
        # thread through its copied context. Expiry never detaches that writer
        # or turns an incomplete operation into permission for another attempt.
        budget = StorageOperationBudget(max(0.001, runtime.offline.deadline_at - time.time()))
        budget_token = storage_operation_budget.set(budget)
        try:
            plan = asyncio.run(runtime.capture_once())
        finally:
            storage_operation_budget.reset(budget_token)
        record(options.journal_directory, 'capture-completed', {'plan_id': plan.id,
            'backup_id': plan.pre_upgrade_backup_id, 'memory_budget_bytes': options.memory_budget_bytes,
            'capture_timings': budget.recovery_capture_timings(),
            'restore_qualification_claimed': False,
            'deployment_qualification_claimed': False, 'operator_must_restore_service': True})
        return 0
    except Exception as error:
        record(options.journal_directory, 'stopped', {'stage': stage,
            'memory_budget_bytes': options.memory_budget_bytes,
            'exception_type': type(error).__name__, 'retry_authorized': False,
            **({'capture_timings': budget.recovery_capture_timings()} if budget is not None else {}),
            # Only these canonical types contain bounded capacity codes and
            # numbers. Other exception strings/causes may expose credentials.
            **({'capacity_refusal': str(error)[:2048]} if type(error) in memory_errors else {}),
            'effect_outcome': 'inspect canonical outcome' if stage == 'capture_once' else 'no capture started',
            'operator_must_inspect_and_restore_service': True})
        return 2
    finally:
        if runtime is not None:
            asyncio.run(runtime.upgrades.recovery.stop())
        token = None
        for fd in reversed(lock_fds):
            os.close(fd)


if __name__ == '__main__':
    raise SystemExit(main())
