from __future__ import annotations

import contextlib
import os
import tempfile
import threading
from pathlib import Path


_STATE_FILE_LOCKS: dict[str, threading.RLock] = {}


def state_file_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    lock = _STATE_FILE_LOCKS.get(key)
    if lock is None:
        lock = threading.RLock()
        _STATE_FILE_LOCKS[key] = lock
    return lock


def atomic_write_text(path: Path, text: str, *, private: bool = False) -> None:
    """Atomically replace one state file and fsync the new contents."""

    path.parent.mkdir(parents=True, exist_ok=True)
    lock = state_file_lock(path)
    with lock:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary_path = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600 if private else 0o644)
            with os.fdopen(descriptor, "w") as handle:
                descriptor = -1
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            with contextlib.suppress(FileNotFoundError):
                temporary_path.unlink()
