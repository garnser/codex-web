import unittest
import re
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from codex_web.runtime.codex import CodexRuntime
from tests.test_codex_runtime import _Host


class NativeRuntimeVersionTests(unittest.IsolatedAsyncioTestCase):
    async def test_release_defaults_produce_a_qualified_initialize_version(self):
        root = Path(__file__).resolve().parents[1]
        pins = []
        for filename, pattern in (
            ('.env.example', r'^CODEX_VERSION=([^\s]+)$'),
            ('Dockerfile', r'^ARG CODEX_VERSION=([^\s]+)$'),
            ('Dockerfile.base', r'^ARG CODEX_VERSION=([^\s]+)$'),
            ('compose.yaml', r'CODEX_VERSION: \$\{CODEX_VERSION:-([^}]+)\}'),
        ):
            match = re.search(pattern, (root / filename).read_text(), re.MULTILINE)
            self.assertIsNotNone(match, filename)
            pins.append(match.group(1))
        self.assertEqual(len(set(pins)), 1, pins)
        process = Mock(pid=123, poll=Mock(return_value=None), wait=Mock(return_value=0))
        runtime = CodexRuntime(_Host(), popen=Mock(return_value=process))
        runtime._read_loop = AsyncMock()
        runtime._stderr_loop = AsyncMock()
        runtime.notify = AsyncMock()
        runtime.request = AsyncMock(return_value={'userAgent': f'codex_web_local/{pins[0]}'})
        self.addAsyncCleanup(runtime.stop)
        await runtime.start()
        self.assertEqual(runtime.native_runtime_version, pins[0])

    async def test_initialize_qualifies_production_client_and_rejects_other_versions(self):
        process = Mock(pid=123, poll=Mock(return_value=None), wait=Mock(return_value=0))
        runtime = CodexRuntime(_Host(), popen=Mock(return_value=process))
        runtime._read_loop = AsyncMock()
        runtime._stderr_loop = AsyncMock()
        runtime.notify = AsyncMock()
        self.addAsyncCleanup(runtime.stop)
        cases = [
            ({'userAgent': 'codex_web_local/0.160.0 (Fedora 43.0.0; x86_64) dumb (codex_web_local; 0.1.0)'}, '0.160.0'),
            ({}, None),  # A later generation cannot borrow the prior proof.
            ({'userAgent': 'codex_cli_rs/0.160.0 (Linux)'}, '0.160.0'),
            ({'userAgent': 'other_client/0.160.0'}, None),
            ({'userAgent': 'codex_web_local/0.160.1'}, None),
            ({'userAgent': 'codex_web_local/0.160.0-rc1'}, None),
            ({'userAgent': 'prefix codex_web_local/0.160.0'}, None),
            ({'userAgent': 160}, None),
            (None, None),
        ]
        for response, expected in cases:
            with self.subTest(response=response):
                runtime.request = AsyncMock(return_value=response)
                await runtime.start()
                try:
                    request = runtime.request.await_args
                    self.assertEqual(request.args[0], 'initialize')
                    self.assertEqual(request.args[1]['clientInfo']['name'], 'codex_web_local')
                    self.assertEqual(runtime.native_runtime_version, expected)
                finally:
                    await runtime.stop()
