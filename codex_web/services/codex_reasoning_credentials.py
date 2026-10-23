"""Restricted launch inside the existing trusted-local Codex credential owner.

Only references are mounted. This module never reads, parses, copies or exports
credential bytes, and it grants no refresh or operational tool capability.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Callable, Any

from codex_web.runtime.codex import trusted_local_codex_command
from codex_web.services.codex_worker_session import (
    CodexTrustedLocalGrant, TrustedLocalCodexCredentialProvider,
)
from codex_web.services.codex_reasoning_policy import (
    REASONING_CATALOG, REASONING_CODEX_HOME, NativeReasoningPolicyError,
    reasoning_config_arguments,
)


@dataclass(frozen=True, slots=True)
class ReasoningCodexLaunch:
    delegation: CodexTrustedLocalGrant
    command: tuple[str, ...]
    environment: dict[str, str]
    trusted_mounts: tuple[tuple[Path, Path], ...]
    trusted_writable_mounts: tuple[tuple[Path, Path], ...] = ()


class TrustedLocalReasoningCredentialProvider(TrustedLocalCodexCredentialProvider):
    """Same canonical lease grant, without ambient Codex configuration access."""

    def __init__(self, *, catalog_path: Path, catalog_sha256: str,
                 validate_context: Callable[[Any], None], **kwargs):
        super().__init__(**kwargs)
        self.catalog_path = catalog_path
        self.catalog_sha256 = catalog_sha256
        self.validate_context = validate_context

    def use(self, assignment, *, worker_id, fence, actor, consumer):
        self._require_worker_actor(actor)
        self.validate_context(assignment)
        grant = self._grant(assignment, worker_id=worker_id, fence=fence)
        # Inspect file metadata only. The existing native sign-in owner retains
        # authentication and refresh; no token becomes a Python value here.
        auth_reference = self.codex_home / 'auth.json'
        if not auth_reference.is_file() or auth_reference.is_symlink():
            raise NativeReasoningPolicyError('Native sign-in owner requires authentication or refresh')
        if (self.catalog_path.is_symlink() or not self.catalog_path.is_file()
                or self.catalog_path.stat().st_size > 2_000_000):
            raise NativeReasoningPolicyError('Native reasoning catalog artifact unavailable')
        payload = self.catalog_path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != self.catalog_sha256:
            raise NativeReasoningPolicyError('Native reasoning catalog artifact changed')
        catalog = json.loads(payload)
        rows = catalog.get('models')
        if (not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict)
                or rows[0].get('shell_type') != 'disabled'
                or rows[0].get('apply_patch_tool_type') is not None
                or rows[0].get('experimental_supported_tools') != []
                or rows[0].get('tool_mode') is not None):
            raise NativeReasoningPolicyError('Native reasoning catalog reductions missing')
        self.validate_current(grant, assignment, actor=actor)
        self.validate_context(assignment)
        return consumer(ReasoningCodexLaunch(
            delegation=grant,
            command=trusted_local_codex_command(executable=self.executable,
                subcommand=(*reasoning_config_arguments(), 'app-server')),
            environment={'CODEX_HOME': str(REASONING_CODEX_HOME)},
            trusted_mounts=((auth_reference, REASONING_CODEX_HOME/'auth.json'),
                            (self.catalog_path, REASONING_CATALOG)),
        ))

    def validate_current(self, delegation, assignment, *, actor):
        super().validate_current(delegation, assignment, actor=actor)
        self.validate_context(assignment)
