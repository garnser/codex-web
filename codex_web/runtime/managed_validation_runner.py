"""Execution-plane managed-output protocol. Standard library only; no credentials.

Run inside the existing bounded worker namespace:
python "$CODEX_WEB_MANAGED_VALIDATION_RUNNER" --source tests/example.py -- command args
The report never contains command text, stdout/stderr, secrets or a pass claim.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid


def source_name(value):
    if (not isinstance(value, str) or len(value) > 1024 or not re.fullmatch(r"[A-Za-z0-9_./-]+", value)
            or value.startswith('/') or any(p in {'', '.', '..', '.git', '.venv', '.secrets', 'private-keys'} for p in value.split('/'))
            or any(p == '.env' or p.startswith('.env.') for p in value.split('/'))
            or value.lower().endswith(('.pem', '.key', '.p12', '.pfx', '.jks', '.keystore'))):
        raise ValueError('invalid managed validation source path')
    return value


def sanitize_report(value):
    """Reject arbitrary payloads; callers persist only this protocol, never logs."""
    fields = {'schema_version','run_id','command_digest','sources','started_at','finished_at','exit_code','state'}
    if not isinstance(value, dict) or set(value) != fields or value['schema_version'] != '1.0':
        raise ValueError('invalid managed validation report')
    if not re.fullmatch(r'[0-9a-f]{32}', str(value['run_id'])) or not re.fullmatch(r'[0-9a-f]{64}', str(value['command_digest'])):
        raise ValueError('invalid validation identity')
    if value['state'] not in {'running','finished'} or not isinstance(value['sources'], list) or not 1 <= len(value['sources']) <= 32:
        raise ValueError('invalid validation state')
    for source in value['sources']:
        if not isinstance(source, dict) or set(source) != {'path','sha256'}:
            raise ValueError('invalid source attribution')
        source_name(source['path'])
        if not re.fullmatch(r'[0-9a-f]{64}', str(source['sha256'])):
            raise ValueError('invalid source digest')
    for field in ('started_at','finished_at'):
        number = value[field]
        if number is None and field == 'finished_at': continue
        if isinstance(number, bool) or not isinstance(number, (int,float)) or not math.isfinite(number) or not 0 < number < 1e12:
            raise ValueError('invalid validation timestamp')
    code = value['exit_code']
    if code is not None and (type(code) is not int or not -255 <= code <= 255):
        raise ValueError('invalid validation exit code')
    if value['state'] == 'running' and (code is not None or value['finished_at'] is not None):
        raise ValueError('unfinished validation cannot claim an outcome')
    if value['state'] == 'finished' and (code is None or value['finished_at'] is None):
        raise ValueError('finished validation lacks an exit observation')
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', action='append', required=True)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    root = Path.cwd().resolve()
    directory = Path(os.environ['CODEX_WEB_MANAGED_VALIDATION_DIR'])
    if not command or not directory.is_dir() or directory.is_symlink() or len(args.source) > 32:
        raise ValueError('managed output registration or command unavailable')
    sources = []
    for name in args.source:
        name = source_name(name)
        path = root / name
        if path.resolve() != path or not path.is_file() or path.stat().st_size > 4 * 1024**2:
            raise ValueError('source must be a bounded regular workspace file')
        sources.append({'path':name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
    report = dict(schema_version='1.0',run_id=uuid.uuid4().hex,
        command_digest=hashlib.sha256(json.dumps(command,separators=(',',':')).encode()).hexdigest(),
        sources=sources,started_at=time.time(),finished_at=None,exit_code=None,state='running')
    target = directory / f"report-{report['run_id']}.json"
    def write():
        content = json.dumps(sanitize_report(report),sort_keys=True,separators=(',',':')).encode()
        temporary = target.with_suffix('.pending')
        fd = os.open(temporary, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd,'wb') as output:
            output.write(content); output.flush(); os.fsync(output.fileno())
        os.replace(temporary,target)
    write()  # Before launch: interruption leaves an attributed unknown report.
    process = subprocess.Popen(command)  # Same existing worker process group/limits.
    report.update(exit_code=process.wait(),finished_at=time.time(),state='finished')
    write()
    return report['exit_code']


if __name__ == '__main__':
    sys.exit(main())
