"""Explicit network qualification, excluded from ordinary deterministic test runs.

CODEX_WEB_TEST_DEPENDENCY_HTTPS=1 python -m unittest tests.test_dependency_egress_transport
Uses scratch directories and a separate broker; never publishes a Definition or
changes a running worker. No Codex process or production credentials are used.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path

from codex_web.execution_profiles import DependencyEgressEnvelope
from codex_web.execution_workers import NetworkPolicy
from codex_web.local_execution_backend import BubblewrapExecutionBackend
from codex_web.services.agent_model_egress import (
    AGENT_MODEL_EGRESS_RELAY_SCRIPT, AssignmentBoundDependencyEgressBroker,
)
from tests.test_local_execution_worker import _assignment


PACKAGE_PROBE = r'''
import hashlib, json, os, socket, urllib.request
assert 'CODEX_ACCESS_TOKEN' not in os.environ
assert 'OPENAI_API_KEY' not in os.environ
assert not os.path.exists('/etc/resolv.conf')
assert not os.path.exists('/run/podman/podman.sock')
for target in [('1.1.1.1', 443), ('127.0.0.1', int(os.environ['HOST_LISTENER_PORT']))]:
    connection = socket.socket()
    connection.settimeout(1)
    try:
        connection.connect(target)
    except OSError:
        pass
    else:
        raise AssertionError('direct host/public networking unexpectedly available')
    finally:
        connection.close()
proxy = urllib.request.ProxyHandler({'https': os.environ['QUALIFICATION_DEPENDENCY_PROXY']})
client = urllib.request.build_opener(proxy)
with client.open('https://pypi.org/pypi/pip/25.3/json', timeout=20) as response:
    metadata = json.load(response)
wheel = next(item for item in metadata['urls'] if item['filename'] == 'pip-25.3-py3-none-any.whl')
with client.open(wheel['url'], timeout=20) as response:
    content = response.read(4 * 1024 * 1024)
assert hashlib.sha256(content).hexdigest() == wheel['digests']['sha256']
print(json.dumps({'package': wheel['filename'], 'sha256': wheel['digests']['sha256'],
                  'bytes': len(content), 'direct_network_isolated': True,
                  'child_model_credentials_absent': True}))
'''


@unittest.skipUnless(os.environ.get('CODEX_WEB_TEST_DEPENDENCY_HTTPS') == '1', 'explicit public HTTPS qualification only')
class DependencyTransportQualification(unittest.IsolatedAsyncioTestCase):
    async def test_real_package_https_through_separate_proxy_with_direct_network_isolated(self):
        # The immutable package size/digest below are independent of the JSON
        # metadata used to locate the wheel. This fixture is not a native-session
        # or live Profile-publication qualification.
        import socket
        backend = BubblewrapExecutionBackend()
        self.assertTrue(backend.probe().ready, backend.probe().reason)
        policy = DependencyEgressEnvelope(allowed_hosts=('pypi.org', 'files.pythonhosted.org'))
        broker = AssignmentBoundDependencyEgressBroker(policy.allowed_hosts, validator=lambda: None)
        await broker.start()
        try:
            with tempfile.TemporaryDirectory() as root, socket.socket() as host_listener:
                host_listener.bind(('127.0.0.1', 0))
                host_listener.listen(1)
                assignment = _assignment(network=NetworkPolicy())
                result = await asyncio.to_thread(backend.run, assignment,
                    argv=('/usr/bin/python3', '-u', '-c', AGENT_MODEL_EGRESS_RELAY_SCRIPT,
                          str(broker.sandbox_socket_path), '8789',
                          '/usr/bin/python3', '-c', PACKAGE_PROBE),
                    workspace_path=Path(root),
                    environment={'QUALIFICATION_DEPENDENCY_PROXY': broker.proxy_url,
                                 'HOST_LISTENER_PORT': str(host_listener.getsockname()[1])},
                    trusted_readonly_mounts=((broker.mount_source, broker.mount_destination),))
                self.assertTrue(result.succeeded, result.stderr)
                observation = json.loads(result.stdout)
                self.assertTrue(observation['direct_network_isolated'])
                self.assertTrue(observation['child_model_credentials_absent'])
                self.assertEqual(observation['bytes'], 1778622)
                self.assertEqual(observation['sha256'],
                    '9655943313a94722b7774661c21049070f6bbb0a1516bf02f7c8d5d9201514cd')
                self.assertGreaterEqual(broker.connections, 2)
                # Public transport observation only, never proxy capabilities.
                print(json.dumps(observation, sort_keys=True))
        finally:
            await broker.stop()
