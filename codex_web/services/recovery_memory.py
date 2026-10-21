"""Cooperative Recovery allocation estimates, not reservations or OOM guarantees."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


class RecoveryMemoryRefused(RuntimeError):
    pass


@dataclass(frozen=True)
class MemoryHeadroom:
    available_bytes: int
    source: str


@dataclass(frozen=True)
class ProcessMemoryLimit:
    memory_max_bytes: int
    memory_swap_max_bytes: int
    current_bytes: int


def _number(value: str) -> int:
    if not value.strip().isdigit():
        raise ValueError("invalid memory counter")
    return int(value.strip())


def _unified_cgroup(proc: Path, cgroup: Path) -> tuple[Path, Path]:
    memberships = (proc / 'self/cgroup').read_text().splitlines()
    unified = [line[3:] for line in memberships if line.startswith('0::')]
    if len(unified) != 1 or any(not line.startswith('0::') for line in memberships):
        raise ValueError('unsupported cgroup hierarchy')
    membership = Path(unified[0])
    if not membership.is_absolute() or '..' in membership.parts:
        raise ValueError('invalid cgroup membership')
    mounts = []
    for line in (proc / 'self/mountinfo').read_text().splitlines():
        before, _, after = line.partition(' - ')
        if after.split()[:1] == ['cgroup2']:
            parts = before.split()
            unescape = lambda s: re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), s)
            mounts.append((Path(unescape(parts[3])), Path(unescape(parts[4]))))
    matching = [(root, mount) for root, mount in mounts if mount == cgroup]
    if len(matching) != 1:
        raise ValueError('unknown cgroup mount')
    root, mount = matching[0]
    return mount / membership.relative_to(root), mount


def require_process_memory_limit(
    *, maximum_bytes: int, maximum_swap_bytes: int = 0,
    proc: Path = Path('/proc'), cgroup: Path = Path('/sys/fs/cgroup'),
) -> ProcessMemoryLimit:
    """Require actual finite limits on this process's cgroup, not CLI assertions.

    This is a budget observation for a separately supervised operation. It does
    not set limits, reserve memory, authorize Recovery or replace fresh headroom
    admission. A caller must retain its supervision and lifecycle guards.
    """
    if type(maximum_bytes) is not int or maximum_bytes <= 0 or \
            type(maximum_swap_bytes) is not int or maximum_swap_bytes < 0:
        raise RecoveryMemoryRefused('recovery_memory_limit_invalid: invalid operation budget')
    try:
        current, _ = _unified_cgroup(proc, cgroup)
        limit = _number((current / 'memory.max').read_text())
        swap = _number((current / 'memory.swap.max').read_text())
        used = _number((current / 'memory.current').read_text())
        if limit <= 0:
            raise ValueError('invalid cgroup limit')
    except (OSError, ValueError, KeyError, IndexError) as exc:
        raise RecoveryMemoryRefused(
            'recovery_memory_limit_unknown: require a dedicated cgroup-v2 operation '
            'with observable finite memory.max and memory.swap.max'
        ) from exc
    if limit > maximum_bytes or swap > maximum_swap_bytes:
        raise RecoveryMemoryRefused(
            f'recovery_memory_limit_exceeded: observed_memory_max_bytes={limit}; '
            f'observed_memory_swap_max_bytes={swap}; maximum_bytes={maximum_bytes}; '
            f'maximum_swap_bytes={maximum_swap_bytes}; configure the supervised '
            'operation budget before Recovery; no limits changed'
        )
    return ProcessMemoryLimit(limit, swap, used)


def memory_headroom(*, proc: Path = Path('/proc'), cgroup: Path = Path('/sys/fs/cgroup')) -> MemoryHeadroom:
    """Observe host and every visible cgroup-v2 ancestor; never add swap."""
    try:
        fields = {}
        for line in (proc / 'meminfo').read_text().splitlines():
            name, _, value = line.partition(':')
            if name in ('MemAvailable', 'MemTotal'):
                amount, unit = value.split()
                if unit != 'kB':
                    raise ValueError('unknown memory units')
                fields[name] = _number(amount) * 1024
        available, total = fields['MemAvailable'], fields['MemTotal']
        if not 0 <= available <= total or total <= 0:
            raise ValueError('invalid host memory')
        current, mount = _unified_cgroup(proc, cgroup)
        finite = 0
        while True:
            maximum = current / 'memory.max'
            if maximum.exists():
                limit = maximum.read_text().strip()
                if limit != 'max':
                    limit_value = _number(limit)
                    used = _number((current / 'memory.current').read_text())
                    if limit_value <= 0:
                        raise ValueError('invalid cgroup limit')
                    available = min(available, max(0, limit_value - used))
                    finite += 1
            elif current != mount:
                raise ValueError('missing cgroup limit')
            if current == mount:
                break
            current = current.parent
        return MemoryHeadroom(available, f'host_memavailable+cgroup_v2_ancestors:{finite}')
    except (OSError, ValueError, KeyError, IndexError) as exc:
        raise RecoveryMemoryRefused(
            'recovery_memory_capacity_unknown: cannot read current host/cgroup capacity; '
            'inspect runtime memory accounting before Recovery admission'
        ) from exc


def snapshot_size(state_store) -> tuple[int, int]:
    """Only SQL COUNT/SUM lengths, never payload retrieval or JSON decoding."""
    from codex_web.storage.postgres_state import PostgresStateStore
    from codex_web.storage.sqlite_state import SQLiteStateStore
    if not isinstance(state_store, (PostgresStateStore, SQLiteStateStore)):
        raise RecoveryMemoryRefused('recovery_memory_size_unknown: StateStore sizing unsupported')
    pg = isinstance(state_store, PostgresStateStore)
    try:
        with state_store._connection(**({'read_only_snapshot': True} if pg else {})) as connection:
            table = 'codex_state_documents' if pg else 'state_documents'
            length = 'octet_length' if pg else 'length'
            payload = 'payload' if pg else 'CAST(payload AS BLOB)'
            key = 'namespace' if pg else 'CAST(namespace AS BLOB)'
            cursor = connection.cursor()
            try:
                cursor.execute(f'SELECT COUNT(*),COALESCE(SUM({length}({payload})+{length}({key})),0) FROM {table}')
                rows, size = cursor.fetchone()
            finally:
                cursor.close()
        if type(rows) is not int or type(size) is not int or min(rows, size) < 0:
            raise ValueError('invalid inventory')
        return rows, size
    except RecoveryMemoryRefused:
        raise
    except Exception as exc:
        raise RecoveryMemoryRefused('recovery_memory_size_unknown: metadata-only StateStore sizing failed') from exc


# Measured operational admission estimates; arbitrary JSON inflation and concurrent
# process growth can exceed them. They neither reserve RAM nor change schema/PASS.
SAFETY_MARGIN_BYTES = 512 * 1024**2


def admit(stage: str, estimated_allocation_bytes: int) -> MemoryHeadroom:
    if type(estimated_allocation_bytes) is not int or estimated_allocation_bytes < 0:
        raise RecoveryMemoryRefused('recovery_memory_size_unknown: invalid allocation estimate')
    observed = memory_headroom()
    required = estimated_allocation_bytes + SAFETY_MARGIN_BYTES
    if required > observed.available_bytes:
        raise RecoveryMemoryRefused(
            f'recovery_memory_insufficient: stage={stage}; estimated_required_bytes={required}; '
            f'observed_available_bytes={observed.available_bytes}; source={observed.source}; '
            'estimate_not_reservation; defer Recovery until workload settles or use reviewed '
            'capacity planning; no backup/restore qualification asserted'
        )
    return observed
