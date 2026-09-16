from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import subprocess
from pathlib import Path
from typing import Any, Callable

from fastapi import HTTPException


logger = logging.getLogger(__name__)


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
        command: tuple[str, ...] = ("codex", "app-server"),
        cwd: Path | None = None,
        popen: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
    ) -> None:
        self.host = host
        self.command = command
        self.cwd = cwd or Path.home()
        self._popen = popen

        self.proc: subprocess.Popen[str] | None = None
        self.next_id = 1
        self.pending: dict[int | str, asyncio.Future[dict[str, Any]]] = {}
        self.pending_approvals: dict[int | str, dict[str, Any]] = {}
        self.last_error: str | None = None
        self.write_lock = asyncio.Lock()
        self.ready = asyncio.Event()
        self.reader_task: asyncio.Task[None] | None = None
        self.stderr_task: asyncio.Task[None] | None = None
        self.lifecycle_lock = asyncio.Lock()

    async def start(self) -> None:
        async with self.lifecycle_lock:
            if self.proc and self.proc.poll() is None and self.ready.is_set():
                return
            if self.proc and self.proc.poll() is None:
                await self.stop()

            self.ready.clear()
            self.last_error = None
            self._fail_pending(RuntimeError("Codex app-server restarted"))
            self.proc = self._popen(
                list(self.command),
                cwd=str(self.cwd),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            self.reader_task = asyncio.create_task(self._read_loop(), name="codex-app-server-stdout")
            self.stderr_task = asyncio.create_task(self._stderr_loop(), name="codex-app-server-stderr")

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
                await self.host.hub.publish({"type": "codex.ready", "initialize": init})
            except Exception as exc:
                self.last_error = str(exc)
                logger.exception("Codex app-server failed to initialize")
                await self.host.hub.publish({"type": "codex.error", "error": self.last_error})
                with contextlib.suppress(Exception):
                    await self.stop()
                self.last_error = str(exc)
                raise

    async def stop(self) -> None:
        self._fail_pending(RuntimeError("Codex app-server stopped"))
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(asyncio.to_thread(self.proc.wait), timeout=5)
            except asyncio.TimeoutError:
                self.proc.kill()
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(self.proc.wait)

        self.proc = None
        self.ready.clear()
        tasks = (self.reader_task, self.stderr_task)
        for task in tasks:
            if task and not task.done():
                task.cancel()
        for task in tasks:
            if task:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self.reader_task = None
        self.stderr_task = None

    def _fail_pending(self, exc: Exception) -> None:
        for future in self.pending.values():
            if not future.done():
                future.set_exception(exc)
        self.pending.clear()

    async def _stderr_loop(self) -> None:
        assert self.proc and self.proc.stderr
        while True:
            line = await asyncio.to_thread(self.proc.stderr.readline)
            if not line:
                return
            text = line.rstrip("\n")
            self.last_error = text
            logger.warning("codex app-server stderr: %s", text)
            await self.host.hub.publish({"type": "codex.stderr", "text": text})

    async def _read_loop(self) -> None:
        assert self.proc and self.proc.stdout
        while True:
            line = await asyncio.to_thread(self.proc.stdout.readline)
            if not line:
                self.ready.clear()
                self.last_error = "Codex app-server stopped"
                self._fail_pending(RuntimeError(self.last_error))
                logger.warning("codex app-server stdout closed")
                if self.proc and self.proc.poll() is not None:
                    self.proc = None
                await self.host.hub.publish({"type": "codex.closed"})
                return

            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                await self.host.hub.publish({"type": "codex.raw", "text": line.rstrip("\n")})
                continue

            await self._handle_message(message)

    async def _handle_message(self, message: dict[str, Any]) -> None:
        message_id = message.get("id")
        if message_id is not None and "method" not in message:
            future = self.pending.pop(message_id, None)
            if future and not future.done():
                if "error" in message:
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

            self.pending_approvals[message_id] = message
            await self.host._record_bot_approval_request(message)
            await self.host.hub.publish({"type": "approval.request", "request": message})
            return

        self.host._record_thread_activity(message)
        method = message.get("method")
        params = message.get("params") or {}
        thread_id = params.get("threadId") or (params.get("turn") or {}).get("threadId")
        terminal_recovery_scheduled = self.host._record_terminal_turn_result(message)

        if method == "thread/name/updated":
            asyncio.create_task(
                self.host._restore_bot_thread_name(thread_id),
                name=f"restore-thread-name-{thread_id or 'unknown'}",
            )
        if method in {"turn/completed", "turn/failed"}:
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
        try:
            if timeout is None:
                return await future
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            self.pending.pop(message_id, None)
            self.last_error = f"{method} timed out after {timeout}s"
            logger.warning("codex app-server request timed out: %s", self.last_error)
            await self.host.hub.publish({"type": "codex.error", "error": self.last_error})
            raise HTTPException(status_code=504, detail=self.last_error) from exc

    async def notify(self, method: str, params: Any = None) -> None:
        await self._send({"method": method, "params": params})

    async def ensure_started(self, skip: bool = False) -> None:
        if skip:
            return
        if (
            not self.proc
            or self.proc.poll() is not None
            or (self.reader_task is not None and self.reader_task.done() and not self.ready.is_set())
        ):
            await self.start()
        elif not self.ready.is_set():
            try:
                await asyncio.wait_for(self.ready.wait(), timeout=15)
            except asyncio.TimeoutError:
                await self.stop()
                await self.start()

    async def respond_to_server_request(self, request_id: int | str, result: dict[str, Any]) -> None:
        self.pending_approvals.pop(request_id, None)
        await self._send({"id": request_id, "result": result})
        await self.host.hub.publish({"type": "approval.resolved", "id": request_id, "result": result})


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
