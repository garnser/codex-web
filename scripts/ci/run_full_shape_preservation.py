"""Serialized Linux operator qualification using only allocated, synthetic storage."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import resource
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid

TARGETS = (
    'tests.test_storage_preservation.SQLiteStoragePreservationTests.test_full_current_shape_crosses_old_row_and_value_limits',
    'tests.test_storage_preservation.PostgresStoragePreservationTests.test_full_current_shape_crosses_old_row_and_value_limits',
    'tests.test_recovery_storage_operator.FullShapeEncryptedStorageTests.test_full_current_shape_governed_encrypted_capture_and_isolated_verify',
)
NOTICE = ('Full-shape preservation: NOT EXECUTED by default gates. Owner/invocation: '
          'docs/architecture/full-shape-qualification.md; '
          'python scripts/ci/run_full_shape_preservation.py --workspace-parent PATH')
IMAGE = 'docker.io/library/postgres@sha256:aa90e97ee862e558111d34cfb8b2c4bec768c2b039fb791341686928560263b3'
GIB = 1024 ** 3
DISK_BYTES = 20 * GIB
WALL_SECONDS = 1200


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def successful(report):
    """Neither skipped cases nor an incomplete selection qualify the boundary."""
    return (report.get('tests') == list(TARGETS) and
            report.get('successful') == list(TARGETS) and
            not any(report.get(k) for k in ('skipped', 'errors', 'failures')))


def child(output):
    # Parent stages the exact committed source and supplies its own ephemeral DSN.
    root = Path(__file__).resolve().parents[2]
    require((root / 'data').is_dir() and not (root / 'data').is_symlink(), 'isolated data directory required')
    require(root.name == 'source' and root.parent.name == 'allocated' and
            root.stat().st_dev != root.parent.parent.stat().st_dev,
            'child requires the dedicated allocated filesystem')
    require(output == root.parent.parent / 'tests.json', 'owned result path required')
    require(os.environ.get('CODEX_WEB_TEST_PRESERVATION_SCALE') == '1', 'explicit scale admission missing')
    os.chdir(root)
    sys.path.insert(0, str(root))
    import psycopg
    expected = Path(os.environ['CODEX_WEB_TEST_POSTGRES_DATA_DIRECTORY'])
    reservation = Path(os.environ['CODEX_WEB_TEST_PRESERVATION_RESERVATION_DIR'])
    require(expected == root.parent / 'postgres' and reservation == root.parent / 'reservations',
            'only the allocated private PostgreSQL topology is supported')
    with psycopg.connect(os.environ['CODEX_WEB_TEST_POSTGRES_DSN']) as connection:
        actual = Path(connection.execute('SHOW data_directory').fetchone()[0])
        version = connection.execute('SELECT version()').fetchone()[0]
    require(actual == expected and expected.is_dir(), 'actual PGDATA mapping mismatch')
    require(expected.stat().st_dev == reservation.stat().st_dev == root.stat().st_dev,
            'PGDATA, reservations and source must share the allocated filesystem')
    import unittest
    report = {'tests': [], 'successful': [], 'skipped': [], 'errors': [], 'failures': [],
              'postgres_version': version, 'python_version': sys.version.split()[0]}

    class Result(unittest.TextTestResult):
        def startTest(self, test):
            report['tests'].append(test.id())
            super().startTest(test)

        def addSuccess(self, test):
            report['successful'].append(test.id())
            super().addSuccess(test)

        def addSkip(self, test, reason):
            report['skipped'].append({'id': test.id(), 'reason': reason})
            super().addSkip(test, reason)

        def addError(self, test, error):
            report['errors'].append(test.id())
            super().addError(test, error)

        def addFailure(self, test, error):
            report['failures'].append(test.id())
            super().addFailure(test, error)

    started = time.monotonic()
    suite = unittest.TestLoader().loadTestsFromNames(TARGETS)
    result = unittest.TextTestRunner(verbosity=2, resultclass=Result).run(suite)
    report['seconds'] = time.monotonic() - started
    report['qualified'] = result.wasSuccessful() and successful(report)
    output.write_text(json.dumps(report, indent=2) + '\n')
    return 0 if report['qualified'] else 1


def limits():
    resource.setrlimit(resource.RLIMIT_AS, (4 * GIB, 4 * GIB))
    resource.setrlimit(resource.RLIMIT_CPU, (WALL_SECONDS, WALL_SECONDS))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def qualify(parent, engine, sudo_engine):
    os.umask(0o077)
    require(parent.is_absolute() and parent.resolve() == parent, 'workspace parent must be absolute without symlinks')
    require(parent.is_dir() and parent.stat().st_uid == os.getuid(), 'workspace parent must exist and belong to caller')
    require(shutil.disk_usage(parent).free >= DISK_BYTES + 2 * GIB, 'at least 22 GiB free required before allocation')
    # Lock the caller's operator workspace, not the application state or database.
    lock = os.open(parent / '.qualification.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock)
        raise RuntimeError('another full-shape qualification owns this workspace') from None
    prefix = [] if os.geteuid() == 0 else ['sudo', '-n']
    container_cli = (prefix if sudo_engine else []) + [engine]
    root = Path(__file__).resolve().parents[2]
    job = Path(tempfile.mkdtemp(prefix='full-shape-', dir=parent))
    mount = job / 'allocated'
    image = job / 'disk.ext4'
    mount.mkdir()
    started = time.monotonic()
    deadline = started + WALL_SECONDS
    report = {'qualified': False, 'status': 'not_executed', 'targets': list(TARGETS),
              'postgres_image': IMAGE, 'disk_bytes': DISK_BYTES, 'wall_seconds': WALL_SECONDS,
              'client_virtual_memory_bytes': 4 * GIB, 'postgres_memory_bytes': 2 * GIB,
              'evidence_directory': str(job), 'cleanup_errors': []}
    device = None
    mounted = False
    container = 'codex-full-shape-' + uuid.uuid4().hex
    container_attempted = False
    report['container_name'] = container
    process = None

    def run(command, *, timeout=120):
        remaining = deadline - time.monotonic()
        require(remaining > 0, 'qualification wall budget exhausted')
        result = subprocess.run(command, capture_output=True, text=True, check=True,
                                timeout=min(timeout, remaining))
        return result.stdout.strip()

    try:
        report['source_revision'] = run(['git', '-C', str(root), 'rev-parse', 'HEAD'])
        require(not run(['git', '-C', str(root), 'status', '--porcelain']), 'commit source changes before qualification')
        with image.open('xb') as allocated:
            os.posix_fallocate(allocated.fileno(), 0, DISK_BYTES)
        require(image.stat().st_blocks * 512 >= DISK_BYTES, 'whole-job disk allocation incomplete')
        device = run(prefix + ['losetup', '--find', '--show', str(image)])
        require(re.fullmatch(r'/dev/loop[0-9]+', device), 'unexpected allocated loop device')
        report['loop_device'] = device
        run(prefix + ['mkfs.ext4', '-q', '-m', '0', '-F', device])
        run(prefix + ['mount', '-o', 'nodev,nosuid,noatime', device, str(mount)])
        mounted = True
        run(prefix + ['chown', f'{os.getuid()}:{os.getgid()}', str(mount)])
        source = mount / 'source'
        source.mkdir()
        # git archive never dereferences the tracked deployment data symlink.
        with tempfile.TemporaryFile(dir=job) as archive:
            subprocess.run(['git', '-C', str(root), 'archive', 'HEAD'], stdout=archive, check=True,
                           timeout=max(1, deadline - time.monotonic()))
            archive.seek(0)
            with tarfile.open(fileobj=archive) as contents:
                contents.extractall(source, members=[m for m in contents.getmembers()
                                    if m.name != 'data' and not m.name.startswith('data/')], filter='data')
        (source / 'data').mkdir()
        for name in ('postgres', 'tmp', 'home', 'reservations'):
            (mount / name).mkdir()
        pgdata = mount / 'postgres'
        # Bind the real server files at the SAME absolute path returned by SHOW.
        container_attempted = True
        run(container_cli + ['run', '--detach', '--name', container, '--memory=2g', '--cpus=2',
            '--pids-limit=256', '--stop-timeout=5', '-e', 'POSTGRES_PASSWORD=synthetic-qualification',
            '-e', 'POSTGRES_DB=qualification', '-e', 'PGDATA=' + str(pgdata),
            '--read-only', '--tmpfs', '/tmp:rw,size=64m',
            '--tmpfs', '/var/run/postgresql:rw,size=16m,mode=3777', '--log-opt', 'max-size=10m',
            '-v', f'{pgdata}:{pgdata}:Z', '-p', '127.0.0.1::5432', IMAGE])
        for _ in range(60):
            ready = subprocess.run(container_cli + ['exec', container, 'pg_isready', '-U', 'postgres',
                                                    '-d', 'qualification'], capture_output=True, timeout=5)
            if ready.returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError('private PostgreSQL startup failed')
        port = run(container_cli + ['port', container, '5432/tcp']).rsplit(':', 1)[1]
        env = {k: v for k, v in os.environ.items() if not k.startswith(('CODEX_WEB_', 'PG'))}
        env.update(CODEX_WEB_TEST_PRESERVATION_SCALE='1',
            CODEX_WEB_TEST_POSTGRES_DSN=f'postgresql://postgres:synthetic-qualification@127.0.0.1:{port}/qualification',
            CODEX_WEB_TEST_POSTGRES_DATA_DIRECTORY=str(pgdata),
            CODEX_WEB_TEST_PRESERVATION_RESERVATION_DIR=str(mount / 'reservations'),
            TMPDIR=str(mount / 'tmp'), HOME=str(mount / 'home'), PYTHONPATH=str(source),
            PYTHONDONTWRITEBYTECODE='1', PYTHONUNBUFFERED='1')
        report['status'] = 'running'
        (job / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
        print('Qualification evidence: ' + str(job), flush=True)
        with (job / 'tests.log').open('w') as log:
            process = subprocess.Popen([sys.executable, str(source / 'scripts/ci/run_full_shape_preservation.py'),
                '--child-result', str(job / 'tests.json')], cwd=source, env=env, stdout=log,
                stderr=subprocess.STDOUT, preexec_fn=limits, start_new_session=True)
            process.wait(timeout=max(1, deadline - time.monotonic()))
        result = json.loads((job / 'tests.json').read_text())
        require(process.returncode == 0 and result['qualified'] and successful(result),
                'full-shape cases failed, skipped or incomplete; see tests.log')
        report['status'] = 'passed'
        report['test_result'] = result
    except BaseException as error:
        report['status'] = 'failed_or_unexecuted'
        report['error'] = str(error)
        print(str(error), file=sys.stderr)
    finally:
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        cleanup = []
        if container_attempted:
            try:
                found = subprocess.run(container_cli + ['ps', '-a', '--format', '{{.Names}}',
                                                       '--filter', 'name=' + container],
                                       capture_output=True, text=True, check=True, timeout=30)
                if container in found.stdout.splitlines():
                    cleanup.append(container_cli + ['rm', '-f', '-v', container])
            except Exception as error:
                report['cleanup_errors'].append(str(error))
        if mounted:
            cleanup.append(prefix + ['umount', str(mount)])
        # Detach only after successful unmount; preserve an occupied allocation.
        for command in (cleanup if not report['cleanup_errors'] else []):
            try:
                subprocess.run(command, capture_output=True, text=True, check=True, timeout=30)
            except Exception as error:
                report['cleanup_errors'].append(str(error))
                break
        if device is not None and not report['cleanup_errors']:
            try:
                subprocess.run(prefix + ['losetup', '-d', device], capture_output=True, check=True, timeout=30)
            except Exception as error:
                report['cleanup_errors'].append(str(error))
        if not report['cleanup_errors']:
            try:
                image.unlink(missing_ok=True)
                mount.rmdir()
            except Exception as error:
                report['cleanup_errors'].append(str(error))
        if report['cleanup_errors']:
            report['status'] = 'cleanup_failed'
        report['qualified'] = report['status'] == 'passed'
        report['seconds'] = time.monotonic() - started
        (job / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
        os.close(lock)
        print(json.dumps(report, indent=2), flush=True)
    return 0 if report['qualified'] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--status', action='store_true', help='report ownership without running tests')
    parser.add_argument('--workspace-parent', type=Path, help='existing caller-owned directory with 22 GiB free')
    parser.add_argument('--engine', choices=('docker', 'podman'), default='docker')
    parser.add_argument('--sudo-engine', action='store_true', help='use noninteractive sudo for the container engine')
    parser.add_argument('--child-result', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child_result:
        return child(args.child_result)
    if args.status:
        print(NOTICE)
        print(json.dumps({'status': 'not_executed', 'distinct_targets': list(TARGETS),
                          'default_scale_skips': 29, 'inherited_non_scale_cases': 26}))
        return 0
    if args.workspace_parent is None:
        parser.error('--workspace-parent is required for qualification')
    return qualify(args.workspace_parent, args.engine, args.sudo_engine)


if __name__ == '__main__':
    sys.exit(main())
