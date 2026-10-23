"""Explicit host suboperations on the existing local worker execution engine.

This backend is never selected by a native sandbox flag. The native runner keeps
its Bubblewrap backend; only the canonical Home ActionProvider may enter here.
Full host filesystem/network visibility is intentional under the configured OS
principal. No root, sudo, environment or credential privilege is manufactured.
"""
from contextvars import ContextVar
import os
from pathlib import Path

from codex_web.execution_profiles import HOME_HOST_COMMAND_CONTRACT
from codex_web.local_execution_backend import (
    BubblewrapExecutionBackend, LocalIsolationStatus, LocalExecutionPolicyError,
)


class TrustedHostActionBackend(BubblewrapExecutionBackend):
    def __init__(self, *, worker_id: str, expected_uid: int, enabled: bool = False, **kwargs):
        self._output_limit = ContextVar("trusted_host_output_limit", default=None)
        kwargs.setdefault("max_output_bytes", 65536)
        super().__init__(**kwargs)
        self.worker_id = worker_id
        self.expected_uid = expected_uid
        self.enabled = enabled
        self._admission = ContextVar("trusted_host_operation", default=None)

    @property
    def max_output_bytes(self):
        selected = self._output_limit.get()
        return self._configured_output_limit if selected is None else min(selected, self._configured_output_limit)

    @max_output_bytes.setter
    def max_output_bytes(self, value):
        self._configured_output_limit = value

    def probe(self):
        ready = (self.enabled is True and os.name == "posix"
                 and type(self.expected_uid) is int and os.geteuid() == self.expected_uid)
        return LocalIsolationStatus("explicit-trusted-host", ready,
            None if ready else "Trusted host worker OS identity is not explicitly enabled",
            supports_network_disabled=False, supports_network_allowlist=False,
            supports_resource_limits=True)

    def run(self, *args, **kwargs):
        raise LocalExecutionPolicyError("Trusted host backend requires canonical action admission")

    def _current(self, assignment):
        admitted = self._admission.get()
        if (admitted is None or not self.probe().ready
                or assignment.execution_contract_version != HOME_HOST_COMMAND_CONTRACT
                or assignment.assigned_worker_id != self.worker_id):
            raise LocalExecutionPolicyError("Trusted host action admission is unavailable")
        admitted()

    def validate_assignment(self, assignment):
        self._current(assignment)

    def build_command(self, assignment, *, argv, **kwargs):
        self._current(assignment)
        if (not argv or len(argv) > 64 or not Path(argv[0]).is_absolute()
                or any(not isinstance(v, str) or "\x00" in v or len(v) > 8192 for v in argv)
                or sum(len(v.encode("utf-8")) for v in argv) > 32768):
            raise LocalExecutionPolicyError("Host command requires bounded argv and absolute executable")
        return tuple(argv)

    def run_action(self, assignment, *, argv, workspace_path, admission_guard, max_output_bytes=16384):
        if not callable(admission_guard):
            raise LocalExecutionPolicyError("Current host action guard is required")
        if type(max_output_bytes) is not int or not 1024 <= max_output_bytes <= 65536:
            raise LocalExecutionPolicyError("Bounded host output limit is required")
        token = self._admission.set(admission_guard)
        output_token = self._output_limit.set(max_output_bytes)
        try:
            # Reuse the SAME pre-spawn/poll guard, rlimits, output bound, timeout,
            # group termination and positive wait as normal local worker commands.
            self._current(assignment)
            return super().run(assignment, argv=argv, workspace_path=workspace_path,
                environment={}, poll_hook=lambda: self._current(assignment))
        finally:
            self._output_limit.reset(output_token)
            self._admission.reset(token)
