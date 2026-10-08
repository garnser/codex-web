from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from fastapi import HTTPException

from codex_web.observability import RuntimeMetrics, log_event


logger = logging.getLogger(__name__)


TRUSTED_LOCAL_CHILD_HOME = "/tmp/codex-local-shell-home"

SENSITIVE_CODEX_DIAGNOSTIC_ENV_KEYS = (
    "CODEX_ACCESS_TOKEN",
    "CODEX_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "CODEX_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
    "SSH_AUTH_SOCK",
    "GNOME_KEYRING_CONTROL",
)


def redact_codex_diagnostic(value: object) -> str:
    """Redact credential material and auth-path carriers from Codex diagnostics."""
    text = str(value or "")
    for key in SENSITIVE_CODEX_DIAGNOSTIC_ENV_KEYS:
        sensitive = os.environ.get(key)
        if sensitive and len(sensitive) >= 4:
            text = text.replace(sensitive, "[REDACTED]")
    text = re.sub(
        r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}",
        "Bearer [REDACTED]",
        text,
    )
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[REDACTED]", text)
    text = re.sub(
        r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",
        "[REDACTED]",
        text,
    )
    return text[:2000]

# The trusted-local app-server may read operator-owned Codex authentication
# state itself, but repository-controlled shell commands must not inherit
# credential carriers or a login shell that can reconstruct them.
TRUSTED_LOCAL_CHILD_ENVIRONMENT_CONFIG = (
    # Repository workers use codex-web's governed control-plane tools. Avoid
    # making remote Apps a pre-sampling network dependency inside the
    # assignment-bound, network-isolated runtime.
    "features.apps=false",
    'shell_environment_policy.inherit="none"',
    "shell_environment_policy.ignore_default_excludes=false",
    # This non-secret loopback locator is useful only when an assignment worker
    # mounts the matching broker relay. Set it explicitly because inherit="none"
    # must continue to keep process credentials out of repository commands.
    'shell_environment_policy.set={PATH="/opt/codex-playwright/node_modules/.bin:/usr/bin:/usr/local/bin:/bin",HOME="/tmp/codex-local-shell-home",PYTHONPATH="/opt/codex-python/lib64/python3.14/site-packages:/opt/codex-python/lib/python3.14/site-packages",NODE_PATH="/opt/codex-playwright/node_modules",PLAYWRIGHT_BROWSERS_PATH="/opt/codex-playwright-browsers",CODEX_WEB_CONTROL_PLANE_URL="http://127.0.0.1:8788"}',
    'shell_environment_policy.filters.CODEX_ACCESS_TOKEN="exclude"',
    'shell_environment_policy.filters.CODEX_API_KEY="exclude"',
    'shell_environment_policy.filters.OPENAI_API_KEY="exclude"',
    'shell_environment_policy.filters.ANTHROPIC_API_KEY="exclude"',
    'shell_environment_policy.filters.GEMINI_API_KEY="exclude"',
    'shell_environment_policy.filters.CODEX_HOME="exclude"',
    'shell_environment_policy.filters.XDG_CONFIG_HOME="exclude"',
    'shell_environment_policy.filters.XDG_DATA_HOME="exclude"',
    'shell_environment_policy.filters.XDG_RUNTIME_DIR="exclude"',
    'shell_environment_policy.filters.DBUS_SESSION_BUS_ADDRESS="exclude"',
    'shell_environment_policy.filters.SSH_AUTH_SOCK="exclude"',
    'shell_environment_policy.filters.GNOME_KEYRING_CONTROL="exclude"',
    "allow_login_shell=false",
)

# V8's sandbox reserves a large, sparse virtual address range even when its
# committed heap is small. The trusted Codex app-server requires this virtual
# headroom; repository commands still use the assignment's strict RLIMIT_AS.
CODEX_MINIMUM_ADDRESS_SPACE_BYTES = 2 * 1024**4
# RLIMIT_NPROC is charged against the host UID instead of the Bubblewrap PID
# namespace. Keep enough shared-host headroom for Chromium's helper threads
# while retaining a finite ceiling for the trusted interactive runtime.
CODEX_MINIMUM_PROCESS_COUNT = 4096


def _npm_native_codex_executable(launcher: Path) -> Path | None:
    """Resolve an npm Codex launcher to its co-installed native executable."""

    resolved = launcher.resolve()
    scope = next(
        (parent for parent in resolved.parents if parent.name == "@openai"),
        None,
    )
    if scope is None:
        return None
    system = platform.system().lower()
    machine = platform.machine().lower()
    architecture = {
        "x86_64": "x64",
        "amd64": "x64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }.get(machine)
    if system not in {"linux", "darwin", "windows"} or architecture is None:
        return None
    package = scope / f"codex-{system}-{architecture}"
    candidates = tuple(package.glob("vendor/*/bin/codex"))
    if len(candidates) != 1:
        return None
    native = candidates[0]
    return native if native.is_file() and os.access(native, os.X_OK) else None


def resolve_codex_executable() -> str | None:
    """Resolve the deployment-managed Codex binary deterministically.

    The service PATH is the deployment contract. A personal standalone install
    is only a fallback, so an unrelated desktop upgrade cannot silently change
    the execution-worker version on the next service restart.
    """

    configured = os.environ.get("CODEX_WEB_CODEX_EXECUTABLE", "").strip()
    if configured:
        candidate = Path(configured).expanduser()
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise RuntimeError(
                "CODEX_WEB_CODEX_EXECUTABLE must name an executable file"
            )
        return str(candidate.resolve())

    managed = shutil.which("codex")
    if managed:
        managed_path = Path(managed)
        native = _npm_native_codex_executable(managed_path)
        return str((native or managed_path).resolve())

    standalone = Path.home() / ".local" / "bin" / "codex"
    if standalone.is_file() and os.access(standalone, os.X_OK):
        return str(standalone.resolve())
    return None


def trusted_local_codex_command(
    *,
    executable: str = "codex",
    subcommand: tuple[str, ...] = ("app-server",),
) -> tuple[str, ...]:
    command: list[str] = [executable]
    for value in TRUSTED_LOCAL_CHILD_ENVIRONMENT_CONFIG:
        command.extend(("--config", value))
    command.extend(subcommand)
    return tuple(command)


def request_timeout(method: str) -> float | None:
    if method == "initialize":
        return 15
    if method in {"thread/list", "thread/read", "account/rateLimits/read"}:
        return 10
    if method in {"thread/resume", "thread/start", "thread/name/set", "turn/start"}:
        return 60
    if method == "turn/interrupt":
        return 10
    return 20


class CodexRuntime:
    """Own the Codex app-server subprocess and JSON-RPC protocol lifecycle."""

    def __init__(
        self,
        host: Any,
        *,
        command: tuple[str, ...] | None = None,
        cwd: Path | None = None,
        popen: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
        metrics: RuntimeMetrics | None = None,
        restart_on_timeout: bool = True,
    ) -> None:
        self.host = host
        self.command = command or trusted_local_codex_command()
        self.cwd = cwd or Path.home()
        self._popen = popen
        self.metrics = metrics
        self.restart_on_timeout = restart_on_timeout

        self.proc: subprocess.Popen[str] | None = None
        self.next_id = 1
        self.pending: dict[int | str, asyncio.Future[dict[str, Any]]] = {}
        self.pending_approvals: dict[int | str, dict[str, Any]] = {}
        self.pending_approval_rpc_ids: dict[int | str, int | str] = {}
        self.approval_namespace: str | None = None
        self.last_error: str | None = None
        self.write_lock = asyncio.Lock()
        self.ready = asyncio.Event()
        self.reader_task: asyncio.Task[None] | None = None
        self.stderr_task: asyncio.Task[None] | None = None
        self.lifecycle_lock = asyncio.Lock()
        self._pipe_executor: ThreadPoolExecutor | None = None

    async def start(self) -> None:
        async with self.lifecycle_lock:
            if self.proc and self.proc.poll() is None and self.ready.is_set():
                return
            if self.proc and self.proc.poll() is None:
                await self.stop()

            self.ready.clear()
            self.last_error = None
            self._fail_pending(RuntimeError("Codex app-server restarted"))
            self._pipe_reader_executor()
            self.proc = self._popen(
                list(self.command),
                cwd=str(self.cwd),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                close_fds=True,
            )
            if self.metrics:
                self.metrics.increment("codex.process_starts")
            log_event(
                logger,
                logging.INFO,
                "codex.process_started",
                "Codex app-server process started",
                pid=self.proc.pid,
                command=" ".join(self.command),
            )
            process = self.proc
            self.reader_task = asyncio.create_task(
                self._read_loop(process),
                name="codex-app-server-stdout",
            )
            self.stderr_task = asyncio.create_task(
                self._stderr_loop(process),
                name="codex-app-server-stderr",
            )

            try:
                init = await asyncio.wait_for(
                    self.request(
                        "initialize",
                        {
                            "clientInfo": {
                                "name": "codex_web_local",
                                "title": "Codex Web Local",
                                "version": "0.1.0",
                            },
                            "capabilities": {"experimentalApi": True},
                        },
                    ),
                    timeout=15,
                )
                await self.notify("initialized", {})
                self.ready.set()
                if self.metrics:
                    self.metrics.increment("codex.ready")
                await self.host.hub.publish({"type": "codex.ready", "initialize": init})
            except Exception as exc:
                safe_error = redact_codex_diagnostic(exc)
                self.last_error = safe_error
                if self.metrics:
                    self.metrics.increment("codex.initialization_failures")
                log_event(
                    logger,
                    logging.ERROR,
                    "codex.initialize_failed",
                    "Codex app-server failed to initialize",
                    error=safe_error,
                )
                await self.host.hub.publish({"type": "codex.error", "error": self.last_error})
                with contextlib.suppress(Exception):
                    await self.stop()
                self.last_error = safe_error
                raise

    async def stop(self) -> None:
        self._fail_pending(RuntimeError("Codex app-server stopped"))
        process = self.proc
        # Retire this generation before terminating it. Reader tasks retain
        # their own process reference and can now distinguish an intentional
        # stop from an unexpected close without racing a cleared self.proc.
        if self.proc is process:
            self.proc = None
        self.ready.clear()
        if process and process.poll() is None:
            pid = process.pid
            process.terminate()
            try:
                await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(process.wait)
            log_event(
                logger,
                logging.INFO,
                "codex.process_stopped",
                "Codex app-server process stopped",
                pid=pid,
            )

        tasks = (self.reader_task, self.stderr_task, getattr(self, "notification_task", None))
        for task in tasks:
            if task and not task.done():
                task.cancel()
        for task in tasks:
            if task:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self.reader_task = None
        self.stderr_task = None
        self.notification_task = None
        self.notification_queue = None
        pipe_executor = self._pipe_executor
        self._pipe_executor = None
        if pipe_executor is not None:
            pipe_executor.shutdown(wait=False, cancel_futures=True)
        self.pending_approvals.clear()
        self.pending_approval_rpc_ids.clear()

    def _pipe_reader_executor(self) -> ThreadPoolExecutor:
        executor = self._pipe_executor
        if executor is None:
            executor = ThreadPoolExecutor(
                max_workers=2,
                thread_name_prefix="codex-app-server-pipe",
            )
            self._pipe_executor = executor
        return executor

    def _approval_public_id(self, request_id: int | str) -> int | str:
        if not self.approval_namespace:
            return request_id
        return f"{self.approval_namespace}:{request_id}"

    def _fail_pending(self, exc: Exception) -> None:
        failed = 0
        for future in self.pending.values():
            if not future.done():
                future.set_exception(exc)
                failed += 1
        self.pending.clear()
        if self.metrics and failed:
            self.metrics.increment("codex.pending_requests_failed", failed)

    async def _stderr_loop(self, process: subprocess.Popen[str] | None = None) -> None:
        proc = process or self.proc
        assert proc and proc.stderr
        executor = self._pipe_reader_executor()
        loop = asyncio.get_running_loop()
        while True:
            line = await loop.run_in_executor(executor, proc.stderr.readline)
            if not line:
                return
            text = redact_codex_diagnostic(line.rstrip("\n"))
            self.last_error = text
            if self.metrics:
                self.metrics.increment("codex.stderr_lines")
            log_event(
                logger,
                logging.WARNING,
                "codex.stderr",
                "Codex app-server wrote to stderr",
                text=text,
            )
            await self.host.hub.publish({"type": "codex.stderr", "text": text})

    async def _read_loop(self, process: subprocess.Popen[str] | None = None) -> None:
        proc = process or self.proc
        assert proc and proc.stdout
        executor = self._pipe_reader_executor()
        loop = asyncio.get_running_loop()
        while True:
            line = await loop.run_in_executor(executor, proc.stdout.readline)
            if not line:
                if self.proc is not proc:
                    return
                self.ready.clear()
                self.last_error = "Codex app-server stopped"
                self._fail_pending(RuntimeError(self.last_error))
                if self.metrics:
                    self.metrics.increment("codex.unexpected_closes")
                log_event(
                    logger,
                    logging.WARNING,
                    "codex.stdout_closed",
                    "Codex app-server stdout closed",
                    pid=proc.pid,
                )
                if self.proc is proc and proc.poll() is not None:
                    self.proc = None
                await self.host.hub.publish({"type": "codex.closed"})
                return

            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                if self.metrics:
                    self.metrics.increment("codex.raw_lines")
                await self.host.hub.publish({"type": "codex.raw", "text": line.rstrip("\n")})
                continue

            await self._dispatch_message(message)

    async def _dispatch_message(self, message: dict[str, Any]) -> None:
        if message.get("id") is not None and "method" not in message:
            await CodexRuntime._handle_message_safely(self, message)
            return
        queue = getattr(self, "notification_queue", None)
        if queue is None:
            queue = self.notification_queue = asyncio.Queue(maxsize=1024)
            self.notification_task = asyncio.create_task(
                self._process_notifications(queue), name="codex-app-server-notifications"
            )
        # Adjacent chunks for the same native item are one ordered display
        # update. Retain every character without filling the bounded queue
        # with one entry per token and holding RPC responses behind it.
        if message.get("method") in {
            "item/agentMessage/delta", "item/reasoning/textDelta",
            "item/reasoning/summaryTextDelta", "item/commandExecution/outputDelta",
            "item/fileChange/outputDelta",
        } and queue._queue:
            previous = queue._queue[-1]
            params = message.get("params") or {}
            prior = previous.get("params") or {}
            if (previous.get("method") == message.get("method")
                and isinstance(params.get("delta"), str)
                and isinstance(prior.get("delta"), str)
                and {k: v for k, v in params.items() if k != "delta"}
                    == {k: v for k, v in prior.items() if k != "delta"}):
                prior["delta"] += params["delta"]
                return
        await queue.put(message)

    async def _process_notifications(self, queue: asyncio.Queue) -> None:
        # Preserve notification order without holding RPC responses behind
        # durable projections, accounting, or Slack delivery.
        while True:
            message = await queue.get()
            try:
                await CodexRuntime._handle_message_safely(self, message)
            finally:
                queue.task_done()

    async def _handle_message_safely(self, message: dict[str, Any]) -> None:
        try:
            await self._handle_message(message)
        except Exception as exc:
            # A state projection or delivery failure must not kill the stdout
            # reader. Otherwise the child remains alive and the runtime keeps
            # advertising readiness while no task consumes RPC responses.
            safe_error = redact_codex_diagnostic(exc)
            self.last_error = f"Codex message handling failed: {safe_error}"
            if self.metrics:
                self.metrics.increment("codex.message_handler_failures")
            log_event(
                logger,
                logging.ERROR,
                "codex.message_handler_failed",
                "Codex app-server message handling failed",
                error=safe_error,
                method=message.get("method"),
                message_id=message.get("id"),
            )
            await self.host.hub.publish(
                {
                    "type": "codex.error",
                    "error": self.last_error,
                }
            )

    async def _handle_message(self, message: dict[str, Any]) -> None:
        message_id = message.get("id")
        if message_id is not None and "method" not in message:
            future = self.pending.pop(message_id, None)
            if future and not future.done():
                if "error" in message:
                    if self.metrics:
                        self.metrics.increment("codex.rpc_errors")
                    future.set_exception(RuntimeError(message["error"]))
                else:
                    future.set_result(message.get("result", {}))
            await self.host.hub.publish({"type": "rpc.response", "message": message})
            return

        if message_id is not None and "method" in message:
            approval_thread_id = self.host._approval_thread_id(message)
            approval_settings = self.host._approval_run_settings(message)
            if approval_settings.approval_policy == "never":
                result = self.host._approval_result(message["method"], "acceptForSession")
                await self._send({"id": message_id, "result": result})
                if self.metrics:
                    self.metrics.increment("codex.approvals_auto_resolved")
                await self.host.hub.publish(
                    {"type": "approval.auto_resolved", "id": message_id, "result": result}
                )
                self.host._append_bot_event(
                    {
                        "type": "approval_auto_resolved",
                        "thread_id": approval_thread_id,
                        "request_id": message_id,
                        "method": message.get("method"),
                        "approval_policy": approval_settings.approval_policy,
                    }
                )
                return

            public_id = self._approval_public_id(message_id)
            public_message = {**message, "id": public_id}
            self.pending_approvals[public_id] = public_message
            self.pending_approval_rpc_ids[public_id] = message_id
            if self.metrics:
                self.metrics.increment("codex.approvals_requested")

            registrar = getattr(
                self.host,
                "_register_canonical_approval_request",
                None,
            )
            if callable(registrar):
                try:
                    await registrar(public_message)
                except Exception as exc:
                    self.pending_approvals.pop(public_id, None)
                    self.pending_approval_rpc_ids.pop(public_id, None)
                    result = self.host._approval_result(
                        message["method"],
                        "decline",
                    )
                    await self._send({"id": message_id, "result": result})
                    log_event(
                        logger,
                        logging.ERROR,
                        "codex.approval_registration_failed",
                        "Native approval was denied because canonical registration failed",
                        request_id=public_id,
                        method=message.get("method"),
                        error=str(exc),
                    )
                    await self.host.hub.publish(
                        {
                            "type": "approval.registration_failed",
                            "id": public_id,
                            "error": str(exc),
                        }
                    )
                    return

            await self.host._record_bot_approval_request(public_message)
            await self.host.hub.publish(
                {"type": "approval.request", "request": public_message}
            )
            return

        # Activity projection reads and updates durable runtime state. Keep
        # that work off the app-server reader's event loop so a large state
        # catalog cannot stall RPC responses and HTTP readiness together.
        transcript_recorder = getattr(
            self.host,
            "_record_codex_runtime_transcript_event",
            None,
        )
        if callable(transcript_recorder):
            await asyncio.to_thread(transcript_recorder, message)
        method = message.get("method")
        params = message.get("params") or {}
        thread_id = params.get("threadId") or (params.get("turn") or {}).get("threadId")
        stream_times = getattr(self, "stream_activity_at", None)
        if stream_times is None:
            stream_times = self.stream_activity_at = {}
        display_chunk = method in {
            "item/agentMessage/delta", "item/reasoning/textDelta",
            "item/reasoning/summaryTextDelta", "item/commandExecution/outputDelta",
            "item/fileChange/outputDelta",
        }
        now = time.monotonic()
        if not display_chunk or now - stream_times.get(thread_id, 0) >= 5:
            await asyncio.to_thread(self.host._record_thread_activity, message)
            stream_times[thread_id] = now
        terminal_recovery_scheduled = self.host._record_terminal_turn_result(message)

        if method == "thread/name/updated":
            asyncio.create_task(
                self.host._restore_bot_thread_name(thread_id),
                name=f"restore-thread-name-{thread_id or 'unknown'}",
            )
        if method in {"turn/completed", "turn/failed"}:
            if self.metrics:
                self.metrics.increment(f"codex.events.{method.replace('/', '_')}")
            if not terminal_recovery_scheduled:
                self.host._schedule_queue_drain(thread_id)
        elif method == "thread/status/changed":
            status_type = (params.get("status") or {}).get("type")
            if status_type in {"idle", "systemError", "notLoaded"}:
                self.host._schedule_queue_drain(thread_id)

        await self.host._record_bot_outbound(message)
        await self.host.hub.publish({"type": "codex.event", "message": message})

    async def _send(self, message: dict[str, Any]) -> None:
        if not self.proc or self.proc.poll() is not None or not self.proc.stdin:
            raise HTTPException(status_code=503, detail="Codex app-server is not running")
        async with self.write_lock:
            self.proc.stdin.write(json.dumps(message) + "\n")
            self.proc.stdin.flush()

    async def request(self, method: str, params: Any = None) -> dict[str, Any]:
        await self.ensure_started(method == "initialize")
        message_id = self.next_id
        self.next_id += 1
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self.pending[message_id] = future
        await self._send({"method": method, "id": message_id, "params": params})

        timeout = request_timeout(method)
        started = time.monotonic()
        if self.metrics:
            self.metrics.increment("codex.rpc_requests")
            self.metrics.increment(f"codex.rpc_requests.{method.replace('/', '_')}")
        try:
            if timeout is None:
                result = await future
            else:
                result = await asyncio.wait_for(future, timeout=timeout)
            if self.metrics:
                self.metrics.increment("codex.rpc_success")
            return result
        except asyncio.TimeoutError as exc:
            self.pending.pop(message_id, None)
            timeout_error = f"{method} timed out after {timeout}s"
            self.last_error = timeout_error
            if self.metrics:
                self.metrics.increment("codex.rpc_timeouts")
                self.metrics.increment(f"codex.rpc_timeouts.{method.replace('/', '_')}")
            log_event(
                logger,
                logging.WARNING,
                "codex.rpc_timeout",
                "Codex app-server request timed out",
                method=method,
                request_id=message_id,
                timeout_seconds=timeout,
            )
            await self.host.hub.publish({"type": "codex.error", "error": self.last_error})
            thread_id = (
                str(params.get("threadId") or "").strip()
                if isinstance(params, dict)
                else ""
            )
            handoff_check = getattr(
                self.host,
                "_thread_handoff_in_progress",
                None,
            )
            active_check = getattr(self.host, "_thread_is_active", None)
            handoff_active = bool(
                callable(handoff_check) and handoff_check(thread_id)
            )
            turn_active = bool(
                callable(active_check) and active_check(thread_id)
            )
            preserve_generation = (
                method == "thread/read"
                and thread_id
                and (handoff_active or turn_active)
            )
            if preserve_generation:
                if self.metrics:
                    self.metrics.increment(
                        "codex.rpc_timeout_retirement_suppressed"
                    )
                append_event = getattr(self.host, "_append_bot_event", None)
                if callable(append_event):
                    append_event(
                        {
                            "type": "thread_read_timeout_handoff_preserved",
                            "thread_id": thread_id,
                            "request_id": message_id,
                        }
                    )
            elif method == "account/rateLimits/read" and self.ready.is_set():
                # Quota is an optional observation. A slow quota response does
                # not prove the authenticated transport has died; retiring it
                # here also prevents otherwise healthy workers bootstrapping.
                if self.metrics:
                    self.metrics.increment("codex.quota_probe_timeout_preserved")
            elif self.restart_on_timeout:
                # A live process is insufficient evidence of a live JSON-RPC
                # transport. Invalidate this generation and start its
                # replacement immediately. Depending on a later request to do
                # that leaves the shared runtime down when the control plane is
                # already congested and no further RPC reaches it.
                timed_out_proc = self.proc
                self.ready.clear()
                restart_generation = False
                async with self.lifecycle_lock:
                    if self.proc is timed_out_proc:
                        await self.stop()
                        restart_generation = True
                if restart_generation:
                    try:
                        await self.ensure_started()
                    except Exception as restart_exc:
                        safe_error = redact_codex_diagnostic(restart_exc)
                        if self.metrics:
                            self.metrics.increment("codex.timeout_restart_failures")
                        log_event(
                            logger,
                            logging.ERROR,
                            "codex.timeout_restart_failed",
                            "Codex app-server failed to restart after an RPC timeout",
                            method=method,
                            error=safe_error,
                        )
                        await self.host.hub.publish(
                            {
                                "type": "codex.error",
                                "error": f"Codex timeout recovery failed: {safe_error}",
                            }
                        )
            raise HTTPException(status_code=504, detail=timeout_error) from exc
        finally:
            if self.metrics:
                self.metrics.observe("codex.rpc_duration", time.monotonic() - started)

    async def notify(self, method: str, params: Any = None) -> None:
        await self._send({"method": method, "params": params})

    async def ensure_started(self, skip: bool = False) -> None:
        if skip:
            return
        if (
            not self.proc
            or self.proc.poll() is not None
            or (self.reader_task is not None and self.reader_task.done())
        ):
            await self.start()
        elif not self.ready.is_set():
            try:
                await asyncio.wait_for(self.ready.wait(), timeout=15)
            except asyncio.TimeoutError:
                if self.metrics:
                    self.metrics.increment("codex.readiness_timeouts")
                await self.stop()
                await self.start()

    async def respond_to_server_request(self, request_id: int | str, result: dict[str, Any]) -> None:
        self.pending_approvals.pop(request_id, None)
        rpc_request_id = self.pending_approval_rpc_ids.pop(request_id, request_id)
        await self._send({"id": rpc_request_id, "result": result})
        if self.metrics:
            self.metrics.increment("codex.approvals_resolved")
        await self.host.hub.publish(
            {"type": "approval.resolved", "id": request_id, "result": result}
        )


def install_codex_runtime(app: Any, host: Any) -> CodexRuntime:
    """Install the extracted Codex runtime before application lifecycle startup."""

    existing = getattr(app.state, "codex_runtime", None)
    if isinstance(existing, CodexRuntime) and existing.host is host:
        host.codex = existing
        host.CodexAppServer = CodexRuntime
        host._codex_request_timeout = request_timeout
        return existing

    runtime = CodexRuntime(host)
    host.codex = runtime
    # Keep the historical server-facing names mapped to the extracted runtime
    # while compatibility code is removed incrementally from core.py.
    host.CodexAppServer = CodexRuntime
    host._codex_request_timeout = request_timeout
    app.state.codex_runtime = runtime
    return runtime
