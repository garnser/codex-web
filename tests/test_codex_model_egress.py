from __future__ import annotations

import asyncio
import base64
import tempfile
import unittest
from pathlib import Path

from codex_web.services.agent_model_egress import (
    AgentRuntimeModelEgressEndpoint,
    model_egress_endpoints_from_base_urls,
)
from codex_web.services.codex_model_egress import (
    AssignmentBoundModelEgressBroker,
    CodexModelEgressEndpoint,
    endpoints_from_provider_base_urls,
)


class AgentRuntimeModelEgressEndpointTests(unittest.TestCase):
    def test_generic_resolver_has_no_implicit_provider_destinations(self) -> None:
        endpoints = model_egress_endpoints_from_base_urls(
            (
                "https://models.example.com/v1",
                "http://unsafe.example.com/v1",
            ),
            default_endpoints=(
                AgentRuntimeModelEgressEndpoint("runtime.example.test", 8443),
            ),
        )

        self.assertEqual(
            {(item.host, item.port) for item in endpoints},
            {
                ("models.example.com", 443),
                ("runtime.example.test", 8443),
            },
        )

    def test_generic_resolver_without_defaults_fails_closed_to_empty(self) -> None:
        self.assertEqual(
            model_egress_endpoints_from_base_urls(
                ("http://unsafe.example.com/v1",),
            ),
            (),
        )


class CodexModelEgressEndpointTests(unittest.TestCase):
    def test_provider_urls_resolve_to_exact_https_destinations(self) -> None:
        endpoints = endpoints_from_provider_base_urls(
            (
                "https://models.example.com/v1",
                "https://models.example.com:8443/api",
                "http://unsafe.example.com/v1",
                None,
            )
        )

        values = {(item.host, item.port) for item in endpoints}
        self.assertIn(("api.openai.com", 443), values)
        self.assertIn(("chatgpt.com", 443), values)
        self.assertIn(("models.example.com", 443), values)
        self.assertIn(("models.example.com", 8443), values)
        self.assertNotIn(("unsafe.example.com", 80), values)

    def test_custom_only_mode_fails_closed_when_no_https_provider_exists(self) -> None:
        endpoints = endpoints_from_provider_base_urls(
            ("http://unsafe.example.com/v1",),
            include_default_openai=False,
        )
        self.assertEqual(endpoints, ())


class CodexModelEgressBrokerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.echo_server = await asyncio.start_server(
            self._echo,
            host="127.0.0.1",
            port=0,
        )
        socket = self.echo_server.sockets[0]
        self.echo_port = int(socket.getsockname()[1])
        self.validation_calls = 0
        self.stale = False
        self.broker = AssignmentBoundModelEgressBroker(
            (CodexModelEgressEndpoint("127.0.0.1", self.echo_port),),
            validator=self._validate,
        )
        await self.broker.start()

    async def asyncTearDown(self) -> None:
        await self.broker.stop()
        self.echo_server.close()
        await self.echo_server.wait_closed()

    async def _echo(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            while True:
                chunk = await reader.read(64 * 1024)
                if not chunk:
                    return
                writer.write(chunk)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    def _validate(self) -> None:
        self.validation_calls += 1
        if self.stale:
            raise RuntimeError("stale fence")

    def _auth(self, *, capability: str | None = None) -> str:
        value = capability if capability is not None else self.broker.capability
        encoded = base64.b64encode(f"agent-runtime:{value}".encode()).decode()
        return f"Basic {encoded}"

    async def _connect(self, target: str, auth: str) -> tuple[
        asyncio.StreamReader,
        asyncio.StreamWriter,
        bytes,
    ]:
        reader, writer = await asyncio.open_unix_connection(
            str(self.broker.socket_path)
        )
        writer.write(
            (
                f"CONNECT {target} HTTP/1.1\r\n"
                f"Host: {target}\r\n"
                f"Proxy-Authorization: {auth}\r\n"
                "\r\n"
            ).encode()
        )
        await writer.drain()
        response = await reader.readuntil(b"\r\n\r\n")
        return reader, writer, response

    async def test_authenticated_allowed_connect_streams_bidirectionally(self) -> None:
        reader, writer, response = await self._connect(
            f"127.0.0.1:{self.echo_port}",
            self._auth(),
        )
        self.assertIn(b"200 Connection Established", response)

        writer.write(b"stream-through-broker")
        await writer.drain()
        echoed = await asyncio.wait_for(
            reader.readexactly(len(b"stream-through-broker")),
            timeout=1,
        )

        self.assertEqual(echoed, b"stream-through-broker")
        self.assertEqual(self.broker.connections, 1)
        self.assertGreaterEqual(self.validation_calls, 2)
        writer.close()
        await writer.wait_closed()

    async def test_bad_capability_is_denied_before_upstream_connect(self) -> None:
        _reader, writer, response = await self._connect(
            f"127.0.0.1:{self.echo_port}",
            self._auth(capability="wrong-capability"),
        )

        self.assertIn(b"407 Proxy Authentication Required", response)
        self.assertEqual(self.broker.connections, 0)
        writer.close()
        await writer.wait_closed()

    async def test_non_allowlisted_destination_is_denied(self) -> None:
        _reader, writer, response = await self._connect(
            "example.invalid:443",
            self._auth(),
        )

        self.assertIn(b"403 Forbidden", response)
        self.assertEqual(self.broker.connections, 0)
        writer.close()
        await writer.wait_closed()

    async def test_stale_assignment_authority_denies_new_connect(self) -> None:
        self.stale = True
        _reader, writer, response = await self._connect(
            f"127.0.0.1:{self.echo_port}",
            self._auth(),
        )

        self.assertIn(b"403 Forbidden", response)
        self.assertEqual(self.broker.connections, 0)
        writer.close()
        await writer.wait_closed()

    async def test_stop_removes_private_socket_directory(self) -> None:
        root = self.broker.mount_source
        self.assertTrue(root.exists())

        await self.broker.stop()

        self.assertFalse(root.exists())


if __name__ == "__main__":
    unittest.main()
