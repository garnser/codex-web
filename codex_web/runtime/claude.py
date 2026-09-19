from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import subprocess
import uuid
from pathlib import Path
from typing import Any, Callable


logger = logging.getLogger(__name__)


class ClaudeCodeRuntime:
    """Own one assignment-bound Claude Code stream-json subprocess.

    The worker launches the process inside the canonical sandbox. This class is
    transport only: canonical AgentSession identity, worker lease/fence state and
    approval authority remain outside the provider runtime.
    """

    def __init__(
        self,
        host: Any,
        *,
        command: tuple[str, ...],
        cwd: Path | None = None,
        popen: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
    ) -> None:
        self.host = host
        self.command = command
        self.cwd = cwd or Path.home()
        self._popen = popen

        self.proc: subprocess.Popen[str] | None = None
        self.ready = asyncio.Event()
        self.reader_task: asyncio.Task[None] | None = None
        self.stderr_task: asyncio.Task[None] | None = None
        self.write_lock = asyncio.Lock()
        self.lifecycle_lock = asyncio.Lock()
        self.last_error: str | None = None
        self.approval_namespace: str | None = None
        self.pending_approvals: dict[int | str, dict[str, Any]] = {}
        self.pending_approval_rpc_ids: dict[int | str, int | str] = {}
        self.pending_controls: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self.turn_future: asyncio.Future[dict[str, Any]] | None = None
        self.native_session_id = self._configured_session_id(command)
        self.runtime_version: str | None = None
        self.last_message: dict[str, Any] | None = None

    @staticmethod
    def _configured_session_id(command: tuple[str, ...]) -> str:
        try:
            index = command.index("--session-id")
            value = command[index + 1]
        except (ValueError, IndexError) as exc:
            raise ValueError("Claude runtime command requires --session-id") from exc
        try:
            return str(uuid.UUID(str(value)))
        except ValueError as exc:
            raise ValueError("Claude runtime --session-id must be a UUID") from exc

    async def start(self) -> None:
        async with self.lifecycle_lock:
            if self.proc is not None and self.proc.poll() is None:
                self.ready.set()
                return
            self.last_error = None
            self.proc = self._popen(
                list(self.command),
                cwd=str(self.cwd),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            self.reader_task = asyncio.create_task(
                self._read_loop(),
                name="claude-stream-json-stdout",
            )
            self.stderr_task = asyncio.create_task(
                self._stderr_loop(),
                name="claude-stream-json-stderr",
            )
            self.ready.set()
            await self.host.hub.publish(
                {
                    "type": "claude.ready",
                    "session_id": self.native_session_id,
                }
            )

    async def stop(self) -> None:
        self._fail_pending(RuntimeError("Claude runtime stopped"))
        proc = self.proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                await asyncio.wait_for(asyncio.to_thread(proc.wait), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(proc.wait)
        self.proc = None
        self.ready.clear()
        for task in (self.reader_task, self.stderr_task):
            if task is not None and not task.done():
                task.cancel()
        for task in (self.reader_task, self.stderr_task):
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self.reader_task = None
        self.stderr_task = None
        self.pending_approvals.clear()
        self.pending_approval_rpc_ids.clear()

    async def ensure_started(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            await self.start()
        elif not self.ready.is_set():
            await asyncio.wait_for(self.ready.wait(), timeout=15)

    def _fail_pending(self, exc: Exception) -> None:
        if self.turn_future is not None and not self.turn_future.done():
            self.turn_future.set_exception(exc)
        self.turn_future = None
        for future in self.pending_controls.values():
            if not future.done():
                future.set_exception(exc)
        self.pending_controls.clear()

    async def _stderr_loop(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        while True:
            line = await asyncio.to_thread(self.proc.stderr.readline)
            if not line:
                return
            text = line.rstrip("\n")
            self.last_error = text
            await self.host.hub.publish({"type": "claude.stderr", "text": text})

    async def _read_loop(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        while True:
            line = await asyncio.to_thread(self.proc.stdout.readline)
            if not line:
                self.ready.clear()
                if self.proc is not None and self.proc.poll() is not None:
                    self.proc = None
                self._fail_pending(RuntimeError("Claude runtime stdout closed"))
                await self.host.hub.publish({"type": "claude.closed"})
                return
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                await self.host.hub.publish(
                    {"type": "claude.raw", "text": line.rstrip("\n")}
                )
                continue
            if not isinstance(message, dict):
                continue
            await self._handle_message(message)

    async def _handle_message(self, message: dict[str, Any]) -> None:
        self.last_message = message
        message_type = message.get("type")
        if message_type == "system" and message.get("subtype") == "init":
            session_id = message.get("session_id")
            if session_id and str(session_id) != self.native_session_id:
                self.last_error = "Claude runtime returned an unexpected session id"
                self._fail_pending(RuntimeError(self.last_error))
                await self.stop()
                return
            version = (
                message.get("claude_code_version")
                or message.get("version")
                or message.get("runtime_version")
            )
            if version:
                self.runtime_version = str(version)

        if message_type == "control_response":
            response = message.get("response")
            if isinstance(response, dict):
                request_id = response.get("request_id")
                if request_id is not None:
                    future = self.pending_controls.pop(str(request_id), None)
                    if future is not None and not future.done():
                        future.set_result(message)
            await self.host.hub.publish({"type": "claude.event", "message": message})
            return

        if message_type == "control_request":
            request = message.get("request")
            if isinstance(request, dict) and request.get("subtype") == "can_use_tool":
                await self._handle_permission_request(message)
                return

        if message_type == "result":
            future = self.turn_future
            self.turn_future = None
            if future is not None and not future.done():
                future.set_result(message)

        await self.host.hub.publish({"type": "claude.event", "message": message})

    def _approval_public_id(self, request_id: int | str) -> int | str:
        if not self.approval_namespace:
            return request_id
        return f"{self.approval_namespace}:{request_id}"

    async def _handle_permission_request(self, message: dict[str, Any]) -> None:
        request_id = message.get("request_id")
        request = message.get("request")
        if request_id is None or not isinstance(request, dict):
            return

        approval_message = {
            "id": self._approval_public_id(request_id),
            "method": "claude/tool/canUse",
            "params": {
                "toolName": request.get("tool_name"),
                "toolUseId": request.get("tool_use_id"),
                "input": request.get("input") or {},
                "permissionSuggestions": request.get("permission_suggestions") or [],
                "sessionId": self.native_session_id,
            },
        }
        public_id = approval_message["id"]
        self.pending_approvals[public_id] = approval_message
        self.pending_approval_rpc_ids[public_id] = request_id

        registrar = getattr(self.host, "_register_canonical_approval_request", None)
        if callable(registrar):
            try:
                await registrar(approval_message)
            except Exception as exc:
                self.pending_approvals.pop(public_id, None)
                self.pending_approval_rpc_ids.pop(public_id, None)
                await self._write_control_response(
                    request_id,
                    {
                        "behavior": "deny",
                        "message": f"Canonical approval registration failed: {exc}",
                    },
                )
                return

        recorder = getattr(self.host, "_record_bot_approval_request", None)
        if callable(recorder):
            await recorder(approval_message)
        await self.host.hub.publish(
            {"type": "approval.request", "request": approval_message}
        )

    async def _send(self, message: dict[str, Any]) -> None:
        await self.ensure_started()
        if self.proc is None or self.proc.poll() is not None or self.proc.stdin is None:
            raise RuntimeError("Claude runtime is not running")
        async with self.write_lock:
            self.proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            self.proc.stdin.flush()

    async def _write_control_response(
        self,
        request_id: int | str,
        response: dict[str, Any],
    ) -> None:
        await self._send(
            {
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": request_id,
                    "response": response,
                },
            }
        )

    async def request(self, method: str, params: Any = None) -> dict[str, Any]:
        await self.ensure_started()
        values = params if isinstance(params, dict) else {}
        if method == "session/list":
            return {
                "sessions": [
                    {
                        "session_id": self.native_session_id,
                        "status": "ready" if self.ready.is_set() else "closed",
                        "runtime_version": self.runtime_version,
                    }
                ]
            }
        if method in {"session/create", "session/read", "session/restore"}:
            requested = values.get("session_id")
            if requested and str(requested) != self.native_session_id:
                raise RuntimeError("Claude runtime session id does not match assignment")
            return {
                "type": "system",
                "subtype": "init",
                "session_id": self.native_session_id,
                "runtime_version": self.runtime_version,
            }
        if method == "session/resume":
            requested = str(values.get("session_id") or "")
            if requested != self.native_session_id:
                raise RuntimeError(
                    "Claude session resume requires a worker process relaunched for that native session"
                )
            return {
                "type": "system",
                "subtype": "resumed",
                "session_id": self.native_session_id,
                "runtime_version": self.runtime_version,
            }
        if method == "session/close":
            return {
                "type": "system",
                "subtype": "closed",
                "session_id": self.native_session_id,
            }
        if method == "turn/start":
            requested = str(values.get("session_id") or "")
            if requested != self.native_session_id:
                raise RuntimeError("Claude turn session does not match assignment")
            if self.turn_future is not None and not self.turn_future.done():
                raise RuntimeError("Claude runtime already has an active turn")
            loop = asyncio.get_running_loop()
            future: asyncio.Future[dict[str, Any]] = loop.create_future()
            self.turn_future = future
            await self._send(
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": str(values.get("message") or ""),
                    },
                    "parent_tool_use_id": None,
                    "session_id": self.native_session_id,
                }
            )
            try:
                return await future
            finally:
                if self.turn_future is future:
                    self.turn_future = None
        if method == "turn/interrupt":
            requested = str(values.get("session_id") or "")
            if requested != self.native_session_id:
                raise RuntimeError("Claude interrupt session does not match assignment")
            request_id = f"interrupt-{uuid.uuid4().hex}"
            loop = asyncio.get_running_loop()
            future: asyncio.Future[dict[str, Any]] = loop.create_future()
            self.pending_controls[request_id] = future
            await self._send(
                {
                    "type": "control_request",
                    "request_id": request_id,
                    "request": {"subtype": "interrupt"},
                }
            )
            try:
                return await asyncio.wait_for(future, timeout=10)
            finally:
                self.pending_controls.pop(request_id, None)
        raise RuntimeError(f"unsupported Claude runtime request: {method}")

    async def notify(self, method: str, params: Any = None) -> None:
        if method == "turn/interrupt":
            await self.request(method, params)
            return
        raise RuntimeError(f"unsupported Claude runtime notification: {method}")

    @staticmethod
    def _approval_behavior(
        result: dict[str, Any],
        *,
        original_input: dict[str, Any],
    ) -> dict[str, Any]:
        behavior = str(result.get("behavior") or "").strip()
        decision = str(result.get("decision") or result.get("outcome") or "").strip()
        normalized = (behavior or decision).casefold()
        if normalized in {
            "allow",
            "approve",
            "approved",
            "accept",
            "acceptforsession",
            "accept_for_session",
        }:
            return {
                "behavior": "allow",
                "updatedInput": original_input,
            }
        message = str(
            result.get("message")
            or result.get("reason")
            or "Canonical approval denied this tool use"
        )
        return {"behavior": "deny", "message": message}

    async def respond_to_server_request(
        self,
        request_id: int | str,
        result: dict[str, Any],
    ) -> None:
        message = self.pending_approvals.pop(request_id, None)
        rpc_request_id = self.pending_approval_rpc_ids.pop(request_id, None)
        if message is None or rpc_request_id is None:
            raise RuntimeError("Claude approval request is not pending")
        original_input = ((message.get("params") or {}).get("input") or {})
        response = self._approval_behavior(
            result,
            original_input=original_input,
        )
        await self._write_control_response(rpc_request_id, response)
        await self.host.hub.publish(
            {"type": "approval.resolved", "id": request_id, "result": result}
        )
