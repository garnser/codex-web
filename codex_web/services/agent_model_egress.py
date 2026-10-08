from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
import logging
import os
import secrets
import shutil
import socket
import tempfile
import time
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urlparse


logger = logging.getLogger(__name__)


class AgentRuntimeModelEgressError(RuntimeError):
    pass


class AgentRuntimeModelEgressDeniedError(AgentRuntimeModelEgressError):
    pass


@dataclass(frozen=True, slots=True)
class AgentRuntimeModelEgressEndpoint:
    host: str
    port: int = 443

    def normalized(self) -> "AgentRuntimeModelEgressEndpoint":
        host = self.host.strip().rstrip(".").casefold()
        if not host:
            raise ValueError("model egress host is required")
        if self.port < 1 or self.port > 65535:
            raise ValueError("model egress port is invalid")
        return AgentRuntimeModelEgressEndpoint(host=host, port=self.port)


def model_egress_endpoints_from_base_urls(
    base_urls: Iterable[str | None],
    *,
    default_endpoints: Iterable[AgentRuntimeModelEgressEndpoint] = (),
) -> tuple[AgentRuntimeModelEgressEndpoint, ...]:
    endpoints: set[tuple[str, int]] = {
        (item.normalized().host, item.normalized().port)
        for item in default_endpoints
    }
    for raw in base_urls:
        value = (raw or "").strip()
        if not value:
            continue
        parsed = urlparse(value)
        if parsed.scheme.casefold() != "https" or not parsed.hostname:
            continue
        endpoints.add((parsed.hostname.casefold().rstrip("."), parsed.port or 443))
    return tuple(
        AgentRuntimeModelEgressEndpoint(host=host, port=port)
        for host, port in sorted(endpoints)
    )


class AssignmentBoundAgentModelEgressBroker:
    """Authenticated fixed-destination CONNECT broker for one worker session.

    The broker listens on a host-side Unix socket. The Bubblewrap namespace gets
    only a read-only bind of the socket directory and remains network-isolated.
    A tiny in-namespace loopback relay forwards the trusted execution runtime's
    HTTPS proxy connection to this Unix socket.

    Repository child commands do not inherit the proxy capability; canonical worker
    sandbox/network policy continues to govern their access.
    """

    def __init__(
        self,
        endpoints: Iterable[AgentRuntimeModelEgressEndpoint],
        *,
        validator: Callable[[], object] | None = None,
        upstream_connect_attempts: int = 3,
        upstream_retry_seconds: float = 0.25,
        upstream_connect_timeout_seconds: float = 15.0,
        validation_cache_seconds: float = 5.0,
    ) -> None:
        normalized = tuple(
            sorted(
                {item.normalized() for item in endpoints},
                key=lambda item: (item.host, item.port),
            )
        )
        if not normalized:
            raise AgentRuntimeModelEgressError(
                "model egress requires at least one canonical HTTPS endpoint"
            )
        self.endpoints = normalized
        self.validator = validator
        self.upstream_connect_attempts = max(
            1, int(upstream_connect_attempts)
        )
        self.upstream_retry_seconds = max(
            0.0, float(upstream_retry_seconds)
        )
        self.upstream_connect_timeout_seconds = max(
            0.1, float(upstream_connect_timeout_seconds)
        )
        self.validation_cache_seconds = max(
            0.0, float(validation_cache_seconds)
        )
        self.capability = secrets.token_urlsafe(32)
        self._username = "agent-runtime"
        self._root = Path(tempfile.mkdtemp(prefix="agent-model-egress-"))
        os.chmod(self._root, 0o700)
        self.socket_path = self._root / "proxy.sock"
        self.server: asyncio.AbstractServer | None = None
        self._handler_tasks: set[asyncio.Task[None]] = set()
        self._stopping = False
        self._validation_lock = asyncio.Lock()
        self._last_validation_at: float | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self.connections = 0
        self.denied_connections = 0
        self._blocking_executor: ThreadPoolExecutor | None = None

    def _blocking_pool(self) -> ThreadPoolExecutor:
        # Model CONNECT admission and DNS must not queue behind unrelated
        # control-plane state work in asyncio's default executor.
        executor = getattr(self, "_blocking_executor", None)
        if executor is None:
            executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="model-egress")
            self._blocking_executor = executor
        return executor

    @property
    def mount_source(self) -> Path:
        return self._root

    @property
    def mount_destination(self) -> Path:
        return Path("/run/agent-model-egress")

    @property
    def sandbox_socket_path(self) -> Path:
        return self.mount_destination / self.socket_path.name

    @property
    def proxy_url(self) -> str:
        return f"http://{self._username}:{self.capability}@127.0.0.1:8787"

    @property
    def allowed_destinations(self) -> tuple[str, ...]:
        return tuple(f"{item.host}:{item.port}" for item in self.endpoints)

    async def start(self) -> "AssignmentBoundAgentModelEgressBroker":
        if self.server is not None:
            return self
        self._stopping = False
        self.server = await asyncio.start_unix_server(
            self._accept,
            path=str(self.socket_path),
        )
        os.chmod(self.socket_path, 0o600)
        return self

    def _accept(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        if self._stopping:
            writer.close()
            return
        task = asyncio.create_task(
            self._handle(reader, writer),
            name="assignment-model-egress",
        )
        self._handler_tasks.add(task)
        task.add_done_callback(self._handler_tasks.discard)

    def _authorized(self, headers: dict[str, str]) -> bool:
        raw = headers.get("proxy-authorization", "")
        scheme, _, encoded = raw.partition(" ")
        if scheme.casefold() != "basic" or not encoded:
            return False
        try:
            decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
        except Exception:
            return False
        username, separator, password = decoded.partition(":")
        return bool(
            separator
            and hmac.compare_digest(username, self._username)
            and hmac.compare_digest(password, self.capability)
        )

    def _destination(self, target: str) -> AgentRuntimeModelEgressEndpoint:
        host, separator, raw_port = target.rpartition(":")
        if not separator or not host or not raw_port.isdigit():
            raise AgentRuntimeModelEgressDeniedError("CONNECT target must be host:port")
        candidate = AgentRuntimeModelEgressEndpoint(
            host=host.strip("[]").casefold().rstrip("."),
            port=int(raw_port),
        ).normalized()
        if candidate not in self.endpoints:
            raise AgentRuntimeModelEgressDeniedError(
                f"model egress destination is not allowed: {candidate.host}:{candidate.port}"
            )
        return candidate

    def _validate_current(self) -> None:
        if self.validator is not None:
            self.validator()

    async def _validate_current_async(self) -> None:
        # Validators may decode persistent worker state or inspect a workspace.
        # Keep synchronous validation off the ASGI loop and coalesce bursts of
        # authenticated CONNECTs. The session watchdog independently enforces
        # the same authority at one-second cadence.
        async with self._validation_lock:
            now = time.monotonic()
            if (
                self._last_validation_at is not None
                and now - self._last_validation_at
                < self.validation_cache_seconds
            ):
                return
            await asyncio.get_running_loop().run_in_executor(
                self._blocking_pool(), self._validate_current
            )
            self._last_validation_at = time.monotonic()

    @staticmethod
    async def _pipe(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        while True:
            chunk = await reader.read(64 * 1024)
            if not chunk:
                with contextlib.suppress(Exception):
                    if writer.can_write_eof():
                        writer.write_eof()
                        await writer.drain()
                return
            writer.write(chunk)
            await writer.drain()

    async def _deny(
        self,
        writer: asyncio.StreamWriter,
        status: str,
    ) -> None:
        self.denied_connections += 1
        writer.write(
            (
                f"HTTP/1.1 {status}\r\n"
                "Connection: close\r\n"
                "Content-Length: 0\r\n\r\n"
            ).encode("ascii")
        )
        with contextlib.suppress(Exception):
            await writer.drain()
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()

    async def _open_upstream(
        self,
        endpoint: AgentRuntimeModelEgressEndpoint,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        last_error: BaseException | None = None
        for attempt in range(self.upstream_connect_attempts):
            try:
                addresses = await asyncio.wait_for(
                    asyncio.get_running_loop().run_in_executor(
                        self._blocking_pool(),
                        partial(socket.getaddrinfo, endpoint.host, endpoint.port,
                                type=socket.SOCK_STREAM),
                    ),
                    timeout=self.upstream_connect_timeout_seconds,
                )
                seen: set[tuple[int, str, int]] = set()
                candidates: list[tuple[int, int, tuple]] = []
                for family, _kind, protocol, _canonical, address in addresses:
                    key = (family, str(address[0]), int(address[1]))
                    if key in seen:
                        continue
                    seen.add(key)
                    candidates.append((family, protocol, address))
                if not candidates:
                    raise OSError("model egress DNS returned no addresses")
                families = (socket.AF_INET, socket.AF_INET6)
                for selected_family in families:
                    selected = [
                        candidate
                        for candidate in candidates
                        if candidate[0] == selected_family
                    ]
                    if not selected:
                        continue
                    tasks = [
                        asyncio.create_task(
                            asyncio.open_connection(
                                address[0],
                                address[1],
                                family=family,
                                proto=protocol,
                            )
                        )
                        for family, protocol, address in selected
                    ]
                    try:
                        for completed in asyncio.as_completed(
                            tasks,
                            timeout=self.upstream_connect_timeout_seconds,
                        ):
                            try:
                                connection = await completed
                            except OSError as exc:
                                last_error = exc
                                continue
                            return connection
                    except asyncio.TimeoutError as exc:
                        last_error = exc
                    finally:
                        for task in tasks:
                            if not task.done():
                                task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                if last_error is not None:
                    raise last_error
                raise asyncio.TimeoutError
            except (OSError, asyncio.TimeoutError) as exc:
                last_error = exc
                if attempt + 1 >= self.upstream_connect_attempts:
                    break
                await asyncio.sleep(
                    self.upstream_retry_seconds * (attempt + 1)
                )
        assert last_error is not None
        raise AgentRuntimeModelEgressError(
            f"model egress connection failed after "
            f"{self.upstream_connect_attempts} attempts"
        ) from last_error

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        upstream_writer: asyncio.StreamWriter | None = None
        self._writers.add(writer)
        try:
            raw = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"),
                timeout=10,
            )
            if len(raw) > 32 * 1024:
                raise AgentRuntimeModelEgressDeniedError("proxy request headers are too large")
            lines = raw.decode("iso-8859-1").split("\r\n")
            method, target, _version = lines[0].split(" ", 2)
            headers: dict[str, str] = {}
            for line in lines[1:]:
                if not line or ":" not in line:
                    continue
                key, value = line.split(":", 1)
                headers[key.strip().casefold()] = value.strip()
            if not self._authorized(headers):
                await self._deny(writer, "407 Proxy Authentication Required")
                return
            if method.upper() != "CONNECT":
                await self._deny(writer, "405 Method Not Allowed")
                return
            endpoint = self._destination(target)
            try:
                await self._validate_current_async()
            except Exception as exc:
                raise AgentRuntimeModelEgressDeniedError(
                    "assignment model egress authority is stale"
                ) from exc
            upstream_reader, upstream_writer = await self._open_upstream(
                endpoint
            )
            self._writers.add(upstream_writer)
            self.connections += 1
            writer.write(
                b"HTTP/1.1 200 Connection Established\r\n"
                b"Proxy-Agent: codex-web-assignment-egress\r\n\r\n"
            )
            await writer.drain()
            await asyncio.gather(
                self._pipe(reader, upstream_writer),
                self._pipe(upstream_reader, writer),
            )
        except (
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            asyncio.TimeoutError,
            ValueError,
            AgentRuntimeModelEgressDeniedError,
        ) as exc:
            logger.warning(
                "Assignment-bound model egress denied or timed out: %s: %s",
                type(exc).__name__,
                exc,
            )
            if not writer.is_closing():
                await self._deny(writer, "403 Forbidden")
        except Exception as exc:
            logger.warning(
                "Assignment-bound model egress failed: %s: %s",
                type(exc).__name__,
                exc,
            )
            if not writer.is_closing():
                await self._deny(writer, "502 Bad Gateway")
        finally:
            if upstream_writer is not None:
                self._writers.discard(upstream_writer)
                upstream_writer.close()
            self._writers.discard(writer)
            if not writer.is_closing():
                writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def stop(self) -> None:
        self._stopping = True
        server = self.server
        self.server = None
        if server is not None:
            server.close()
        tasks = tuple(self._handler_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._handler_tasks.clear()
        executor = getattr(self, "_blocking_executor", None)
        self._blocking_executor = None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
        if server is not None:
            close_clients = getattr(server, "close_clients", None)
            if callable(close_clients):
                close_clients()
            await server.wait_closed()
        with contextlib.suppress(FileNotFoundError):
            self.socket_path.unlink()
        shutil.rmtree(self._root, ignore_errors=True)


AGENT_MODEL_EGRESS_RELAY_SCRIPT = r"""
import select
import socket
import subprocess
import sys
import threading

socket_path = sys.argv[1]
port = int(sys.argv[2])
command = sys.argv[3:]


def bridge(client):
    upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        upstream.connect(socket_path)
        peers = (client, upstream)
        while True:
            readable, _, _ = select.select(peers, [], [])
            for source in readable:
                data = source.recv(65536)
                if not data:
                    return
                target = upstream if source is client else client
                target.sendall(data)
    finally:
        try:
            client.close()
        except OSError:
            pass
        try:
            upstream.close()
        except OSError:
            pass


def serve():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", port))
    listener.listen(16)
    while True:
        client, _ = listener.accept()
        threading.Thread(target=bridge, args=(client,), daemon=True).start()


threading.Thread(target=serve, daemon=True).start()
raise SystemExit(subprocess.call(command))
"""
