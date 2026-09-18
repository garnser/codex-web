from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Protocol, runtime_checkable


class KeyBackendError(RuntimeError):
    pass


@runtime_checkable
class KeyBackend(Protocol):
    backend_type: str

    def create(self, key_id: str, version: int) -> str: ...
    def get(self, backend_ref: str) -> bytes: ...
    def delete(self, backend_ref: str) -> None: ...
    def exists(self, backend_ref: str) -> bool: ...
    def healthy(self) -> bool: ...


class LocalFileKeyBackend:
    backend_type = "local"

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)

    @staticmethod
    def _validate_ref(value: str) -> str:
        ref = str(value or "").strip()
        if not ref or "/" in ref or "\\" in ref or ref in {".", ".."}:
            raise KeyBackendError("invalid key backend reference")
        return ref

    def _path(self, backend_ref: str) -> Path:
        return self.root / self._validate_ref(backend_ref)

    def create(self, key_id: str, version: int) -> str:
        safe_id = self._validate_ref(key_id)
        if version < 1:
            raise KeyBackendError("key version must be positive")
        ref = f"{safe_id}.v{version}"
        path = self._path(ref)
        if path.exists():
            raise KeyBackendError("key material already exists")
        fd, temporary = tempfile.mkstemp(prefix=".key-", dir=self.root)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(os.urandom(32))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        except Exception:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise
        return ref

    def get(self, backend_ref: str) -> bytes:
        try:
            value = self._path(backend_ref).read_bytes()
        except FileNotFoundError as exc:
            raise KeyBackendError("key material is unavailable") from exc
        if len(value) != 32:
            raise KeyBackendError("key material has invalid length")
        return value

    def delete(self, backend_ref: str) -> None:
        try:
            self._path(backend_ref).unlink()
        except FileNotFoundError:
            return

    def exists(self, backend_ref: str) -> bool:
        return self._path(backend_ref).is_file()

    def healthy(self) -> bool:
        return self.root.is_dir() and os.access(self.root, os.R_OK | os.W_OK)
