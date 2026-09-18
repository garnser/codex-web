from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
import os
import secrets
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urlparse


class CodexModelEgressError(RuntimeError):
    pass


class CodexModelEgressDeniedError(CodexModelEgressError):
    pass


@dataclass(frozen=True, slots=True)
class CodexModelEgressEndpoint:
    host: str
    port: int = 443

    def normalized(self) -> "CodexModelEgressEndpoint":
        host = self.host.strip().rstrip(".").casefold()
        if not host:
            raise ValueError("model egress host is required")
        if self.port < 1 or self.port > 65535:
            raise ValueError("model egress port is invalid")
        return CodexModelEgressEndpoint(host=host, port=self.port)


def endpoints_from_provider_base_urls(
    base_urls: Iterable[str | None],
    *,
    include_default_openai: bool = True,
) -> tuple[CodexModelEgressEndpoint, ...]:
    endpoints: set[tuple[str, int]] = set()
    if include_default_openai:
        endpoints.add(("api.openai.com", 443))
        endpoints.add(("chatgpt.com", 443))
    for raw in base_urls:
        value = (raw or "").strip()
        if not value:
            continue
        parsed = urlparse(value)
        if parsed.scheme.casefold() != "https" or not parsed.hostname:
            continue
        endpoints.add((parsed.hostname.casefold().rstrip("."), parsed.port or 443))
    return tuple(
        CodexModelEgressEndpoint(host=host, port=port)
        for host, port in sorted(endpoints)
    )


class AssignmentBoundModelEgressBroker:
    """Authenticated fixed-destination CONNECT broker for one worker session.

    The broker listens on a host-side Unix socket. The Bubblewrap namespace gets
    only a read-only bind of the socket directory and remains network-isolated.
    A tiny in-namespace loopback relay forwards the trusted Codex app-server's
    HTTPS proxy connection to this Unix socket.

    Repository child commands do not inherit the proxy capability and the
    canonical Codex sandbox policy keeps their network access disabled.
    """

    def __init__(
        self,
        endpoints: Iterable[CodexModelEgressEndpoint],
        *,
        validator: Callable[[], object] | None = None,
    ) -> None:
        normalized = tuple(
            sorted(
                {item.normalized() for item in endpoints},
                key=lambda item: (item.host, item.port),
            )
        )
        if not normalized:
            raise CodexModelEgressError(
                "model egress requires at least one canonical HTTPS endpoint"
            )
        self.endpoints = normalized
        self.validator = validator
        self.capability = secrets.token_urlsafe(32)
        self._username = "codex"
        self._root = Path(tempfile.mkdtemp(prefix="codex-model-egress-"))
        os.chmod(self._root, 0o700)
        self.socket_path = self._root / "proxy.sock"
        self.server: asyncio.AbstractServer | None = None
        self.connections = 0
        self.denied_connections = 0

    @property
    def mount_source(self) -> Path:
        return self._root

    @property
    def mount_destination(self) -> Path:
        return Path("/run/codex-model-egress")

    @property
    def sandbox_socket_path(self) -> Path:
        return self.mount_destination / self.socket_path.name

    @property
    def proxy_url(self) -> str:
        return f"http://{self._username}:{self.capability}@127.0.0.1:8787"

    @property
    def allowed_destinations(self) -> tuple[str, ...]:
        return tuple(f"{item.host}:{item.port}" for item in self.endpoints)

    async def start(self) -> "AssignmentBoundModelEgressBroker":
        if self.server is not None:
            return self
        self.server = await asyncio.start_unix_server(
            self._handle,
            path=str(self.socket_path),
        )
        os.chmod(self.socket_path, 0o600)
        return self

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

    def _destination(self, target: str) -> CodexModelEgressEndpoint:
        host, separator, raw_port = target.rpartition(":")
        if not separator or not host or not raw_port.isdigit():
            raise CodexModelEgressDeniedError("CONNECT target must be host:port")
        candidate = CodexModelEgressEndpoint(
            host=host.strip("[]").casefold().rstrip("."),
            port=int(raw_port),
        ).normalized()
        if candidate not in self.endpoints:
            raise CodexModelEgressDeniedError(
                f"model egress destination is not allowed: {candidate.host}:{candidate.port}"
            )
        return candidate

    def _validate_current(self) -> None:
        if self.validator is not None:
            self.validator()

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

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        upstream_writer: asyncio.StreamWriter | None = None
        try:
            try:
                self._validate_current()
            except Exception as exc:
                raise CodexModelEgressDeniedError(
                    "assignment model egress authority is stale"
                ) from exc
            raw = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"),
                timeout=10,
            )
            if len(raw) > 32 * 1024:
                raise CodexModelEgressDeniedError("proxy request headers are too large")
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
                self._validate_current()
            except Exception as exc:
                raise CodexModelEgressDeniedError(
                    "assignment model egress authority is stale"
                ) from exc
            upstream_reader, upstream_writer = await asyncio.open_connection(
                endpoint.host,
                endpoint.port,
            )
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
            CodexModelEgressDeniedError,
        ):
            if not writer.is_closing():
                await self._deny(writer, "403 Forbidden")
        except Exception:
            if not writer.is_closing():
                await self._deny(writer, "502 Bad Gateway")
        finally:
            if upstream_writer is not None:
                with contextlib.suppress(Exception):
                    upstream_writer.close()
                    await upstream_writer.wait_closed()
            if not writer.is_closing():
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()

    async def stop(self) -> None:
        server = self.server
        self.server = None
        if server is not None:
            server.close()
            await server.wait_closed()
        with contextlib.suppress(FileNotFoundError):
            self.socket_path.unlink()
        shutil.rmtree(self._root, ignore_errors=True)


CODEX_MODEL_EGRESS_RELAY_SCRIPT = r"""
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
