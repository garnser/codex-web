#!/usr/bin/env python3
"""Explicit, single-intent read-only incident settlement; never starts a server.

The running service is not upgraded by this command. Review the tool source and
its tests before use. An unknown outcome requires canonical inspection, not a
second invocation. Credentials remain in memory and are revoked in finally.
"""
from __future__ import annotations

import argparse
import hashlib
import asyncio
import fcntl
import json
import os
from pathlib import Path
import resource
import signal
import subprocess
import sys
import time
from urllib.parse import quote, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def private_record(directory, name, value):
    path = directory / (name + '.json')
    with path.open('x', encoding='utf-8') as output:
        json.dump(value, output, sort_keys=True, indent=2)
        output.flush()
        os.fsync(output.fileno())


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RuntimeError('Canonical identity API redirects are unavailable')


def local_api(base, method, path, payload=None):
    parsed = urlsplit(base)
    if parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', '::1'} or parsed.username or parsed.password:
        raise ValueError('Canonical identity API must be a loopback HTTP endpoint')
    request = Request(base.rstrip('/') + path, method=method,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={'Content-Type': 'application/json'})
    # Never include response/error bodies in operator output: token creation is
    # the one endpoint that legitimately returns a credential once.
    with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=25) as response:
        if response.status != 200:
            raise RuntimeError('Canonical identity API did not return HTTP 200')
        return json.load(response)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    for name in ('intent-id', 'assignment-id', 'worker-id', 'worker-service-identity',
                 'expected-runtime-revision', 'expected-tool-revision', 'gitlab-api-base', 'gitlab-provider-instance'):
        result.add_argument('--' + name, required=True)
    result.add_argument('--fence', type=int, required=True)
    result.add_argument('--expected-runtime-pid', type=int, required=True)
    for name in ('lock-file', 'journal-directory', 'secret-material-directory', 'projects-file', 'sqlite-path'):
        result.add_argument('--' + name, type=Path, required=True)
    result.add_argument('--identity-api', default='http://127.0.0.1:8765')
    result.add_argument('--gitlab-web-base')
    result.add_argument('--execute', action='store_true', required=True)
    return result


def main(argv=None):
    options = parser().parse_args(argv)
    os.umask(0o077)
    options.journal_directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    journal = options.journal_directory
    # Independent OS bounds supplement existing canonical/provider deadlines.
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (180, 180))
    resource.setrlimit(resource.RLIMIT_AS, (3 * 1024**3, 3 * 1024**3))
    def timeout(_signal, _frame):
        raise TimeoutError('Read-only incident settlement wall-clock bound expired')
    signal.signal(signal.SIGALRM, timeout)
    from codex_web.local_deployment import LOCAL_DEPLOY_LOCK
    deploy_lock_fd = os.open(LOCAL_DEPLOY_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    lock_fd = os.open(options.lock_file, os.O_RDWR | os.O_CREAT, 0o600)
    token_id = None
    raw_token = None
    stage = 'preflight'
    exit_code = 2
    try:
        fcntl.flock(deploy_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        from codex_web.storage.state_store import build_state_store, StorageOperationBudget, storage_operation_budget
        from codex_web.storage.action_intents import ActionIntentStore
        from codex_web.storage.execution_workers import ExecutionWorkerStore
        from codex_web.readonly_reconciliation_runtime import (
            build_readonly_reconciliation_runtime, validate_readonly_reconciliation_target)
        store = build_state_store(sqlite_path=options.sqlite_path,
            backend=os.environ.get('CODEX_WEB_STATE_BACKEND', 'sqlite'),
            postgres_dsn=os.environ.get('CODEX_WEB_POSTGRES_DSN'))
        # This canonical guard must run BEFORE credential provisioning or any
        # other mutation, including refusing WorkItem-owning intents.
        validate_readonly_reconciliation_target(store, intent_id=options.intent_id,
            expected_assignment_id=options.assignment_id, expected_fence=options.fence)
        intent = ActionIntentStore(store).get(options.intent_id)
        worker = ExecutionWorkerStore(store).worker(options.worker_id)
        if (intent.readiness_origin.worker_id != options.worker_id or worker is None
                or worker.service_identity_id != options.worker_service_identity):
            raise RuntimeError('Original canonical worker identity changed')
        actual_pid = int(subprocess.check_output(['systemctl', 'show', 'codex-web.service',
            '--property=MainPID', '--value'], text=True, timeout=10).strip())
        if actual_pid != options.expected_runtime_pid or actual_pid <= 0:
            raise RuntimeError('Canonical runtime service PID changed')
        os.kill(actual_pid, 0)
        runtime = local_api(options.identity_api, 'GET', '/api/version')
        if runtime['gitRevision'] != options.expected_runtime_revision:
            raise RuntimeError('Installed runtime source changed')
        checkout = Path(__file__).resolve().parents[1]
        def git(*arguments):
            return subprocess.check_output(['git', '-c', 'core.hooksPath=/dev/null', '-C', str(checkout),
                *arguments], text=True, timeout=10).strip()
        revision = git('rev-parse', 'HEAD')
        if revision != options.expected_tool_revision:
            raise RuntimeError('Reviewed tool revision changed')
        if git('status', '--porcelain', '--untracked-files=all', '--', 'codex_web', 'scripts',
                'requirements.txt', 'requirements-dev.txt', 'pyproject.toml', 'server.py'):
            raise RuntimeError('Executable tool source must match the clean reviewed revision')
        tree = git('rev-parse', 'HEAD^{tree}')
        source_hashes = {str(path.relative_to(checkout)):hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (Path(__file__).resolve(), checkout/'codex_web'/'readonly_reconciliation_runtime.py')}
        private_record(journal, 'preflight', {'tool_revision':revision, 'tool_tree':tree,
            'python_version':sys.version, 'helper_sha256':source_hashes,
            'installed_revision':options.expected_runtime_revision, 'runtime_pid':options.expected_runtime_pid,
            'intent_id':intent.id, 'assignment_id':options.assignment_id, 'fence':options.fence,
            'worker_id':worker.id, 'worker_identity_id':worker.service_identity_id,
            'started_at':time.time(), 'provider_methods':['GET', 'HEAD']})
        signal.alarm(240)
        stage = 'credential_creation'
        private_record(journal, 'credential-creation-attempt', {'at':time.time(),
            'service_identity_id':worker.service_identity_id, 'expires_within_seconds':300})
        credentials = local_api(options.identity_api, 'POST', '/api/identity/service-tokens', {
            'service_identity_id':worker.service_identity_id, 'organization_id':intent.organization_id,
            'workspace_id':intent.workspace_id, 'scopes':['action-intent:worker', 'secret:use'],
            'expires_at':time.time()+300})
        token_id, raw_token = credentials['token_id'], credentials['token']
        private_record(journal, 'credential-created', {'token_id':token_id,
            'expires_at':credentials['expires_at']})
        stage = 'canonical_composition'
        engine = build_readonly_reconciliation_runtime(store, intent_id=intent.id,
            expected_assignment_id=options.assignment_id, expected_fence=options.fence,
            worker_token=raw_token, secret_material_dir=options.secret_material_directory,
            projects_file=options.projects_file, gitlab_api_base=options.gitlab_api_base,
            gitlab_provider_instance=options.gitlab_provider_instance, gitlab_web_base=options.gitlab_web_base)
        stage = 'reconciliation'
        private_record(journal, 'reconciliation-attempt', {'at':time.time(), 'intent_id':intent.id,
            'retry_if_idempotent':False})
        budget = StorageOperationBudget(220)
        budget_token = storage_operation_budget.set(budget)
        try:
            result = asyncio.run(engine.reconcile_once())
        finally:
            storage_operation_budget.reset(budget_token)
        private_record(journal, 'reconciliation-response', {'at':time.time(), 'intent_id':result.id,
            'status':str(result.status), 'last_receipt_id':result.last_receipt_id})
        exit_code = 0 if str(result.status) == 'succeeded' else 1
    except Exception as exc:
        # Do not expose exception strings: a provider or credential backend may
        # include protected material. Preserve precise stage and exception type.
        private_record(journal, 'stopped', {'at':time.time(), 'stage':stage,
            'exception_type':type(exc).__name__, 'outcome_requires_inspection':stage != 'preflight',
            'retry_authorized':False})
        exit_code = 2
    finally:
        signal.alarm(0)
        raw_token = None
        if token_id is not None:
            try:
                local_api(options.identity_api, 'DELETE', '/api/identity/service-tokens/' + quote(token_id, safe=''))
                private_record(journal, 'credential-revoked', {'at':time.time(), 'token_id':token_id})
            except Exception as exc:
                private_record(journal, 'credential-revocation-unconfirmed', {'at':time.time(),
                    'token_id':token_id, 'exception_type':type(exc).__name__})
                exit_code = 3
        os.close(lock_fd)
        os.close(deploy_lock_fd)
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
