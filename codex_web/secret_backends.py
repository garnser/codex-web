from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Protocol, runtime_checkable


class SecretBackendError(RuntimeError):
    pass


@runtime_checkable
class SecretBackend(Protocol):
    backend_type: str

    def put(self, secret_id: str, value: str) -> None: ...
    def get(self, secret_id: str) -> str: ...
    def delete(self, secret_id: str) -> None: ...
    def exists(self, secret_id: str) -> bool: ...


class LocalFileSecretBackend:
    """Local secret-material backend isolated from canonical application state.

    Material files are opaque-ID named, owner-only, and never contain metadata.
    Encryption-at-rest/key management is layered by #167; this backend keeps raw
    material out of SQLite, JSON mirrors, model context and ordinary APIs now.
    """

    backend_type = "local"

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)

    @staticmethod
    def _validate_id(secret_id: str) -> str:
        value = str(secret_id or "").strip()
        if not value or "/" in value or "\\" in value or value in {".", ".."}:
            raise SecretBackendError("invalid secret identifier")
        return value

    def _path(self, secret_id: str) -> Path:
        return self.root / self._validate_id(secret_id)

    def put(self, secret_id: str, value: str) -> None:
        if not isinstance(value, str) or not value:
            raise SecretBackendError("secret value must not be empty")
        path = self._path(secret_id)
        fd, temporary = tempfile.mkstemp(prefix=".secret-", dir=self.root)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(value)
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

    def get(self, secret_id: str) -> str:
        path = self._path(secret_id)
        try:
            value = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise SecretBackendError("secret material is unavailable") from exc
        if not value:
            raise SecretBackendError("secret material is empty")
        return value

    def delete(self, secret_id: str) -> None:
        try:
            self._path(secret_id).unlink()
        except FileNotFoundError:
            return

    def exists(self, secret_id: str) -> bool:
        return self._path(secret_id).is_file()
